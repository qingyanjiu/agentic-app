"""预路由（L0 / L1）与分流判定。

设计要点：
- 分类器不再只回答"哪个域"，而是直接产出一次工具调用计划（RoutePlan）；
- 分类器给不出高置信结果、或检测到跨域语义时返回 None，交给 L2 loop；
- 追问态不走分类器（短回复会被误判），用 pending + 原问句拼接重新规划。

阈值不是拍脑袋：用现有语料跑一遍 top1 分数分布，取准确率拐点再调。
"""
from __future__ import annotations

import logging
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

        # ② L0：场景控制（破坏性动作）暂不接管，交给旧链路
        # 口径与项目现有分流一致：有明确场景动作词，且不是"查询+数据词"
        try:
            from agent.intent.classifier import has_strong_scene_cue, is_business_query

            if has_strong_scene_cue(query) and not is_business_query(query):
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

        # ③ L1：embedding 分类
        info = await self._classify_query(query)
        domain = str(info.get("intent") or "")
        score = float(info.get("score") or 0.0)

        # ③.5 场景兜底（与旧链路同口径）
        # 正则只覆盖了「打开/查看+监控」这类说法，像「关闭监控窗口」「退出监控页面」
        # 动词不在词表里，has_strong_scene_cue 给 False，但向量分明显偏场景。
        # 旧链路一直用 should_route_scene 判定（正则 + 向量兜底），这里必须一致：
        # 否则这类指令会掉进 L2，模型手里只有查询类工具，只能回答"没有这个能力"。
        try:
            from agent.intent.classifier import should_route_scene

            if should_route_scene(info, query):
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
