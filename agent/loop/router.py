"""预路由（L0 / L1）与分流判定。

设计要点：
- 分类器不再只回答"哪个域"，而是直接产出一次工具调用计划（RoutePlan）；
- 分类器给不出高置信结果、或检测到跨域语义时返回 None，交给 L2 loop；
- 追问态不走分类器（短回复会被误判），用 pending + 原问句拼接重新规划。

阈值不是拍脑袋：用现有语料跑一遍 top1 分数分布，取准确率拐点再调。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .domains import (
    canteen_status,
    compositive_overview,
    device_query,
    device_status,
    emergency_fire,
    emergency_perimeter,
    energy_status,
    information_status,
    meeting_status,
    person_status,
    security_status,
    twins_inspection,
    vehicle_status,
)
from .types import DomainPlan

logger = logging.getLogger(__name__)

# 域 → 规划器。新增域时在这里登记一个 (query) -> DomainPlan 的函数。
DOMAIN_PLANNERS: dict[str, Callable[[str], DomainPlan]] = {
    "person_status": person_status.route_plan,
    "security_status": security_status.route_plan,
    "canteen_status": canteen_status.route_plan,
    "vehicle_status": vehicle_status.route_plan,
    "information_status": information_status.route_plan,
    "energy_status": energy_status.route_plan,
    "meeting_status": meeting_status.route_plan,
    "emergency_fire": emergency_fire.route_plan,
    "emergency_perimeter": emergency_perimeter.route_plan,
    "device_status": device_status.route_plan,
    "device_query": device_query.route_plan,
    "compositive_overview": compositive_overview.route_plan,
    "twins_inspection": twins_inspection.route_plan,
}

# 跨域 / 多步信号：出现这些词就不要直连单域，交给 loop
CROSS_DOMAIN_HINTS = (
    "对比", "比较", "分别", "同时", "还有", "以及", "顺便",
    "然后", "再看", "另外", "和昨天", "比昨天",
)

# 监控/摄像头类场景指令。
# 存量 `_SCENE_STRONG_CUES` 里「(查看|打开|调出|看|看下|显示|展示).{0,4}
# (监控|摄像头|画面)」这条分支末尾漏了一个 `|`，和后面的「设备编号」分支粘成
# 了一条（要求"监控"后面必须跟 ≥3 字符的编号），所以从来没生效过：
#   has_strong_scene_cue("查看监控") is False
# 连带 is_business_query 里「明确场景动作优先于问句」也失效，
# 「能打开监控吗」被当成业务查询。
# 存量分类器被 13 张旧图共用，这里不动它，只在新链路的 L0 判定里补一份。
#
# 匹配要求「监控/摄像头」之后只有可选的窗口词或疑问语气词，也就是句末才收。
# 早先的 `.{0,4}` 太宽，把「查看 监控5F客梯厅」「查看 监控 jk_zl_5f_0006」
# 这类**问设备**的话也吞成了场景指令，导致它们 handoff 回旧链路、拉全量设备。
_SCENE_MONITOR_CUE = re.compile(
    r"(查看|看一下|看下|看看|看|打开|调出|显示|展示|切到)"
    r"(?:一下|下)?\s*"
    r"(监控|摄像头|画面|视频)"
    r"(?:\s*(?:窗口|页面|画面|大屏|界面))?"
    r"\s*[吗嘛呢吧]?\s*$"
)

# 出现这些词说明用户在问数据，不是要看画面（「看摄像头在线率」不能被抢到场景）
_DATA_VETO = re.compile(
    r"数据|统计|数量|多少|在线率|占比|排名|趋势|报表|明细|清单|列表|记录|日志|台账|情况"
)

# 「有哪些/几个/都有」= 要一份清单，既不是开关画面，也不是在补答场景追问。
# 实测「7楼有哪些监控」在场景追问态里被拼回场景通道，结果给用户弹「选哪台设备」，
# 而他要的是列表。这条在任何一处抢走之前先兜住。
_LIST_QUESTION = re.compile(r"有哪些|哪些|有什么|多少个|多少|几个|几台|都有")


def asks_for_list(query: str) -> bool:
    """用户是不是在要一份清单（「有哪些」「几个」），而不是要开画面/补参数。"""
    return bool(_LIST_QUESTION.search(query or ""))

# 「监控/摄像头」后面直接跟了具体设备名或编号 → 用户问的是这台设备，不是开关画面。
#   「查看 监控5F客梯厅」  → 命中 → 不算场景，走设备查询
#   「打开5F客梯厅的监控」  → 类型词在句末，不命中 → 仍是场景指令
#   「关闭监控窗口」        → 后面是"窗口"，在停用词里 → 仍是场景指令
_SCENE_DEVICE_TARGET = re.compile(
    r"(?:监控|摄像头|画面|视频)\s*[:：]?\s*"
    r"((?:-?\d+[Ff])?[\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9\-_]{1,19})"
)
_SCENE_DEVICE_STOP = frozenset({
    "窗口", "页面", "画面", "大屏", "界面", "视频",
    "设备", "详情", "信息", "状态", "情况", "列表",
    "清单", "数据", "统计", "数量", "在线率", "记录",
})


# 「打开类」动词是操作意图：哪怕后面跟了设备名（「打开监控7F西通道西向东」），
# 也是在开画面，不能被上面那条否决规则拦成设备查询。
_SCENE_OPEN_VERB = re.compile(r"打开|开启|调出|切到|切换到|切换|显示|展示")

# 打开类动词 + 监控词，允许后面继续跟设备名（区别于 查看类 的严格尾部匹配）
# 打开类动词 + 监控词。中间允许夹设备名/楼层——“打开7F西通道监控”
# （类型词在句末）比“打开监控7F西通道”更自然，两种都要认。
_SCENE_OPEN_MONITOR = re.compile(
    r"(?:打开|开启|调出|切到|切换到|切换|显示|展示)\s*(?:一下|下)?\s*"
    r".{0,20}?"
    r"(?:监控|摄像头|画面|视频)"
)


def _device_target(query: str) -> str | None:
    """取出「监控/摄像头」后面跟的具体设备名或编号；命中的是泛称时返回 None。"""
    match = _SCENE_DEVICE_TARGET.search(query)
    if not match:
        return None
    candidate = match.group(1).strip(" -_")
    return candidate if candidate not in _SCENE_DEVICE_STOP else None


def _asks_specific_device(query: str) -> bool:
    """判断用户是不是在问某台具体设备（而不是要开关监控画面）。

    只对「查看/查询」这类说法生效；带「打开/调出/切到」的一律交给场景通道。
    """
    if _SCENE_OPEN_VERB.search(query):
        return False
    return _device_target(query) is not None


@dataclass(slots=True)
class Pending:
    """追问态：只保留"恢复一次调用"所需的最小信息。"""

    tool: str
    args: dict
    missing: list[str]
    domain: str
    original_query: str


@dataclass(slots=True)
class RoutePlan:
    """一次路由决策。level 取 L0 / L1；None 的路由结果表示交给 L2。"""

    level: str
    domain: str
    tool: str | None
    args: dict = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    confidence: float = 0.0
    reason: str = ""
    needs_confirm: bool = False


class Router:
    def __init__(
        self,
        *,
        classify: Callable[[str], Awaitable[dict]] | None = None,
        high_confidence: float = 0.75,
        low_confidence: float = 0.60,
        cross_hints: tuple[str, ...] = CROSS_DOMAIN_HINTS,
    ) -> None:
        self._classify = classify
        self.high_confidence = high_confidence
        self.low_confidence = low_confidence
        self.cross_hints = cross_hints

    # ------------------------------------------------------------------ #
    async def _classify_query(self, query: str) -> dict:
        if self._classify is not None:
            return await self._classify(query)
        # 延迟导入：只有真正跑路由时才加载 BGE 模型
        from agent.intent.classifier import classify_intent_with_scene

        return await classify_intent_with_scene(query)

    def _has_cross_signal(self, query: str) -> bool:
        return any(hint in query for hint in self.cross_hints)

    # ------------------------------------------------------------------ #
    async def route(self, query: str, pending: Pending | None = None) -> RoutePlan | None:
        """返回 RoutePlan 表示可直连；返回 None 表示交给 L2 loop。"""
        # ① 追问态：拼接原问句重新规划，不进分类器
        if pending is not None:
            return await self._merge_pending(pending, query)

        # 用户在问某台具体设备（「查看 监控5F客梯厅」）时，场景判定一律让位给设备查询：
        # 这类话要的是「这台设备在哪、编号多少」，不是开关画面。
        specific_device = _asks_specific_device(query)

        # ② L0：场景控制（破坏性动作）暂不接管，交给旧链路
        # 口径与项目现有分流一致：有明确场景动作词，且不是"查询+数据词"
        try:
            from agent.intent.classifier import has_strong_scene_cue, is_business_query

            if has_strong_scene_cue(query) and not is_business_query(query) and not specific_device:
                return RoutePlan(
                    level="L0",
                    domain="scene",
                    tool=None,
                    confidence=1.0,
                    reason="scene_command",
                    needs_confirm=True,
                )
        except Exception:  # 分类器依赖缺失时不影响主流程
            pass

        # ②.5 监控/摄像头类场景指令（补存量正则失效的那条分支）
        # 与 ② 分开是因为这里要额外排除数据问法：用户可能问「看摄像头在线率」，
        # 那属于业务，不能因为出现「看+摄像头」就被送进场景。
        if (
            (
                _SCENE_MONITOR_CUE.search(query)
                or _SCENE_OPEN_MONITOR.search(query)
            )
            and not _DATA_VETO.search(query)
            and not specific_device
            and not asks_for_list(query)
        ):
            return RoutePlan(
                level="L0",
                domain="scene",
                tool=None,
                confidence=1.0,
                reason="scene_monitor",
                needs_confirm=True,
            )

        # ③ L1：embedding 分类
        info = await self._classify_query(query)
        domain = str(info.get("intent") or "")
        score = float(info.get("score") or 0.0)

        # ③.5 场景兜底（与旧链路同口径）
        # 正则只覆盖了「打开/查看+监控」这类说法，像「关闭监控窗口」「退出监控页面」
        # 动词不在词表里，has_strong_scene_cue 给 False，但向量分明显偏场景。
        # 旧链路一直用 should_route_scene 判定（正则 + 向量兜底），这里必须一致：
        # 否则这类指令会掉进 L2，模型手里只有查询类工具，只能回答"没有这个能力"。
        # 额外排除数据词：should_route_scene 里 _SCENE_VIEW_CUES 会把「看摄像头在线率」
        # 判成场景（因为只看"看+摄像头"，不看"在线率"），那属于业务问法。
        try:
            from agent.intent.classifier import should_route_scene

            if (
                should_route_scene(info, query)
                and not _DATA_VETO.search(query)
                and not specific_device
                and not asks_for_list(query)
            ):
                logger.info(f"[route] 场景向量兜底，交回旧链路：{query}")
                return RoutePlan(
                    level="L0",
                    domain="scene",
                    tool=None,
                    confidence=1.0,
                    reason="scene_vector",
                    needs_confirm=True,
                )
        except Exception:  # 分类器依赖缺失时不影响业务主流程
            pass

        if domain in ("", "other"):
            return None

        if self._has_cross_signal(query):
            logger.info(f"[route] 跨域信号，转 L2：{query}")
            return None
        if score < self.high_confidence:
            logger.info(f"[route] 置信度不足（{score:.3f} < {self.high_confidence}），转 L2：{query}")
            return None

        planner = DOMAIN_PLANNERS.get(domain)
        if planner is None:
            return None

        plan = planner(query)
        if not plan.tool:
            logger.info(f"[route] {domain} 无可用工具（{plan.note}），转 L2：{query}")
            return None

        return RoutePlan(
            level="L1",
            domain=domain,
            tool=plan.tool,
            args=plan.args,
            missing=plan.missing,
            confidence=score,
            reason=plan.note or "classified",
        )

    # ------------------------------------------------------------------ #
    async def _merge_pending(self, pending: Pending, reply: str) -> RoutePlan | None:
        planner = DOMAIN_PLANNERS.get(pending.domain)
        if planner is None:
            return None
        merged_query = f"{pending.original_query} {reply}".strip()
        plan = planner(merged_query)
        if not plan.tool:
            return None
        return RoutePlan(
            level="L1",
            domain=pending.domain,
            tool=plan.tool,
            args=plan.args,
            missing=plan.missing,
            confidence=1.0,
            reason="pending_merge",
        )
