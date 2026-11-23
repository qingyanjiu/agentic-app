"""能源态势业务域（7 个子类型）。"""
from agent.intent.slots import extract_energy_status_slots

import json

from ._generic import QueryDomain, build_from, preview_from, route_from

# 项目既有口径（与 graph/energy_status_langgraph.py、doc_gen 提示词一致）
_UNIT_ELECTRICITY = "千瓦时"
_UNIT_WATER = "吨"


def _amount(value, unit: str) -> str:
    """数值 → 「123 千瓦时」。0 照写「0 千瓦时」；字段缺失返回空串。"""
    if value is None or value == "":
        return ""
    return f"{value} {unit}"


def _format_overall_energy(raw: str, query_type: str) -> str:
    """总体能耗：JSON 裸数字 → 带单位、带标点的两行。

    语音播报靠标点断句，也靠单位判断"这是个数值"；「今日用电量：0」
    这种写法两样都没有，TTS 容易读糊或跳过。
    """
    if query_type != "overall_energy":
        return raw
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    if not isinstance(payload, dict):
        return raw

    electricity = payload.get("electricity") or {}
    water = payload.get("water") or {}
    if not isinstance(electricity, dict) or not isinstance(water, dict):
        return raw

    groups = (
        [
            ("今日用电", electricity.get("today"), _UNIT_ELECTRICITY),
            ("今日用水", water.get("today"), _UNIT_WATER),
        ],
        [
            ("年度累计用电", electricity.get("annual"), _UNIT_ELECTRICITY),
            ("年度累计用水", water.get("annual"), _UNIT_WATER),
        ],
    )
    lines = []
    for group in groups:
        items = [
            f"{label} {_amount(value, unit)}"
            for label, value, unit in group
            if _amount(value, unit)
        ]
        if items:
            lines.append("；".join(items) + "。")
    return "\n".join(lines) if lines else raw


CONFIG = QueryDomain(
    name="energy_status",
    server="energy",
    description="能源态势：总能耗、计量设备、用电/用水排行、设备状态、实时用量",
    tool_name="energy_status__query",
    tool_description=(
        "查询能源态势数据。适用于「今天总能耗多少」「哪个楼用电最多」"
        "「实时用水量」「计量设备状态」。工具会自己判断子类型。"
        "单位口径：电量一律「千瓦时」，水量一律「吨」；每个数值都要带单位，"
        "0 也要写成「0 千瓦时」/「0 吨」。今日数值为 0 时可补一句"
        "「今日暂无计量数据」，但不要把 0 改写成超过这个范围的意思。"
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
    # getOverallEnergyConsu 只返回数字（{"electricity":{"annual","today"},...}），
    # 没单位。原样交给模型，它会写成「今日用电量：0」——无单位无标点，
    # 语音播报读糊。这里按项目既有口径补齐（电→千瓦时，水→吨）。
    formatter=_format_overall_energy,
)


def build_domain(toolbox):
    return build_from(CONFIG, toolbox)


def route_plan(query: str):
    return route_from(CONFIG, query)


def preview_plan(query: str) -> dict:
    return preview_from(CONFIG, query)

