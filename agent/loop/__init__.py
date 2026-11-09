"""Loop Agent 包。

对外只暴露四样东西：

- LoopAgent：主循环
- ToolRegistry / DomainSpec / ToolSpec：工具声明
- McpToolbox：MCP 调用封装
- build_registry()：装配默认业务域

快速试跑见 agent/loop/cli.py。
"""
from .agent import DEFAULT_SYSTEM_PROMPT, LoopAgent
from .composer import ASK_TEMPLATES, Composer, ask_text
from .executor import Executor
from .govern import govern
from .pipeline import ChatPipeline, PipelineResult, Session
from .registry import DomainSpec, ToolRegistry
from .router import Pending, RoutePlan, Router
from .runtime import McpToolbox, extract_text
from .types import LoopEvent, LoopResult, ToolOutcome, ToolSpec, tool_schema

__all__ = [
    "LoopAgent",
    "DEFAULT_SYSTEM_PROMPT",
    "ToolRegistry",
    "DomainSpec",
    "ToolSpec",
    "ToolOutcome",
    "LoopEvent",
    "LoopResult",
    "tool_schema",
    "McpToolbox",
    "extract_text",
    "govern",
    "Router",
    "RoutePlan",
    "Pending",
    "Executor",
    "Composer",
    "ask_text",
    "ASK_TEMPLATES",
    "ChatPipeline",
    "PipelineResult",
    "Session",
    "build_registry",
]


def build_registry() -> ToolRegistry:
    """装配内置业务域。

    新增一个业务域：在 agent/loop/domains/ 下加一个模块，导出 build_domain(toolbox)，
    然后在这里 add_domain。不需要改 loop 本身。
    """
    from .domains import build_default_domains

    registry = ToolRegistry()
    for domain in build_default_domains():
        registry.add_domain(domain)
    return registry
