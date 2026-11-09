"""综合态势总览业务域（2 个独占子类型）。"""
from agent.intent.slots import extract_compositive_overview_slots

from ._generic import QueryDomain, build_from, preview_from, route_from

CONFIG = QueryDomain(
    name="compositive_overview",
    server="overview",
    description="综合态势总览：园区基础信息、设备健康度",
    tool_name="compositive_overview__query",
    tool_description=(
        "查询园区综合态势总览。适用于「园区基本情况」「园区有多少楼栋多少人」"
        "「设备健康度怎么样」。只覆盖基础信息和设备健康度两个口径；"
        "其他统计请用对应业务域的工具。"
    ),
    extract_slots=extract_compositive_overview_slots,
    tool_map={
        "basic_info": "overview:getBasicInfo",
        "device_health": "overview:getDeviceHealth",
    },
    default_type="basic_info",
)


def build_domain(toolbox):
    return build_from(CONFIG, toolbox)


def route_plan(query: str):
    return route_from(CONFIG, query)


def preview_plan(query: str) -> dict:
    return preview_from(CONFIG, query)
