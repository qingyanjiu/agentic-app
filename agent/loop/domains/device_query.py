"""设备查询业务域（资产库口径）。

两个工具：

- device_query__list：按类型/名称/编号/楼层列设备（listDeviceOnly，不分页）
- device_query__detail：按名称/编号定位到一台设备再查详情（先列表搜 id，再 getDeviceDetail）

筛选策略（重要）：
    MCP 的 listDeviceOnly 只接受 syncSource / status / name / code / spaceId 等参数，
    **没有按楼层筛选的参数**（spaceId 需要内部 id，用户给不出来）。
    所以这里的做法是：
      1) 只把 syncSource 下发给 Java（减少返回体）；
      2) 楼层、名称/编号在 Python 侧做确定性过滤；
      3) 模型只传用户原话，不需要知道 "1F" / "-1F" 这些编码。

楼层归一化：
    用户说法        数据里的写法        归一化结果
    1楼 / 一层      1F                 1F
    负一层 / -1楼   -1F                -1F
    11楼            11F                11F
    （注意：数据里负层是 -1F，不是 B1F）

名称过滤：
    走 rawName / name / code 三者的包含匹配（rawName 是原始设备名，和 name 有 100 行不同）。
"""
from __future__ import annotations

import json
import logging
import re

from agent.intent.slots import DEVICE_TYPE_DICT, extract_device_query_slots

from ..govern import govern
from ..registry import DomainSpec
from ..runtime import McpToolbox
from ..types import DomainPlan, ToolOutcome, ToolSpec, tool_schema

logger = logging.getLogger(__name__)

SERVER = "devicequery"
TOOL_LIST = "device_query:listDeviceOnly"
TOOL_DETAIL = "device_query:getDeviceDetail"

# 返回给模型的行数上限（超出的走 govern 落盘）
MAX_ROWS = 60

_QUERY_PARAM = {
    "query": {
        "type": "string",
        "description": "用户的原始问句。工具会自己解析设备类型、楼层、名称/编号，不要拆字段。",
    }
}

# 匹配 "3F" / "-1F" / "11F"，前后不能紧跟字母数字（避免 1F 命中 11F）
_FLOOR_RE = re.compile(r"(?<![\w])(-?\d+)\s*[Ff](?![\w])")

# 带楼层前缀的设备名，如 "3F-主楼梯MJ" / "-1F-电梯厅MJ"。
# 现有 slots 的 device_keyword 抽不到这种，这里做兜底。
_DEVICE_NAME_RE = re.compile(r"-?\d+[Ff]-[\u4e00-\u9fa5A-Za-z0-9]+")

_CN_DIGITS = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def _cn_to_int(text: str) -> int | None:
    """把 '1' / '一' / '十一' / '二十' 转成整数；失败返回 None。"""
    text = (text or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if text in _CN_DIGITS:
        return _CN_DIGITS[text]
    if "十" in text:
        head, _, tail = text.partition("十")
        tens = _CN_DIGITS.get(head, 1) if head else 1
        ones = _CN_DIGITS.get(tail, 0) if tail else 0
        return tens * 10 + ones
    return None


def normalize_floor(area: object) -> str | None:
    """把用户说的楼层归一成数据里的写法（如 '3F' / '-1F'）。

    支持：1楼、一层、3F、负一层、地下一层、-1楼、B1、11楼。
    注意数据里负层是 -1F，不是 B1F。
    """
    text = str(area or "").strip()
    if not text:
        return None

    # 负层：负一层 / 地下一层 / -1楼 / B1
    negative = re.search(r"(?:负|地下|地下层|B|b)\s*([一二三四五六七八九十\d]+)", text)
    if negative:
        num = _cn_to_int(negative.group(1))
        if num:
            return f"-{num}F"

    # -1F / -1楼 / -1层 这种直接写负号的
    negative_direct = re.match(r"^-\s*([一二三四五六七八九十\d]+)\s*(?:[Ff]|层|楼)?$", text)
    if negative_direct:
        num = _cn_to_int(negative_direct.group(1))
        if num:
            return f"-{num}F"

    # 正层：一层 / 1楼 / 3F / 11层
    positive = re.search(r"([一二三四五六七八九十\d]+)\s*(?:层|楼|[Ff])", text)
    if positive:
        num = _cn_to_int(positive.group(1))
        if num:
            return f"{num}F"
    return None


def _floors_in_row(row: dict) -> set[str]:
    """从 spaceName / name / rawName 里提取该设备所属楼层集合。"""
    text = " ".join(
        str(row.get(key) or "") for key in ("spaceName", "name", "rawName")
    )
    return {f"{m.group(1)}F" for m in _FLOOR_RE.finditer(text)}


def _row_matches_floor(row: dict, floor: str) -> bool:
    return floor in _floors_in_row(row)


def _row_matches_keyword(row: dict, keyword: str) -> bool:
    kw = keyword.strip().lower()
    if not kw:
        return True
    for key in ("rawName", "name", "code"):
        if kw in str(row.get(key) or "").lower():
            return True
    return False


def _sync_source_name(code: object) -> str:
    key = str(code) if code is not None else ""
    aliases = DEVICE_TYPE_DICT.get(key)
    return aliases[0] if aliases else (key or "未知类型")


def _rows_of(raw_text: str) -> list[dict]:
    try:
        data = json.loads(raw_text)
    except Exception:
        return []

    def walk(value):
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
        if isinstance(value, dict):
            for key in ("data", "list", "rows", "records", "devices"):
                if key in value:
                    return walk(value[key])
        return []

    return walk(data)


def _format_rows(
    rows: list[dict],
    *,
    total: int | None = None,
    floor: str | None = None,
    keyword: str | None = None,
) -> str:
    if not rows:
        head = "没有查询到符合条件的设备。"
        if total is not None:
            head += f"（园区共 {total} 台设备"
            if floor:
                head += f"，未匹配到 {floor} 楼层"
            if keyword:
                head += f"，未匹配到「{keyword}」"
            head += "）"
        return head

    parts: list[str] = []
    if total is not None and total != len(rows):
        parts.append(f"共 {total} 台设备，按条件筛出 {len(rows)} 台")
    else:
        parts.append(f"共 {len(rows)} 台设备")
    if floor:
        parts.append(f"楼层 {floor}")
    if keyword:
        parts.append(f"关键词「{keyword}」")
    header = "，".join(parts) + "（status 是启用状态，不是在线状态）："

    lines = [header]
    for row in rows[:MAX_ROWS]:
        name = row.get("rawName") or row.get("name") or "未命名"
        code = row.get("code") or ""
        device_id = row.get("id") or ""
        space = row.get("spaceName") or ""
        status = row.get("status")
        status_text = {"0": "停用", "1": "启用", "2": "维修", "3": "报废"}.get(
            str(status), str(status or "")
        )
        parts_row = [f"- {name}"]
        if code:
            parts_row.append(f"编号 {code}")
        if device_id:
            parts_row.append(f"id {device_id}")
        if space:
            parts_row.append(f"位置 {space}")
        if status_text:
            parts_row.append(f"状态 {status_text}")
        parts_row.append(f"类型 {_sync_source_name(row.get('syncSource'))}")
        lines.append("｜".join(parts_row))

    if len(rows) > MAX_ROWS:
        lines.append(f"（其余 {len(rows) - MAX_ROWS} 台未展示，可加楼层或名称缩小范围）")
    return "\n".join(lines)


def _build_list_args(slots: dict) -> dict:
    """只下发 syncSource；楼层和名称在本地过滤。"""
    args: dict = {}
    device_type = slots.get("device_type")
    if device_type not in (None, ""):
        args["syncSource"] = str(device_type)
    return args


def _filter_rows(
    rows: list[dict],
    slots: dict,
    query: str = "",
) -> tuple[list[dict], str | None, str | None]:
    """按楼层 + 关键词做确定性过滤，返回 (行, 楼层, 关键词)。

    楼层优先从**用户原句**里取：现有 slots.py 的 parse_area_slot 会把
    「负一层」解析成「1楼」，直接用 slots 的 area 会查错楼层。
    """
    floor = normalize_floor(query) or normalize_floor(slots.get("area"))
    keyword = (slots.get("device_keyword") or "").strip() or None
    if not keyword:
        # slots 抽不到 "3F-主楼梯MJ" 这类带楼层前缀的名称，这里兜底
        match = _DEVICE_NAME_RE.search(query)
        if match:
            keyword = match.group(0)

    if floor:
        rows = [row for row in rows if _row_matches_floor(row, floor)]
    if keyword:
        rows = [row for row in rows if _row_matches_keyword(row, keyword)]
    return rows, floor, keyword


def preview_plan(query: str) -> dict:
    """dry-run 用：展示会调用哪个 MCP 工具、带什么参数、本地怎么过滤。"""
    slots = extract_device_query_slots(query)
    return {
        "domain": "device_query",
        "tool": TOOL_LIST,
        "mcp_args": _build_list_args(slots),
        "local_filter": {
            "floor": normalize_floor(query) or normalize_floor(slots.get("area")),
            "keyword": slots.get("device_keyword") or None,
        },
        "note": "楼层/名称在工具内部过滤，不下发给 Java",
    }


def route_plan(query: str) -> DomainPlan:
    """预路由用：query → 调哪个 loop 工具、缺什么参数。

    返回的是 loop 工具名（device_query__list / detail）和它的宽松参数，
    不是 MCP 接口参数——后者由 execute 内部解析。
    """
    slots = extract_device_query_slots(query)
    if slots.get("query_type") == "device_detail":
        if not slots.get("device_keyword"):
            return DomainPlan(
                tool="device_query__detail",
                args={"query": query},
                missing=["device_keyword"],
                note="详情查询缺设备名称/编号",
            )
        return DomainPlan(tool="device_query__detail", args={"query": query})
    return DomainPlan(tool="device_query__list", args={"query": query})


def build_domain(toolbox: McpToolbox) -> DomainSpec:
    async def list_devices(raw_args: dict) -> ToolOutcome:
        query = str(raw_args.get("query") or "").strip()
        if not query:
            return ToolOutcome("缺少用户问题，无法解析查询条件。", is_error=True)

        slots = extract_device_query_slots(query)
        try:
            raw = await toolbox.call(SERVER, TOOL_LIST, _build_list_args(slots))
        except Exception as exc:
            logger.warning(f"[loop] 设备列表查询失败：{exc}")
            return ToolOutcome(f"设备列表查询失败：{exc}", is_error=True)

        rows = _rows_of(raw)
        total = len(rows)
        rows, floor, keyword = _filter_rows(rows, slots, query)
        logger.info(
            f"[device_query] 全量 {total} 台 → 过滤后 {len(rows)} 台"
            f"（floor={floor}, keyword={keyword}）"
        )
        return ToolOutcome(
            govern(
                _format_rows(rows, total=total, floor=floor, keyword=keyword),
                label="device_list",
            )
        )

    async def device_detail(raw_args: dict) -> ToolOutcome:
        query = str(raw_args.get("query") or "").strip()
        if not query:
            return ToolOutcome("缺少用户问题，无法解析查询条件。", is_error=True)

        slots = extract_device_query_slots(query)
        keyword = (slots.get("device_keyword") or "").strip()
        if not keyword:
            return ToolOutcome(
                "缺少要查询的设备。请向用户确认设备名称或设备编号，拿到后再调用本工具。",
                is_error=True,
            )

        # 1) 先按类型 + 楼层缩小范围，再按关键词定位 id
        try:
            raw = await toolbox.call(SERVER, TOOL_LIST, _build_list_args(slots))
        except Exception as exc:
            logger.warning(f"[loop] 设备定位失败：{exc}")
            return ToolOutcome(f"设备定位失败：{exc}", is_error=True)

        rows = _rows_of(raw)
        rows, floor, _ = _filter_rows(rows, {**slots, "device_keyword": ""}, query)
        matched = [row for row in rows if _row_matches_keyword(row, keyword)]

        if not matched:
            scope = f"{floor} " if floor else ""
            return ToolOutcome(
                f"没有找到{scope}名称或编号包含「{keyword}」的设备。"
                f"请让用户确认设备名称/编号，或改用列表查询看看有哪些设备。",
                is_error=True,
            )
        if len(matched) > 1:
            return ToolOutcome(
                govern(_format_rows(matched))
                + "\n\n匹配到多台设备，请让用户确认是哪一台（可提供编号）。",
                is_error=True,
            )

        device_id = matched[0].get("id")
        if not device_id:
            return ToolOutcome("查到的设备缺少内部 id，无法查询详情。", is_error=True)

        try:
            detail = await toolbox.call(SERVER, TOOL_DETAIL, {"id": device_id})
        except Exception as exc:
            logger.warning(f"[loop] 设备详情查询失败：{exc}")
            return ToolOutcome(f"设备详情查询失败：{exc}", is_error=True)

        return ToolOutcome(govern(detail, label="device_detail"))

    return DomainSpec(
        name="device_query",
        description="设备资产台账：查设备清单、查单台设备详情（资产库口径，status 表示启用/停用，不是在线状态）",
        tools=[
            ToolSpec(
                name="device_query__list",
                description=(
                    "查询设备资产清单。适用于「有哪些广播设备」「3楼的门禁都列一下」「负一层的监控」。"
                    "支持按设备类型、楼层、名称/编号筛选——楼层和名称由工具内部过滤，"
                    "调用方只需把用户原话传进来，不需要自己转换 1F / -1F 这类编码。"
                    "返回名称、编号、内部 id、位置、启用状态。"
                    "注意：这是资产库口径，status 是启用/停用/维修/报废，不是实时在线状态。"
                ),
                parameters=tool_schema(_QUERY_PARAM, ["query"]),
                execute=list_devices,
            ),
            ToolSpec(
                name="device_query__detail",
                description=(
                    "查询单台设备的详情。适用于「MH-001 这台设备什么情况」。"
                    "需要用户提供设备名称或编号；如果用户没说清是哪台设备，"
                    "本工具会返回「缺少要查询的设备」，此时应向用户确认，不要自己编造编号。"
                ),
                parameters=tool_schema(_QUERY_PARAM, ["query"]),
                execute=device_detail,
            ),
        ],
    )
