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
from agent.intent.handlers.emergency_perimeter_handler import EmergencyPerimeterHandler
from agent.intent.handlers.device_status_handler import DeviceStatusHandler
from agent.intent.handlers.compositive_overview_handler import CompositiveOverviewHandler
from agent.intent.handlers.device_query_handler import DeviceQueryHandler
from agent.intent.handlers.twins_inspection_handler import TwinsInspectionHandler


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
        "emergency_perimeter": EmergencyPerimeterHandler,
        "device_status": DeviceStatusHandler,
        "compositive_overview": CompositiveOverviewHandler,
        "device_query": DeviceQueryHandler,
        "twins_inspection": TwinsInspectionHandler,
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

    @pytest.mark.parametrize("module_key", ["vehicle_status", "energy_status", "meeting_status", "compositive_overview"])
    def test_vehicle_energy_meeting_need_nothing(self, module_key):
        h = handler_of(module_key)
        assert h._get_missing_params({}) == []

    @pytest.mark.parametrize(
        "slots,expected",
        [
            # 笼统问法：分不清要哪类设备数据 -> 反问哪类设备
            ({"query_type": "count"}, ["device_type"]),
            ({}, ["device_type"]),
            ({"query_type": None}, ["device_type"]),
            # 设备类型选定后按台账口径查，不再缺参数
            ({"query_type": "count", "device_type": "jk"}, []),
            ({"query_type": "device_list", "device_type": "mj"}, []),
            # 统计口径的子类型不接反问（与消防/周界同一约定：兜底值才视为缺失）
            ({"query_type": "equip_class"}, []),
            ({"query_type": "mj_online"}, []),
        ],
    )
    def test_device_status_count_treated_as_missing(self, slots, expected):
        """设备态势：query_type 落 count 兜底时视为缺"哪类设备"，走反问而非报错"""
        h = handler_of("device_status")
        assert h._get_missing_params(slots) == expected

    def test_device_query_needs_type_and_keyword(self):
        """设备类型（deviceType）是后端必填，列表/详情都要；详情另需设备名称/编码"""
        h = handler_of("device_query")
        assert h._get_missing_params({"query_type": "device_list"}) == ["device_type"]
        assert h._get_missing_params({"query_type": "device_list", "device_type": "jk"}) == []
        assert h._get_missing_params({"query_type": "device_detail"}) == ["device_type", "device_keyword"]
        assert h._get_missing_params({"query_type": "device_detail", "device_type": "mj"}) == ["device_keyword"]
        assert h._get_missing_params(
            {"query_type": "device_detail", "device_type": "mj", "device_keyword": "MH-001"}
        ) == []

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

    @pytest.mark.parametrize(
        "slots,expected",
        [
            ({"query_type": "count"}, ["query_type"]),
            ({}, ["query_type"]),
            ({"query_type": "key_metrics"}, []),
            ({"query_type": "alarm_overview"}, []),
        ],
    )
    def test_perimeter_count_treated_as_missing(self, slots, expected):
        """周界与消防同一约定：query_type 落 count 兜底时视为缺失，走追问"""
        h = handler_of("emergency_perimeter")
        assert h._get_missing_params(slots) == expected

    @pytest.mark.parametrize(
        "slots,expected",
        [
            ({"query_type": "count"}, ["query_type"]),
            ({}, ["query_type"]),
            ({"query_type": "today_inspection"}, []),
            ({"query_type": "inspection_execution_status"}, []),
        ],
    )
    def test_inspection_count_treated_as_missing(self, slots, expected):
        """孪生巡检与消防/周界同一约定：query_type 落 count 兜底时视为缺失，走追问"""
        h = handler_of("twins_inspection")
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
            ("emergency_perimeter", ["query_type"],
             "请问您想查询哪类周界数据？关键指标、防区一览、告警统计，还是告警一览？"),
            ("twins_inspection", ["query_type"],
             "请问您想查询哪类巡检数据？今日巡检、今日任务列表、巡检统计，还是巡检执行状态？"),
            ("device_status", ["device_type"],
             "请问您想查询哪类设备？监控、门禁、道闸、广播，还是信息发布设备？"),
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


# ============================================================
# 4.1 周界域追问轮 handle_reply（与消防域同一套行为约定）
# ============================================================
class TestPerimeterHandleReply:
    def test_reply_option_word_fills_query_type(self):
        """用户照反问选项回复「防区一览」→ 正则命中，补槽后 continue"""
        state = make_state("emergency_perimeter", {"query_type": "count"}, ["query_type"])
        result = run(EmergencyPerimeterHandler().handle_reply(state, "防区一览", llm=None))

        assert result["action"] == "continue"
        assert state.slots["query_type"] == "area_overview"
        assert state.missing_params == []

    def test_reply_ordinal_choice_resolves(self):
        """用户回复「第二个」→ 按反问选项顺序映射到 area_overview"""
        state = make_state(
            "emergency_perimeter",
            {"query_type": "count", "_pending_type_clarify": True,
             "_type_options": ["key_metrics", "area_overview", "perimeter_alarm_stats", "alarm_overview"]},
            ["query_type"],
        )
        result = run(EmergencyPerimeterHandler().handle_reply(state, "第二个", llm=None))

        assert result["action"] == "continue"
        assert state.slots["query_type"] == "area_overview"

    def test_reply_unrelated_triggers_re_ask(self):
        state = make_state("emergency_perimeter", {"query_type": "count"}, ["query_type"])
        result = run(EmergencyPerimeterHandler().handle_reply(state, "今天天气不错", llm=None))

        assert result["action"] == "re_ask"
        assert state.unrelated_count == 1

    def test_three_unrelated_replies_give_up(self):
        state = make_state("emergency_perimeter", {"query_type": "count"}, ["query_type"])
        handler = EmergencyPerimeterHandler()
        for _ in range(3):
            result = run(handler.handle_reply(state, "今天天气不错", llm=None))

        assert result["action"] == "give_up"

    def test_reply_count_does_not_overwrite_known_type(self):
        """handle_reply 合并规则：query_type 抽到 count 兜底时不覆盖已识别的子类型"""
        state = make_state("emergency_perimeter", {"query_type": "key_metrics"}, [])
        # 「告警呢」命中周界关键词判相关，且正则抽不出子类型（落 count），
        # 不得把已识别的 key_metrics 覆盖掉
        result = run(EmergencyPerimeterHandler().handle_reply(state, "告警呢", llm=None))

        assert result["action"] == "continue"
        assert state.slots["query_type"] == "key_metrics"


# ============================================================
# 5. 追问轮不重判子类型（docs/对话逻辑修改方案.md §2.1 P0 问题 1）
#    extract_slots(is_followup=True) 只抽填空字段（人名/时间/ID 等），
#    子类型不得被正则默认值 / 分类器误判 / 关键词误命中污染
# ============================================================
class TestFollowupNotOverwriteSubType:
    def test_person_reply_name_keeps_query_type(self):
        """回归用例1：查轨迹缺人名 → 答「张三」→ query_type=trace 不变"""
        state = make_state("person_status", {"query_type": "trace"}, ["person_name"])
        result = run(PersonStatusHandler().handle_reply(state, "张三", llm=None))

        assert result["action"] == "continue"
        assert state.slots["query_type"] == "trace"
        assert state.slots["person_name"] == "张三"

    def test_security_reply_time_keeps_event_type(self):
        """回归用例2：AI告警缺时间 → 答「近一周」→ event_type=ai_alert 不变"""
        state = make_state("security_status", {"event_type": "ai_alert"}, ["date"])
        result = run(SecurityStatusHandler().handle_reply(state, "近一周", llm=None))

        assert result["action"] == "continue"
        assert state.slots["event_type"] == "ai_alert"
        assert state.slots.get("date")

    def test_security_reply_id_keeps_alarm_detail(self):
        """回归用例3：告警详情缺ID → 答「第3条」→ alarm_detail 不变、alarm_id=3"""
        state = make_state("security_status", {"event_type": "alarm_detail"}, ["alarm_id"])
        result = run(SecurityStatusHandler().handle_reply(state, "第3条", llm=None))

        assert result["action"] == "continue"
        assert state.slots["event_type"] == "alarm_detail"
        assert str(state.slots["alarm_id"]) == "3"

    def test_security_reply_keyword_mishit_keeps_event_type(self):
        """缺ID时答「看设备3」：短回复误命中关键词也不得改子类型"""
        state = make_state("security_status", {"event_type": "alarm_detail"}, ["alarm_id"])
        run(SecurityStatusHandler().handle_reply(state, "看设备3", llm=None))

        assert state.slots["event_type"] == "alarm_detail"

    def test_canteen_reply_time_keeps_event_type(self):
        state = make_state("canteen_status", {"event_type": "dish_rank"}, ["date"])
        result = run(CanteenStatusHandler().handle_reply(state, "近三天", llm=None))

        assert result["action"] == "continue"
        assert state.slots["event_type"] == "dish_rank"
        assert state.slots.get("date")

    def test_information_reply_time_keeps_event_type(self):
        state = make_state("information_status", {"event_type": "broadcast_equip"}, ["date"])
        result = run(InformationStatusHandler().handle_reply(state, "昨天呢", llm=None))

        assert result["action"] == "continue"
        assert state.slots["event_type"] == "broadcast_equip"

    def test_followup_extract_slots_has_no_sub_type_key(self):
        """单元行为：followup 抽槽结果不含子类型键，合并天然无法覆盖"""
        security_slots = run(SecurityStatusHandler().extract_slots("第3条", is_followup=True))
        assert "event_type" not in security_slots
        assert security_slots["alarm_id"] == "3"

        person_slots = run(PersonStatusHandler().extract_slots("张三", is_followup=True))
        assert "query_type" not in person_slots
        assert person_slots["person_name"] == "张三"

    def test_fresh_flow_still_judges_sub_type(self):
        """新流程（默认参数 is_followup=False）行为不变：子类型照常判定"""
        fresh = run(SecurityStatusHandler().extract_slots("查一下第3条告警详情"))
        assert fresh["event_type"] == "alarm_detail"

        fresh_person = run(PersonStatusHandler().extract_slots("李四昨天的轨迹"))
        assert fresh_person["query_type"] == "trace"
        assert fresh_person["person_name"] == "李四"

    def test_device_query_reply_type_keeps_query_type(self):
        """设备查询缺类型 → 答「监控」→ query_type=device_list 不变、device_type=jk"""
        state = make_state("device_query", {"query_type": "device_list"}, ["device_type"])
        result = run(DeviceQueryHandler().handle_reply(state, "监控", llm=None))

        assert result["action"] == "continue"
        assert state.slots["query_type"] == "device_list"
        assert state.slots["device_type"] == "jk"

    def test_device_query_reply_code_keeps_device_detail(self):
        """设备详情缺名称/编码 → 答「MJ-003」→ device_detail 不被兜底口径冲掉"""
        state = make_state(
            "device_query",
            {"query_type": "device_detail", "device_type": "mj"},
            ["device_keyword"],
        )
        result = run(DeviceQueryHandler().handle_reply(state, "MJ-003", llm=None))

        assert result["action"] == "continue"
        assert state.slots["query_type"] == "device_detail"
        assert state.slots["device_keyword"] == "MJ-003"

    @pytest.mark.parametrize(
        "query",
        ["查下广播设备清单", "信息屏有哪些", "园区有哪些设备", "道闸设备清单"],
    )
    def test_device_query_list_wording_beats_classifier(self, query):
        """明确的列表措辞以正则为纲，不被子类型分类器改判成详情"""
        slots = run(DeviceQueryHandler().extract_slots(query))
        assert slots["query_type"] == "device_list", f"query={query!r} 落入 {slots['query_type']}"

    def test_device_query_fresh_flow_still_judges_detail(self):
        """没有列表措辞时，分类器兜底照常生效：口语化详情问法仍判 device_detail"""
        slots = run(DeviceQueryHandler().extract_slots("看下这台设备的设备信息"))
        assert slots["query_type"] == "device_detail"


# ============================================================
# 6. 设备态势兜底反问：笼统问法 -> 问"哪类设备" -> 按台账口径查
# ============================================================
class TestDeviceStatusHandleReply:
    def _vague_state(self, slots: dict = None) -> IntentState:
        """兜底轮的状态：query_type=count、缺 device_type"""
        return make_state(
            "device_status",
            slots if slots is not None else {"query_type": "count"},
            ["device_type"],
        )

    def test_reply_type_word_fills_device_type(self):
        """用户照反问选项回「监控」→ device_type=jk，口径定为台账列表"""
        state = self._vague_state()
        result = run(DeviceStatusHandler().handle_reply(state, "监控", llm=None))

        assert result["action"] == "continue"
        assert state.slots["device_type"] == "jk"
        assert state.slots["query_type"] == "device_list"
        assert state.missing_params == []

    @pytest.mark.parametrize(
        "reply,expected_type",
        [
            ("监控", "jk"),
            # 关键回归：「门禁」在设备态势正则里会命中 mj_online（门禁在线率），
            # 但这一轮是在回答"哪类设备"，必须按设备类型解析
            ("门禁", "mj"),
            ("道闸", "dz"),
            ("广播", "gb"),
            ("信息发布设备", "xxfb"),
        ],
    )
    def test_type_words_not_hijacked_by_status_regex(self, reply, expected_type):
        state = self._vague_state()
        result = run(DeviceStatusHandler().handle_reply(state, reply, llm=None))

        assert result["action"] == "continue"
        assert state.slots["device_type"] == expected_type
        assert state.slots["query_type"] == "device_list"

    def test_restated_full_query_keeps_statistics_sub_type(self):
        """回复里带统计口径词（"门禁设备在线率"）说明用户重说了完整问法，不当作类型回答"""
        state = self._vague_state()
        result = run(DeviceStatusHandler().handle_reply(state, "门禁设备在线率", llm=None))

        assert result["action"] == "continue"
        assert state.slots["query_type"] == "mj_online"

    def test_reply_unrelated_triggers_re_ask(self):
        state = self._vague_state()
        result = run(DeviceStatusHandler().handle_reply(state, "今天天气不错", llm=None))

        assert result["action"] == "re_ask"
        assert state.unrelated_count == 1

    def test_three_unrelated_replies_give_up(self):
        state = self._vague_state()
        handler = DeviceStatusHandler()
        for _ in range(3):
            result = run(handler.handle_reply(state, "今天天气不错", llm=None))

        assert result["action"] == "give_up"

    def test_known_sub_type_needs_no_type(self):
        """
        统计口径的子类型不接反问：进追问轮走常规合并，
        不会把"设备"这类泛化词收成 device_type，也不会再问哪类设备
        （此处不断言 query_type：常规路径本来就把子类型判定交给分类器兜底）
        """
        state = make_state("device_status", {"query_type": "equip_class"}, [])
        result = run(DeviceStatusHandler().handle_reply(state, "看下设备", llm=None))

        assert result["action"] == "continue"
        assert "device_type" not in state.slots
        assert state.missing_params == []
