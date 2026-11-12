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
import os
import re
import time

from agent.intent.slots import DEVICE_TYPE_DICT, extract_device_query_slots

from ..entities import KIND_DEVICE, Entity, EntityList
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

# 类型词后面直接跟设备名，如「监控5F客梯厅」「监控 5F客梯厅」「摄像头-1F电梯厅」。
# 数据里的设备名就是 `5F客梯厅` 这种（楼层和名称之间**没有横线**），
# 上面 `_DEVICE_NAME_RE` 要求带横线，所以这类名字全被漏掉——
# 结果就是「查看 监控5F客梯厅」不按名称过滤，直接列出该类型全部设备。
_DEVICE_TYPED_NAME_RE = re.compile(
    r"(?:监控|摄像头|门禁|道闸|梯控|广播|电表|水表|设备)\s*[:：]?\s*"
    r"((?:-?\d+[Ff])?[\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9\-_]{1,19})"
)

# 类型词后面跟的是泛称，不是设备名
_NAME_STOP = frozenset({
    "设备", "画面", "视频", "详情", "信息", "状态", "情况",
    "列表", "清单", "记录", "数据", "统计", "数量", "在线率",
    # 指代词：没有具体对象时不能当名称，交给追问
    "这个", "那个", "这台", "那台", "这台设备", "那台设备", "它", "该设备",
})

# 「名称 + 类型词在句末」：打开7F西通道监控 / 打开7f西侧监控 / 看看A栋的摄像头
# 这是中文里最自然的说法，而上面两条正则都要求类型词在前，全漏。
_NAME_BEFORE_TYPE_RE = re.compile(
    r"(?:打开|开启|查看|查|调出|切到|切换到|显示|展示|看看|看一下|看下|看)"
    r"\s*[:：]?\s*"
    r"([^\s，,。；;:：]{2,20}?)"
    r"\s*(?:的)?\s*(?:监控|摄像头|画面|视频)\s*[吗嘛呢吧]?\s*$"
)


# 候选清洗：类型词后面可能跟着动词或整句（上游把两轮话拼成一句时尤其明显），
# 「监控 打开7F西通道西向东监控」曾因此把整句当成设备名，永远匹配不到。
_LEADING_ACTION = re.compile(r"^(?:打开|开启|查看|查一下|查|调出|切到|切换到|显示|展示|看看|看一下|看下|看)+")
_INLINE_ACTION = re.compile(r"打开|开启|查看|调出|切到|切换到|显示|展示")
# 类型词是描述，不是名字的一部分：「7F西通道西向东监控」→「7F西通道西向东」
_TRAILING_TYPE = re.compile(
    r"(?:监控|摄像头|画面|视频|门禁|道闸|梯控|广播|电表|水表|设备)$"
)


def _clean_candidate(raw: str) -> str | None:
    """把正则捕到的一段整理成设备名：去动词、去尾部类型词、过滤泛称。"""
    text = _LEADING_ACTION.sub("", (raw or "").strip())
    text = _INLINE_ACTION.split(text)[0]
    text = text.strip(" -_")
    text = _TRAILING_TYPE.sub("", text).strip(" -_")
    if len(text) < 2 or text in _NAME_STOP:
        return None
    return text


def _name_fallback(query: str) -> str | None:
    """slots 抽不到设备名时的兜底，三种说法都要认。

    1. 「3F-主楼梯MJ」——带横线的楼层前缀名称
    2. 「监控5F客梯厅」——类型词在前
    3. 「打开7F西通道监控」——类型词在句末（最自然的中文说法）
    """
    match = _DEVICE_NAME_RE.search(query)
    if match:
        return match.group(0)

    for pattern in (_DEVICE_TYPED_NAME_RE, _NAME_BEFORE_TYPE_RE):
        match = pattern.search(query)
        if not match:
            continue
        candidate = _clean_candidate(match.group(1))
        if candidate:
            return candidate
    return None


# ==================================================================
# 设备清单缓存
# ==================================================================
# 一次全量 listDeviceOnly 是 ~1.3MB JSON，但字段有 30 多个，
# 其中只有下面几个对「过滤 + 展示 + 查详情」有用。缓存前先精简，
# 579 条从 1.3MB 降到几十 KB，内存和后续遍历都便宜。

_CACHE_TTL = float(os.getenv("DEVICE_CACHE_TTL") or 300)   # 秒，0 表示不缓存
_SLIM_FIELDS = ("id", "code", "name", "rawName", "spaceName", "status", "syncSource")
_CACHE: dict[str, tuple[float, list[dict]]] = {}


def _slim(rows: list[dict]) -> list[dict]:
    return [
        {key: row.get(key) for key in _SLIM_FIELDS if key in row}
        for row in rows
    ]


def _cache_get(key: str) -> list[dict] | None:
    item = _CACHE.get(key)
    if not item:
        return None
    expire_at, rows = item
    if time.monotonic() > expire_at:
        _CACHE.pop(key, None)
        return None
    return rows


def clear_device_cache() -> None:
    """测试用：清空设备清单缓存。"""
    _CACHE.clear()


async def _fetch_rows(
    toolbox: McpToolbox,
    args: dict,
    *,
    refresh: bool = False,
) -> list[dict]:
    """拿某类型的设备行；命中缓存就直接用，refresh=True 时强制重拉并回写。"""
    cache_key = f"list:{args.get('syncSource', '*')}"
    if not refresh and _CACHE_TTL > 0:
        cached = _cache_get(cache_key)
        if cached is not None:
            logger.info(f"[device_query] 缓存命中 {cache_key}，{len(cached)} 台")
            return cached

    raw = await toolbox.call(SERVER, TOOL_LIST, args)
    rows = _slim(_rows_of(raw))
    if _CACHE_TTL > 0:
        _CACHE[cache_key] = (time.monotonic() + _CACHE_TTL, rows)
    return rows

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


# 方位词归一：「7F西侧」不是任何设备名的子串（数据里是「7F西通道西向东」），
# 严格匹配必然 0 台。去掉「侧/面/边/向」后再匹配，至少能把候选列出来；
# 多台仍然走追问，不猜。
_DIRECTION_LOOSEN = re.compile(r"([东西南北])\s*(?:侧|面|边儿|边|方向|向)")


def _loosen_keyword(keyword: str) -> str | None:
    """「7F西侧」→「7F西」。没有方位词或没变化时返回 None。"""
    loosened = _DIRECTION_LOOSEN.sub(r"\1", keyword or "").strip(" -_")
    return loosened if loosened and loosened != keyword else None


def _name_in_text(query: str, rows: list[dict]) -> str | None:
    """query 里**直接出现**的设备全名，取最长的那个。

    覆盖「打开 7F西通道西向东」（带空格、没有类型词、没有横线）这类说法：
    正则抽不出来，但台帐里就有这个名字，直接反查最稳，也不会误伤短词。
    """
    text = (query or "").lower()
    if not text:
        return None
    best: str | None = None
    for row in rows:
        for key in ("rawName", "name"):
            name = str(row.get(key) or "").strip()
            if len(name) >= 4 and name.lower() in text:
                if best is None or len(name) > len(best):
                    best = name
    return best


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


_STATUS_TEXT = {"0": "停用", "1": "启用", "2": "维修", "3": "报废"}


def _status_text(status: object) -> str:
    return _STATUS_TEXT.get(str(status), str(status or ""))


def _summary(
    rows: list[dict],
    *,
    total: int | None = None,
    floor: str | None = None,
    keyword: str | None = None,
) -> str:
    """一句话概括这批结果，比如「14 台设备，楼层 5F」。

    文本回答的首行和结构化列表的 title 都取这里，保证两边说的是同一件事。
    """
    parts: list[str] = []
    if total is not None and total != len(rows):
        parts.append(f"{len(rows)} 台设备（园区共 {total} 台）")
    else:
        parts.append(f"{len(rows)} 台设备")
    if floor:
        parts.append(f"楼层 {floor}")
    if keyword:
        parts.append(f"关键词「{keyword}」")
    return "，".join(parts)


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

    # 这段文本会原样展示给用户（list 工具是 passthrough），
    # 不要塞"status 不是在线状态"这类给模型看的注解——那是 tool description 的职责。
    header = _summary(rows, total=total, floor=floor, keyword=keyword) + "："

    lines = [header]
    for row in rows[:MAX_ROWS]:
        name = row.get("rawName") or row.get("name") or "未命名"
        code = row.get("code") or ""
        device_id = row.get("id") or ""
        space = row.get("spaceName") or ""
        status_text = _status_text(row.get("status"))
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


def _entities(
    rows: list[dict],
    *,
    total: int | None = None,
    floor: str | None = None,
    keyword: str | None = None,
) -> EntityList | None:
    """把同一批行转成结构化列表，供前端渲染可点控件。

    `id` 用点位编号（就是场景下发要的 tag）；编号缺失时退用内部 id，
    此时前端仍能点，但后端拿到的是内部 id，需要按 id 反查。
    """
    items: list[Entity] = []
    for row in rows[:MAX_ROWS]:
        code = str(row.get("code") or "")
        device_id = str(row.get("id") or "")
        target = code or device_id
        if not target:
            continue
        attrs: dict = {}
        if code:
            attrs["code"] = code
        type_name = _sync_source_name(row.get("syncSource"))
        if type_name and type_name != "未知类型":
            attrs["type"] = type_name
        space = row.get("spaceName") or ""
        if space:
            attrs["space"] = str(space)
        # 楼层单独给：前端要做场景聚焦时只认 "5F" / "-1F" 这种短写法，
        # 直接传 spaceName（园区/科研办公楼/5F）它解析不了。
        floors = sorted(_floors_in_row(row))
        if len(floors) == 1:
            attrs["floor"] = floors[0]
        status_text = _status_text(row.get("status"))
        if status_text:
            attrs["status"] = status_text
        items.append(
            Entity(
                kind=KIND_DEVICE,
                id=target,
                label=str(row.get("rawName") or row.get("name") or target),
                attrs=attrs,
            )
        )
    if not items:
        return None
    return EntityList(
        kind=KIND_DEVICE,
        title=_summary(rows, total=total, floor=floor, keyword=keyword),
        items=items,
        total=total,
        truncated=len(rows) > MAX_ROWS,
        note=(
            f"其余 {len(rows) - MAX_ROWS} 台未展示，可加楼层或名称缩小范围"
            if len(rows) > MAX_ROWS
            else ""
        ),
    )


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
        # slots 抽不到「3F-主楼梯MJ」「5F客梯厅」这类名称，这里兜底
        keyword = _name_fallback(query)

    if not keyword:
        keyword = _name_in_text(query, rows)
    if floor:
        rows = [row for row in rows if _row_matches_floor(row, floor)]
    if keyword:
        matched = [row for row in rows if _row_matches_keyword(row, keyword)]
        if not matched:
            # 方位词对不上设备名（「7F西侧」vs「7F西通道西向东」）：放宽一次，
            # 多台就交给调用方追问，总比回「没有找到」强。
            loose = _loosen_keyword(keyword)
            if loose:
                matched = [row for row in rows if _row_matches_keyword(row, loose)]
                if matched:
                    logger.info(
                        f"[device_query] 关键词「{keyword}」无命中，"
                        f"按「{loose}」匹配到 {len(matched)} 台"
                    )
                    keyword = loose
        rows = matched
    return rows, floor, keyword


def preview_plan(query: str) -> dict:
    """dry-run 用：展示会调用哪个 MCP 工具、带什么参数、本地怎么过滤。"""
    slots = extract_device_query_slots(query)
    floor = normalize_floor(query) or normalize_floor(slots.get("area"))
    keyword = (slots.get("device_keyword") or "").strip() or _name_fallback(query)
    return {
        "domain": "device_query",
        "tool": TOOL_LIST,
        "mcp_args": _build_list_args(slots),
        "local_filter": {"floor": floor, "keyword": keyword},
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


async def find_devices(
    toolbox: McpToolbox,
    query: str,
) -> tuple[list[dict], list[dict], str | None, str | None]:
    """按用户原句在设备清单里定位设备，返回 (全量行, 匹配行, 楼层, 关键词)。

    两个工具共用这一套：设备清单查询、场景控制按名称定位点位。
    给了名称或楼层却一条没命中时，会强制重拉一次缓存再过滤（台账可能刚更新）。
    """
    slots = extract_device_query_slots(query)
    args = _build_list_args(slots)
    rows = await _fetch_rows(toolbox, args)
    matched, floor, keyword = _filter_rows(rows, slots, query)

    if not matched and (keyword or floor):
        try:
            rows = await _fetch_rows(toolbox, args, refresh=True)
        except Exception as exc:
            logger.warning(f"[loop] 设备清单重拉失败：{exc}")
        else:
            matched, floor, keyword = _filter_rows(rows, slots, query)
            logger.info(
                f"[device_query] 缓存未命中，重拉后 {len(matched)}/{len(rows)} 台"
            )
    return rows, matched, floor, keyword


def build_domain(toolbox: McpToolbox) -> DomainSpec:
    async def list_devices(raw_args: dict) -> ToolOutcome:
        query = str(raw_args.get("query") or "").strip()
        if not query:
            return ToolOutcome("缺少用户问题，无法解析查询条件。", is_error=True)

        try:
            rows, matched, floor, keyword = await find_devices(toolbox, query)
        except Exception as exc:
            logger.warning(f"[loop] 设备列表查询失败：{exc}")
            return ToolOutcome(f"设备列表查询失败：{exc}", is_error=True)

        total = len(rows)
        logger.info(
            f"[device_query] 全量 {total} 台 → 过滤后 {len(matched)} 台"
            f"（floor={floor}, keyword={keyword}）"
        )

        if not matched and keyword:
            return ToolOutcome(
                f"没有找到名称或编号包含「{keyword}」的设备。"
                f"请确认设备名称/编号，或去掉名称、只按楼层或类型查询。"
            )

        return ToolOutcome(
            govern(
                _format_rows(matched, total=total, floor=floor, keyword=keyword),
                label="device_list",
            ),
            # 同一批数据再给一份结构化列表：前端直接渲染可点控件，
            # 不用再猜"这段文本是不是设备清单"。
            entities=_entities(matched, total=total, floor=floor, keyword=keyword),
        )

    async def device_detail(raw_args: dict) -> ToolOutcome:
        query = str(raw_args.get("query") or "").strip()
        if not query:
            return ToolOutcome("缺少用户问题，无法解析查询条件。", is_error=True)

        slots = extract_device_query_slots(query)
        keyword = (slots.get("device_keyword") or "").strip() or _name_fallback(query)
        if not keyword:
            return ToolOutcome(
                "缺少要查询的设备。请向用户确认设备名称或设备编号，拿到后再调用本工具。",
                is_error=True,
            )

        # 1) 先按类型 + 楼层缩小范围，再按关键词定位 id
        args = _build_list_args(slots)
        try:
            rows = await _fetch_rows(toolbox, args)
        except Exception as exc:
            logger.warning(f"[loop] 设备定位失败：{exc}")
            return ToolOutcome(f"设备定位失败：{exc}", is_error=True)

        rows, floor, _ = _filter_rows(rows, {**slots, "device_keyword": ""}, query)
        matched = [row for row in rows if _row_matches_keyword(row, keyword)]

        # 没匹配到 → 可能是缓存旧了，重拉一次再试
        if not matched:
            try:
                fresh = await _fetch_rows(toolbox, args, refresh=True)
            except Exception as exc:
                logger.warning(f"[loop] 设备重拉失败：{exc}")
            else:
                fresh, floor, _ = _filter_rows(
                    fresh, {**slots, "device_keyword": ""}, query
                )
                matched = [row for row in fresh if _row_matches_keyword(row, keyword)]

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
                # 列表本身就是成品文本，L1 直出，不交给模型复述
                passthrough=True,
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
        # 自检 / 发现去重用：这个域会打到哪些 MCP 工具
        mcp_tools=((SERVER, TOOL_LIST), (SERVER, TOOL_DETAIL)),
    )
