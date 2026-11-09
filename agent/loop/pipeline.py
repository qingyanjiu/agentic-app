"""三级分流的编排入口。

    Router (L0/L1) ──命中──> Executor ──成功──> Composer ──> 回答
        │                      │
        └──未命中──┐           └──escalate──┐
                   ▼                        ▼
                L2 LoopAgent（自己选工具、多轮）

Pipeline 只做编排，不包含业务规则：业务规则在工具（execute）和域规划器（route_plan）里。
cancel 会一路透传到 loop，供 WebSocket 的"停止"按钮中断。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from .composer import Composer, ask_text
from .executor import Executor
from .router import Pending, Router

logger = logging.getLogger(__name__)


@dataclass
class Session:
    """一次会话的最小状态。

    - history：给 Composer / LoopAgent 的对话历史（OpenAI messages）
    - pending：追问态（只保留恢复一次调用所需的信息）
    """

    user_id: str = ""
    session_id: str = ""
    history: list[dict] = field(default_factory=list)
    pending: Pending | None = None
    max_history: int = 12

    def add_turn(self, user: str, assistant: str) -> None:
        self.history.append({"role": "user", "content": user})
        self.history.append({"role": "assistant", "content": assistant})
        if len(self.history) > self.max_history * 2:
            self.history = self.history[-(self.max_history * 2):]

    def clear_pending(self) -> None:
        self.pending = None


@dataclass
class PipelineResult:
    text: str
    level: str            # L0 / L1 / L2
    stop_reason: str      # answered / asked / handoff / loop_unavailable / aborted / error
    tool_calls: list[dict] = field(default_factory=list)
    escalated: bool = False

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "level": self.level,
            "stop_reason": self.stop_reason,
            "tool_calls": self.tool_calls,
            "escalated": self.escalated,
        }


class ChatPipeline:
    def __init__(
        self,
        *,
        router: Router,
        executor: Executor,
        composer: Composer,
        loop_agent=None,          # LoopAgent | None；None 时 L2 直接返回"暂不支持"
    ) -> None:
        self.router = router
        self.executor = executor
        self.composer = composer
        self.loop_agent = loop_agent

    async def handle(
        self,
        query: str,
        session: Session,
        cancel: asyncio.Event | None = None,
        on_delta=None,
    ) -> PipelineResult:
        plan = await self.router.route(query, session.pending)

        # ---------- L2：交给 loop ----------
        if plan is None:
            return await self._run_loop(query, session, cancel, level="L2")

        # ---------- L0 场景控制：暂不接管，交回旧链路 ----------
        if plan.tool is None:
            return PipelineResult(
                text="场景控制暂未接入新链路，请走原有场景通道。",
                level=plan.level,
                stop_reason="handoff",
                tool_calls=[{"name": "scene", "handoff": True}],
            )

        # ---------- 缺参数：模板追问 + 记 pending ----------
        if plan.missing:
            text = ask_text(plan.missing)
            session.pending = Pending(
                tool=plan.tool,
                args=plan.args,
                missing=plan.missing,
                domain=plan.domain,
                original_query=query,
            )
            session.add_turn(query, text)
            return PipelineResult(
                text=text,
                level=plan.level,
                stop_reason="asked",
                tool_calls=[{"name": plan.tool, "missing": plan.missing}],
            )

        # ---------- L1 直连 ----------
        outcome = await self.executor.execute(plan.tool, plan.args)

        # 工具说"我搞不定" → 升级到 loop 换工具重试
        if outcome.escalate:
            logger.info(f"[pipeline] {plan.tool} 请求升级到 L2：{outcome.text[:120]}")
            result = await self._run_loop(query, session, cancel, level="L2")
            result.escalated = True
            return result

        session.clear_pending()
        text = await self.composer.compose(
            query, outcome.text, session.history, on_delta=on_delta
        )
        session.add_turn(query, text)
        return PipelineResult(
            text=text,
            level=plan.level,
            stop_reason="answered",
            tool_calls=[
                {
                    "name": plan.tool,
                    "args": plan.args,
                    "is_error": outcome.is_error,
                    "domain": plan.domain,
                    "confidence": round(plan.confidence, 4),
                }
            ],
        )

    async def _run_loop(
        self,
        query: str,
        session: Session,
        cancel: asyncio.Event | None = None,
        *,
        level: str = "L2",
    ) -> PipelineResult:
        if self.loop_agent is None:
            return PipelineResult(
                text="这个问题需要多步查询，但 L2 loop 尚未启用。",
                level=level,
                stop_reason="loop_unavailable",
            )
        result = await self.loop_agent.run(query, history=session.history, cancel=cancel)
        if result.stop_reason != "aborted":
            session.add_turn(query, result.text)
        session.clear_pending()
        stop_reason = "answered" if result.stop_reason == "answered" else result.stop_reason
        return PipelineResult(
            text=result.text,
            level=level,
            stop_reason=stop_reason,
            tool_calls=[{"name": c["name"]} for c in result.tool_calls],
        )
