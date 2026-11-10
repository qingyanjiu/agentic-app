"""人员态势业务域。

一个工具覆盖该域下的所有子类型（实时/进入/离开/人数/流动/结构），
子类型由 extract_person_status_slots 解析，再到 MCP 工具名 —— 对模型完全透明。

这样处理的原因：人员态势的子类型区分靠中文关键词，模型自己判断反而不如
现有正则+分类器稳定；把它放在工具内部，既复用了既有逻辑，又不增加一次 LLM 往返。
"""
from __future__ import annotations

import logging

from agent.intent.slots import extract_person_status_slots

from ..govern import govern
from ..registry import DomainSpec
from ..runtime import McpToolbox
from ..types import DomainPlan, ToolOutcome, ToolSpec, tool_schema

logger = logging.getLogger(__name__)

# 配置里人员态势的 server key（Java sidecar /mcp/personStatus）
SERVER = "local"

# query_type → MCP 工具名（与 graph/person_status_langgraph.py 的 _JAVA_TOOL_MAP 一致）
JAVA_TOOL_MAP = {
    "realtime": "person_status:getTodayPersonnelAffairs",
    "enter": "person_status:getTodayPersonnelAffairs",
    "leave": "person_status:getTodayPersonnelAffairs",
    "count": "person_status:getTodayPersonnelAffairs",
    "flow": "person_status:getTodayPersonnelFlow",
    "structure": "person_status:getPersonnelStructure",
}

_QUERY_PARAM = {
    "query": {
        "type": "string",
        "description": "用户的原始问句。工具会自己解析是「实时/进入/离开/人数/流动/结构」中的哪一类。",
    }
}


def preview_plan(query: str) -> dict:
    slots = extract_person_status_slots(query)
    query_type = slots.get("query_type") or "count"
    tool = JAVA_TOOL_MAP.get(query_type)
    if tool is None:
        return {
            "domain": "person_status",
            "tool": None,
            "args": {},
            "note": f"query_type={query_type} 后端暂不支持，应直接告知用户",
        }
    return {"domain": "person_status", "tool": tool, "args": {}, "note": "按今天口径查询"}


def route_plan(query: str) -> DomainPlan:
    """预路由用：人员态势只有一个 loop 工具，子类型在工具内部判定。"""
    slots = extract_person_status_slots(query)
    query_type = slots.get("query_type") or "count"
    if query_type not in JAVA_TOOL_MAP:
        return DomainPlan(tool="", note=f"query_type={query_type} 后端暂未接入")
    return DomainPlan(tool="person_status__query", args={"query": query})


def build_domain(toolbox: McpToolbox) -> DomainSpec:
    async def person_status(raw_args: dict) -> ToolOutcome:
        query = str(raw_args.get("query") or "").strip()
        if not query:
            return ToolOutcome("缺少用户问题，无法解析查询条件。", is_error=True)

        slots = extract_person_status_slots(query)
        query_type = slots.get("query_type") or "count"
        tool_name = JAVA_TOOL_MAP.get(query_type)
        if tool_name is None:
            return ToolOutcome(
                f"人员态势的「{query_type}」查询暂未接入后端，请直接告诉用户这个口径暂时查不了。",
                is_error=True,
            )

        # 大屏口径默认「今天」，现有图也是这个默认；这里先不传时间参数，
        # 需要历史区间时再按 Java 侧 schema 补 startTime/endTime。
        try:
            raw = await toolbox.call(SERVER, tool_name, {})
        except Exception as exc:
            logger.warning(f"[loop] 人员态势查询失败（{tool_name}）：{exc}")
            return ToolOutcome(f"人员态势查询失败：{exc}", is_error=True)

        return ToolOutcome(govern(raw, label=f"person_{query_type}"))

    return DomainSpec(
        name="person_status",
        description="人员态势：实时人数、进出、流动、人员结构（园区大屏口径）",
        tools=[
            ToolSpec(
                name="person_status__query",
                description=(
                    "查询人员态势数据。适用于「现在园区有多少人」「今天进了多少人」"
                    "「人员流动情况」「人员结构是怎样的」。"
                    "工具会自己判断是人数、进出、流动还是结构，调用方不需要指定子类型。"
                ),
                parameters=tool_schema(_QUERY_PARAM, ["query"]),
                execute=person_status,
            )
        ],
        # 自检 / 发现去重用：这个域会打到哪些 MCP 工具（去重保序）
        mcp_tools=tuple(
            (SERVER, name) for name in dict.fromkeys(JAVA_TOOL_MAP.values())
        ),
    )
