"""能源态势业务域（7 个子类型）。"""
from agent.intent.slots import extract_energy_status_slots

from ._generic import QueryDomain, build_from, preview_from, route_from

CONFIG = QueryDomain(
    name="energy_status",
    server="energy",
    description="能源态势：总能耗、计量设备、用电/用水排行、设备状态、实时用量",
    tool_name="energy_status__query",
    tool_description=(
        "查询能源态势数据。适用于「今天总能耗多少」「哪个楼用电最多」"
        "「实时用水量」「计量设备状态」。工具会自己判断子类型。"
    ),
    extract_slots=extract_energy_status_slots,
    tool_map={
        "overall_energy": "energy:getOverallEnergyConsu",
        "metering_equipment": "energy:getMeteringEquipment",
        "electricity_rank": "energy:getElectricityUsageRanking",
        "water_rank": "energy:getWaterUsageRanking",
        "device_status": "energy:getDeviceStatus",
        "realtime_electricity": "energy:getRealtimeElectricityUsage",
        "realtime_water": "energy:getRealtimeWaterUsage",
    },
    default_type="overall_energy",
)


def build_domain(toolbox):
    return build_from(CONFIG, toolbox)


def route_plan(query: str):
    return route_from(CONFIG, query)


def preview_plan(query: str) -> dict:
    return preview_from(CONFIG, query)
