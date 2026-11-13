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
    # 可选：接口正常但没有数据时，给用户的结论话术（默认「没有查到记录」）
    empty_hint: str = ""

    def resolve(self, query: str) -> tuple[str, str | None, dict, list[str]]:
        slots = self.extract_slots(query) or {}
        # 子类型字段两套写法并存：slots.py 里一半抽取器给 query_type，
        # 另一半给 event_type（会议/食堂/信息发布/安防）。只读前者会让后四个域
        # 永远落到 default_type —— 表现就是"不管问什么，都打同一个 MCP 工具"。
        query_type = (
            slots.get("query_type")
            or slots.get("event_type")
            or self.default_type
        )
        # `count` 是抽取器的"没认出子类型"标记（旧图靠它反问），它不在 tool_map 里。
        # 到 loop 这一层必须落到域默认工具，否则工具解析成 None → 用户看到"后端暂未接入"。
        if query_type not in self.tool_map:
            query_type = self.default_type
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

        if _is_empty_payload(raw):
            return ToolOutcome(_empty_note(cfg, query_type))

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


# 接口"正常但没数据"的几种写法。
# 实测 security:getSecurityAlarmList / getAlarmListWithType 今天返回的就是
# 字面字符串 `null`（isError 还是 False），模型看到 "null" 就说
# 「两次都返回空结果，可能服务异常，建议稍后再试」——空结果被说成了故障。
_EMPTY_LITERALS = frozenset({"", "null", "none", "[]", "{}", "nil", "undefined"})


def _is_empty_payload(raw: str | None) -> bool:
    """接口调用成功，但没有数据（null / 空数组 / 空对象 / 空串）。

    先去掉所有空白再比：后端偶尔给 `[ ]`、`{ }` 这种写法。
    """
    return "".join((raw or "").split()).lower() in _EMPTY_LITERALS


def _empty_note(cfg: QueryDomain, query_type: str) -> str:
    """空结果要说得像"没有"，不像"坏了"。

    这段会进模型上下文（L1 走 composer、L2 由模型作答），措辞直接决定
    它会不会编「服务异常」「稍后再试」。所以既给结论，也明确禁止瞎猜。
    """
    conclusion = cfg.empty_hint or "该时段没有查到记录"
    return (
        f"{conclusion}（接口正常返回、值为空，不是故障）。"
        f"直接按这个结论回答用户即可；不要说接口异常、服务异常、数据未生成，"
        f"也不要让用户稍后再试。"
    )
