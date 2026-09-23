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

from datetime import datetime

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
    # 车辆后端仅支持今日数据：区间查询需带 _confirm_proceed 跳过 confirm_today，
    # 确认分支本身由 test_vehicle_confirm_today_flow 单独覆盖
    ("vehicle_status", {"query_type": "traffic_flow", "date": SPAN_DATE,
                        "_confirm_proceed": True}),
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
    # 设备查询：资产库口径，设备类型码是 syncSource（0门禁 1道闸 2梯控 3监控
    # 4入侵报警 5广播 6水表 7电表），筛选参数全可选；
    # 详情另需设备名称/编号（据此先在列表里搜出内部 id）
    ("device_query", {"query_type": "device_list", "device_type": "3", "area": "A栋3楼"}),
    ("device_query", {"query_type": "device_list", "device_type": "0"}),
    ("device_query", {"query_type": "device_detail", "device_type": "3",
                      "device_keyword": "CY-HIK-JK-001-0001"}),
    ("device_query", {"query_type": "device_detail", "device_type": "3",
                      "device_keyword": "A栋枪机"}),
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


# ------------------------------------------------------------
# 1.1 车辆域「非今日查询确认」分支：
#     后端仅支持今日数据，非今天日期先反问"是否看今日"，
#     用户同意（_confirm_proceed）后才执行查询
# ------------------------------------------------------------
def test_vehicle_confirm_today_flow(domain_graphs):
    """非今日区间 -> 确认轮追问 -> 同意后 -> 正常查询出答案"""
    slots = {"query_type": "traffic_flow", "date": SPAN_DATE}

    # 第一轮：非今天日期且未确认 -> 追问确认，流程不结束
    final = run(domain_graphs("vehicle_status").ainvoke(graph_input(slots)))
    assert final.get("error") is None
    assert not final.get("done"), "确认轮不应结束流程"
    assert final["slots"].get("_pending_confirm") is True

    ask_events = asks_of(final)
    assert len(ask_events) == 1
    assert "今日" in ask_events[0]["question"]

    # 第二轮：模拟 handle_reply 同意后的状态回写
    # （vehicle handler 置 _confirm_proceed、清 _pending_confirm），
    # 带同一份 slots 重进图 -> 直达工具调用
    slots = dict(final["slots"])
    slots["_confirm_proceed"] = True
    slots.pop("_pending_confirm", None)
    final = run(domain_graphs("vehicle_status").ainvoke(graph_input(slots)))

    assert final.get("error") is None
    assert final.get("done") is True
    assert asks_of(final) == [], "确认后不应再追问"
    assert answers_of(final), "确认后没有 answer 事件"


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
    # 设备查询：列表接口筛选参数全可选，所以列表不缺参数、不反问；
    # 只有详情缺设备名称/编号时 -> 反问哪台设备
    ("device_query", {"query_type": "device_detail"},
     "请问您想查看哪台设备的详情？"),
    ("device_query", {"query_type": "device_detail", "device_type": "0"},
     "请问您想查看哪台设备的详情？"),
    # 周界与消防同一约定：笼统问法 query_type=count -> 反问而非报错
    ("emergency_perimeter", {"query_type": "count"},
     "请问您想查询哪类周界数据？关键指标、防区一览、告警统计，还是告警一览？"),
    ("emergency_perimeter", {},
     "请问您想查询哪类周界数据？关键指标、防区一览、告警统计，还是告警一览？"),
    # 设备态势同理：笼统问法 -> 反问哪类设备（选定后按资产库台账口径列设备）
    ("device_status", {"query_type": "count"},
     "请问您想查询哪类设备？门禁、道闸、梯控、监控、入侵报警、广播、水表，还是电表？"),
    ("device_status", {},
     "请问您想查询哪类设备？门禁、道闸、梯控、监控、入侵报警、广播、水表，还是电表？"),
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
        ("device_status", {"query_type": "count"}),
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
# 能源域专用：route_date_type 排在 call_tool 之前（案例 16 双保险日期校验），
# date 为空会先落 unsupported_date 分支（「仅支持今天或今年」），守卫轮不到。
# 生产路径 parse_time_slot 对无时间词的输入兜底默认「今天」，date 永远存在——
# 这里构造同形状的今天区间，让用例真正走到子类型守卫分支。
TODAY_DATE = {
    "time_type": "span",
    "start_time": datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),
    "end_time": datetime.now().isoformat(),
    "raw": "今天",
}

GUARD_CASES = [
    ("person_status", {"query_type": "no_such_type"}),
    ("security_status", {"event_type": "no_such_type", "date": SPAN_DATE}),
    ("canteen_status", {"event_type": "no_such_type", "date": SPAN_DATE}),
    ("vehicle_status", {"query_type": "no_such_type"}),
    ("information_status", {"event_type": "no_such_type", "date": SPAN_DATE}),
    ("energy_status", {"query_type": "no_such_type", "date": TODAY_DATE}),
    ("meeting_status", {"event_type": "no_such_type"}),
    # 注意：fire 的 count 在 check_missing_params 就被反问拦截，到不了守卫；
    # 这里用「非法枚举值」验证守卫分支本身存在
    ("emergency_fire", {"query_type": "no_such_type"}),
    # 周界同理：count 在 check_missing_params 被反问拦截，守卫用非法枚举值验证
    ("emergency_perimeter", {"query_type": "no_such_type"}),
    # 设备态势同理：count 已被"哪类设备"反问拦截（见 ASK_CASES）
    ("device_status", {"query_type": "no_such_type"}),
    ("compositive_overview", {"query_type": "no_such_type"}),
    # 设备查询：详情缺名称/编号会被反问拦截，这里带齐参数，验证非法 query_type 的守卫分支
    ("device_query", {"query_type": "no_such_type", "device_type": "0",
                      "device_keyword": "MJ-003"}),
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
# 8.1 设备查询（资产库口径）：列表筛选透传 / 详情先拿名称编号换内部 id
# ============================================================
# 资产库设备清单：listDeviceOnly 不分页，data 直接是设备数组
# （也没有 deviceTypeName——不翻译，类型名靠 syncSource 对照）
# 注意 status 是启用状态，空间名是 spaceName 全路径
_ASSET_ROWS = (
    '{"code":200,"data":['
    '{"id":"1001","name":"A栋枪机","code":"CY-HIK-JK-001-0001","syncSource":"3",'
    '"status":"1","spaceName":"园区/A栋/3楼"},'
    '{"id":"1002","name":"A栋球机","code":"CY-HIK-JK-001-0002","syncSource":"3",'
    '"status":"1","spaceName":"园区/A栋/3楼"}]'
)


def test_device_query_list_passes_sync_source():
    """列表查询：设备类型以 syncSource 下发给 device_query:listDeviceOnly（区域不下发）"""
    from conftest import FakeTool

    mod = get_graph_module("device_query")
    tool = FakeTool("device_query:listDeviceOnly", '{"code":200,"data":[]}')
    node = mod.make_call_tool_node({"device_query:listDeviceOnly": tool})

    out = run(node({
        "slots": {"query_type": "device_list", "device_type": "3"},
        "original_query": "有哪些监控设备",
    }))

    assert out.get("error") is None
    assert tool.calls == [{"syncSource": "3"}], f"实际调用参数: {tool.calls}"


def test_device_query_list_area_filters_locally():
    """位置名后端只认 spaceId，不下发；listDeviceOnly 一次给全，按 spaceName 本地过滤"""
    from conftest import FakeTool

    mod = get_graph_module("device_query")
    tool = FakeTool("device_query:listDeviceOnly", '{"code":200,"data":[]}')
    node = mod.make_call_tool_node({"device_query:listDeviceOnly": tool})

    out = run(node({
        "slots": {"query_type": "device_list", "device_type": "3", "area": "A栋3楼"},
        "original_query": "A栋3楼有哪些监控设备",
    }))

    assert out.get("error") is None
    assert tool.calls == [{"syncSource": "3"}], f"实际调用参数: {tool.calls}"


def test_device_query_detail_resolves_id_by_name():
    """详情查询：用户报名称时，先用列表搜出设备，再拿内部 id 调详情"""
    from conftest import FakeTool

    mod = get_graph_module("device_query")
    list_tool = FakeTool("device_query:listDeviceOnly", _ASSET_ROWS)
    detail_tool = FakeTool("device_query:getDeviceDetail", '{"code":200,"data":{}}')
    node = mod.make_call_tool_node({
        "device_query:listDeviceOnly": list_tool,
        "device_query:getDeviceDetail": detail_tool,
    })

    out = run(node({
        "slots": {"query_type": "device_detail", "device_type": "3",
                  "device_keyword": "A栋枪机"},
        "original_query": "A栋枪机的详情",
    }))

    assert out.get("error") is None
    # 名称走模糊匹配查一次就够了（两条数据里"完全相等"的只有 A栋枪机）
    assert list_tool.calls == [{"name": "A栋枪机"}], f"实际调用参数: {list_tool.calls}"
    assert detail_tool.calls == [{"id": "1001"}], f"实际调用参数: {detail_tool.calls}"


def test_device_query_detail_falls_back_to_code_search():
    """详情查询：用户报编号时，名称搜不到再按编号精确搜一次"""
    from conftest import FakeTool

    mod = get_graph_module("device_query")
    detail_tool = FakeTool("device_query:getDeviceDetail", '{"code":200,"data":{}}')

    class SequenceTool(FakeTool):
        """第一次（按名称搜）空空如也，第二次（按编号搜）才返回数据"""

        async def ainvoke(self, args=None, **kwargs):
            self.calls.append(args)
            if len(self.calls) == 1:
                return '{"code":200,"data":[]}'
            return _ASSET_ROWS

    list_tool = SequenceTool("device_query:listDeviceOnly", "")
    node = mod.make_call_tool_node({
        "device_query:listDeviceOnly": list_tool,
        "device_query:getDeviceDetail": detail_tool,
    })

    out = run(node({
        "slots": {"query_type": "device_detail", "device_type": "3",
                  "device_keyword": "CY-HIK-JK-001-0002"},
        "original_query": "CY-HIK-JK-001-0002的详情",
    }))

    assert out.get("error") is None
    assert list_tool.calls == [
        {"name": "CY-HIK-JK-001-0002"},
        {"code": "CY-HIK-JK-001-0002"},
    ], f"实际调用参数: {list_tool.calls}"
    assert detail_tool.calls == [{"id": "1002"}], f"实际调用参数: {detail_tool.calls}"


def test_device_query_detail_not_found():
    """详情查询：列表里搜不到就不调详情接口，如实说没找到"""
    from conftest import FakeTool

    mod = get_graph_module("device_query")
    list_tool = FakeTool("device_query:listDeviceOnly", '{"code":200,"data":[]}')
    detail_tool = FakeTool("device_query:getDeviceDetail", '{"code":200,"data":{}}')
    node = mod.make_call_tool_node({
        "device_query:listDeviceOnly": list_tool,
        "device_query:getDeviceDetail": detail_tool,
    })

    out = run(node({
        "slots": {"query_type": "device_detail", "device_type": "0",
                  "device_keyword": "不存在的设备"},
        "original_query": "不存在的设备的详情",
    }))

    assert out.get("error") is None
    assert detail_tool.calls == [], "搜不到设备时不该再调详情接口"
    assert "没有找到" in out["answer"]


def test_device_query_detail_ambiguous_lists_candidates():
    """详情查询：匹配到多台又不完全相等时不猜，把候选列出来让用户指定"""
    from conftest import FakeTool

    mod = get_graph_module("device_query")
    list_tool = FakeTool("device_query:listDeviceOnly", _ASSET_ROWS)
    detail_tool = FakeTool("device_query:getDeviceDetail", '{"code":200,"data":{}}')
    node = mod.make_call_tool_node({
        "device_query:listDeviceOnly": list_tool,
        "device_query:getDeviceDetail": detail_tool,
    })

    out = run(node({
        "slots": {"query_type": "device_detail", "device_type": "3",
                  "device_keyword": "A栋"},
        "original_query": "A栋的详情",
    }))

    assert out.get("error") is None
    assert detail_tool.calls == [], "有歧义时不该猜一台去查详情"
    answer = out["answer"]
    assert "匹配到 2 台设备" in answer
    assert "CY-HIK-JK-001-0001" in answer and "CY-HIK-JK-001-0002" in answer


class TestDeviceQueryFullFlow:
    """两轮完整流程：详情缺设备名称/编号 -> 反问 -> 用户答编号 -> 正常查询"""

    def test_detail_ask_keyword_then_answer(self, domain_graphs):
        from agent.intent.handlers.device_query_handler import DeviceQueryHandler
        from memory.session_state import IntentState

        handler = DeviceQueryHandler()

        # ---- 第一轮：「看下设备详情」没有名称/编号，图应反问哪台设备 ----
        slots = run(handler.extract_slots("看下设备详情"))
        assert slots["query_type"] == "device_detail"
        assert slots["device_keyword"] == ""
        assert handler._get_missing_params(slots) == ["device_keyword"]

        r1 = run(domain_graphs("device_query").ainvoke(graph_input(slots)))
        ask_events = asks_of(r1)
        assert len(ask_events) == 1
        assert "哪台设备" in ask_events[0]["question"]
        assert ask_events[0]["missing_params"] == ["device_keyword"]
        assert not r1.get("done"), "追问轮不应结束流程"
        assert answers_of(r1) == [], "这一次不该给出答案"

        # ---- 第二轮：用户回编号，走 handler.handle_reply 补槽 ----
        state = IntentState(
            module="device_query",
            slots=dict(r1["slots"]),
            missing_params=["device_keyword"],
            ask_count=1,
            unrelated_count=0,
            original_query="看下设备详情",
            done=False,
        )
        reply = run(handler.handle_reply(state, "CY-HIK-JK-001-0001", llm=None))
        assert reply["action"] == "continue"
        assert state.slots["device_keyword"] == "CY-HIK-JK-001-0001"
        assert state.slots["query_type"] == "device_detail", "补槽不该把详情口径冲成列表"
        assert state.missing_params == []

        r2 = run(domain_graphs("device_query").ainvoke(graph_input(state.slots)))
        answer_list = answers_of(r2)
        assert answer_list, "第二轮应给出答案"
        assert "暂不支持" not in answer_list[-1]
        assert "工具未加载" not in answer_list[-1]
        assert "A栋枪机" in answer_list[-1], "答案应来自 mock 的设备详情数据"
        assert r2.get("done") is True

    def test_list_without_type_answers_directly(self, domain_graphs):
        """列表口径不再强制问设备类型：不给类型也直接出答案（资产库接口全可选）"""
        from agent.intent.handlers.device_query_handler import DeviceQueryHandler

        handler = DeviceQueryHandler()
        slots = run(handler.extract_slots("园区有哪些设备"))
        assert handler._get_missing_params(slots) == []

        final = run(domain_graphs("device_query").ainvoke(graph_input(slots)))
        assert asks_of(final) == [], "列表口径不该反问"
        answer_list = answers_of(final)
        assert answer_list and "A栋枪机" in answer_list[-1]


class TestDeviceStatusFullFlow:
    """
    两轮完整流程：设备态势笼统问法 -> 反问"哪类设备" -> 用户答类型 -> 按台账口径列设备

    对照 device_query 的同一场景：设备域只有"设备列表/设备详情"两个台账工具，
    因此设备态势兜底选定的类型最终落到 device_query:listDeviceOnly（资产库口径的 syncSource）。
    """

    def test_vague_query_ask_type_then_list(self, domain_graphs):

        from agent.intent.handlers.device_status_handler import DeviceStatusHandler
        from agent.intent.slots import extract_device_status_slots
        from memory.session_state import IntentState

        handler = DeviceStatusHandler()

        # ---- 第一轮：「查下设备情况」正则落 count -> 图反问哪类设备 ----
        slots = extract_device_status_slots("查下设备情况")
        assert slots["query_type"] == "count"
        assert handler._get_missing_params(slots) == ["device_type"]

        r1 = run(domain_graphs("device_status").ainvoke(graph_input(slots)))
        ask_events = asks_of(r1)
        assert len(ask_events) == 1
        assert ask_events[0]["question"].startswith("请问您想查询哪类设备？")
        assert ask_events[0]["missing_params"] == ["device_type"]
        assert not r1.get("done"), "追问轮不应结束流程"
        assert answers_of(r1) == [], "这一次不该给出答案"

        # ---- 第二轮：用户回「监控」，走 handler.handle_reply 补槽 ----
        state = IntentState(
            module="device_status",
            slots=dict(r1["slots"]),
            missing_params=["device_type"],
            ask_count=r1["ask_count"],
            unrelated_count=0,
            original_query="查下设备情况",
            done=False,
        )
        reply = run(handler.handle_reply(state, "监控", llm=None))
        assert reply["action"] == "continue"
        assert state.slots["device_type"] == "3", "监控在资产库口径里是 syncSource=3"
        assert state.slots["query_type"] == "device_list"
        assert state.missing_params == []

        r2 = run(domain_graphs("device_status").ainvoke(graph_input(state.slots)))
        answer_list = answers_of(r2)
        assert answer_list, "第二轮应给出答案"
        assert "暂不支持" not in answer_list[-1]
        assert "工具未加载" not in answer_list[-1]
        assert "A栋枪机" in answer_list[-1], "答案应来自 mock 的设备列表数据"
        assert r2.get("done") is True


def test_device_status_list_passes_sync_source():
    """台账分支：设备类型码（syncSource）应作为入参下发给 device_query:listDeviceOnly"""
    from conftest import FakeTool

    device_mod = get_graph_module("device_status")
    tool = FakeTool("device_query:listDeviceOnly", '{"code":200,"data":[]}')
    node = device_mod.make_call_tool_node({"device_query:listDeviceOnly": tool})

    out = run(node({
        "slots": {"query_type": "device_list", "device_type": "0"},
        "original_query": "看下门禁设备",
    }))

    assert out.get("error") is None
    assert tool.calls == [{"syncSource": "0"}], f"实际调用参数: {tool.calls}"


# ============================================================
# 6. 案例1 回归：工具未加载 -> error 事件（不再是静默空列表）
# ============================================================
TOOL_MISSING_CASES = [
    ("person_status", {"query_type": "realtime"}),
    ("canteen_status", {"event_type": "dining_count", "date": SPAN_DATE}),
    ("emergency_fire", {"query_type": "fire_alarm_list"}),
    ("device_query", {"query_type": "device_list", "device_type": "3"}),
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
