"""孪生巡检业务域（4 个子类型）。"""
from agent.intent.slots import extract_twins_inspection_slots

from ._generic import QueryDomain, build_from, preview_from, route_from

CONFIG = QueryDomain(
    name="twins_inspection",
    server="inspection",
    description="孪生巡检：今日巡检、今日任务、巡检统计、执行状态",
    tool_name="twins_inspection__query",
    tool_description=(
        "查询孪生巡检数据。适用于「今天巡检完成了吗」「今天有哪些巡检任务」"
        "「巡检统计」「巡检执行状态」。工具会自己判断子类型。"
    ),
    extract_slots=extract_twins_inspection_slots,
    tool_map={
        # 以真实 MCP 工具列表为准：旧图里的 getTodayInspection/getTodayTasks/
        # getInspectionStatistics/getInspectionExecutionStatus 后端已改成下面这组
        "today_inspection": "inspection:getTodayOverview",
        "today_tasks": "inspection:getTodayTaskList",
        "inspection_statistics": "inspection:getStatistics",
        "inspection_execution_status": "inspection:getExecutionStatus",
    },
    default_type="today_inspection",
)


def build_domain(toolbox):
    return build_from(CONFIG, toolbox)


def route_plan(query: str):
    return route_from(CONFIG, query)


def preview_plan(query: str) -> dict:
    return preview_from(CONFIG, query)
