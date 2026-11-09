"""Agent loop 主体。

结构对齐 pi 的 runLoop，但按业务宿主做了裁剪。同时保留三个健壮性判断
（这三条是从 pi 的 loop 里抄的，缺了会在生产里出事）：

1. finish_reason="length" 时不执行工具 —— 参数可能被截断；
2. 单个工具超时保护 —— max_steps 只管轮数，管不住单次卡死；
3. cancel 信号 —— 用户在页面上点"停止"必须能中断。

流式：默认 stream=True，模型输出逐字回调 assistant_delta；
工具调用的 arguments 是分片增量，按 index 累加后再拼成完整 JSON。

其余取舍：

- 工具串行执行。并行会和现有 MCP 客户端/Java sidecar 的连接模型冲突，
  而且这里的工具大多有先后依赖（先列表定位 id，再查详情）。
- 不隐式重试。工具失败以 is_error 回给模型，由模型决定换参数还是转述。
- 缺参数不报错。工具返回"缺少 X，请问用户"的提示，模型据此发起追问。
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace as NS
from typing import Awaitable, Callable

from openai import AsyncOpenAI

from .registry import ToolRegistry
from .types import LoopEvent, LoopResult, ToolOutcome, parse_args
from .validate import compress_tool_messages, validate_args

logger = logging.getLogger(__name__)

EventHandler = Callable[[LoopEvent], Awaitable[None] | None]

# before_tool 返回非 None 即阻断（用返回的 ToolOutcome 作为给模型的结果）
BeforeTool = Callable[[str, dict], Awaitable[ToolOutcome | None]]
# after_tool 可以改写工具结果（脱敏、追加提示、改 is_error）
AfterTool = Callable[[str, dict, ToolOutcome], Awaitable[ToolOutcome]]


DEFAULT_SYSTEM_PROMPT = """你是智慧园区的数据查询助手。用户用自然语言提问，你通过工具查询真实数据后回答。

规则：
1. 回答任何业务数据之前，必须先调用工具。禁止凭常识编造数据、数字或状态。
2. 工具参数请直接传用户的原话（比如 time_text 写"昨天"），工具内部会解析成接口格式。
3. 工具返回"缺少某参数"时，不要猜，直接用一句自然的中文向用户确认，然后停止本轮。
4. 工具报错时，把关键原因转述给用户，不要沉默，也不要假装成功。
5. 动作类工具（场景控制等）返回"已下发"只代表指令已发出，不要说"已完成"。
6. 回答简洁，用中文，不要输出 JSON 原文。

可用业务域与工具：
{tool_overview}
"""


class LoopAgent:
    def __init__(
        self,
        *,
        client: AsyncOpenAI,
        model: str,
        registry: ToolRegistry,
        system_prompt: str | None = None,
        max_steps: int = 8,
        temperature: float = 0.0,
        tool_timeout: float = 30.0,
        context_budget: int = 60_000,
        stream: bool = True,
        before_tool: BeforeTool | None = None,
        after_tool: AfterTool | None = None,
        on_event: EventHandler | None = None,
    ) -> None:
        self.client = client
        self.model = model
        self.registry = registry
        self.system_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT.format(
            tool_overview=registry.describe()
        )
        self.max_steps = max_steps
        self.temperature = temperature
        self.tool_timeout = tool_timeout
        self.context_budget = context_budget
        self.stream = stream
        self.before_tool = before_tool
        self.after_tool = after_tool
        self.on_event = on_event

    # ------------------------------------------------------------------ #
    # 事件与状态
    # ------------------------------------------------------------------ #
    async def _emit(
        self,
        handler: EventHandler | None,
        type_: str,
        data: dict | None = None,
    ) -> None:
        if handler is None:
            return
        event = LoopEvent(type_, data or {})
        result = handler(event)
        if hasattr(result, "__await__"):
            await result  # type: ignore[func-returns-value]

    @staticmethod
    def _cancelled(cancel: asyncio.Event | None) -> bool:
        return cancel is not None and cancel.is_set()

    async def _aborted(
        self,
        handler: EventHandler | None,
        messages: list[dict],
        executed: list[dict],
        steps: int,
    ) -> LoopResult:
        text = "已取消本轮查询。"
        await self._emit(handler, "aborted", {"steps": steps})
        return LoopResult(
            text=text,
            messages=messages,
            tool_calls=executed,
            steps=steps,
            stop_reason="aborted",
        )

    # ------------------------------------------------------------------ #
    # 模型调用（流式 / 非流式）
    # ------------------------------------------------------------------ #
    async def _call_model(
        self,
        messages: list[dict],
        tools: list[dict],
        handler: EventHandler | None,
    ) -> tuple[str, list, str | None]:
        """返回 (文本, tool_calls, finish_reason)。

        流式下 tool_calls 的 arguments 是分片字符串，必须按 index 累加。
        """
        if not self.stream:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                temperature=self.temperature,
            )
            choice = response.choices[0]
            message = choice.message
            return (
                message.content or "",
                list(message.tool_calls or []),
                getattr(choice, "finish_reason", None),
            )

        stream = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            temperature=self.temperature,
            stream=True,
        )
        parts: list[str] = []
        slots: dict[int, dict] = {}
        finish_reason: str | None = None

        async for chunk in stream:
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            choice = choices[0]
            if getattr(choice, "finish_reason", None):
                finish_reason = choice.finish_reason
            delta = getattr(choice, "delta", None)
            if delta is None:
                continue

            content = getattr(delta, "content", None)
            if content:
                parts.append(content)
                await self._emit(handler, "assistant_delta", {"text": content})

            for tc in getattr(delta, "tool_calls", None) or []:
                index = getattr(tc, "index", 0) or 0
                slot = slots.setdefault(index, {"id": "", "name": "", "arguments": ""})
                if getattr(tc, "id", None):
                    slot["id"] = tc.id
                fn = getattr(tc, "function", None)
                if fn is not None:
                    if getattr(fn, "name", None):
                        slot["name"] = fn.name
                    if getattr(fn, "arguments", None):
                        slot["arguments"] += fn.arguments

        tool_calls = [
            NS(
                id=slot["id"] or f"call_{index}",
                function=NS(
                    name=slot["name"],
                    arguments=slot["arguments"] or "{}",
                ),
            )
            for index, slot in sorted(slots.items())
            if slot["name"]
        ]
        return "".join(parts), tool_calls, finish_reason

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #
    async def run(
        self,
        user_input: str,
        history: list[dict] | None = None,
        cancel: asyncio.Event | None = None,
        on_event: EventHandler | None = None,
    ) -> LoopResult:
        # 优先用本次调用传入的 handler，避免单例被并发会话串台
        handler = on_event or self.on_event

        messages: list[dict] = [{"role": "system", "content": self.system_prompt}]
        messages.extend(history or [])
        messages.append({"role": "user", "content": user_input})

        tools = self.registry.to_openai_tools()
        executed: list[dict] = []
        steps = 0

        while steps < self.max_steps:
            if self._cancelled(cancel):
                return await self._aborted(handler, messages, executed, steps)

            # 工具结果总量超预算时，压缩最早的长结果（不调模型）
            compressed = compress_tool_messages(messages, self.context_budget)
            if compressed:
                logger.info(f"[loop] 上下文压缩：{compressed} 条工具结果")

            steps += 1
            try:
                text, tool_calls, finish_reason = await self._call_model(
                    messages, tools, handler
                )
            except Exception as exc:  # 网络/鉴权/模型侧错误
                logger.exception("[loop] 模型调用失败")
                await self._emit(handler, "error", {"message": str(exc)})
                return LoopResult(
                    text=f"模型调用失败：{exc}",
                    messages=messages,
                    tool_calls=executed,
                    steps=steps,
                    stop_reason="error",
                )

            # 没有工具调用 → 本轮就是最终回答
            if not tool_calls:
                text = text.strip()
                await self._emit(handler, "assistant_message", {"text": text})
                await self._emit(handler, "done", {"steps": steps})
                return LoopResult(
                    text=text,
                    messages=messages,
                    tool_calls=executed,
                    steps=steps,
                    stop_reason="answered",
                )

            # 把 assistant 的 tool_calls 原样回填，保持 id 对应
            messages.append(
                {
                    "role": "assistant",
                    "content": text,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments or "{}",
                            },
                        }
                        for tc in tool_calls
                    ],
                }
            )

            # 输出被长度截断：参数可能是半截 JSON，一律不执行，要求重发
            if finish_reason == "length":
                for tc in tool_calls:
                    name = tc.function.name
                    notice = (
                        f'工具调用 "{name}" 未执行：模型输出达到长度上限，'
                        f"参数可能被截断。请重新生成完整参数后再调用。"
                    )
                    logger.warning(f"[loop] 截断保护：跳过 {name}")
                    executed.append(
                        {
                            "name": name,
                            "arguments": tc.function.arguments,
                            "is_error": True,
                            "preview": notice[:200],
                        }
                    )
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.id, "content": notice}
                    )
                    await self._emit(
                        handler,
                        "tool_end",
                        {"name": name, "is_error": True, "preview": notice[:200]},
                    )
                continue

            all_terminate = True
            for tc in tool_calls:
                if self._cancelled(cancel):
                    return await self._aborted(handler, messages, executed, steps)

                name = tc.function.name
                await self._emit(
                    handler,
                    "tool_start",
                    {"name": name, "arguments": tc.function.arguments},
                )
                outcome = await self._execute(name, tc.function.arguments)
                all_terminate = all_terminate and outcome.terminate
                executed.append(
                    {
                        "name": name,
                        "arguments": tc.function.arguments,
                        "is_error": outcome.is_error,
                        "preview": outcome.text[:200],
                    }
                )
                messages.append(
                    {"role": "tool", "tool_call_id": tc.id, "content": outcome.text}
                )
                await self._emit(
                    handler,
                    "tool_end",
                    {
                        "name": name,
                        "is_error": outcome.is_error,
                        "preview": outcome.text[:200],
                    },
                )

            if all_terminate:
                break

        # 步数用尽：再要一次不带工具的总结，避免给用户一个空回答
        await self._emit(handler, "error", {"message": "达到最大工具调用步数"})
        summary = await self._finalize(messages)
        return LoopResult(
            text=summary,
            messages=messages,
            tool_calls=executed,
            steps=steps,
            stop_reason="max_steps",
        )

    async def _execute(self, name: str, raw_args: str | None) -> ToolOutcome:
        spec = self.registry.get(name)
        if spec is None:
            available = ", ".join(self.registry.tool_names())
            return ToolOutcome(
                text=f"工具不存在：{name}。可用工具：{available}",
                is_error=True,
            )
        try:
            args = parse_args(raw_args)
        except ValueError as exc:
            return ToolOutcome(text=f"{exc}。请重新生成合法的 JSON 参数。", is_error=True)

        errors = validate_args(spec.parameters, args)
        if errors:
            return ToolOutcome(
                text="参数校验失败：" + "；".join(errors) + "。请修正后重试。",
                is_error=True,
            )

        if self.before_tool is not None:
            try:
                blocked = await self.before_tool(name, args)
            except Exception as exc:
                logger.exception("[loop] before_tool 钩子异常")
                return ToolOutcome(text=f"工具前置检查失败：{exc}", is_error=True)
            if blocked is not None:
                logger.info(f"[loop] before_tool 阻断了 {name}")
                return blocked

        try:
            outcome = await asyncio.wait_for(
                spec.execute(args), timeout=self.tool_timeout
            )
        except asyncio.TimeoutError:
            logger.warning(f"[loop] 工具 {name} 超时（>{self.tool_timeout}s）")
            return ToolOutcome(
                text=(
                    f"工具 {name} 执行超时（超过 {self.tool_timeout:.0f} 秒）。"
                    f"请缩小查询范围后重试，或换一种方式回答用户。"
                ),
                is_error=True,
            )
        except Exception as exc:
            logger.exception(f"[loop] 工具 {name} 执行异常")
            return ToolOutcome(text=f"工具执行失败：{exc}", is_error=True)

        if self.after_tool is not None:
            try:
                outcome = await self.after_tool(name, args, outcome)
            except Exception as exc:
                logger.exception("[loop] after_tool 钩子异常，保留原始结果")
                _ = exc
        return outcome

    async def _finalize(self, messages: list[dict]) -> str:
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=list(messages)
                + [{"role": "user", "content": "请基于已获得的数据，用一句话向用户说明当前结论。"}],
                temperature=self.temperature,
            )
            return (response.choices[0].message.content or "").strip()
        except Exception as exc:
            logger.warning(f"[loop] 收尾总结失败：{exc}")
            return "查询步骤过多，我先停在这里。请把问题拆小一点再问我。"
