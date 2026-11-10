"""工具注册表。

把「业务域」声明成一份数据：域名 + 描述 + 工具列表。
registry 负责：

- 汇总所有工具，生成给模型的 OpenAI tools 数组；
- 按名字查找 ToolSpec（loop 执行时用）；
- 给 system prompt 生成一段工具概览（模型据此判断"该问用户还是该查数据"）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .types import ToolSpec


@dataclass(slots=True)
class DomainSpec:
    """一个业务域。

    name 用于工具名前缀（device_query__list），description 会写进 system prompt，
    帮模型理解"这个域负责什么"，但真正的路由仍然由工具自身的 description 决定。
    """

    name: str
    description: str
    tools: list[ToolSpec] = field(default_factory=list)
    # 该域依赖的 MCP 工具：(server, tool_name)。
    # 只给启动自检和动态发现去重用，不参与运行时调用；
    # 漏写只会让自检少查几个，不影响查询本身。
    mcp_tools: tuple[tuple[str, str], ...] = ()


class ToolRegistry:
    def __init__(self) -> None:
        self._domains: dict[str, DomainSpec] = {}
        self._by_name: dict[str, ToolSpec] = {}

    def add_domain(self, domain: DomainSpec) -> None:
        if domain.name in self._domains:
            raise ValueError(f"业务域重复注册：{domain.name}")
        self._domains[domain.name] = domain
        for tool in domain.tools:
            if tool.name in self._by_name:
                raise ValueError(f"工具名重复：{tool.name}")
            self._by_name[tool.name] = tool

    @property
    def domains(self) -> list[DomainSpec]:
        return list(self._domains.values())

    @property
    def tools(self) -> list[ToolSpec]:
        return list(self._by_name.values())

    def get(self, name: str) -> ToolSpec | None:
        return self._by_name.get(name)

    def to_openai_tools(self) -> list[dict]:
        return [tool.to_openai() for tool in self.tools]

    def describe(self) -> str:
        """生成 system prompt 里的工具概览。"""
        lines: list[str] = []
        for domain in self.domains:
            lines.append(f"- {domain.name}：{domain.description}")
            for tool in domain.tools:
                lines.append(f"  - {tool.name}")
        return "\n".join(lines)

    def tool_names(self) -> list[str]:
        return sorted(self._by_name)
