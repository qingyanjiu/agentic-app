"""场景话术 → 点位 tag。

`scene_show_camera` 的 `tag` 是设备编号（如 jk_zl_7f_0002），而这套编号用户
说不出来，只会说名字（「打开7F西通道监控」）。这里用设备台账（走 device_query
的缓存）把名字换成编号。

三种输出：

- 唯一命中 → 返回 tag，场景图直接下发；
- 多台命中 → 返回候选列表 + 一句追问，**候选要存进会话态**，
  用户下一轮说「第三个」「第一个」时据此解析；
- 一台没有 → 返回"没找到"，别把名字甩给服务端瞎猜
  （服务端按名称匹配不分设备类型，会误命中非监控点位）。

指代（「这个」「它」）只在候选唯一时采用，多台时再问一次，不猜。

注意：这里只做解析，不下发。场景动作必须由 `SceneMcpClient` 下发
（scene 是 direct_client，要逐请求 X-Session-Id，且动作类不能重试）。
"""
from __future__ import annotations

import logging
import re

from .domains.device_query import find_devices
from .entities import KIND_DEVICE, Entity, EntityList
from .runtime import McpToolbox

logger = logging.getLogger(__name__)

# 最多列出/记住几个候选（用户口头也只会数到前几个）
MAX_CANDIDATES = 6

_CN_DIGITS = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
# 「第三个」「第 2 台」「3」——注意不能把楼层、编号里的数字当序号
_ORDINAL_RE = re.compile(r"第\s*([一二三四五六七八九十\d]+)\s*(?:个|台|号)?|^([一二三四五六七八九十\d])$")
# 指代词：没有独立语义，只能靠上一轮候选
_PRONOUN_RE = re.compile(r"这个|那个|这台|那台|它|该设备")


def _candidate_line(row: dict) -> str:
    name = row.get("name") or row.get("rawName") or "未命名"
    code = row.get("code") or "无编号"
    space = row.get("spaceName") or ""
    return f"- {name}（{code}）{(' ' + space) if space else ''}"


def _ordinal_index(query: str) -> int | None:
    """从「第三个」「第2台」「3」里取出 1-based 序号；取不到返回 None。"""
    match = _ORDINAL_RE.search(query or "")
    if not match:
        return None
    raw = match.group(1) or match.group(2) or ""
    if raw.isdigit():
        return int(raw)
    return _CN_DIGITS.get(raw)


def _question(rows: list[dict], scope: str) -> str:
    return (
        f"{scope}匹配到 {len(rows)} 台设备，请补充设备类型（监控/门禁/广播等）"
        f"或直接说序号/编号：\n"
        + "\n".join(_candidate_line(row) for row in rows)
    )


def clarify_headline(clarify: str | None) -> str:
    """追问话术的第一句（去掉候选明细行）。

    候选明细改由 `list` 事件下发（可点），文本里再列一遍用户会看到两份。
    拿不到结构化候选时（有的设备没有编号）调用方应原样发完整话术。
    """
    if not clarify:
        return ""
    lines = [
        line.strip()
        for line in clarify.splitlines()
        if line.strip() and not line.lstrip().startswith(("-", "•", "*"))
    ]
    return " ".join(lines)


def candidate_entities(rows: list[dict], scope: str = "") -> EntityList | None:
    """把追问候选转成前端可点的结构化列表（kind=device）。"""
    items: list[Entity] = []
    for row in rows:
        code = str(row.get("code") or "").strip()
        target = code or str(row.get("id") or "").strip()
        if not target:
            continue
        attrs: dict = {}
        if code:
            attrs["code"] = code
        space = row.get("spaceName") or ""
        if space:
            attrs["space"] = str(space)
        items.append(
            Entity(
                kind=KIND_DEVICE,
                id=target,
                label=str(row.get("name") or row.get("rawName") or target),
                attrs=attrs,
            )
        )
    if not items:
        return None
    prefix = f"{scope}匹配到 {len(items)} 台设备" if scope else f"匹配到 {len(items)} 台设备"
    return EntityList(
        kind=KIND_DEVICE,
        title=f"{prefix}，请选择",
        items=items,
    )


async def resolve_scene_device_tag(
    toolbox: McpToolbox,
    query: str,
    candidates: list[dict] | None = None,
) -> tuple[str | None, str | None, list[dict]]:
    """按用户原话定位一台设备并取出它的点位 tag。

    :param candidates: 上一轮追问时列给用户的候选（会话态里存的）
    :return: (tag, 追问话术, 候选列表)。
             tag 有值 → 直接下发；追问话术有值 → 需要用户澄清，
             同时把候选回传，调用方存进会话态供下一轮解析「第三个」。
    """
    pool = list(candidates or [])

    # 1) 用户用序号/指代回复上一轮的候选
    index = _ordinal_index(query) if pool else None
    picked = pool[index - 1] if index and 0 < index <= len(pool) else None
    if picked is None and pool and len(pool) == 1 and _PRONOUN_RE.search(query):
        picked = pool[0]

    if picked is not None:
        code = str(picked.get("code") or "").strip()
        name = picked.get("name") or picked.get("rawName") or ""
        if code:
            logger.info(f"[scene] 按序号/指代解析 → tag={code}（{name}）")
            return code, None, pool
        return None, "这台设备没有设备编号，无法作为场景点位 tag。", pool

    if pool and _PRONOUN_RE.search(query) and len(pool) > 1:
        return None, _question(pool, "你指的是哪一台？"), pool

    # 2) 正常按名称查台账
    _, matched, floor, keyword = await find_devices(toolbox, query)

    if not matched:
        hint = f"「{keyword}」" if keyword else ""
        return None, f"没有找到{hint}对应的设备，请确认一下名称或编号。", pool

    if len(matched) == 1:
        code = str(matched[0].get("code") or "").strip()
        if not code:
            return None, "这台设备没有设备编号，无法作为场景点位 tag。", pool
        logger.info(f"[scene] 设备名称「{keyword}」→ tag={code}")
        return code, None, pool

    # 3) 多台：给候选，后续可以按序号选
    rows = matched[:MAX_CANDIDATES]
    scope = f"「{keyword}」" if keyword else "这个条件"
    return None, _question(rows, scope), rows
