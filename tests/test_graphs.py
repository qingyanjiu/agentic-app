# -*- coding: utf-8 -*-
"""
LangGraph 端到端测试（8 个域全覆盖，MCP 工具用 FakeTool 模拟、LLM 传 None 原样返回）。

覆盖每张图的全部节点路径：
  init -> check_missing_params -> ask_param / give_up / check_date
      -> future_date / vague_date / call_tool（正常 / 守卫 / 工具未加载）-> finalize

并包含 docs/问题排查记录.md 的回归：
  案例1：「xxx 工具未加载」错误事件
  案例2：消防笼统问法 -> 反问 -> 用户回选项词 -> 正常查询（两轮完整流程）
"""
import asyncio

import pytest

from agent.intent.slots import extract_emergency_fire_slots
from conftest import (
    SPAN_DATE,
    answers_of,
    asks_of,
    build_graph,
    domain_graphs,  # noqa: F401  fixture
    events_of,
    graph_input,
    get_graph_module,
)


def run(coro):
    return asyncio.run(coro)


# ============================================================
# 1. 正常查询路径：8 个域（安防 5 条：免时间的指数/总览 + 带时间的列表/趋势/明细）
# ============================================================
HAPPY_PATH_CASES = [
    ("person_status", {"query_type": "realtime"}),
    ("person_status", {"query_type": "structure"}),
    ("security_status", {"event_type": "security_index"}),
    ("security_status", {"event_type": "ai_overview"}),
    ("security_status", {"event_type": "alarm_list", "date": SPAN_DATE}),
    ("security_status", {"event_type": "ai_trend", "date": SPAN_DATE}),
    ("security_status", {"event_type": "ai_alarm_list", "date": SPAN_DATE}),
    ("canteen_status", {"event_type": "dining_count", "date": SPAN_DATE}),
    ("canteen_status", {"event_type": "week_menu", "date": SPAN_DATE}),
    ("vehicle_status", {"query_type": "parking_space"}),
    ("vehicle_status", {"query_type": "traffic_flow", "date": SPAN_DATE}),
    ("information_status", {"event_type": "info_view", "date": SPAN_DATE}),
    ("information_status", {"event_type": "task_trend", "date": SPAN_DATE}),
    ("energy_status", {"query_type": "overall_energy"}),
    ("energy_status", {"query_type": "electricity_rank", "date": SPAN_DATE}),
    ("meeting_status", {"event_type": "meeting_statistics"}),
    ("meeting_status", {"event_type": "room_meet_list_by_day", "date": SPAN_DATE}),
    ("emergency_fire", {"query_type": "fire_alarm_list"}),
    ("emergency_fire", {"query_type": "fire_alarm_num"}),
    ("emergency_fire", {"query_type": "fire_assets"}),
    ("emergency_fire", {"query_type": "month_repair"}),
    ("emergency_perimeter", {"query_type": "key_metrics"}),
    ("emergency_perimeter", {"query_type": "area_overview"}),
    ("emergency_perimeter", {"query_type": "perimeter_alarm_stats"}),
    ("emergency_perimeter", {"query_type": "alarm_overview"}),
    ("device_status", {"query_type": "equip_class"}),
    ("device_status", {"query_type": "category_health"}),
    ("device_status", {"query_type": "month_maintenance", "date": SPAN_DATE}),
    ("device_status", {"query_type": "month_repair", "date": SPAN_DATE}),
    ("device_status", {"query_type": "statis_region"}),
    ("device_status", {"query_type": "anfang_online"}),
    ("device_status", {"query_type": "gb_online"}),
    ("device_status", {"query_type": "mj_online"}),
    ("compositive_overview", {"query_type": "basic_info"}),
    ("compositive_overview", {"query_type": "device_health"}),
    # 设备查询：列表无必填参数；详情带设备名称/编码（名称靠列表反查编码）
    ("device_query", {"query_type": "device_list", "device_type": "消防设备", "area": "A栋3楼"}),
    ("device_query", {"query_type": "device_detail", "device_keyword": "MH-001"}),
    ("device_query", {"query_type": "device_detail", "device_keyword": "干粉灭火器"}),
]


@pytest.mark.parametrize("module_key,slots", HAPPY_PATH_CASES)
def test_happy_path_answers(domain_graphs, module_key, slots):
    """合法 slots -> 调到 MCP 工具 -> answer + done，无 error/追问"""
    final = run(domain_graphs(module_key).ainvoke(graph_input(slots)))

    assert final.get("error") is None, f"{module_key} 出错: {final.get('error')}"
    assert final.get("done") is True
    assert asks_of(final) == [], f"{module_key} 不应追问: {asks_of(final)}"

    answer_list = answers_of(final)
    assert answer_list, f"{module_key} 没有 answer 事件"
    content = answer_list[-1]
    assert "暂不支持" not in content
    assert "工具未加载" not in content
    assert content.strip(), f"{module_key} answer 为空"


# ============================================================
# 2. 参数缺失 -> 追问（ask_param 路径）
# ============================================================
ASK_CASES = [
    ("person_status", {"query_type": "location"}, "请问您要查询哪位人员？"),
    ("security_status", {"event_type": "alarm_list"}, "请问您想查询哪个时间段的告警？"),
    ("canteen_status", {"event_type": "dining_count"}, "请问您想查询哪天的食堂信息？"),
    ("information_status", {"event_type": "info_view"}, "请问您想查询哪天的信息发布数据？"),
    # 案例2：笼统问法 query_type=count -> 反问而非报错
    ("emergency_fire", {"query_type": "count"},
     "请问您想查询哪类消防数据？设备台账、告警统计、实时告警，还是月度报修？"),
    ("emergency_fire", {},
     "请问您想查询哪类消防数据？设备台账、告警统计、实时告警，还是月度报修？"),
    # 设备详情缺设备名称/编码 -> 反问哪台设备
    ("device_query", {"query_type": "device_detail"},
     "请问您想查看哪台设备的详情？"),
    ("device_query", {"query_type": "device_detail", "device_type": "消防设备"},
     "请问您想查看哪台设备的详情？"),
    # 周界与消防同一约定：笼统问法 query_type=count -> 反问而非报错
    ("emergency_perimeter", {"query_type": "count"},
     "请问您想查询哪类周界数据？关键指标、防区一览、告警统计，还是告警一览？"),
    ("emergency_perimeter", {},
     "请问您想查询哪类周界数据？关键指标、防区一览、告警统计，还是告警一览？"),
]


@pytest.mark.parametrize("module_key,slots,expected_question", ASK_CASES)
def test_missing_params_asks_back(domain_graphs, module_key, slots, expected_question):
    final = run(domain_graphs(module_key).ainvoke(graph_input(slots)))

    ask_events = asks_of(final)
    assert len(ask_events) == 1, f"{module_key} 应恰好追问一次"
    assert ask_events[0]["question"].startswith(expected_question)
    assert ask_events[0]["ask_count"] == 1
    assert not final.get("done"), "追问轮不应结束流程"
    assert answers_of(final) == [], "追问轮不应有答案"


# ============================================================
# 3. 追问超限 -> 放弃（give_up 路径）
# ============================================================
@pytest.mark.parametrize(
    "module_key,slots",
    [
        ("person_status", {"query_type": "location"}),
        ("emergency_fire", {"query_type": "count"}),
        ("device_query", {"query_type": "device_detail"}),
        ("emergency_perimeter", {"query_type": "count"}),
    ],
)
def test_give_up_after_max_asks(domain_graphs, module_key, slots):
    final = run(domain_graphs(module_key).ainvoke(graph_input(slots, ask_count=3)))

    assert final.get("done") is True
    answer_list = answers_of(final)
    assert answer_list and "追问次数过多" in answer_list[0]
    assert events_of(final, "done"), "放弃时应发送 done 事件"


# ============================================================
# 4. 日期分支：未来时间拒绝 / 模糊时间反问（以消防图为代表）
# ============================================================
class TestDateRoutes:
    def test_future_date_rejected(self, domain_graphs):
        slots = {"query_type": "fire_alarm_list", "date": {"time_type": "future"}}
        final = run(domain_graphs("emergency_fire").ainvoke(graph_input(slots)))

        assert final.get("done") is True
        content = answers_of(final)[0]
        assert "未来" in content
        assert events_of(final, "done")

    def test_vague_date_asks_options(self, domain_graphs):
        slots = {
            "query_type": "fire_alarm_list",
            "date": {"time_type": "vague", "options": ["近三天", "近一周", "近一个月"]},
        }
        final = run(domain_graphs("emergency_fire").ainvoke(graph_input(slots)))

        ask_events = asks_of(final)
        assert len(ask_events) == 1
        assert "近三天" in ask_events[0]["question"]


# ============================================================
# 5. call_tool 守卫安全网：非法子类型 -> 「暂不支持」（8 个域都有该分支）
# ============================================================
GUARD_CASES = [
    ("person_status", {"query_type": "no_such_type"}),
    ("security_status", {"event_type": "no_such_type", "date": SPAN_DATE}),
    ("canteen_status", {"event_type": "no_such_type", "date": SPAN_DATE}),
    ("vehicle_status", {"query_type": "no_such_type"}),
    ("information_status", {"event_type": "no_such_type", "date": SPAN_DATE}),
    ("energy_status", {"query_type": "no_such_type"}),
    ("meeting_status", {"event_type": "no_such_type"}),
    # 注意：fire 的 count 在 check_missing_params 就被反问拦截，到不了守卫；
    # 这里用「非法枚举值」验证守卫分支本身存在
    ("emergency_fire", {"query_type": "no_such_type"}),
    # 周界同理：count 在 check_missing_params 被反问拦截，守卫用非法枚举值验证
    ("emergency_perimeter", {"query_type": "no_such_type"}),
    ("device_status", {"query_type": "no_such_type"}),
    ("compositive_overview", {"query_type": "no_such_type"}),
    ("device_query", {"query_type": "no_such_type", "device_keyword": "MH-001"}),
]


@pytest.mark.parametrize("module_key,slots", GUARD_CASES)
def test_unsupported_query_type_guard(domain_graphs, module_key, slots):
    final = run(domain_graphs(module_key).ainvoke(graph_input(slots)))

    answer_list = answers_of(final)
    assert answer_list, f"{module_key} 守卫分支应返回答案"
    assert "暂不支持" in answer_list[-1]
    assert final.get("done") is True


def test_fire_guard_branch_direct_call():
    """案例2：call_tool 守卫分支保留作安全网，直接调用节点验证兜底话术"""
    fire_mod = get_graph_module("emergency_fire")
    node = fire_mod.make_call_tool_node({})
    out = run(node({"slots": {"query_type": "count"}}))

    assert out["done"] is True
    assert out["events"][0]["data"]["content"] == "该查询类型当前 MCP 后端暂不支持，请稍后再试。"


# ============================================================
# 8. 设备态势：月度趋势工具的 date 参数透传（yyyy-MM）
# ============================================================
def test_device_month_tool_passes_date():
    """month_maintenance 带 date slot 时，应向 MCP 工具透传 yyyy-MM"""
    from conftest import FakeTool

    device_mod = get_graph_module("device_status")
    tool = FakeTool("device:getMonthMaintenance", '{"code":200,"xData":[],"series":[]}')
    node = device_mod.make_call_tool_node({"device:getMonthMaintenance": tool})

    out = run(node({
        "slots": {"query_type": "month_maintenance", "date": SPAN_DATE},
        "original_query": "九月维修趋势",
    }))

    assert out.get("error") is None
    assert tool.calls == [{"date": "2026-09"}], f"实际调用参数: {tool.calls}"


def test_device_month_tool_without_date():
    """month_repair 不带 date slot 时，应空参调用"""
    from conftest import FakeTool

    device_mod = get_graph_module("device_status")
    tool = FakeTool("device:getMonthRepair", '{"code":200,"xData":[],"series":[]}')
    node = device_mod.make_call_tool_node({"device:getMonthRepair": tool})

    out = run(node({
        "slots": {"query_type": "month_repair", "date": {}},
        "original_query": "报修趋势",
    }))

    assert out.get("error") is None
    assert tool.calls == [{}], f"实际调用参数: {tool.calls}"


# ============================================================
# 8.1 设备查询：列表筛选参数透传 / 详情先反查设备编码
# ============================================================
def test_device_query_list_passes_filters():
    """列表查询：设备类型 + 区域 应作为入参下发给 device:getDeviceList"""
    from conftest import FakeTool

    mod = get_graph_module("device_query")
    tool = FakeTool("device:getDeviceList", '{"code":200,"rows":[]}')
    node = mod.make_call_tool_node({"device:getDeviceList": tool})

    out = run(node({
        "slots": {"query_type": "device_list", "device_type": "消防设备", "area": "A栋3楼"},
        "original_query": "A栋3楼有哪些消防设备",
    }))

    assert out.get("error") is None
    assert tool.calls == [{"deviceType": "消防设备", "area": "A栋3楼"}], f"实际调用参数: {tool.calls}"


def test_device_query_detail_resolves_code_by_name():
    """详情查询：用户只报设备名称时，先用列表反查编码，再拿编码调详情"""
    from conftest import FakeTool

    mod = get_graph_module("device_query")
    list_tool = FakeTool(
        "device:getDeviceList",
        '{"code":200,"rows":[{"deviceCode":"MH-001","deviceName":"干粉灭火器"},'
        '{"deviceCode":"CAM-102","deviceName":"A栋枪机"}]}',
    )
    detail_tool = FakeTool("device:getDeviceDetail", '{"code":200,"deviceCode":"MH-001"}')
    node = mod.make_call_tool_node({
        "device:getDeviceList": list_tool,
        "device:getDeviceDetail": detail_tool,
    })

    out = run(node({
        "slots": {"query_type": "device_detail", "device_keyword": "干粉灭火器"},
        "original_query": "干粉灭火器的详情",
    }))

    assert out.get("error") is None
    assert detail_tool.calls == [{"deviceCode": "MH-001"}], f"实际调用参数: {detail_tool.calls}"


def test_device_query_detail_uses_code_directly():
    """详情查询：用户直接报编码时，编码原样作为 deviceCode 下发"""
    from conftest import FakeTool

    mod = get_graph_module("device_query")
    list_tool = FakeTool("device:getDeviceList", '{"code":200,"rows":[]}')
    detail_tool = FakeTool("device:getDeviceDetail", '{"code":200,"deviceCode":"CAM-102"}')
    node = mod.make_call_tool_node({
        "device:getDeviceList": list_tool,
        "device:getDeviceDetail": detail_tool,
    })

    out = run(node({
        "slots": {"query_type": "device_detail", "device_keyword": "CAM-102"},
        "original_query": "CAM-102的详情",
    }))

    assert out.get("error") is None
    assert detail_tool.calls == [{"deviceCode": "CAM-102"}], f"实际调用参数: {detail_tool.calls}"


# ============================================================
# 6. 案例1 回归：工具未加载 -> error 事件（不再是静默空列表）
# ============================================================
TOOL_MISSING_CASES = [
    ("person_status", {"query_type": "realtime"}),
    ("canteen_status", {"event_type": "dining_count", "date": SPAN_DATE}),
    ("emergency_fire", {"query_type": "fire_alarm_list"}),
    ("device_query", {"query_type": "device_list"}),
]


@pytest.mark.parametrize("module_key,slots", TOOL_MISSING_CASES)
def test_unloaded_tool_returns_error_event(module_key, slots):
    """tools 字典里没有对应 java 工具时，call_tool 返回「工具未加载」error 事件"""
    graph = build_graph(module_key, tools={})
    final = run(graph.ainvoke(graph_input(slots)))

    error_events = events_of(final, "error")
    assert error_events, f"{module_key} 工具缺失时应发送 error 事件"
    assert "工具未加载" in error_events[0]["content"]
    assert final.get("error")


# ============================================================
# 7. 案例2 完整两轮回归：笼统问法 -> 反问 -> 回复选项词 -> 正常查询
# ============================================================
class TestCase2FullFlow:
    def test_vague_fire_query_ask_then_answer(self, domain_graphs):
        # ---- 第一轮：「查下最新的消防」正则落 count，图应反问 ----
        slots = extract_emergency_fire_slots("查下最新的消防")
        assert slots["query_type"] == "count"

        r1 = run(domain_graphs("emergency_fire").ainvoke(graph_input(slots)))
        ask_events = asks_of(r1)
        assert len(ask_events) == 1
        assert "设备台账" in ask_events[0]["question"]

        # ---- 第二轮：模拟 handler.handle_reply 的补槽（用户回复「实时告警」）----
        merged = dict(r1["slots"])
        for k, v in extract_emergency_fire_slots("实时告警").items():
            if v and (k != "query_type" or v != "count"):
                merged[k] = v
        assert merged["query_type"] == "fire_alarm_list"

        r2 = run(domain_graphs("emergency_fire").ainvoke(graph_input(merged)))
        answer_list = answers_of(r2)
        assert answer_list, "第二轮应给出答案"
        assert "暂不支持" not in answer_list[-1]
        assert "烟感火警" in answer_list[-1], "答案应来自 mock 的告警 rows 数据"
        assert r2.get("done") is True
