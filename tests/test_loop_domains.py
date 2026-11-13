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


# ------------------------------------------------------------------
# 子类型 → MCP 工具 全量矩阵
#
# 用真实说法（不是规则原文）当输入，锁住"这句话该打哪个 MCP 工具"。
# 加新说法就往这张表里加一行——MISS 就说明规则漏了词或顺序被抢了。
# ------------------------------------------------------------------
def _configs():
    from agent.loop.domains import (
        canteen_status,
        compositive_overview,
        device_status,
        emergency_fire,
        emergency_perimeter,
        energy_status,
        information_status,
        meeting_status,
        security_status,
        twins_inspection,
        vehicle_status,
    )

    return {m.CONFIG.name: m.CONFIG for m in [
        meeting_status, canteen_status, security_status, energy_status,
        information_status, vehicle_status, twins_inspection,
        emergency_fire, emergency_perimeter, compositive_overview, device_status,
    ]}


SUBTYPE_MATRIX = [
    # 会议
    ("meeting_status", "列出各会议室使用情况", "meeting_room_overview"),
    ("meeting_status", "会议室概览", "meeting_room_overview"),
    ("meeting_status", "会议室现在有人吗", "meeting_room_status"),
    ("meeting_status", "哪些会议室空闲", "meeting_room_status"),
    ("meeting_status", "今天开了多少会", "number_of_meetings"),
    ("meeting_status", "今天有几场会", "number_of_meetings"),
    ("meeting_status", "今天的会议列表", "room_meet_list_by_day"),
    ("meeting_status", "今天有什么会", "room_meet_list_by_day"),
    ("meeting_status", "哪个会议室用得最多", "high_freq_meeting_rooms"),
    ("meeting_status", "会议室使用排行", "high_freq_meeting_rooms"),
    ("meeting_status", "平均会议时长", "meeting_statistics"),
    # 食堂
    ("canteen_status", "今天食堂多少人", "dining_count"),
    ("canteen_status", "本周菜谱", "week_menu"),
    ("canteen_status", "什么菜最受欢迎", "dish_rank"),
    # 安防
    ("security_status", "今天的告警", "alarm_list"),
    ("security_status", "安防概览", "ai_overview"),
    ("security_status", "告警总览", "ai_overview"),
    ("security_status", "安全指数", "security_index"),
    ("security_status", "园区安全吗", "security_index"),
    ("security_status", "告警趋势", "ai_trend"),
    ("security_status", "巡查任务", "patrol"),
    # 能源
    ("energy_status", "今天用水多少", "realtime_water"),
    ("energy_status", "本月电费", "overall_energy"),
    ("energy_status", "总能耗", "overall_energy"),
    ("energy_status", "用电排名", "electricity_rank"),
    ("energy_status", "水表在线情况", "metering_equipment"),
    # 其余域的关键子类型
    ("information_status", "今天发了几条信息", "info_view"),
    ("vehicle_status", "还剩多少停车位", "parking_space"),
    ("vehicle_status", "今天的车辆通行记录", "traffic_flow"),
    ("twins_inspection", "今天巡检任务", "today_tasks"),
    ("twins_inspection", "巡检统计", "inspection_statistics"),
    ("emergency_fire", "消防设备有多少", "fire_assets"),
    ("emergency_perimeter", "周界告警统计", "perimeter_alarm_stats"),
    ("compositive_overview", "园区基本情况", "basic_info"),
    ("device_status", "设备健康度", "category_health"),
]


@pytest.mark.parametrize("domain, query, expected_type", SUBTYPE_MATRIX)
def test_subtype_maps_to_expected_mcp_tool(domain, query, expected_type):
    config = _configs()[domain]
    query_type, tool, _args, _missing = config.resolve(query)

    assert query_type == expected_type, (
        f"{domain} / {query!r} 判成 {query_type}（期望 {expected_type}），"
        f"会打到 {tool}"
    )
    # 抽出来的子类型必须真能映射到工具，否则等于"后端未接入"
    assert tool == config.tool_map[expected_type]


@pytest.mark.parametrize("domain", sorted({item[0] for item in SUBTYPE_MATRIX}))
def test_neutral_query_falls_back_to_a_real_tool(domain):
    """兜底子类型必须在 tool_map 里。

    slots.py 里有 7 个抽取器的兜底原来写成 `count`，而 tool_map 从来没有 count，
    结果"规则没命中"就变成工具解析成 None → 用户看到"后端暂未接入"。
    """
    config = _configs()[domain]

    for probe in ["你好", "随便问问"]:
        query_type, tool, _args, _missing = config.resolve(probe)
        assert tool is not None, (
            f"{domain} / {probe!r} 落到 {query_type}（不在 tool_map），"
            f"default={config.default_type}"
        )


# ----------------------------------------------------------------------
# 人员态势：后端返回的是一句中文，不是 JSON（模型曾照描述当成 JSON 解析失败）
# ----------------------------------------------------------------------
def test_person_status_picks_the_asked_number():
    """"现在园区有多少人"只回答实时在园人数，别把三项全倒出来。"""
    from agent.loop.domains.person_status import _pick

    raw = "实时在园人数：375人；今日进入人数：472人；今日离开人数：97人。"
    assert _pick(raw, "realtime") == "实时在园人数：375人"
    assert _pick(raw, "count") == "实时在园人数：375人"
    assert _pick(raw, "enter") == "今日进入人数：472人"
    assert _pick(raw, "leave") == "今日离开人数：97人"


def test_person_status_keeps_chart_data_untouched():
    """流动/结构是图表数据，不能被句子抽取改写。"""
    from agent.loop.domains.person_status import _pick

    flow = '[{"进入人数": [1, 2, 3]}]'
    assert _pick(flow, "flow") == flow
    assert _pick('{"公司A": 12}', "structure") == '{"公司A": 12}'


def test_person_status_falls_back_to_raw_when_field_missing():
    """抽不到就原样返回，宁可多给也不能丢数据。"""
    from agent.loop.domains.person_status import _pick

    raw = "实时在园人数：0人"
    assert _pick(raw, "leave") == raw
    assert _pick("", "realtime") == ""


# ----------------------------------------------------------------------
# 接口正常但没数据：要说"没有"，不能说"服务异常"
# ----------------------------------------------------------------------
def test_empty_payload_detection():
    from agent.loop.domains._generic import _is_empty_payload

    for raw in [None, "", "  ", "null", "NULL", "[]", "{}", "[ ]"]:
        assert _is_empty_payload(raw) is True, raw
    # 有内容就不能判空
    for raw in ['[{"a": 1}]', "实时在园人数：0人", "0", '{"todayCount": 0}']:
        assert _is_empty_payload(raw) is False, raw


def test_security_empty_result_says_no_alarm_not_failure():
    """实测 getSecurityAlarmList 今天返回字面 "null"，isError=False。"""
    import asyncio
    import json

    from agent.loop.domains import security_status

    class Box:
        async def call(self, server, tool, args=None):
            return "null"

    tool = {
        t.name: t for t in security_status.build_domain(Box()).tools
    }["security_status__query"]

    out = asyncio.run(tool.execute({"query": "今天园区安防事件有哪些"}))

    assert out.is_error is False
    assert "没有安防告警事件" in out.text
    # 明确禁止的三种瞎猜都要点到，否则模型换个说法继续编
    assert "不是故障" in out.text
    assert "服务异常" in out.text          # 作为"不要说"的宾语出现
    assert "稍后再试" in out.text


def test_empty_result_does_not_trigger_for_normal_payload():
    """有数据时不能走空结果分支。"""
    import asyncio
    import json

    from agent.loop.domains import security_status

    class Box:
        async def call(self, server, tool, args=None):
            return json.dumps([{"alarmTypeName": "周界入侵", "handleStat": "0"}],
                              ensure_ascii=False)

    tool = {
        t.name: t for t in security_status.build_domain(Box()).tools
    }["security_status__query"]

    out = asyncio.run(tool.execute({"query": "今天园区安防事件有哪些"}))

    assert "周界入侵" in out.text
    assert "没有安防告警事件" not in out.text
