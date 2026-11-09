"""回答生成（Composer）与追问模板。

L1 直连时只需要一次 LLM：把工具结果组织成自然语言。
追问不调 LLM —— 模板化能省 0.5s，而且是确定性输出。

流式：传 on_delta 时走 stream=True，边生成边回调，前端可以立即出字。
"""
from __future__ import annotations

import inspect
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

COMPOSE_SYSTEM = """你是智慧园区的数据查询助手。用户的问题已经由工具查过，下面会给出查询结果。
规则：
1. 只根据查询结果回答，不要补充结果里没有的数据、数字或状态。
2. 用中文，简洁，不要输出 JSON 原文，不要把内部字段名念给用户（如 syncSource）。
3. 结果为空或报错时，如实说明，不要编造。
4. 涉及"已下发"的动作类结果，不要说"已完成"。"""

# 追问模板：key 是工具返回的 missing 字段名
ASK_TEMPLATES: dict[str, str] = {
    "device_keyword": "请问您想查看哪台设备？可以说设备名称或编号。",
    "person_name": "请问查谁的？可以告诉我姓名。",
    "date": "请问查哪个时间范围？比如今天、昨天、上周。",
    "alarm_id": "请问您要查哪条告警？可以提供告警编号。",
}

DeltaHandler = Callable[[str], Awaitable[None] | None]


def ask_text(missing: list[str]) -> str:
    """把缺口翻译成一句自然的追问；不认识的缺口给个通用问法。"""
    for key in missing:
        template = ASK_TEMPLATES.get(key)
        if template:
            return template
    if missing:
        return f"还需要补充：{'、'.join(missing)}。"
    return "请再补充一点信息可以吗？"


async def _emit_delta(handler: DeltaHandler | None, text: str) -> None:
    if handler is None or not text:
        return
    result = handler(text)
    if inspect.isawaitable(result):
        await result


class Composer:
    def __init__(self, client: Any, model: str, temperature: float = 0.0) -> None:
        self.client = client
        self.model = model
        self.temperature = temperature

    def _messages(self, query: str, tool_text: str, history: list[dict] | None) -> list[dict]:
        messages: list[dict] = [{"role": "system", "content": COMPOSE_SYSTEM}]
        messages.extend(history or [])
        messages.append(
            {
                "role": "user",
                "content": f"用户问题：{query}\n\n查询结果：\n{tool_text}",
            }
        )
        return messages

    async def compose(
        self,
        query: str,
        tool_text: str,
        history: list[dict] | None = None,
        on_delta: DeltaHandler | None = None,
    ) -> str:
        messages = self._messages(query, tool_text, history)

        if on_delta is None:
            return await self._compose_once(messages)
        return await self._compose_stream(messages, on_delta, fallback=tool_text)

    async def _compose_once(self, messages: list[dict]) -> str:
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
            )
            return (response.choices[0].message.content or "").strip()
        except Exception as exc:
            logger.exception("[composer] 生成回答失败")
            return self._fallback_text(messages, exc)

    async def _compose_stream(
        self,
        messages: list[dict],
        on_delta: DeltaHandler,
        fallback: str,
    ) -> str:
        parts: list[str] = []
        try:
            stream = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                stream=True,
            )
            async for chunk in stream:
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                delta = getattr(choices[0], "delta", None)
                text = getattr(delta, "content", None) if delta is not None else None
                if text:
                    parts.append(text)
                    await _emit_delta(on_delta, text)
            return "".join(parts).strip()
        except Exception as exc:
            logger.exception("[composer] 流式生成失败，回落非流式")
            if parts:
                return "".join(parts).strip()
            return await self._compose_once(messages) or fallback

    @staticmethod
    def _fallback_text(messages: list[dict], exc: Exception) -> str:
        # 失败时回落到工具原文，至少不让用户空手而归
        content = messages[-1].get("content") or ""
        if "查询结果：" in content:
            return content.split("查询结果：", 1)[1].strip()
        return f"生成回答失败：{exc}"
