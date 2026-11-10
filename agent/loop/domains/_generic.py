"""通用业务域工厂。

11 个查询类业务域结构完全一样：一个 loop 工具 + 一张 `query_type → MCP 工具` 映射表。
差异只在映射表、slots 抽取函数、默认子类型和必填参数，所以抽成配置。

每个域文件只需要声明一个 `QueryDomain`，然后导出三个函数：
    build_domain(toolbox) / route_plan(query) / preview_plan(query)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..govern import govern
from ..registry import DomainSpec
from ..runtime import McpToolbox
from ..types import DomainPlan, ToolOutcome, ToolSpec, tool_schema

_QUERY_PARAM = {
    "query": {
        "type": "string",
        "description": "用户的原始问句。工具会自己解析子类型、时间和区域，不要拆字段。",
    }
}


@dataclass(frozen=True)
class QueryDomain:
    name: str
    server: str
    description: str
    tool_name: str
    tool_description: str
    extract_slots: Callable[[str], dict]
    tool_map: dict[str, str]
    default_type: str
    # query_type → 该子类型必须有的 slot 名；缺失则走追问
    required: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # slot 名 → 追问时给模型看的提示
    missing_hint: dict[str, str] = field(default_factory=dict)
    # 可选：把 slots 转成 MCP 参数（默认不传，让 Java 用默认口径）
    args_builder: Callable[[dict], dict] | None = None

    def resolve(self, query: str) -> tuple[str, str | None, dict, list[str]]:
        slots = self.extract_slots(query) or {}
        query_type = slots.get("query_type") or self.default_type
        tool = self.tool_map.get(query_type)
        missing = [k for k in self.required.get(query_type, ()) if not slots.get(k)]
        args = self.args_builder(slots) if self.args_builder else {}
        return query_type, tool, args, missing


def route_from(cfg: QueryDomain, query: str) -> DomainPlan:
    query_type, tool, _args, missing = cfg.resolve(query)
    if not tool:
        return DomainPlan(
            tool="",
            note=f"query_type={query_type} 后端暂未接入",
        )
    return DomainPlan(
        tool=cfg.tool_name,
        args={"query": query},
        missing=missing,
        note=missing[0] if missing else "",
    )


def preview_from(cfg: QueryDomain, query: str) -> dict:
    query_type, tool, args, missing = cfg.resolve(query)
    return {
        "domain": cfg.name,
        "query_type": query_type,
        "tool": tool,
        "args": args,
        "missing": missing,
        "note": "缺参数，应先向用户确认" if missing else "",
    }


def build_from(cfg: QueryDomain, toolbox: McpToolbox) -> DomainSpec:
    async def execute(raw_args: dict) -> ToolOutcome:
        query = str(raw_args.get("query") or "").strip()
        if not query:
            return ToolOutcome("缺少用户问题，无法解析查询条件。", is_error=True)

        query_type, tool, args, missing = cfg.resolve(query)

        if missing:
            hint = cfg.missing_hint.get(
                missing[0], f"还需要：{'、'.join(missing)}"
            )
            return ToolOutcome(
                f"{hint}（请先向用户确认，不要自己编造）",
                is_error=True,
            )

        if not tool:
            return ToolOutcome(
                f"「{query_type}」这个口径暂未接入后端，请如实告诉用户查不了。",
                is_error=True,
            )

        try:
            raw = await toolbox.call(cfg.server, tool, args)
        except Exception as exc:
            return ToolOutcome(f"查询失败：{exc}", is_error=True)

        return ToolOutcome(govern(raw, label=f"{cfg.name}_{query_type}"))

    return DomainSpec(
        name=cfg.name,
        description=cfg.description,
        tools=[
            ToolSpec(
                name=cfg.tool_name,
                description=cfg.tool_description,
                parameters=tool_schema(_QUERY_PARAM, ["query"]),
                execute=execute,
            )
        ],
        # 自检 / 发现去重用：这个域会打到哪些 MCP 工具（去重保序）
        mcp_tools=tuple(
            (cfg.server, name) for name in dict.fromkeys(cfg.tool_map.values())
        ),
    )
