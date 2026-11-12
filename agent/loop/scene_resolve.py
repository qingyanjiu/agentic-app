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

# 一次最多列/记几个候选。7F 一层就有 10 台监控，6 台会把后面几台截掉，
# 用户看得见却点不到；12 能覆盖单层常规数量。
MAX_CANDIDATES = 12

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


_TYPE_WORD_RE = re.compile(r"监控|摄像头|画面|视频|门禁|道闸|梯控|广播|电表|水表")

# 追问态出口话术：用户既没给序号、也没说出设备名 → 不再猜，直接放开追问。
# 调用方（app.py）看到这句要清掉会话态，否则下一句还会被当成补答。
ESCAPE_CLARIFY = (
    "我没听明白你要看哪台设备。请直接给出完整的要求，"
    "比如「打开7F西通道西向东的监控」，或者用「第三个」这样选。"
)
# 动作词：判断「这句话除动作之外还说了什么」时要先剥掉
_STRIP_ACTION = re.compile(
    r"^(?:切回|切到|切换到|切换|回到|返回|退出|关闭|关掉|打开|开启|查看|调出|显示|展示|看看|看一下|看下|看)+"
)
# 范围词：整栋楼/全部楼层这种是场景视角，不是设备名
_SCOPE_WORD = re.compile(r"整栋|全楼|整个|全部|所有|园区|整层|全层|楼层|大楼")


def has_device_reference(query: str) -> bool:
    """本轮话术里有没有"具体设备"的线索。

    追问态下不能见到什么都去查台帐：`切回整栋楼` 没有设备线索，
    之前却被拿去查全表，回了用户一句「匹配到 268 台设备」。
    """
    text = (query or "").strip()
    if not text:
        return False
    if _ORDINAL_RE.search(text) or _PRONOUN_RE.search(text):
        return True
    if _TYPE_WORD_RE.search(text):
        return True
    residual = _STRIP_ACTION.sub("", text).strip(" 的，,。：:")
    if len(residual) < 3 or _SCOPE_WORD.search(residual):
        return False
    return True


def _answers_pick(query: str, keyword: str | None) -> bool:
    """追问态下，这句话有没有在回答"选哪一台"。

    只认两种：序号（「第三个」「3」），或者说得出的设备名/编号
    （`_filter_rows` 会把它落到 keyword 上）。

    多台候选时说「这个」不算 —— 它等于没选，再问一遍只会绕圈子；
    候选只有一个时「这个」在前面就被处理掉了。
    """
    if _ordinal_index(query) is not None:
        return True
    return bool(keyword)


def _question(
    rows: list[dict],
    scope: str,
    *,
    total: int | None = None,
    hint: str | None = None,
) -> str:
    count = total if total is not None else len(rows)
    head = f"{scope}匹配到 {count} 台设备"
    if count > len(rows):
        head += f"（先列出前 {len(rows)} 台）"
    return (
        f"{head}。{hint or '请说序号/编号，或补充设备名称'}：\n"
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


def clarify_entities(clarify: str | None, rows: list[dict], scope: str = "") -> EntityList | None:
    """只有「多台命中、请用户选」这类追问才配可点候选。

    「没有找到 X」的追问也把上一轮候选带回来了（供「第二个」继续用），
    但那批候选和本轮这句话无关 —— 发出去就是「请选择 3 台」+「没有找到 X」
    自相矛盾，线上已经踩过。
    """
    if not clarify or "匹配到" not in clarify:
        return None
    return candidate_entities(rows, scope)


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

    # 多台候选时说「这个/那个」等于没选：交给下面的 _answers_pick 判定为跳出，
    # 不再二次追问（问一遍「你指的是哪一台」用户还是不知道从哪选）。

    # 2) 正常按名称查台账
    _, matched, floor, keyword = await find_devices(toolbox, query)

    # 追问态出口：既没给序号/指代，也没说出设备名 → 不再追问第二遍。
    # 这一条是"防止追问无限循环"的总闸：否则用户说什么都会被当成补答。
    if pool and not _answers_pick(query, keyword):
        logger.info(f"[scene] 追问态下没识别出设备，跳出追问：{query[:30]}")
        return None, ESCAPE_CLARIFY, []

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
    typed = bool(_TYPE_WORD_RE.search(query))
    if keyword:
        scope = f"「{keyword}」"
    elif floor:
        # 「打开7F监控」这类：说了楼层和类型，没说具体点位
        scope = f"{floor} 楼层"
    else:
        scope = "这个条件"
    if typed and not keyword:
        hint = "该层点位比较多，请说具体点位名称或序号"
    elif typed:
        hint = "请说具体点位名称或序号"
    else:
        hint = "请补充设备类型（监控/门禁/广播等），或直接说点位名称/序号"
    return None, _question(rows, scope, total=len(matched), hint=hint), rows
