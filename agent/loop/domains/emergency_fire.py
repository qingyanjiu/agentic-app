"""消防态势业务域（4 个子类型）。"""
from agent.intent.slots import extract_emergency_fire_slots

from ._generic import QueryDomain, build_from, preview_from, route_from

CONFIG = QueryDomain(
    name="emergency_fire",
    server="fire",
    description="消防态势：消防资产、火警数量、火警列表、本月维修",
    tool_name="emergency_fire__query",
    tool_description=(
        "查询消防态势数据。适用于「消防设备有多少」「今天火警几条」"
        "「火警列表」「本月维修情况」。工具会自己判断子类型。"
    ),
    extract_slots=extract_emergency_fire_slots,
    tool_map={
        "fire_assets": "fire:getFireAssets",
        "fire_alarm_num": "fire:getFireAlarmNum",
        "fire_alarm_list": "fire:getFireAlarmList",
        "month_repair": "fire:getMonthRepair",
    },
    default_type="fire_alarm_list",
)


def build_domain(toolbox):
    return build_from(CONFIG, toolbox)


def route_plan(query: str):
    return route_from(CONFIG, query)


def preview_plan(query: str) -> dict:
    return preview_from(CONFIG, query)
