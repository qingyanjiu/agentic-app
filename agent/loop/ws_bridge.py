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
import os
import time
from typing import Any

from memory.chat_store import get_store

from .pipeline import ChatPipeline, PipelineResult, Session
from .rollout import RolloutDecision

logger = logging.getLogger(__name__)

# 会话池：key = user_id:session_id
_sessions: dict[str, Session] = {}
_touched: dict[str, float] = {}

# 空闲多久回收（秒）。默认 30 分钟：追问/指代（「第三个」「这个」）常常要跨几分钟，
# 5 分钟太短，用户回来就失忆了。与场景追问态共用 SESSION_TTL_SECONDS。
SESSION_TTL = float(os.getenv("SESSION_TTL_SECONDS") or 1800)

_pipeline: ChatPipeline | None = None

# 正在跑的轮次：key = user_id:session_id → 该轮的 cancel 事件。
# 前端点"停止"时通过 request_stop() 置位，LoopAgent 在下一个检查点退出。
_active_cancels: dict[str, asyncio.Event] = {}


def request_stop(user_id: str, session_id: str) -> bool:
    """请求中断某个会话正在执行的这一轮；没有在跑就返回 False。"""
    event = _active_cancels.get(f"{user_id}:{session_id}")
    if event is None:
        return False
    event.set()
    logger.info(f"[loop] 收到停止请求，已通知本轮退出 -- {user_id}:{session_id}")
    return True


async def _send(websocket: Any, payload: dict) -> bool:
    """发一帧；客户端已断开时返回 False 并静默。

    线上踩过：前端断开（刷新/切页）后服务端还在推事件，
    `send_text` 直接抛 → 整个 ASGI 应用报
    `Unexpected ASGI message 'websocket.send'`，还会被上层误判成"灰度链路异常"再走一遍旧链路。
    这里把"对端已关闭"当成正常结束，不打断本轮处理。
    """
    try:
        await websocket.send_text(json.dumps(payload, ensure_ascii=False))
        return True
    except Exception as exc:  # WebSocketDisconnect / RuntimeError / OSError
        logger.debug(f"[loop] 发送失败，客户端可能已断开：{type(exc).__name__}")
        return False
def get_history_messages() -> int:
    """喂给模型的条数上限（`CHAT_HISTORY_MESSAGES`）；store 单例里已解析好。"""
    return get_store().history_messages

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
        # 内存保留轮数跟着 CHAT_HISTORY_MESSAGES 走（1 轮 = 2 条），
        # 否则"还原 24 条、第一轮就被裁到别的数"会对不上
        session = Session(
            user_id=user_id,
            session_id=session_id,
            max_history=max(1, get_history_messages() // 2),
        )
        _sessions[key] = session
    _touched[key] = now
    return session


def restore_history(session: Session) -> int:
    """从落盘的历史里补回本会话的上下文，返回补了几条。

    只在内存里还没有历史时调用（重启后第一轮、或会话被 TTL 回收又回来）。
    读失败不抛：拿不到历史就当新会话，别让持久化把对话带崩。
    """
    if session.history:
        return 0
    try:
        # 条数由 CHAT_HISTORY_MESSAGES 决定（默认 24 ≈ 12 轮），store 自己兜默认值
        history = get_store().load(session.user_id, session.session_id)
    except Exception as exc:  # 存储异常一律降级
        logger.warning(f"[loop] 读取历史失败，按新会话处理: {exc}")
        return 0
    if history:
        session.history = history
        logger.info(
            f"[loop] 已恢复历史 {len(history)} 条 -- "
            f"{session.user_id}:{session.session_id}"
        )
    return len(history)


def persist_messages(session: Session, messages: list[dict]) -> int:
    """把本轮新增的消息写盘，返回写入条数。失败只记日志。"""
    if not messages:
        return 0
    try:
        return get_store().append_messages(
            session.user_id, session.session_id, messages
        )
    except Exception as exc:
        logger.warning(f"[loop] 历史写盘失败（本轮只留内存）: {exc}")
        return 0


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
    # 落盘的历史只在内存为空时补一次（重启后第一轮 / 会话被 TTL 回收又回来）。
    # 放线程池里跑：sqlite 是同步 IO，别占住事件循环。
    if not session.history:
        await asyncio.to_thread(restore_history, session)
    history_before = len(session.history)
    cancel = asyncio.Event()
    _active_cancels[f"{user_id}:{session_id}"] = cancel
    logger.info(
        f"[loop] 开始处理提问 -- {user_id}:{session_id} "
        f"rollout={decision.reason if decision else '-'} query={query!r}"
    )
    streamed_chars = 0

    async def on_delta(text: str) -> None:
        # 前端 case 'answer' 是「追加」语义，流式分块必须用这个 type
        nonlocal streamed_chars
        if not text:
            return
        streamed_chars += len(text)
        await _send(
            websocket,
            {"event": "custom", "data": {"type": "answer", "content": text}},
        )

    async def on_event(event: Any) -> None:
        """把 loop 内部事件翻译成前端认识的事件。

        - assistant_delta → answer（追加），模型边说边出字；
        - tool_start → tool（前端据此把已收到的文字落成「过程稿」气泡）；
        - tool_end 不发：下一次 answer 会自然清掉状态提示。
        """
        kind = getattr(event, "type", None)
        data = getattr(event, "data", None) or {}
        if kind == "assistant_delta":
            await on_delta(str(data.get("text") or ""))
        elif kind == "tool_start":
            await _send(
                websocket,
                {
                    "event": "custom",
                    "data": {"type": "tool", "name": data.get("name") or ""},
                },
            )
        elif kind == "assistant_message":
            # 工具直出（passthrough）和「步数用尽」的总结都是成品文本，
            # 这里不接的话只能靠结尾 streamed==0 兜底补发，前面说过话就丢了。
            await on_delta(str(data.get("text") or ""))
        elif kind == "entities":
            # 结构化清单（设备列表这类）：前端直接渲染可点控件。
            # 文本回答里也带着逐行明细（旧前端能继续用），这份是为准的。
            payload = data.get("list")
            if payload:
                await _send(
                    websocket,
                    {"event": "custom", "data": {"type": "list", **payload}},
                )
        elif kind == "error":
            logger.warning(f"[loop] loop 内部错误：{data.get('message')}")

    try:
        result: PipelineResult = await pipeline.handle(
            query, session, cancel=cancel, on_delta=on_delta, on_event=on_event
        )
    finally:
        # 本轮结束（正常/异常/被停止）都要摘掉，否则 request_stop 会打到已结束的轮次
        _active_cancels.pop(f"{user_id}:{session_id}", None)

    # 本轮新增的消息写盘（answered / asked / aborted 都记）：只写真的进了 history 的部分，
    # 所以不需要在每个 stop_reason 分支各写一次。handoff 不写（专用链路自己管上下文）。
    new_messages = session.history[history_before:]
    if result.stop_reason != "handoff" and new_messages:
        try:
            await asyncio.to_thread(persist_messages, session, new_messages)
        except Exception as exc:  # persist_messages 内部已兜底，这里再保险一层
            logger.warning(f"[loop] 历史写盘异常（忽略）: {exc}")

    if result.stop_reason == "handoff":
        # 场景控制等专用链路，交回旧流程
        logger.info(f"[loop] handoff to legacy: {query[:40]}")
        return False

    # L1 直出：结构化清单单独发一个 list 事件（文本已经按追加语义发过了）。
    # L2 的清单是执行工具时就发出去的，不在这里重复。
    if result.entities:
        await _send(
            websocket,
            {"event": "custom", "data": {"type": "list", **result.entities.to_dict()}},
        )

    if result.stop_reason == "asked":
        # 前端读的是 question 字段，不是 content
        await _send(
            websocket,
            {"event": "custom", "data": {"type": "ask", "question": result.text}},
        )
    elif result.stop_reason == "aborted":
        await _send(
            websocket,
            {"event": "custom", "data": {"type": "answer", "content": result.text}},
        )
    elif result.stop_reason == "error":
        await _send(
            websocket,
            {"event": "custom", "data": {"type": "error", "content": result.text}},
        )
    elif streamed_chars == 0:
        # 一个字符都没流出去（L2 关流式 / 模型直接给了结论），一次性给出完整回答
        await _send(
            websocket,
            {"event": "custom", "data": {"type": "answer", "content": result.text}},
        )

    await _send(
        websocket,
        {
            "status": "done",
            "level": result.level,
            "escalated": result.escalated,
        },
    )
    logger.info(
        f"[loop] done level={result.level} reason={result.stop_reason} "
        f"escalated={result.escalated} streamed={streamed_chars} "
        f"rollout={decision.reason if decision else '-'} query={query!r}"
    )
    return True
