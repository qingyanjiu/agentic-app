"""人员态势业务域。

一个工具覆盖该域下的所有子类型（实时/进入/离开/人数/流动/结构），
子类型由 extract_person_status_slots 解析，再到 MCP 工具名 —— 对模型完全透明。

这样处理的原因：人员态势的子类型区分靠中文关键词，模型自己判断反而不如
现有正则+分类器稳定；把它放在工具内部，既复用了既有逻辑，又不增加一次 LLM 往返。
"""
from __future__ import annotations

import logging
import re

from agent.intent.slots import extract_person_status_slots

from ..govern import govern
from ..registry import DomainSpec
from ..runtime import McpToolbox
from ..types import DomainPlan, ToolOutcome, ToolSpec, tool_schema

logger = logging.getLogger(__name__)

# 配置里人员态势的 server key（Java sidecar /mcp/personStatus）
SERVER = "local"

# query_type → MCP 工具名（原 legacy 图里的 _JAVA_TOOL_MAP 已随图删除，这里是唯一映射）
JAVA_TOOL_MAP = {
    "realtime": "person_status:getTodayPersonnelAffairs",
    "enter": "person_status:getTodayPersonnelAffairs",
    "leave": "person_status:getTodayPersonnelAffairs",
    "count": "person_status:getTodayPersonnelAffairs",
    "flow": "person_status:getTodayPersonnelFlow",
    "structure": "person_status:getPersonnelStructure",
}

_QUERY_PARAM = {
    "query": {
        "type": "string",
        "description": "用户的原始问句。工具会自己解析是「实时/进入/离开/人数/流动/结构」中的哪一类。",
    }
}


# 后端 getTodayPersonnelAffairs 的实际返回是一句中文：
#   「实时在园人数：375人；今日进入人数：472人；今日离开人数：97人。」
# 但它的 MCP 描述写的是三个字段名（realTimeNum / jingNum / chuNum），
# 模型照着描述去等 JSON，拿到文本就判"解析失败"，于是编出
# 「接口返回的是字符串而不是预期的 JSON 结构（类型转换报错）」这种话。
# 这里先把用户问的那一项抽出来，模型（或 L1 直出）拿到就是答案。
_PERSON_FIELD = {
    "realtime": (r"实时在园人数[：:]\s*(\d+)\s*人", "实时在园人数"),
    "count": (r"实时在园人数[：:]\s*(\d+)\s*人", "实时在园人数"),
    "enter": (r"今日进入人数[：:]\s*(\d+)\s*人", "今日进入人数"),
    "leave": (r"今日离开人数[：:]\s*(\d+)\s*人", "今日离开人数"),
}


# 三类指标各自的识别词。只有**恰好命中一类**才裁剪成那一项：
#   「现在在园多少人」       命中 1 类 -> 只给实时在园
#   「查询今日人员态势」      命中 0 类 -> 三项全给（泛问）
#   「包括在园、进入、离开」  命中 3 类 -> 三项全给（点名多项）
# 后两种都必须把后端那句原文交出去，否则模型只看到一个数字，
# 与 sidecar 里 tool description「没明确指定时返回全部三项」的契约相反。
_METRIC_HINTS = {
    "realtime": re.compile(
        r"在园|在线人数|实时.{0,4}人数|现在.{0,4}多少|当前.{0,4}多少|目前.{0,4}多少"
    ),
    "enter": re.compile(r"进入|入园|进来|进了多少|来了多少"),
    "leave": re.compile(r"离开|出园|出去|走了多少|出了多少"),
}


def _pick(raw: str, query_type: str, query: str = "") -> str:
    """从后端那句中文里挑出用户问的那一项。

    flow / structure 是图表数据（时间轴序列、公司人数），不是这句话，
    原样交给模型，别去动它。抽不到也原样返回——宁可多给，不能丢数据。
    """
    spec = _PERSON_FIELD.get(query_type)
    if spec is None:
        return raw
    if query:
        hits = [name for name, pat in _METRIC_HINTS.items() if pat.search(query)]
        # count 是兜底类型，问句只说「在园人数」时也该认作实时在园
        target = "realtime" if query_type == "count" else query_type
        if len(hits) != 1 or hits[0] != target:
            return raw
    pattern, label = spec
    match = re.search(pattern, raw or "")
    if not match:
        return raw
    return f"{label}：{match.group(1)}人"

# 「人员流动」的粒度只有三档，由 sidecar 的 type 参数决定，不能传区间。
# 顺序有意义：month 先判（「本月」里也有「月」），再 week，最后 day。
_FLOW_TYPE_HINTS = (
    (
        "month",
        ("本月", "这个月", "近30天", "近三十天", "近一个月", "最近一个月", "近1个月"),
    ),
    ("week", ("本周", "这周", "近7天", "近七天", "近一周", "近1周", "最近一周")),
    ("day", ("今天", "今日", "现在", "当前", "目前")),
)


def _flow_type(date_slot: dict) -> str | None:
    """把问句的时间粒度映射成 sidecar 的 type（day / week / month）。

    接口没有区间参数，所以「昨天」「上周」这类对不上的不返回：宁可用后端默认
    day 并由模型说明粒度，也不要把今天的数据说成昨天。
    """
    raw = str((date_slot or {}).get("raw") or "")
    for value, hints in _FLOW_TYPE_HINTS:
        if any(h in raw for h in hints):
            return value
    return None



def preview_plan(query: str) -> dict:
    slots = extract_person_status_slots(query)
    query_type = slots.get("query_type") or "count"
    tool = JAVA_TOOL_MAP.get(query_type)
    if tool is None:
        return {
            "domain": "person_status",
            "tool": None,
            "args": {},
            "note": f"query_type={query_type} 后端暂不支持，应直接告知用户",
        }
    args: dict = {}
    if query_type == "flow":
        flow_type = _flow_type(slots.get("date") or {})
        if flow_type:
            args["type"] = flow_type
    return {
        "domain": "person_status",
        "tool": tool,
        "args": args,
        "note": f"流动粒度={args['type']}" if args.get("type") else "按今天口径查询",
    }


def route_plan(query: str) -> DomainPlan:
    """预路由用：人员态势只有一个 loop 工具，子类型在工具内部判定。"""
    slots = extract_person_status_slots(query)
    query_type = slots.get("query_type") or "count"
    if query_type not in JAVA_TOOL_MAP:
        return DomainPlan(tool="", note=f"query_type={query_type} 后端暂未接入")
    return DomainPlan(tool="person_status__query", args={"query": query})


def build_domain(toolbox: McpToolbox) -> DomainSpec:
    async def person_status(raw_args: dict) -> ToolOutcome:
        query = str(raw_args.get("query") or "").strip()
        if not query:
            return ToolOutcome("缺少用户问题，无法解析查询条件。", is_error=True)

        slots = extract_person_status_slots(query)
        query_type = slots.get("query_type") or "count"
        tool_name = JAVA_TOOL_MAP.get(query_type)
        if tool_name is None:
            return ToolOutcome(
                f"人员态势的「{query_type}」查询暂未接入后端，请直接告诉用户这个口径暂时查不了。",
                is_error=True,
            )

        # 「人员流动」的粒度由 type 决定（day=今天按小时 / week=近7天 / month=近30天）。
        # 不传就是后端默认 day —— 用户问「本周进出」会拿到今天的按小时数据。
        args: dict = {}
        if query_type == "flow":
            flow_type = _flow_type(slots.get("date") or {})
            if flow_type:
                args["type"] = flow_type
        try:
            raw = await toolbox.call(SERVER, tool_name, args)
        except Exception as exc:
            logger.warning(f"[loop] 人员态势查询失败（{tool_name}）：{exc}")
            return ToolOutcome(f"人员态势查询失败：{exc}", is_error=True)

        return ToolOutcome(
            govern(_pick(raw, query_type, query), label=f"person_{query_type}")
        )

    return DomainSpec(
        name="person_status",
        description="人员态势：实时人数、进出、流动、人员结构（园区大屏口径）",
        tools=[
            ToolSpec(
                name="person_status__query",
                description=(
                    "查询人员态势数据。适用于「现在园区有多少人」「今天进了多少人」"
                    "「人员流动情况」「人员结构是怎样的」。"
                    "工具会自己判断是人数、进出、流动还是结构，调用方不需要指定子类型。"
                    "注意：后端返回的是**中文文本**（如「实时在园人数：375人；今日进入…」），"
                    "不是 JSON；本工具已按用户问的那一项抽好，直接读文本作答即可。"
                    "「人员流动」只有三种粒度：今天按小时(day)、近 7 天按天(week)、"
                    "近 30 天按天(month)，没有按任意日期回溯的能力。用户问「昨天/某天"
                    "的人员流动」时，如实说明只有这三种粒度，不要拿今天的数据当昨天。"
                ),
                parameters=tool_schema(_QUERY_PARAM, ["query"]),
                execute=person_status,
            )
        ],
        # 自检 / 发现去重用：这个域会打到哪些 MCP 工具（去重保序）
        mcp_tools=tuple(
            (SERVER, name) for name in dict.fromkeys(JAVA_TOOL_MAP.values())
        ),
    )
