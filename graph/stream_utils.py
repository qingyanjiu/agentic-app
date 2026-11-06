# -*- coding: utf-8 -*-
"""业务图里 LLM 回答的流式工具。

各业务域原来的回答节点都是 `await llm.ainvoke(prompt)`，整段生成完才把
answer 事件推给前端，首字延迟等于整段生成时间。改成 `astream` 后，每收到
一个 chunk 就通过 LangGraph 的 custom 通道推给 app.py，由 WebSocket 即时转发。

`get_stream_writer()` 只在图被 `astream` 执行时可用；单测里直接调用格式化
函数（图没有流式执行）时拿不到 writer，此时退化为普通累积，不影响结果。
"""
import logging
from typing import Any

from langgraph.config import get_stream_writer

logger = logging.getLogger(__name__)


def _chunk_text(chunk: Any) -> str:
    """兼容 str / AIMessageChunk.content(str 或 block 列表)"""
    content = getattr(chunk, "content", chunk)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") if isinstance(b, dict) else str(b)
            for b in content
        )
    if content is None:
        return ""
    return str(content)


def _get_writer():
    """图内流式执行时返回 writer；普通调用返回 None"""
    try:
        return get_stream_writer()
    except Exception:
        return None


async def stream_llm_text(llm: Any, prompt: str) -> str:
    """流式生成回答并返回完整文本。

    - 有 writer：每个 token 以 {"type": "answer", "content": ...} 推出；
    - 无 writer（单测/非流式调用）：仅累积文本；
    - 流式中途失败但已有内容：返回已生成部分，避免再发一次完整 answer 造成重复；
    - 一个 token 都没拿到就失败：向上抛，由调用方降级到模板文本。
    """
    writer = _get_writer()
    parts = []

    try:
        async for chunk in llm.astream(prompt):
            text = _chunk_text(chunk)
            if not text:
                continue
            parts.append(text)
            if writer is not None:
                try:
                    writer({"type": "answer", "content": text})
                except Exception as e:
                    # writer 失效不影响生成，最多退化成非流式
                    logger.debug(f"流式 writer 推送失败，后续改为累积: {e}")
                    writer = None
    except Exception:
        if not parts:
            raise
        logger.warning("LLM 流式中断，使用已生成的部分回答", exc_info=True)

    return "".join(parts).strip().strip('"\n')
