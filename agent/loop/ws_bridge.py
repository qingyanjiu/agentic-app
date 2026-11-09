"""Loop 链路的 WebSocket 桥接。

把 ChatPipeline 的结果转成现有前端认识的事件格式：

    流式增量  → {"event": "custom", "data": {"type": "answer_delta", "content": "..."}}
    追问      → {"event": "custom", "data": {"type": "ask", "content": "...", "missing": [...]}}
    最终回答  → {"event": "custom", "data": {"type": "answer", "content": "..."}}
    结束      → {"status": "done"}
    错误      → {"event": "custom", "data": {"type": "error", "content": "..."}}

设计约束：
- 任何异常都向上抛，由 app.py 捕获后回落旧链路；
- 返回 False 表示"本模块没接管"（例如场景控制 handoff），调用方继续走旧流程。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from .pipeline import ChatPipeline, PipelineResult, Session
from .rollout import RolloutDecision

logger = logging.getLogger(__name__)

# 会话池：key = user_id:session_id
_sessions: dict[str, Session] = {}
_touched: dict[str, float] = {}

# 空闲多久回收（秒）
SESSION_TTL = 300.0

_pipeline: ChatPipeline | None = None


def _send(websocket: Any, payload: dict) -> Any:
    return websocket.send_text(json.dumps(payload, ensure_ascii=False))


def get_session(user_id: str, session_id: str) -> Session:
    """按 user:session 取会话，顺手做一次空闲回收。"""
    now = time.time()
    expired = [k for k, ts in _touched.items() if now - ts > SESSION_TTL]
    for key in expired:
        _sessions.pop(key, None)
        _touched.pop(key, None)

    key = f"{user_id}:{session_id}"
    session = _sessions.get(key)
    if session is None:
        session = Session(user_id=user_id, session_id=session_id)
        _sessions[key] = session
    _touched[key] = now
    return session


def reset_sessions() -> None:
    """测试用。"""
    _sessions.clear()
    _touched.clear()


def build_pipeline(llm: Any) -> ChatPipeline:
    """从现有的 ChatOpenAI 实例构造 pipeline（复用同一套模型配置）。"""
    from openai import AsyncOpenAI

    from . import LoopAgent, build_registry
    from .composer import Composer
    from .executor import Executor
    from .router import Router

    base_url = getattr(llm, "openai_api_base", None)
    api_key = getattr(llm, "openai_api_key", None)
    if hasattr(api_key, "get_secret_value"):
        api_key = api_key.get_secret_value()
    model = getattr(llm, "model_name", None) or getattr(llm, "model", "")

    client = AsyncOpenAI(base_url=base_url, api_key=api_key)
    registry = build_registry()
    return ChatPipeline(
        router=Router(),
        executor=Executor(registry),
        composer=Composer(client, model),
        loop_agent=LoopAgent(client=client, model=model, registry=registry),
    )


def get_pipeline(llm: Any) -> ChatPipeline:
    global _pipeline
    if _pipeline is None:
        _pipeline = build_pipeline(llm)
    return _pipeline


def reset_pipeline() -> None:
    """测试或换模型时用。"""
    global _pipeline
    _pipeline = None


async def handle_chat(
    websocket: Any,
    query: str,
    user_id: str,
    session_id: str,
    llm: Any,
    decision: RolloutDecision | None = None,
) -> bool:
    """跑一次 Loop 链路。

    :return: True 表示已处理（调用方应 continue）；False 表示未接管（走旧链路）。
    """
    pipeline = get_pipeline(llm)
    session = get_session(user_id, session_id)
    cancel = asyncio.Event()

    async def on_delta(text: str) -> None:
        await _send(
            websocket,
            {"event": "custom", "data": {"type": "answer_delta", "content": text}},
        )

    result: PipelineResult = await pipeline.handle(
        query, session, cancel=cancel, on_delta=on_delta
    )

    if result.stop_reason == "handoff":
        # 场景控制等专用链路，交回旧流程
        logger.info(f"[loop] handoff to legacy: {query[:40]}")
        return False

    if result.stop_reason == "asked":
        await _send(
            websocket,
            {
                "event": "custom",
                "data": {"type": "ask", "content": result.text},
            },
        )
    elif result.stop_reason == "aborted":
        await _send(
            websocket,
            {"event": "custom", "data": {"type": "info", "content": result.text}},
        )
    elif result.stop_reason == "error":
        await _send(
            websocket,
            {"event": "custom", "data": {"type": "error", "content": result.text}},
        )
    else:
        await _send(
            websocket,
            {
                "event": "custom",
                "data": {
                    "type": "answer",
                    "content": result.text,
                    "level": result.level,
                    "escalated": result.escalated,
                },
            },
        )

    await websocket.send_text(json.dumps({"status": "done"}))
    logger.info(
        f"[loop] done level={result.level} reason={result.stop_reason} "
        f"escalated={result.escalated} rollout={decision.reason if decision else '-'}"
    )
    return True
