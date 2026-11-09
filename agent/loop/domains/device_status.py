"""设备态势业务域（8 个统计子类型）。

注意与 device_query 的区别：
- device_status 是**统计口径**（某类设备多少台、在线率、维保维修数）；
- device_query 是**台账口径**（具体某台设备是谁、在哪、什么状态）。
两者都走 /mcp/device 与 /mcp/devicequery 两个不同端点，不要混用。
"""
from agent.intent.slots import extract_device_status_slots

from ._generic import QueryDomain, build_from, preview_from, route_from

CONFIG = QueryDomain(
    name="device_status",
    server="device",
    description="设备态势（统计口径）：设备分类、健康度、维保维修、区域分布、各网在线率",
    tool_name="device_status__query",
    tool_description=(
        "查询设备态势统计。适用于「园区有多少台设备」「设备健康度怎么样」"
        "「这个月维修了几台」「各区域设备分布」「安防设备在线率」。"
        "注意：这是统计口径；如果要查某一台具体设备，应该用 device_query 系列工具。"
    ),
    extract_slots=extract_device_status_slots,
    tool_map={
        "equip_class": "device:getEquipClass",
        "category_health": "device:getCategoryHealth",
        "month_maintenance": "device:getMonthMaintenance",
        "month_repair": "device:getMonthRepair",
        "statis_region": "device:getStatisRegion",
        "anfang_online": "device:getAnfangDeviceOnlinePercentage",
        "gb_online": "device:getGbOnlinePercentage",
        "mj_online": "device:getMjOnlinePercentage",
    },
    default_type="equip_class",
)


def build_domain(toolbox):
    return build_from(CONFIG, toolbox)


def route_plan(query: str):
    return route_from(CONFIG, query)


def preview_plan(query: str) -> dict:
    return preview_from(CONFIG, query)
