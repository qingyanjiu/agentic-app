"""车辆态势业务域（5 个子类型）。"""
from agent.intent.slots import extract_vehicle_status_slots

from ._generic import QueryDomain, build_from, preview_from, route_from

CONFIG = QueryDomain(
    name="vehicle_status",
    server="vehicle",
    description="车辆态势：停车位、车流量、车位结构、公务车、停车时长排行",
    tool_name="vehicle_status__query",
    tool_description=(
        "查询车辆态势数据。适用于「还剩多少停车位」「今天车流量多少」"
        "「公务车有几辆」「停车时长排行」。工具会自己判断子类型。"
    ),
    extract_slots=extract_vehicle_status_slots,
    tool_map={
        "parking_space": "vehicle_status:getParkingSpace",
        "traffic_flow": "vehicle_status:getTrafficVolume",
        "parking_structure": "vehicle_status:getParkingStructure",
        "official_vehicle": "vehicle_status:getCarStatistic",
        "parking_duration_rank": "vehicle_status:getParkingRank",
    },
    default_type="parking_space",
)


def build_domain(toolbox):
    return build_from(CONFIG, toolbox)


def route_plan(query: str):
    return route_from(CONFIG, query)


def preview_plan(query: str) -> dict:
    return preview_from(CONFIG, query)
