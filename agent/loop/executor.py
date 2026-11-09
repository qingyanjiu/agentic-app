"""工具执行器。

L0/L1 直连和 L2 loop 共用同一个执行入口，区别只在"谁决定调哪个工具"。

escalate 语义：
- 参数不足 → 不 escalate，走追问；
- 工具不存在 / 后端不可用 / 超时 → escalate，交给 loop 换工具重试。

钩子与 loop 保持同一套语义：before_tool 返回非 None 即阻断，
after_tool 可以改写结果（脱敏、追加口径提示）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from .registry import ToolRegistry
from .types import ToolOutcome
from .validate import validate_args

logger = logging.getLogger(__name__)

BeforeTool = Callable[[str, dict], Awaitable[ToolOutcome | None]]
AfterTool = Callable[[str, dict, ToolOutcome], Awaitable[ToolOutcome]]


class Executor:
    def __init__(
        self,
        registry: ToolRegistry,
        timeout: float = 30.0,
        before_tool: BeforeTool | None = None,
        after_tool: AfterTool | None = None,
    ) -> None:
        self.registry = registry
        self.timeout = timeout
        self.before_tool = before_tool
        self.after_tool = after_tool

    async def execute(self, tool: str, args: dict | None = None) -> ToolOutcome:
        spec = self.registry.get(tool)
        if spec is None:
            available = ", ".join(self.registry.tool_names())
            return ToolOutcome(
                text=f"工具不存在：{tool}。可用工具：{available}",
                is_error=True,
                escalate=True,
            )

        payload = args or {}
        errors = validate_args(spec.parameters, payload)
        if errors:
            # 参数问题不升级：让模型/调用方补齐后重试
            return ToolOutcome(
                text="参数校验失败：" + "；".join(errors) + "。",
                is_error=True,
            )

        if self.before_tool is not None:
            try:
                blocked = await self.before_tool(tool, payload)
            except Exception as exc:
                logger.exception("[executor] before_tool 钩子异常")
                return ToolOutcome(text=f"工具前置检查失败：{exc}", is_error=True)
            if blocked is not None:
                return blocked

        try:
            outcome = await asyncio.wait_for(spec.execute(payload), timeout=self.timeout)
        except asyncio.TimeoutError:
            logger.warning(f"[executor] 工具 {tool} 超时（>{self.timeout}s）")
            return ToolOutcome(
                text=f"工具 {tool} 执行超时（超过 {self.timeout:.0f} 秒）。",
                is_error=True,
                escalate=True,
            )
        except Exception as exc:
            logger.exception(f"[executor] 工具 {tool} 执行异常")
            return ToolOutcome(
                text=f"工具执行失败：{exc}",
                is_error=True,
                escalate=True,
            )

        if self.after_tool is not None:
            try:
                outcome = await self.after_tool(tool, payload, outcome)
            except Exception:
                logger.exception("[executor] after_tool 钩子异常，保留原始结果")
        return outcome
