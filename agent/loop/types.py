"""Loop Agent 的核心数据结构。

设计原则（参考 pi 的 agent-loop）：

- 工具是声明式的：名称 + 描述 + JSON Schema + execute；
- 模型只负责"选哪个工具、给什么参数"，业务口径与参数规范化都放在
  execute 内部，对模型透明；
- loop 只做一件事：调模型 → 执行 tool_calls → 回填结果 → 再调模型。

这样新增一个业务域 = 加一份 ToolSpec 声明，不需要再写一张 LangGraph 图。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass(slots=True)
class ToolOutcome:
    """一次工具执行的返回值。

    text 是回填给模型的 tool message 内容；is_error 让模型知道这次调用失败、
    可以换参数重试或转述给用户；terminate 表示整批工具都要求结束本轮。
    """

    text: str
    is_error: bool = False
    terminate: bool = False
    # 本工具搞不定、建议升级到 loop 重新选工具（参数不足不算，参数不足走追问）
    escalate: bool = False


@dataclass(slots=True)
class ToolSpec:
    """给模型看的工具声明。

    parameters 建议使用"宽松 schema"：把用户原话作为一个字符串字段传进来，
    由 execute 内部调用现有的 extract_*_slots 做规范化。
    这样模型不需要知道 syncSource、startTime 这类接口细节。
    """

    name: str
    description: str
    parameters: dict
    execute: Callable[[dict], Awaitable[ToolOutcome]]
    # execute 的返回值是否已经是"面向用户"的成品文本（如设备清单）。
    # True 时 L1 直接把它当回答返回，不再交给 Composer 复述——
    # 列表类结果让模型复写一遍，只会多出"根据查询结果""以下是"这类废话，
    # 还多花 1~2 秒和一次 token。
    passthrough: bool = False

    def to_openai(self) -> dict:
        """转成 OpenAI function calling 的 tool 定义。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass(slots=True)
class LoopEvent:
    """loop 向宿主（WebSocket / CLI）推送的事件。

    对齐 pi 的词汇表，前端只需要认识这几种：
    - assistant_message：一段完整的助手文本
    - tool_start / tool_end：工具调用开始/结束
    - done / error：本轮结束
    """

    type: str
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"type": self.type, "data": self.data}


@dataclass(slots=True)
class LoopResult:
    """一次 agent run 的结果。"""

    text: str
    messages: list[dict]
    tool_calls: list[dict]
    steps: int
    stop_reason: str  # answered / max_steps / error

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "steps": self.steps,
            "stop_reason": self.stop_reason,
            "tool_calls": self.tool_calls,
        }


def tool_schema(
    properties: dict | None = None,
    required: list[str] | None = None,
) -> dict:
    """生成一个 object 类型的 JSON Schema（省去手写样板）。"""
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


def parse_args(raw: str | None) -> dict:
    """解析模型给的 arguments；空串/None 当成空对象，坏 JSON 抛 ValueError。"""
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"工具参数不是合法 JSON：{exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("工具参数必须是 JSON 对象")
    return value
@dataclass(slots=True)
class DomainPlan:
    """路由层要的"一次调用计划"。

    注意 args 是给 loop 工具（ToolSpec）的宽松参数（通常只有一个 query），
    不是 MCP 工具的接口参数——接口参数由工具内部解析，路由层不感知。
    """

    tool: str
    args: dict = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    note: str = ""
