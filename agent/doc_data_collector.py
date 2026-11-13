"""报告生成：指令解析 + 真实业务数据采集。

职责边界：
    对话框里的自然语言指令
        → parse_instruction()：解析出 company / start_date / end_date / dimensions
        → collect()：按维度调用 MCP 工具，拉取真实业务数据
        → 组装成 GenDocPipeline 需要的 dimension_data：
          {security: {...}, energy: {...}, operation: {...}}

设计约定：
    - 单个工具失败只影响该工具，不影响整个报告（raw 里记录 _error）；
    - 每个维度都产出 summary（中文摘要，放在字典最前面），
      因为 gen_dimension_analysis 只截取 JSON 的前若干字符喂给子维度 prompt；
    - 不在本模块里编造业务数字，取不到就标记「数据缺失」。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 维度中文名 -> dimension_data 的 key
DIMENSION_KEYS = {"安防": "security", "能耗": "energy", "运营": "operation"}
DIMENSION_ORDER = ["安防", "能耗", "运营"]

# 报告内容（前端「报告内容」勾选项）-> 所属维度
# 通行/设备/食堂/会议/巡检 都归到运营维度，只是取数时按项裁剪工具
TOPIC_TO_DIMENSION = {
    "安防": "安防",
    "能耗": "能耗",
    "通行": "运营",
    "网络": "运营",
    "设备": "运营",
    "食堂": "运营",
    "会议": "运营",
    "巡检": "运营",
}
TOPIC_ORDER = ["安防", "能耗", "通行", "网络", "设备", "食堂", "会议", "巡检"]

# 运营维度下每个内容项依赖的工具（细化取数）
OPERATION_TOPIC_TOOLS = {
    "通行": ("personnel", "parking"),
    "设备": ("device_health", "equip_class"),
    "食堂": ("canteen",),
    "会议": ("meeting",),
    "巡检": ("inspection",),
}

# 暂未接入 MCP 的内容项：报告里明确标注「数据缺失」，不编造
UNSUPPORTED_TOPICS = {"网络"}

# 报告周期预设（前端「报告周期」按钮组）
PERIOD_LABELS = ("日报", "周报", "月报")

# 采集时的单工具超时（秒）
_TOOL_TIMEOUT = 20.0


# ============================================================
# 通用解析
# ============================================================
def _extract_text(result: Any) -> str:
    """从 MCP / LangChain 工具的返回里取出文本。"""
    if isinstance(result, str):
        return result

    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            result = content

    if isinstance(result, list) and result:
        first = result[0]
        if isinstance(first, dict):
            return first.get("text") or str(first)

    return str(result)


def _parse_json_text(text: Any) -> Any:
    """尽量把工具返回的文本解析成 JSON；失败返回 None。"""
    if not isinstance(text, str):
        return text
    s = text.strip()
    if not s:
        return None
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return None


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _rows(value: Any) -> List[Dict[str, Any]]:
    """兼容 rows / data / list 三种列表包装。"""
    if isinstance(value, list):
        return [x for x in value if isinstance(x, dict)]
    if isinstance(value, dict):
        for key in ("rows", "data", "list", "items"):
            v = value.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
            # data 里再套一层 {data: [...]}
            if isinstance(v, dict):
                for k2 in ("rows", "data", "list"):
                    v2 = v.get(k2)
                    if isinstance(v2, list):
                        return [x for x in v2 if isinstance(x, dict)]
    return []


def _first_num(data: Any, keys: List[str]) -> Optional[float]:
    d = _as_dict(data)
    for k in keys:
        v = d.get(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            try:
                return float(v.replace(",", "").replace("%", ""))
            except ValueError:
                continue
    return None


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").replace("%", "").strip())
        except ValueError:
            return None
    return None


def _fmt_num(value: Optional[float]) -> str:
    if value is None:
        return "数据缺失"
    if abs(value - int(value)) < 1e-6:
        return str(int(value))
    return f"{value:.1f}"


def _fmt_pct(value: Optional[float]) -> str:
    if value is None:
        return "数据缺失"
    return f"{value:g}%"


def _compact(value: Any, limit: int = 1200) -> Any:
    """限制单个工具返回的体积，避免 prompt 过长。"""
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    if len(text) <= limit:
        return value
    return text[:limit] + "…"


def _iso_range(params: Dict[str, Any]) -> Tuple[str, str]:
    start = params.get("start_date") or ""
    end = params.get("end_date") or ""
    return (
        f"{start}T00:00:00" if start else "",
        f"{end}T23:59:59" if end else "",
    )


def _period_label(params: Dict[str, Any]) -> str:
    start = params.get("start_date") or ""
    end = params.get("end_date") or ""
    period = params.get("period") or ""
    if start and end:
        rng = start if start == end else f"{start}~{end}"
    else:
        rng = start or end or "未知周期"
    return f"{period} {rng}" if period else rng


def _period_dates(period: str, today: date) -> Optional[Tuple[str, str]]:
    """报告周期预设 -> (开始日期, 结束日期)。"""
    if period == "日报":
        return today.isoformat(), today.isoformat()
    if period == "周报":
        return (today - timedelta(days=6)).isoformat(), today.isoformat()
    if period == "月报":
        return f"{today.year}-{today.month:02d}-01", today.isoformat()
    return None


def _normalize_topics(value: Any) -> List[str]:
    """把前端传的内容项规范成固定顺序，丢弃不认识的项。"""
    if isinstance(value, str):
        value = [v for v in re.split(r"[,，、;；\s]+", value) if v]
    if not isinstance(value, (list, tuple, set)):
        return []
    picked = {str(v).strip() for v in value}
    return [t for t in TOPIC_ORDER if t in picked]


def _parse_topics_by_rules(text: str) -> List[str]:
    """从自然语言里识别报告内容项（规则兜底）。"""
    return [t for t in TOPIC_ORDER if t in text]


def _topics_to_dimensions(topics: List[str]) -> List[str]:
    dims = {TOPIC_TO_DIMENSION[t] for t in topics if t in TOPIC_TO_DIMENSION}
    return [d for d in DIMENSION_ORDER if d in dims]


# ============================================================
# 指令解析
# ============================================================
_DIM_KEYWORDS = {
    "安防": ("安防", "安全", "告警", "监控", "周界", "消防", "入侵"),
    "能耗": ("能耗", "能源", "用电", "用水", "水电", "电费", "水费", "节能", "电耗", "水耗"),
    "运营": ("运营维度", "运营数据", "运营情况", "综合运营", "园区运营"),
}


def _month_last_day(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - timedelta(days=1)).day


def _safe_date(y: int, m: int, d: int) -> Optional[date]:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _month_range(year: int, month: int) -> Tuple[date, date]:
    start = date(year, month, 1)
    end = date(year, month, _month_last_day(year, month))
    return start, end


def _parse_dates_by_rules(text: str, today: date) -> Dict[str, str]:
    """规则日期解析，作为 LLM 解析的兜底。"""
    text = text.strip()

    # 1) 显式区间：2025年1月1日至2025年12月31日 / 2025-01-01~2025-12-31
    m = re.search(
        r"(\d{4})\s*[年/-]\s*(\d{1,2})\s*(?:[月/-]\s*(\d{1,2})\s*日?)?\s*"
        r"(?:至|到|~|～|—|–|--)\s*"
        r"(\d{4})\s*[年/-]\s*(\d{1,2})\s*(?:[月/-]\s*(\d{1,2})\s*日?)?",
        text,
    )
    if m:
        y1, m1, d1, y2, m2, d2 = m.groups()
        start = _safe_date(int(y1), int(m1), int(d1) if d1 else 1)
        end = _safe_date(
            int(y2), int(m2), int(d2) if d2 else _month_last_day(int(y2), int(m2))
        )
        if start and end:
            return {"start_date": start.isoformat(), "end_date": end.isoformat()}

    # 2) 年 + 月：2025年10月
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月", text)
    if m:
        start, end = _month_range(int(m.group(1)), int(m.group(2)))
        return {"start_date": start.isoformat(), "end_date": end.isoformat()}

    # 3) 只给年份：2025年
    m = re.search(r"(\d{4})\s*年", text)
    if m:
        year = int(m.group(1))
        return {"start_date": f"{year}-01-01", "end_date": f"{year}-12-31"}

    # 4) 相对时间
    if "去年" in text or "上一年" in text:
        year = today.year - 1
        return {"start_date": f"{year}-01-01", "end_date": f"{year}-12-31"}
    if "今年" in text or "本年" in text or "本年度" in text:
        return {"start_date": f"{today.year}-01-01", "end_date": today.isoformat()}
    if "上个月" in text or "上月" in text:
        first = date(today.year, today.month, 1) - timedelta(days=1)
        start, end = _month_range(first.year, first.month)
        return {"start_date": start.isoformat(), "end_date": end.isoformat()}
    if "本月" in text or "这个月" in text:
        return {"start_date": f"{today.year}-{today.month:02d}-01", "end_date": today.isoformat()}
    if "本周" in text or "这一周" in text:
        start = today - timedelta(days=today.weekday())
        return {"start_date": start.isoformat(), "end_date": today.isoformat()}

    m = re.search(r"(?:最近|近)\s*(\d{1,2})\s*天", text)
    if m:
        days = max(1, int(m.group(1)))
        return {
            "start_date": (today - timedelta(days=days - 1)).isoformat(),
            "end_date": today.isoformat(),
        }
    if "最近一周" in text or "近一周" in text or "近7天" in text:
        return {
            "start_date": (today - timedelta(days=6)).isoformat(),
            "end_date": today.isoformat(),
        }
    m = re.search(r"(?:最近|近)\s*(\d{1,2})\s*个?月", text)
    if m:
        months = max(1, int(m.group(1)))
        start = today - timedelta(days=30 * months)
        return {"start_date": start.isoformat(), "end_date": today.isoformat()}

    # 5) 默认：本年度 1 月 1 日至今
    return {"start_date": f"{today.year}-01-01", "end_date": today.isoformat()}


def _dimension_probe_text(text: str) -> str:
    # 「运营报告 / 运营分析 / 运营情况」都是文档类型，不是维度诉求，先剔除再判断
    probe = text
    for phrase in (
        "运营情况分析报告", "运营分析报告", "运营情况报告",
        "运营分析", "运营情况", "运营报告",
    ):
        probe = probe.replace(phrase, "")
    return probe


def _has_explicit_dimension(text: str) -> bool:
    probe = _dimension_probe_text(text)
    return any(
        any(k in probe for k in _DIM_KEYWORDS[dim])
        for dim in DIMENSION_ORDER
    )


def _parse_dimensions_by_rules(text: str) -> List[str]:
    probe = _dimension_probe_text(text)
    found = []
    for dim in DIMENSION_ORDER:
        keywords = _DIM_KEYWORDS[dim]
        if any(k in probe for k in keywords):
            found.append(dim)
    if not found:
        return list(DIMENSION_ORDER)
    return [d for d in DIMENSION_ORDER if d in found]


def _parse_company_by_rules(text: str) -> str:
    m = re.search(r"([一-龥A-Za-z0-9]{2,20}?(?:公司|集团|园区))", text)
    if not m:
        return "园区"
    name = m.group(1)
    name = re.sub(r"^(?:请|帮我|帮忙|麻烦|为|给|对|生成|输出|撰写|编写|写|一份|一个)+", "", name)
    name = name.strip("的")
    return name or "园区"


def _normalize_date(value: Any, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    s = value.strip().replace("/", "-")
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if not m:
        return fallback
    d = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return d.isoformat() if d else fallback


class DocDataCollector:
    """指令解析 + 真实数据采集。"""

    def __init__(self, llm: Any = None):
        self.llm = llm

    # ---------- 指令解析 ----------
    async def parse_instruction(
        self,
        instruction: str,
        overrides: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        today = date.today()
        fallback = {
            "company": _parse_company_by_rules(instruction),
            **_parse_dates_by_rules(instruction, today),
            "dimensions": _parse_dimensions_by_rules(instruction),
            "topics": _parse_topics_by_rules(instruction),
        }
        explicit_dim = _has_explicit_dimension(instruction)

        llm_result = await self._llm_parse(instruction, today)
        params = dict(fallback)
        if isinstance(llm_result, dict):
            if llm_result.get("company"):
                params["company"] = str(llm_result["company"]).strip() or params["company"]
            params["start_date"] = _normalize_date(
                llm_result.get("start_date"), params["start_date"]
            )
            params["end_date"] = _normalize_date(
                llm_result.get("end_date"), params["end_date"]
            )
            dims = llm_result.get("dimensions")
            # 指令里没点名维度时，一律按三个维度出报告；
            # 避免 LLM 把「运营报告」误解成只要运营维度
            if isinstance(dims, list) and explicit_dim:
                picked = [d for d in dims if d in DIMENSION_KEYS]
                if picked:
                    params["dimensions"] = [d for d in DIMENSION_ORDER if d in picked]

        # 调用方显式传的字段优先（模态框 / 表单）
        overrides = overrides or {}

        # 1) 报告周期预设：只有没显式给日期时才生效
        period = overrides.get("period") or ""
        if period in PERIOD_LABELS:
            params["period"] = period
            if not overrides.get("start_date") and not overrides.get("end_date"):
                rng = _period_dates(period, today)
                if rng:
                    params["start_date"], params["end_date"] = rng

        # 2) 报告内容项：决定取哪些维度和哪些子主题
        override_topics = _normalize_topics(overrides.get("topics"))
        if override_topics:
            # 模态框勾选是明确诉求：维度一律由内容项推导（除非另外显式给了 dimensions）
            params["topics"] = override_topics
            if not overrides.get("dimensions"):
                params["dimensions"] = _topics_to_dimensions(override_topics)
        else:
            topics = _normalize_topics(params.get("topics"))
            if topics:
                params["topics"] = topics
                dims_from_topics = _topics_to_dimensions(topics)
                if explicit_dim:
                    # 指令点名的维度 + 内容项隐含的维度取并集
                    # 例：「能耗和食堂报告」= 能耗维度 + 运营维度（食堂）
                    merged = set(params["dimensions"]) | set(dims_from_topics)
                    params["dimensions"] = [
                        d for d in DIMENSION_ORDER if d in merged
                    ]
                else:
                    params["dimensions"] = dims_from_topics

        if overrides.get("company"):
            params["company"] = str(overrides["company"]).strip()
        if overrides.get("start_date"):
            params["start_date"] = _normalize_date(
                overrides["start_date"], params["start_date"]
            )
        if overrides.get("end_date"):
            params["end_date"] = _normalize_date(
                overrides["end_date"], params["end_date"]
            )
        if isinstance(overrides.get("dimensions"), list):
            picked = [d for d in overrides["dimensions"] if d in DIMENSION_KEYS]
            if picked:
                params["dimensions"] = [d for d in DIMENSION_ORDER if d in picked]

        if params["start_date"] > params["end_date"]:
            params["start_date"], params["end_date"] = (
                params["end_date"],
                params["start_date"],
            )
        if not params["dimensions"]:
            params["dimensions"] = list(DIMENSION_ORDER)

        params["instruction"] = instruction
        return params

    async def _llm_parse(self, instruction: str, today: date) -> Optional[Dict[str, Any]]:
        if self.llm is None or not instruction.strip():
            return None
        prompt = (
            "你是园区运营报告的参数解析器。根据用户指令提取报告参数，"
            "只输出一个 JSON 对象，不要输出任何解释。\n"
            f"今天是 {today.isoformat()}。\n\n"
            "JSON 字段：\n"
            '  "company": 报告主体，如「中通服园区」「设计院」；没提到就用「园区」；\n'
            '  "start_date": 开始日期，格式 YYYY-MM-DD；\n'
            '  "end_date": 结束日期，格式 YYYY-MM-DD；\n'
            '  "dimensions": 要分析的维度数组，只能取 "安防" / "能耗" / "运营"；\n'
            "解析规则：\n"
            "1. 「今年」指今年 1 月 1 日至今天；「去年」指去年全年；\n"
            "2. 「本月」「上月」「本周」「最近一周」「最近 N 天」按今天推算；\n"
            "3. 只提到「生成XX报告」而没有点名维度时，三个维度全部返回；\n"
            "4. 「运营报告」是文档类型，不代表只需要运营维度；\n"
            "5. 日期必须落在合理范围，开始日期不能晚于结束日期。\n\n"
            f"用户指令：{instruction}\n"
        )
        try:
            resp = await self.llm.ainvoke(prompt)
            content = getattr(resp, "content", None)
            if isinstance(content, list):
                content = "".join(
                    b.get("text", "") if isinstance(b, dict) else str(b)
                    for b in content
                )
            parsed = _parse_json_text(content)
            return parsed if isinstance(parsed, dict) else None
        except Exception as e:
            logger.warning("报告指令 LLM 解析失败，使用规则兜底：%s", e)
            return None

    # ---------- 数据采集 ----------
    async def collect(
        self,
        params: Dict[str, Any],
        tools: Dict[str, Any],
        emit: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    ) -> Dict[str, Any]:
        emit = emit or _noop

        data: Dict[str, Any] = {}
        for dim in params.get("dimensions") or DIMENSION_ORDER:
            key = DIMENSION_KEYS.get(dim)
            if not key:
                continue
            await emit(
                {
                    "type": "doc_step",
                    "step": f"collect:{key}",
                    "status": "running",
                    "message": f"正在获取{dim}数据…",
                }
            )
            try:
                if dim == "安防":
                    block = await self._collect_security(tools, params)
                elif dim == "能耗":
                    block = await self._collect_energy(tools, params)
                else:
                    block = await self._collect_operation(tools, params)
            except Exception as e:
                logger.error("采集%s数据失败：%s", dim, e, exc_info=True)
                block = {
                    "type": dim,
                    "date": _period_label(params),
                    "summary": f"{dim}数据获取失败：{e}",
                    "overview": {},
                    "raw": {},
                }
            data[key] = block
            await emit(
                {
                    "type": "doc_data",
                    "dimension": dim,
                    "key": key,
                    "ok": _has_payload(block.get("raw")),
                    "summary": block.get("summary", ""),
                }
            )
            await emit(
                {
                    "type": "doc_step",
                    "step": f"collect:{key}",
                    "status": "done",
                    "message": f"{dim}数据已获取",
                }
            )
        return data

    async def _invoke(self, tools: Dict[str, Any], name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        tool = tools.get(name)
        if tool is None:
            return {"_error": f"工具未加载：{name}"}
        try:
            result = await asyncio.wait_for(tool.ainvoke(args), timeout=_TOOL_TIMEOUT)
            text = _extract_text(result)
            parsed = _parse_json_text(text)
            return {
                "text": text,
                "data": parsed if parsed is not None else text,
            }
        except asyncio.TimeoutError:
            logger.warning("报告数据采集超时：%s", name)
            return {"_error": f"调用超时：{name}"}
        except Exception as e:
            logger.warning("报告数据采集失败 %s：%s", name, e)
            return {"_error": str(e)}

    async def _call_many(
        self,
        tools: Dict[str, Any],
        spec: Dict[str, tuple],
    ) -> Dict[str, Dict[str, Any]]:
        names = list(spec.keys())
        results = await asyncio.gather(
            *(self._invoke(tools, spec[n][0], spec[n][1]) for n in names)
        )
        return dict(zip(names, results))

    # ---------- 安防 ----------
    async def _collect_security(self, tools: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        start_iso, end_iso = _iso_range(params)
        window = {"startTime": start_iso, "endTime": end_iso}

        raw = await self._call_many(
            tools,
            {
                "alarm_view": ("security:getAlarmView", {}),
                "ai_alert_situation": ("security:getAiAlertSituation", dict(window)),
                "alert_trend": ("security:getAlertSituation", dict(window)),
                "alarm_list_with_type": ("security:getAlarmListWithType", dict(window)),
                "security_index": ("security:getSecurityIndex", {}),
                "patrol": ("security:getPatrolMission", {}),
            },
        )

        view = _payload(raw.get("alarm_view"))
        ai_rows = _rows(_payload(raw.get("ai_alert_situation")))
        type_rows = _rows(_payload(raw.get("alarm_list_with_type")))
        trend_rows = _rows(_payload(raw.get("alert_trend")))
        index_payload = _payload(raw.get("security_index"))
        patrol_payload = _payload(raw.get("patrol"))

        total_alerts = _first_num(view, ["totalAlarmCount", "yearCount", "total"])
        today_alerts = _first_num(view, ["todayAlarmCount", "todayCount"])
        algorithm_count = _first_num(view, ["algorithmTypeCount", "alarmTypeCount"])
        accuracy = _first_num(view, ["accuracy"])

        if total_alerts is None and type_rows:
            total_alerts = sum(
                _num(r.get("count")) or 0 for r in type_rows
            ) or None

        distribution = []
        for r in type_rows or ai_rows:
            name = r.get("type") or r.get("类别") or r.get("name") or r.get("alarmType")
            count = _num(r.get("count") if r.get("count") is not None else r.get("value"))
            pct = _num(r.get("percentage") if r.get("percentage") is not None else r.get("ratio"))
            if name or count is not None:
                distribution.append(
                    {
                        "name": str(name) if name is not None else "未知",
                        "count": count,
                        "percentage": _fmt_pct(pct) if pct is not None else None,
                    }
                )

        trend = []
        for r in trend_rows:
            label = r.get("date") or r.get("timeLabel") or r.get("time")
            count = _num(r.get("count") if r.get("count") is not None else r.get("value"))
            if label is not None and count is not None:
                trend.append({"date": str(label), "count": count})

        security_index = None
        if isinstance(index_payload, dict):
            security_index = _first_num(index_payload, ["index", "score"])
            if security_index is None:
                rows = _rows(index_payload)
                if rows:
                    security_index = _first_num(rows[0], ["index", "score", "value"])

        patrol_count = None
        if isinstance(patrol_payload, dict):
            patrol_count = _first_num(patrol_payload, ["total"])
            if patrol_count is None:
                patrol_count = len(_rows(patrol_payload)) or None

        overview = {
            "total_alerts": total_alerts,
            "today_alerts": today_alerts,
            "algorithm_type_count": algorithm_count,
            "recognition_accuracy": _fmt_pct(accuracy) if accuracy is not None else "数据缺失",
            "security_index": security_index,
            "alarm_type_distribution": distribution,
            "alarm_trend": trend[:31],
            "patrol_mission_count": patrol_count,
            # 兼容历史 prompt 字段（取不到就如实标注）
            "alert_type_ratio": {
                "intrusion_alert": _ratio_of(distribution, ("入侵", "安防")),
                "abnormal_gathering_alert": _ratio_of(distribution, ("聚集", "管理")),
                "fire_alert": _ratio_of(distribution, ("消防", "环境")),
            },
            "severe_alerts": {"count": "数据缺失"},
            "face_recognition_match_rate": {"rate": "数据缺失"},
            "cctv_online_rate": {"rate": "数据缺失"},
        }

        summary = self._security_summary(overview)
        period = _period_label(params)
        return {
            "type": "安防",
            "date": period,
            "start_date": params.get("start_date"),
            "end_date": params.get("end_date"),
            "summary": f"统计周期：{period}；{summary}" if summary else f"统计周期：{period}；安防数据获取为空。",
            "overview": overview,
            "raw": {k: _raw_entry(v) for k, v in raw.items()},
        }

    def _security_summary(self, ov: Dict[str, Any]) -> str:
        parts = []
        if ov.get("total_alerts") is not None:
            parts.append(f"累计告警 {_fmt_num(ov['total_alerts'])} 次")
        if ov.get("today_alerts") is not None:
            parts.append(f"今日告警 {_fmt_num(ov['today_alerts'])} 次")
        if ov.get("algorithm_type_count") is not None:
            parts.append(f"算法类型 {_fmt_num(ov['algorithm_type_count'])} 类")
        if ov.get("recognition_accuracy") not in (None, "数据缺失"):
            parts.append(f"AI 识别准确率 {ov['recognition_accuracy']}")
        if ov.get("security_index") is not None:
            parts.append(f"安全指数 {_fmt_num(ov['security_index'])}")
        dist = ov.get("alarm_type_distribution") or []
        if dist:
            dist_text = "、".join(
                f"{d['name']} {_fmt_num(d['count'])} 次"
                + (f"（{d['percentage']}）" if d.get("percentage") else "")
                for d in dist
            )
            parts.append(f"告警类型分布：{dist_text}")
        trend = ov.get("alarm_trend") or []
        if trend:
            peak = max(trend, key=lambda x: x["count"])
            parts.append(
                f"告警趋势：{trend[0]['date']}~{trend[-1]['date']}，"
                f"峰值 {peak['date']} 共 {_fmt_num(peak['count'])} 次"
            )
        if ov.get("patrol_mission_count") is not None:
            parts.append(f"巡查任务 {_fmt_num(ov['patrol_mission_count'])} 项")
        return "；".join(parts) + "。" if parts else "安防数据获取为空。"

    # ---------- 能耗 ----------
    async def _collect_energy(self, tools: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        raw = await self._call_many(
            tools,
            {
                "overall_energy": ("energy:getOverallEnergyConsu", {}),
                "device_status": ("energy:getDeviceStatus", {}),
                "metering": ("energy:getMeteringEquipment", {}),
                "electricity_rank": ("energy:getElectricityUsageRanking", {}),
                "water_rank": ("energy:getWaterUsageRanking", {}),
            },
        )

        overall = _payload(raw.get("overall_energy"))
        elec_annual, elec_today = _energy_pair(overall, "electricity")
        water_annual, water_today = _energy_pair(overall, "water")

        device_status = _as_dict(_payload(raw.get("device_status")))
        online = _first_num(device_status, ["onlineCount", "online"])
        offline = _first_num(device_status, ["offlineCount", "offline"])

        metering = _as_dict(_payload(raw.get("metering")))
        elec_meters = _first_num(metering, ["electricityMeterCount"])
        water_meters = _first_num(metering, ["waterMeterCount"])

        elec_rank = _rank_rows(_payload(raw.get("electricity_rank")), limit=5)
        water_rank = _rank_rows(_payload(raw.get("water_rank")), limit=5)

        overview = {
            "electricity_annual": elec_annual,
            "electricity_today": elec_today,
            "water_annual": water_annual,
            "water_today": water_today,
            "device_online": online,
            "device_offline": offline,
            "electricity_meter_count": elec_meters,
            "water_meter_count": water_meters,
            "electricity_rank": elec_rank,
            "water_rank": water_rank,
        }

        parts = []
        if elec_annual is not None:
            parts.append(f"累计用电 {_fmt_num(elec_annual)} 千瓦时")
        if elec_today is not None:
            parts.append(f"今日用电 {_fmt_num(elec_today)} 千瓦时")
        if water_annual is not None:
            parts.append(f"累计用水 {_fmt_num(water_annual)} 吨")
        if water_today is not None:
            parts.append(f"今日用水 {_fmt_num(water_today)} 吨")
        if online is not None or offline is not None:
            parts.append(
                f"能耗设备在线 {_fmt_num(online)} 台、离线 {_fmt_num(offline)} 台"
            )
        if elec_meters is not None or water_meters is not None:
            parts.append(
                f"表具：电表 {_fmt_num(elec_meters)} 个、水表 {_fmt_num(water_meters)} 个"
            )
        if elec_rank:
            parts.append(
                "用电排名：" + "、".join(f"{r['name']} {_fmt_num(r['value'])}" for r in elec_rank)
            )
        if water_rank:
            parts.append(
                "用水排名：" + "、".join(f"{r['name']} {_fmt_num(r['value'])}" for r in water_rank)
            )

        period = _period_label(params)
        body = "；".join(parts) + "。" if parts else "能耗数据获取为空。"
        return {
            "type": "能耗",
            "date": period,
            "start_date": params.get("start_date"),
            "end_date": params.get("end_date"),
            "summary": f"统计周期：{period}；{body}",
            "overview": overview,
            "raw": {k: _raw_entry(v) for k, v in raw.items()},
        }

    # ---------- 运营 ----------
    async def _collect_operation(self, tools: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        start_iso, end_iso = _iso_range(params)
        window = {"startTime": start_iso, "endTime": end_iso}

        # 报告内容项 -> 要调用的工具（不勾选的不取数）
        all_topics = params.get("topics") or []
        op_topics = [t for t in all_topics if TOPIC_TO_DIMENSION.get(t) == "运营"]
        topic_missing = [t for t in op_topics if t in UNSUPPORTED_TOPICS]

        spec = {
            "basic_info": ("overview:getBasicInfo", {}),
            "device_health": ("overview:getDeviceHealth", {}),
            "personnel": ("person_status:getTodayPersonnelAffairs", {}),
            "parking": ("vehicle_status:getParkingSpace", {}),
            "canteen": ("canteen:getDiningCount", dict(window)),
            "meeting": ("meeting:getMeetingStatistics", {}),
            "inspection": ("inspection:getTodayInspection", {}),
            "equip_class": ("device:getEquipClass", {}),
        }
        if op_topics:
            wanted = {"basic_info"}
            for topic in op_topics:
                wanted.update(OPERATION_TOPIC_TOOLS.get(topic, ()))
            spec = {k: v for k, v in spec.items() if k in wanted}

        raw = await self._call_many(tools, spec)

        basic = _as_dict(_payload(raw.get("basic_info")))
        center_area = _first_num(basic, ["centerArea", "area"])
        iot_count = _first_num(basic, ["iotDeviceCount", "deviceCount"])

        health_rows = _rows(_payload(raw.get("device_health")))
        health_scores = []
        online_pcts = []
        for r in health_rows:
            score = _num(r.get("score"))
            if score is not None:
                health_scores.append(score)
            rate = _as_dict(r.get("onlineRate"))
            pct = _num(rate.get("percent"))
            if pct is not None:
                online_pcts.append(pct)

        personnel_payload = _payload(raw.get("personnel"))
        realtime_count = None
        enter_count = None
        leave_count = None
        if isinstance(personnel_payload, dict):
            realtime_count = _first_num(personnel_payload, ["realtimeCount", "realtime"])
            enter_count = _first_num(personnel_payload, ["enterCount", "enter"])
            leave_count = _first_num(personnel_payload, ["leaveCount", "leave"])
        personnel_text = (
            personnel_payload
            if isinstance(personnel_payload, str)
            else None
        )

        parking_rows = _rows(_payload(raw.get("parking")))
        parking = parking_rows[0] if parking_rows else {}
        parking_total = _num(parking.get("total"))
        parking_used = _num(parking.get("used"))
        parking_remain = _num(parking.get("remain"))

        canteen_rows = _rows(_payload(raw.get("canteen")))
        dining_count = None
        if canteen_rows:
            dining_count = _num(canteen_rows[0].get("count")) or sum(
                _num(r.get("count")) or 0 for r in canteen_rows
            )
        if dining_count is None:
            canteen_payload = _as_dict(_payload(raw.get("canteen")))
            dining_count = _first_num(canteen_payload, ["total", "count"])

        meeting_rows = _rows(_payload(raw.get("meeting")))
        meeting_count = sum(_num(r.get("count")) or 0 for r in meeting_rows) or None
        avg_duration = None
        if meeting_rows:
            durations = [_num(r.get("avgDuration")) for r in meeting_rows]
            durations = [d for d in durations if d is not None]
            if durations:
                avg_duration = sum(durations) / len(durations)

        inspection = _as_dict(_payload(raw.get("inspection")))
        task_count = _first_num(inspection, ["taskCount"])
        point_count = _first_num(inspection, ["pointCount"])
        completion_rate = _first_num(inspection, ["completionRate"])

        equip_rows = _rows(_payload(raw.get("equip_class")))
        equip_top = []
        for r in equip_rows[:5]:
            name = r.get("name")
            value = _num(r.get("value"))
            if name is not None:
                equip_top.append({"name": str(name), "value": value})

        overview = {
            "topics": op_topics,
            "topics_missing": topic_missing,
            "center_area": center_area,
            "iot_device_count": iot_count,
            "realtime_personnel": realtime_count,
            "today_enter": enter_count,
            "today_leave": leave_count,
            "personnel_text": personnel_text,
            "parking_total": parking_total,
            "parking_used": parking_used,
            "parking_remain": parking_remain,
            "dining_count": dining_count,
            "meeting_count": meeting_count,
            "meeting_avg_duration": avg_duration,
            "inspection_task_count": task_count,
            "inspection_point_count": point_count,
            "inspection_completion_rate": _fmt_pct(completion_rate) if completion_rate is not None else "数据缺失",
            "device_health_avg_score": round(sum(health_scores) / len(health_scores), 1) if health_scores else None,
            "device_online_rate_avg": _fmt_pct(sum(online_pcts) / len(online_pcts)) if online_pcts else "数据缺失",
            "device_category_top": equip_top,
        }

        parts = []
        if center_area is not None:
            parts.append(f"园区面积 {_fmt_num(center_area)} 万平方米")
        if iot_count is not None:
            parts.append(f"IoT 设备 {_fmt_num(iot_count)} 台")
        if realtime_count is not None:
            parts.append(f"实时在园 {_fmt_num(realtime_count)} 人")
        if enter_count is not None or leave_count is not None:
            parts.append(
                f"今日进入 {_fmt_num(enter_count)} 人、离开 {_fmt_num(leave_count)} 人"
            )
        if personnel_text:
            parts.append(f"人员态势原始信息：{personnel_text[:80]}")
        if parking_total is not None:
            parts.append(
                f"车位 {_fmt_num(parking_total)} 个，已用 {_fmt_num(parking_used)} 个，"
                f"剩余 {_fmt_num(parking_remain)} 个"
            )
        if dining_count is not None:
            parts.append(f"食堂就餐 {_fmt_num(dining_count)} 人次")
        if meeting_count is not None:
            parts.append(f"会议 {_fmt_num(meeting_count)} 场")
        if avg_duration is not None:
            parts.append(f"平均会议时长 {_fmt_num(avg_duration)} 分钟")
        if task_count is not None:
            parts.append(f"今日巡检任务 {_fmt_num(task_count)} 个")
        if completion_rate is not None:
            parts.append(f"巡检完成率 {_fmt_pct(completion_rate)}")
        if overview["device_health_avg_score"] is not None:
            parts.append(f"设备平均健康分 {_fmt_num(overview['device_health_avg_score'])}")
        if overview["device_online_rate_avg"] != "数据缺失":
            parts.append(f"设备平均在线率 {overview['device_online_rate_avg']}")
        if equip_top:
            parts.append(
                "设备分类：" + "、".join(
                    f"{e['name']} {_fmt_num(e['value'])} 台" for e in equip_top
                )
            )

        period = _period_label(params)
        body = "；".join(parts) + "。" if parts else "运营数据获取为空。"
        focus = f"本期关注：{'、'.join(op_topics)}。" if op_topics else ""
        missing = (
            f"{'、'.join(topic_missing)}暂无对应数据接口，报告中须标注「数据缺失」，不得编造。"
            if topic_missing
            else ""
        )
        return {
            "type": "运营",
            "date": period,
            "start_date": params.get("start_date"),
            "end_date": params.get("end_date"),
            "summary": f"统计周期：{period}；{focus}{missing}{body}",
            "overview": overview,
            "raw": {k: _raw_entry(v) for k, v in raw.items()},
        }


# ============================================================
# 小工具
# ============================================================
async def _noop(_event: Dict[str, Any]) -> None:
    return None


def _payload(result: Any) -> Any:
    """取出工具返回的实际业务数据。

    MCP 接口有两种包装：
      - {"code":200,"data":{...}}    → 取内层 data
      - {"code":200,"rows":[...]}    → 没有 data，保留整体，交给 _rows 解析
      - {"code":200,"todayAlarmCount":...} → 字段平铺，保留整体
    """
    if not isinstance(result, dict):
        return None
    if "_error" in result and "data" not in result:
        return None

    parsed = result.get("data")
    if isinstance(parsed, dict):
        inner = parsed.get("data")
        # 只有「外层仅剩 code/msg/data」时才剥壳，避免把业务字段误当包装层
        if inner is not None and set(parsed.keys()) <= {"code", "msg", "message", "data"}:
            return inner
    return parsed


def _raw_entry(result: Any) -> Any:
    """raw 里保留每个工具的返回；失败时保留错误信息，便于排查。"""
    if not isinstance(result, dict):
        return None
    if "_error" in result and "data" not in result:
        return {"_error": result["_error"]}
    return _compact(result.get("data"))


def _has_payload(raw: Any) -> bool:
    if not isinstance(raw, dict):
        return False
    for value in raw.values():
        if isinstance(value, dict) and "_error" in value:
            continue
        if value not in (None, {}, [], ""):
            return True
    return False


def _energy_pair(payload: Any, kind: str) -> Tuple[Optional[float], Optional[float]]:
    """兼容 {electricity:{annual,today}} 与 rows[{electricity:12000}] 两种口径。"""
    if isinstance(payload, dict):
        v = payload.get(kind)
        if isinstance(v, dict):
            return _num(v.get("annual")), _num(v.get("today"))
        if isinstance(v, (int, float)):
            return _num(v), None
        rows = _rows(payload)
        if rows:
            v2 = rows[0].get(kind)
            if isinstance(v2, dict):
                return _num(v2.get("annual")), _num(v2.get("today"))
            if v2 is not None:
                return _num(v2), None
    return None, None


def _rank_rows(payload: Any, limit: int = 5) -> List[Dict[str, Any]]:
    result = []
    for r in _rows(payload)[:limit]:
        name = r.get("area") or r.get("name") or r.get("label")
        value = _num(r.get("value") if r.get("value") is not None else r.get("count"))
        if name is not None:
            result.append({"name": str(name), "value": value})
    return result


def _ratio_of(distribution: List[Dict[str, Any]], keywords: tuple) -> Dict[str, Any]:
    for item in distribution:
        name = item.get("name") or ""
        if any(k in name for k in keywords):
            return {
                "ratio": item.get("percentage") or "数据缺失",
                "count": item.get("count"),
            }
    return {"ratio": "数据缺失"}
