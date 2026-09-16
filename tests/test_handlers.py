# -*- coding: utf-8 -*-
"""
意图处理器（handler）测试：8 个域的
  - _get_missing_params：缺参判定
  - generate_question：追问话术（与对应 graph 的 ask_param 保持一致）
  - handle_reply：追问轮的补槽 / 不相关追问 / 放弃（以消防域为代表）

docs/问题排查记录.md 案例2 修复要求：
  handler 与 graph 的缺参规则、反问话术必须保持会话状态一致。
"""
import asyncio

import pytest

from conftest import get_graph_module
from memory.session_state import IntentState

from agent.intent.handlers.person_status_handler import PersonStatusHandler
from agent.intent.handlers.security_status_handler import SecurityStatusHandler
from agent.intent.handlers.canteen_status_handler import CanteenStatusHandler
from agent.intent.handlers.vehicle_status_handler import VehicleStatusHandler
from agent.intent.handlers.information_status_handler import InformationStatusHandler
from agent.intent.handlers.energy_status_handler import EnergyStatusHandler
from agent.intent.handlers.meeting_status_handler import MeetingStatusHandler
from agent.intent.handlers.emergency_fire_handler import EmergencyFireHandler
from agent.intent.handlers.device_status_handler import DeviceStatusHandler
from agent.intent.handlers.compositive_overview_handler import CompositiveOverviewHandler
from agent.intent.handlers.device_query_handler import DeviceQueryHandler


def handler_of(module_key: str):
    return {
        "person_status": PersonStatusHandler,
        "security_status": SecurityStatusHandler,
        "canteen_status": CanteenStatusHandler,
        "vehicle_status": VehicleStatusHandler,
        "information_status": InformationStatusHandler,
        "energy_status": EnergyStatusHandler,
        "meeting_status": MeetingStatusHandler,
        "emergency_fire": EmergencyFireHandler,
        "device_status": DeviceStatusHandler,
        "compositive_overview": CompositiveOverviewHandler,
        "device_query": DeviceQueryHandler,
    }[module_key]()


def run(coro):
    return asyncio.run(coro)


def make_state(module: str, slots: dict, missing: list = None) -> IntentState:
    return IntentState(
        module=module,
        slots=slots,
        missing_params=missing if missing is not None else [],
        ask_count=0,
    )


# ============================================================
# 1. 缺参判定 _get_missing_params（8 个域全覆盖）
# ============================================================
class TestMissingParams:
    def test_person_location_needs_name(self):
        h = handler_of("person_status")
        assert h._get_missing_params({"query_type": "location"}) == ["person_name"]
        assert h._get_missing_params({"query_type": "location", "person_name": "张三"}) == []
        assert h._get_missing_params({"query_type": "trace"}) == ["person_name"]

    def test_person_other_types_need_nothing(self):
        h = handler_of("person_status")
        assert h._get_missing_params({"query_type": "realtime"}) == []
        assert h._get_missing_params({"query_type": "enter"}) == []

    def test_security_needs_date(self):
        h = handler_of("security_status")
        assert h._get_missing_params({"event_type": "alarm_list"}) == ["date"]
        assert h._get_missing_params({"event_type": "alarm_list", "date": {"time_type": "span"}}) == []
        # ai_overview（AI 告警总览）为无参实时查询，与 security_index 一致无需时间
        assert h._get_missing_params({"event_type": "ai_overview"}) == []
        # ai_trend / ai_alarm_list 按时间区间查询，仍需要时间范围
        assert h._get_missing_params({"event_type": "ai_trend"}) == ["date"]
        assert h._get_missing_params({"event_type": "ai_alarm_list"}) == ["date"]

    def test_security_alarm_detail_needs_id(self):
        h = handler_of("security_status")
        assert h._get_missing_params({"event_type": "alarm_detail"}) == ["alarm_id"]
        assert h._get_missing_params({"event_type": "alarm_detail", "alarm_id": "3"}) == []

    @pytest.mark.parametrize("module_key", ["canteen_status", "information_status"])
    def test_canteen_information_need_date(self, module_key):
        h = handler_of(module_key)
        assert h._get_missing_params({"event_type": "x"}) == ["date"]
        assert h._get_missing_params({"event_type": "x", "date": {"time_type": "span"}}) == []

    @pytest.mark.parametrize("module_key", ["vehicle_status", "energy_status", "meeting_status", "device_status", "compositive_overview"])
    def test_vehicle_energy_meeting_need_nothing(self, module_key):
        h = handler_of(module_key)
        assert h._get_missing_params({}) == []

    def test_device_query_needs_keyword_only_for_detail(self):
        """设备列表无必填参数；设备详情必须有设备名称/编码"""
        h = handler_of("device_query")
        assert h._get_missing_params({"query_type": "device_list"}) == []
        assert h._get_missing_params({"query_type": "device_list", "device_type": "消防设备"}) == []
        assert h._get_missing_params({"query_type": "device_detail"}) == ["device_keyword"]
        assert h._get_missing_params({"query_type": "device_detail", "device_keyword": "MH-001"}) == []

    @pytest.mark.parametrize(
        "slots,expected",
        [
            ({"query_type": "count"}, ["query_type"]),
            ({}, ["query_type"]),
            ({"query_type": "fire_alarm_list"}, []),
            ({"query_type": "fire_assets"}, []),
        ],
    )
    def test_fire_count_treated_as_missing(self, slots, expected):
        """案例2：消防 query_type 落 count 兜底时视为缺失，走追问而非报错"""
        h = handler_of("emergency_fire")
        assert h._get_missing_params(slots) == expected


# ============================================================
# 2. 追问话术 generate_question：handler 与 graph 的 ask_param 保持一致
# （security/canteen/information 三个 handler 没有该方法，追问由图负责）
# ============================================================
class TestQuestionConsistency:
    @pytest.mark.parametrize(
        "module_key,missing,expected_question",
        [
            ("person_status", ["person_name"], "请问您要查询哪位人员？"),
            ("energy_status", ["date"], "请问您想查询哪一天的能源数据？"),
            ("meeting_status", ["date"], "请问您想查询哪一天的会议信息？"),
            ("vehicle_status", ["area"], "请问您想查询哪个停车场或区域？"),
            ("emergency_fire", ["query_type"],
             "请问您想查询哪类消防数据？设备台账、告警统计、实时告警，还是月度报修？"),
        ],
    )
    def test_handler_question_matches_graph_ask_param(self, module_key, missing, expected_question):
        state = make_state(module_key, {}, missing)
        handler = handler_of(module_key)
        question = handler.generate_question(state)

        assert question == expected_question
        # 状态同步：last_question 记录、ask_count 自增（案例2 修复的一致性要求）
        assert state.last_question == expected_question
        assert state.ask_count == 1

        # 与对应 graph 的 ask_param 输出一致
        graph_mod = get_graph_module(module_key)
        graph_out = graph_mod.ask_param({"missing_params": missing, "ask_count": 0})
        assert graph_out["last_question"] == question

    @pytest.mark.parametrize("module_key", ["security_status", "canteen_status", "information_status"])
    def test_handlers_without_generate_question(self, module_key):
        """这三个域 handler 没有 generate_question（第一轮追问完全由图负责），做存在性记录"""
        assert not hasattr(handler_of(module_key), "generate_question")


# ============================================================
# 3. graph 与 handler 缺参规则的已知不一致（案例排查记录风格：先记录，待修）
# ============================================================
@pytest.mark.xfail(strict=False, reason="安防域 graph 与 handler 缺参规则不一致："
                                        "graph 对 security_index 不要求 date、对 alarm_detail 仍要求 date；"
                                        "handler 对 security_index 要求 date、对 alarm_detail 只要求 alarm_id")
class TestKnownDivergenceSecurity:
    @pytest.mark.parametrize(
        "slots",
        [
            {"event_type": "security_index"},
            {"event_type": "alarm_detail", "alarm_id": "3"},
        ],
    )
    def test_rules_should_be_consistent(self, slots):
        graph_mod = get_graph_module("security_status")
        graph_missing = graph_mod.check_missing_params({"slots": slots})["missing_params"]
        handler_missing = SecurityStatusHandler()._get_missing_params(slots)
        assert graph_missing == handler_missing


# ============================================================
# 4. 消防域追问轮 handle_reply（案例2 修复的核心行为）
# ============================================================
class TestFireHandleReply:
    def test_reply_option_word_fills_query_type(self):
        """用户照反问选项回复「实时告警」→ 正则命中，补槽后 continue"""
        state = make_state("emergency_fire", {"query_type": "count"}, ["query_type"])
        result = run(EmergencyFireHandler().handle_reply(state, "实时告警", llm=None))

        assert result["action"] == "continue"
        assert state.slots["query_type"] == "fire_alarm_list"
        assert state.missing_params == []

    def test_reply_unrelated_triggers_re_ask(self):
        state = make_state("emergency_fire", {"query_type": "count"}, ["query_type"])
        result = run(EmergencyFireHandler().handle_reply(state, "今天天气不错", llm=None))

        assert result["action"] == "re_ask"
        assert state.unrelated_count == 1

    def test_three_unrelated_replies_give_up(self):
        state = make_state("emergency_fire", {"query_type": "count"}, ["query_type"])
        handler = EmergencyFireHandler()
        for _ in range(3):
            result = run(handler.handle_reply(state, "今天天气不错", llm=None))

        assert result["action"] == "give_up"

    def test_decline_reply_counts_as_unrelated(self):
        """拒绝词「算了」在首轮追问只视为不相关（re_ask），连续多次才 give_up"""
        state = make_state("emergency_fire", {"query_type": "count"}, ["query_type"])
        result = run(EmergencyFireHandler().handle_reply(state, "算了不查了", llm=None))
        assert result["action"] == "re_ask"
        assert state.unrelated_count == 1

    def test_reply_count_does_not_overwrite_known_type(self):
        """handle_reply 合并规则：query_type 抽到 count 兜底时不覆盖已识别的子类型"""
        state = make_state("emergency_fire", {"query_type": "fire_assets"}, [])
        result = run(EmergencyFireHandler().handle_reply(state, "看下灭火器", llm=None))

        assert result["action"] == "continue"
        assert state.slots["query_type"] == "fire_assets"
