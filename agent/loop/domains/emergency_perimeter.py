"""周界态势业务域（4 个子类型）。"""
from agent.intent.slots import extract_emergency_perimeter_slots

from ._generic import QueryDomain, build_from, preview_from, route_from

CONFIG = QueryDomain(
    name="emergency_perimeter",
    server="perimeter",
    description="周界态势：关键指标、防区概览、周界告警统计、告警总览",
    tool_name="emergency_perimeter__query",
    tool_description=(
        "查询周界态势数据。适用于「周界有几个防区」「哪个防区告警最多」"
        "「周界告警统计」「防区布防情况」。工具会自己判断子类型。"
    ),
    extract_slots=extract_emergency_perimeter_slots,
    tool_map={
        "key_metrics": "perimeter:getKeyMetrics",
        "area_overview": "perimeter:getAreaOverview",
        "perimeter_alarm_stats": "perimeter:getPerimeterAlarmStats",
        "alarm_overview": "perimeter:getAlarmOverview",
    },
    default_type="alarm_overview",
)


def build_domain(toolbox):
    return build_from(CONFIG, toolbox)


def route_plan(query: str):
    return route_from(CONFIG, query)


def preview_plan(query: str) -> dict:
    return preview_from(CONFIG, query)
