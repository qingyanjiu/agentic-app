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
    """装配内置业务域 + 启动时动态发现到的 MCP 工具。

    新增一个显式业务域：在 agent/loop/domains/ 下加一个模块，导出
    build_domain(toolbox)，然后登记到 domains/_MODULES。

    动态发现的部分来自 discover.warm_discovered()（startup 时预热）：
    未预热、或 MCP 不可达时为空的，此时行为与「只有显式工具」完全一致。
    """
    from .discover import get_discovered
    from .domains import build_default_domains

    registry = ToolRegistry()
    for domain in build_default_domains():
        registry.add_domain(domain)

    for domain in get_discovered():
        if domain.name in {item.name for item in registry.domains}:
            continue
        registry.add_domain(domain)
    return registry
