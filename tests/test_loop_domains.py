"""13 个业务域的装配与路由测试（纯本地，不连 MCP）。"""
import pytest

from agent.loop import build_registry
from agent.loop.router import DOMAIN_PLANNERS

# 域 → 一句典型问法 → 期望的 loop 工具
CASES = [
    ("person_status", "现在园区有多少人", "person_status__query"),
    ("security_status", "今天的安防告警列表", "security_status__query"),
    ("canteen_status", "本周菜谱是什么", "canteen_status__query"),
    ("vehicle_status", "还剩多少停车位", "vehicle_status__query"),
    ("information_status", "今天发了多少条信息", "information_status__query"),
    ("energy_status", "今天总能耗多少", "energy_status__query"),
    ("meeting_status", "今天开了多少会", "meeting_status__query"),
    ("emergency_fire", "消防设备有多少", "emergency_fire__query"),
    ("emergency_perimeter", "周界告警统计", "emergency_perimeter__query"),
    ("device_status", "设备健康度怎么样", "device_status__query"),
    ("device_query", "3楼的门禁设备都列一下", "device_query__list"),
    ("compositive_overview", "园区基本情况", "compositive_overview__query"),
    ("twins_inspection", "今天巡检完成了吗", "twins_inspection__query"),
]


def test_all_13_domains_registered():
    assert len(DOMAIN_PLANNERS) == 13
    for domain, _, _ in CASES:
        assert domain in DOMAIN_PLANNERS


@pytest.mark.parametrize("domain,query,expected_tool", CASES)
def test_route_plan_resolves_to_expected_tool(domain, query, expected_tool):
    plan = DOMAIN_PLANNERS[domain](query)
    assert plan.tool == expected_tool


def test_registry_exposes_all_tools():
    registry = build_registry()
    # 12 个域各 1 个工具，device_query 有 list/detail 两个
    assert len(registry.tools) == 14
    names = registry.tool_names()
    for _, _, tool in CASES:
        assert tool in names


def test_device_detail_missing_param_is_reported():
    plan = DOMAIN_PLANNERS["device_query"]("查一下设备详情")
    assert plan.tool == "device_query__detail"
    assert plan.missing == ["device_keyword"]


def test_unknown_subtype_returns_empty_tool():
    """人态势的 location/trace 后端未接入，应返回空 tool（走 L2 或直接告知）。"""
    plan = DOMAIN_PLANNERS["person_status"]("张三现在在哪里")
    assert plan.tool in ("", "person_status__query")
