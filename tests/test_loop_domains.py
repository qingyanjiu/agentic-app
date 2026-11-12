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


# ------------------------------------------------------------------
# 子类型 → MCP 工具 的映射（线上问题：问什么都打同一个 MCP 工具）
#
# 根因：slots.py 里一半抽取器给 query_type，另一半给 event_type，
# 而 _generic.QueryDomain.resolve 只读 query_type → 会议/食堂/信息发布/安防
# 四个域永远落到 default_type。表现就是"走的 MCP 不对"。
# ------------------------------------------------------------------
def test_meeting_subtype_maps_to_expected_mcp_tool():
    from agent.loop.domains import meeting_status as domain

    cases = {
        "列出各会议室使用情况": ("meeting_room_overview", "meeting:getMeetingRoomOverview"),
        "各会议室使用情况": ("meeting_room_overview", "meeting:getMeetingRoomOverview"),
        "会议室概览": ("meeting_room_overview", "meeting:getMeetingRoomOverview"),
        "会议室现在有人吗": ("meeting_room_status", "meeting:getMeetingRoomStatus"),
        "今天的会议列表": ("room_meet_list_by_day", "meeting:getRoomMeetListByDay"),
        "哪个会议室用得最多": ("high_freq_meeting_rooms", "meeting:getHighFrequencyMeetingRooms"),
    }
    for query, (expected_type, expected_tool) in cases.items():
        query_type, tool, _args, _missing = domain.CONFIG.resolve(query)
        assert query_type == expected_type, query
        assert tool == expected_tool, query


def test_meeting_types_are_not_all_the_same_tool():
    """所有子类型塌成一个工具，是这次 bug 的症状。"""
    from agent.loop.domains import meeting_status as domain

    tools = {
        domain.CONFIG.resolve(q)[1]
        for q in [
            "列出各会议室使用情况",
            "会议室现在有人吗",
            "今天的会议列表",
            "哪个会议室用得最多",
        ]
    }
    assert len(tools) >= 4


def test_event_type_domains_resolve_distinct_tools():
    """用 event_type 的四个域都不能塌到 default_type。"""
    from agent.loop.domains import canteen_status, meeting_status, security_status

    # 食堂：三种问法三种工具（原先全部是 week_menu）
    canteen = {canteen_status.CONFIG.resolve(q)[1] for q in [
        "今天食堂多少人", "本周菜谱", "什么菜最受欢迎",
    ]}
    assert len(canteen) == 3

    # 会议：概览与状态必须分开
    assert (
        meeting_status.CONFIG.resolve("会议室概览")[1]
        != meeting_status.CONFIG.resolve("会议室现在有人吗")[1]
    )

    # 安防：至少不会因为字段名不认而全部落空
    for query in ["今天的告警", "巡检趋势"]:
        _query_type, tool, _args, _missing = security_status.CONFIG.resolve(query)
        assert tool, query
