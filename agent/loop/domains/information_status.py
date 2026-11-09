"""信息发布业务域（6 个子类型）。"""
from agent.intent.slots import extract_information_status_slots

from ._generic import QueryDomain, build_from, preview_from, route_from

CONFIG = QueryDomain(
    name="information_status",
    server="information",
    description="信息发布：信息发布量、广播播放、任务趋势、节目数、发布/广播设备",
    tool_name="information_status__query",
    tool_description=(
        "查询信息发布数据。适用于「今天发了多少条信息」「广播播放情况」"
        "「信息发布任务趋势」「发布设备在线情况」。工具会自己判断子类型。"
    ),
    extract_slots=extract_information_status_slots,
    tool_map={
        "info_view": "information:getInfoView",
        "broadcast_view": "information:getBroadcastView",
        "task_trend": "information:getTaskTrend",
        "program_count": "information:getProgramCount",
        "info_equip": "information:getInfoPublishiEquip",
        "broadcast_equip": "information:getBroadcastEquip",
    },
    default_type="info_view",
)


def build_domain(toolbox):
    return build_from(CONFIG, toolbox)


def route_plan(query: str):
    return route_from(CONFIG, query)


def preview_plan(query: str) -> dict:
    return preview_from(CONFIG, query)
