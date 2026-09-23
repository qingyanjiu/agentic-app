# -*- coding: utf-8 -*-
"""
多轮场景矩阵（docs/测试方案.md L3）核心 4 条：
  C1 缺参追问闭环 —— 9 个会追问的域走完整两轮闭环 + 4 个恒不追问域的记录性用例
  C4 追问中新意图接管 —— handler 层真测试 + app.py 接管判定的规格快照
  C5 同意图子类型显式切换 —— 真槽位抽取 + app.py 合并规则的规格快照（案例 13/17 回归）
  C6 省略式追问继承 —— 同上（「昨天呢」双继承口径）

本文件从不 import app.py（import 期会初始化 LLM 工厂与上传目录）：
  - handler / graph / slots 层全部测真代码；
  - app.py agent_ws 内联的接管判定（情况 1.2，app.py:1754-1770）与
    省略式槽位合并（情况 1.5，app.py:1871-1890）以「规格快照」复刻进本文件，
    注释标明源码位置 —— app.py 相应分支改动后须人工同步快照（已知局限，
    真实路径由 docs/测试方案.md 的人工冒烟 T1-T5 兜底）。

全部无模型：追问轮 handle_reply 的 llm 参数传 None（13 个 handler 均零引用），
追问轮 extract_slots(is_followup=True) 跳过 embedding 分类器。
"""
import asyncio
import re

import pytest

from conftest import (
    answers_of,
    asks_of,
    domain_graphs,  # noqa: F401  fixture
    graph_input,
)
from memory.session_state import IntentState

from agent.intent.slots import (
    extract_canteen_status_slots,
    extract_person_status_slots,
    extract_security_status_slots,
)
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


def run(coro):
    return asyncio.run(coro)


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


def make_state(module: str, slots: dict, missing: list = None) -> IntentState:
    return IntentState(
        module=module,
        slots=slots,
        missing_params=missing if missing is not None else [],
        ask_count=0,
    )


# ============================================================
# C1：缺参追问闭环（docs/测试方案.md 场景矩阵 C1）
# 13 域中 9 个域会缺参追问；两轮模式照 test_graphs.TestDeviceQueryFullFlow：
#   第一轮  graph(缺参 slots) -> ask
#   补槽轮  handler.handle_reply(用户回复) -> continue + 槽位补全
#   第二轮  graph(补全 slots) -> done + 答案
# answer_keyword 为 None 的行不断言答案内容（见 person 行注释）
# ============================================================
C1_CASES = [
    # person 域后端无 location 查询工具（图内 _JAVA_TOOL_MAP 无 location 条目），
    # 补全人名后落 call_tool 守卫分支 -> done=True + 「暂不支持」提示。
    # 本行验证的是追问补槽闭环，答案内容锁定守卫口径。
    ("person_status", {"query_type": "location"},
     "请问您要查询哪位人员？", "张三",
     {"person_name": "张三"}, "暂不支持"),
    ("security_status", {"event_type": "alarm_list"},
     "请问您想查询哪个时间段的告警？", "昨天",
     {"date": "昨天"}, "周界入侵"),
    ("canteen_status", {"event_type": "dining_count"},
     "请问您想查询哪天的食堂信息？", "昨天",
     {"date": "昨天"}, "321"),
    ("information_status", {"event_type": "info_view"},
     "请问您想查询哪天的信息发布数据？", "昨天",
     {"date": "昨天"}, "大堂信息屏"),
    # 消防/周界/设备态势/孪生巡检：笼统问法 query_type=count -> 反问哪类 ->
    # 用户照选项词回复 -> 解析成具体子类型（案例2 同款链路）
    ("emergency_fire", {"query_type": "count"},
     "请问您想查询哪类消防数据？", "实时告警",
     {"query_type": "fire_alarm_list"}, "烟感火警"),
    ("emergency_perimeter", {"query_type": "count"},
     "请问您想查询哪类周界数据？", "关键指标",
     {"query_type": "key_metrics"}, "defenseTotal"),
    ("device_status", {"query_type": "count"},
     "请问您想查询哪类设备？", "监控",
     {"query_type": "device_list", "device_type": "3"}, "A栋枪机"),
    ("device_query", {"query_type": "device_detail"},
     "请问您想查看哪台设备的详情？", "CY-HIK-JK-001-0001",
     {"device_keyword": "CY-HIK-JK-001-0001"}, "A栋枪机"),
    ("twins_inspection", {"query_type": "count"},
     "请问您想查询哪类巡检数据？", "今日巡检",
     {"query_type": "today_inspection"}, "completionRate"),
]


def _assert_slots_filled(module: str, slots: dict, expect: dict) -> None:
    for k, v in expect.items():
        if k == "date":
            # 时间槽位只锁「本轮说法进了 raw」，不锁解析出的具体区间
            assert slots.get("date"), f"{module} 应填上 date"
            assert v in str(slots["date"].get("raw", "")), \
                f"{module} date.raw 应含 {v!r}，实际 {slots['date'].get('raw')!r}"
        else:
            assert slots.get(k) == v, \
                f"{module} slots[{k!r}] 应为 {v!r}，实际 {slots.get(k)!r}"


@pytest.mark.parametrize(
    "module,turn1_slots,ask_prefix,reply,expect_slots,answer_keyword",
    C1_CASES,
)
def test_c1_missing_param_closure(
    domain_graphs, module, turn1_slots, ask_prefix, reply, expect_slots, answer_keyword
):
    """缺参 -> 追问 -> 用户补充 -> 补槽 -> 重进图出答案（每域一条完整闭环）"""
    # ---- 第一轮：缺参 -> 图追问，流程不结束 ----
    r1 = run(domain_graphs(module).ainvoke(graph_input(turn1_slots)))
    ask_events = asks_of(r1)
    assert len(ask_events) == 1, f"{module} 应恰好追问一次: {ask_events}"
    assert ask_events[0]["question"].startswith(ask_prefix), \
        f"{module} 追问话术不符: {ask_events[0]['question']!r}"
    assert not r1.get("done"), f"{module} 追问轮不应结束流程"
    assert answers_of(r1) == [], f"{module} 追问轮不应有答案"

    # ---- 补槽轮：从图输出重建会话状态（app.py 情况 1 的等价物），补参 ----
    state = IntentState(
        module=module,
        slots=dict(r1["slots"]),
        missing_params=handler_of(module)._get_missing_params(r1["slots"]),
        ask_count=r1["ask_count"],
        unrelated_count=0,
        original_query="测试问题",
        done=False,
    )
    reply_result = run(handler_of(module).handle_reply(state, reply, llm=None))
    assert reply_result["action"] == "continue", \
        f"{module} 补槽轮应 continue: {reply_result.get('answer')!r}"
    assert state.missing_params == [], \
        f"{module} 补槽后不应再缺参: {state.missing_params}"
    _assert_slots_filled(module, state.slots, expect_slots)

    # ---- 第二轮：带补全 slots 重进图，直接出答案 ----
    r2 = run(domain_graphs(module).ainvoke(graph_input(state.slots)))
    assert r2.get("error") is None, f"{module} 出错: {r2.get('error')}"
    assert r2.get("done") is True, f"{module} 补参后应完成流程"
    assert asks_of(r2) == [], f"{module} 补参后不应再追问: {asks_of(r2)}"
    answer_list = answers_of(r2)
    assert answer_list, f"{module} 第二轮没有答案"
    assert answer_keyword in answer_list[-1], \
        f"{module} 答案应含 {answer_keyword!r}，实际 {answer_list[-1][:120]!r}"


# vehicle / energy / meeting / compositive_overview 四域的 _get_missing_params
# 恒返回 []（永不追问）—— 记录性用例：典型查询单轮直达答案
C1_NO_ASK_CASES = [
    ("vehicle_status", {"query_type": "parking_space"}),
    ("energy_status", {"query_type": "overall_energy"}),
    ("meeting_status", {"event_type": "meeting_statistics"}),
    ("compositive_overview", {"query_type": "basic_info"}),
]


@pytest.mark.parametrize("module,slots", C1_NO_ASK_CASES)
def test_c1_domains_without_ask_flow(domain_graphs, module, slots):
    assert handler_of(module)._get_missing_params(slots) == [], \
        f"{module} 口径变更（开始缺参追问）时应把本域挪进 C1_CASES 补闭环用例"

    final = run(domain_graphs(module).ainvoke(graph_input(slots)))
    assert final.get("error") is None
    assert final.get("done") is True
    assert asks_of(final) == [], f"{module} 不应追问"
    assert answers_of(final), f"{module} 应有答案"


# ============================================================
# C4：追问中新意图接管
# 真代码层：handler 判不相关 -> re_ask（接管判定的入口分支）。
# 注：is_related 是关键词启发式 —— person 域缺人名时 ≤8 字纯中文回复
# 一律视为补充人名（规则 2），因此本组跨域样本刻意取 >8 字的句子，
# 保证「判不相关」是确定性行为。
# 接管判定本身内联在 app.py 情况 1.2，见下方规格快照。
# ============================================================
class TestC4Takeover:
    def _waiting_state(self) -> IntentState:
        """追问人名挂起中的会话态（C4 矩阵的标准前置）"""
        return make_state("person_status", {"query_type": "location"}, ["person_name"])

    def test_cross_domain_reply_judged_unrelated(self):
        """追问人名时答「停车场还有多少空位」（9 字 > 8 字上限，无人员关键词）-> re_ask"""
        state = self._waiting_state()
        result = run(PersonStatusHandler().handle_reply(state, "停车场还有多少空位", llm=None))

        assert result["action"] == "re_ask"
        assert state.unrelated_count == 1
        # 未超阈值：状态保留（接管与否由 app.py 情况 1.2 决定，见快照用例）
        assert result["state"] is state

    def test_second_cross_domain_sample_also_unrelated(self):
        """第二跨域样本「查一下今天的能耗总量」（10 字）-> 同样 re_ask"""
        state = self._waiting_state()
        result = run(PersonStatusHandler().handle_reply(state, "查一下今天的能耗总量", llm=None))

        assert result["action"] == "re_ask"
        assert state.unrelated_count == 1

    def test_takeover_window_closes_after_max_unrelated(self):
        """连续 3 次无关 -> give_up（app.py 情况 1.1 先于 1.2 判定：放弃优先于接管）"""
        state = self._waiting_state()
        handler = PersonStatusHandler()
        result = None
        for _ in range(3):
            result = run(handler.handle_reply(state, "停车场还有多少空位", llm=None))

        assert result["action"] == "give_up"
        assert state.unrelated_count == 3
        assert "不想继续" in result["answer"]

    # --------------------------------------------------------
    # 规格快照：app.py 情况 1.2 的接管判定（agent_ws re_ask 分支内联逻辑）
    # 快照自 app.py:1730-1770（give_up 优先 + classify 异常兜底 + 跨域才接管），
    # 改动 app.py 相应分支时须人工同步本快照。
    # --------------------------------------------------------
    # app.py MODULE_HANDLERS 的 13 个业务域键（快照）
    _MODULES = {
        "person_status", "security_status", "canteen_status", "vehicle_status",
        "information_status", "energy_status", "emergency_fire",
        "emergency_perimeter", "meeting_status", "device_status",
        "compositive_overview", "device_query", "twins_inspection",
    }

    @classmethod
    def _takeover_decision(cls, result_action, t_intent, active_module):
        # 情况 1.1：give_up 优先 —— 连续 3 次无关先放弃，不再尝试接管
        if result_action == "give_up":
            return "give_up"
        # 情况 1.2：补跑一次意图分类；分类异常时 t_intent 为 None，维持追问
        if t_intent is None:
            return "维持追问"
        # 识别出其他业务域且 ≠ 当前域 -> 放弃旧任务、按新意图全新流程
        if t_intent in cls._MODULES and t_intent != active_module:
            return "接管"
        # 仍是本域 / other（低于阈值）：维持追问
        return "维持追问"

    @pytest.mark.parametrize(
        "result_action,t_intent,active_module,expected",
        [
            # C4 标准场景：追问人名时问车位 -> vehicle_status 跨域 -> 接管
            ("re_ask", "vehicle_status", "person_status", "接管"),
            # 第二跨域样本：能耗
            ("re_ask", "energy_status", "person_status", "接管"),
            # 分类落 other（低于阈值）：维持追问
            ("re_ask", "other", "person_status", "维持追问"),
            # 仍是本域：不能自己接管自己
            ("re_ask", "person_status", "person_status", "维持追问"),
            # 分类抛异常：维持追问（app.py except 分支）
            ("re_ask", None, "person_status", "维持追问"),
            # 已达 MAX_UNRELATED：give_up 优先，即便分类出了新意图
            ("give_up", "vehicle_status", "person_status", "give_up"),
        ],
    )
    def test_takeover_decision_table(self, result_action, t_intent, active_module, expected):
        assert self._takeover_decision(result_action, t_intent, active_module) == expected


# ============================================================
# 规格快照：app.py 情况 1.5 的省略式槽位合并（agent_ws 内联逻辑，
# 快照自 app.py:1871-1890 的 ②③ 两步；改动 app.py 时须人工同步）
#   ② 同域继承本轮没抽到的实体槽；时间（date）永不继承
#   ③ 子类型：本轮抽出具体值则以本轮为准（案例 13/17），
#      本轮落到兜底值（输入不含子类型信息，如「昨天呢」）才继承上轮
# ①（槽位只用本轮输入抽取）与 ④（original_query 不回写拼接串）
# 是调用方职责，在下方 TestEllipsisSequence 里体现。
# ============================================================
_ENTITY_SLOTS = ("person_name", "area", "alarm_id", "meal")
_SUBTYPE_FALLBACKS_SNAPSHOT = {
    "query_type": {"count", "basic_info", "device_list"},
    "event_type": {"count", "alarm_list", "week_menu", "info_view"},
}


def _merge_ellipsis_slots(prev_slots: dict, fresh_slots: dict, same_domain: bool) -> dict:
    merged = dict(fresh_slots)
    if same_domain:
        # ② 同域：本轮没抽到的实体槽继承上轮
        for k in _ENTITY_SLOTS:
            if not merged.get(k) and prev_slots.get(k):
                merged[k] = prev_slots[k]
        # ③ 子类型继承：仅本轮落兜底值（或没抽到）才继承上轮
        for k in ("query_type", "event_type"):
            prev_subtype = prev_slots.get(k)
            if not prev_subtype:
                continue
            fresh_subtype = merged.get(k)
            if not fresh_subtype or fresh_subtype in _SUBTYPE_FALLBACKS_SNAPSHOT.get(k, ()):
                merged[k] = prev_subtype
    return merged


class TestC5SubtypeSwitch:
    """C5：同意图子类型显式切换（排查记录案例 13 / 案例 17 的回归口径）"""

    def test_followup_extraction_fact_leave(self):
        """真代码事实：「那出去的呢」正则真的抽出 leave（显式切换的原料）"""
        fresh = extract_person_status_slots("那出去的呢")
        assert fresh["query_type"] == "leave"

    def test_explicit_switch_beats_subtype_inherit(self):
        """上轮 enter，本轮抽出具体子类型 leave -> 合并后以本轮为准，不被继承压回"""
        # 上轮时间用「上周」做标记：无时间词的本轮抽取会默认落「今天」
        # （parse_time_slot 兜底，slots.py 末尾），标记词不能与默认值撞车，
        # 否则「时间不继承」断言无法区分"默认今天"和"继承了上轮"
        prev = {"query_type": "enter", "person_name": "",
                "date": {"time_type": "span", "raw": "上周"}}
        fresh = extract_person_status_slots("那出去的呢")

        merged = _merge_ellipsis_slots(prev, fresh, same_domain=True)

        assert merged["query_type"] == "leave", \
            "本轮明确抽出 leave 时不得被子类型继承改回 enter（案例 17 修正点）"
        # 时间永不继承：merged 的 date 只能来自本轮抽取（默认今天），不得带上轮的「上周」
        assert "上周" not in str((merged.get("date") or {}).get("raw", ""))


class TestC6EllipsisInherit:
    """C6：省略式追问继承（「昨天呢」口径）"""

    def test_followup_extraction_fact_count_fallback(self):
        """真代码事实：「昨天呢」抽出 date=昨天，子类型落 count 兜底（= 本轮无子类型信息）"""
        fresh = extract_person_status_slots("昨天呢")
        assert fresh["query_type"] == "count"
        assert fresh.get("date"), "昨天呢 应抽出时间"
        assert "昨天" in str(fresh["date"].get("raw", ""))

    def test_double_inherit_subtype_and_entity(self):
        """双继承样本：上轮 trace+李四，本轮「昨天呢」-> 子类型 trace、实体李四都继承，时间用本轮"""
        prev = {"query_type": "trace", "person_name": "李四",
                "date": {"time_type": "point", "raw": "今天"}}
        fresh = extract_person_status_slots("昨天呢")

        merged = _merge_ellipsis_slots(prev, fresh, same_domain=True)

        assert merged["query_type"] == "trace", "count 兜底应继承上轮 trace"
        assert merged["person_name"] == "李四", "本轮没抽到实体应继承上轮"
        assert "昨天" in str(merged["date"].get("raw", "")), "时间永不继承"
        assert "今天" not in str(merged["date"].get("raw", ""))

    @pytest.mark.parametrize(
        "extractor,fallback_event_type",
        [
            (extract_security_status_slots, "alarm_list"),
            (extract_canteen_status_slots, "week_menu"),
        ],
    )
    def test_security_canteen_defaults_align_with_fallbacks(self, extractor, fallback_event_type):
        """安防/食堂抽取器对无子类型输入的兜底值必须落在 _SUBTYPE_FALLBACKS 集合内，
        否则省略式追问会把兜底值当「本轮明确子类型」而不继承（快照与 slots.py 的对齐断言）"""
        assert fallback_event_type in _SUBTYPE_FALLBACKS_SNAPSHOT["event_type"]
        fresh = extractor("昨天呢")
        assert fresh["event_type"] == fallback_event_type

    def test_cross_domain_ellipsis_inherits_nothing(self):
        """拼接分类命中另一域（same_domain=False）：实体与子类型都不继承"""
        prev = {"query_type": "enter", "person_name": "张三"}
        fresh = {"event_type": "alarm_list", "date": None, "alarm_id": ""}

        merged = _merge_ellipsis_slots(prev, fresh, same_domain=False)

        assert merged == {"event_type": "alarm_list", "date": None, "alarm_id": ""}
        assert "person_name" not in merged
        assert "query_type" not in merged

    def test_merge_does_not_mutate_inputs(self):
        """合并不得改写入参（app.py 里 prev/fresh 还要被后续逻辑使用）"""
        prev = {"query_type": "enter", "person_name": "张三"}
        fresh = {"query_type": "count", "person_name": ""}

        _merge_ellipsis_slots(prev, fresh, same_domain=True)

        assert prev == {"query_type": "enter", "person_name": "张三"}
        assert fresh == {"query_type": "count", "person_name": ""}


class TestEllipsisSequence:
    """情况 1.5 全序（①→④）：
      ① 槽位只用本轮输入抽取 —— 此处用抽取器的正则内核（无模型确定性等价；
         生产路径 handler.extract_slots 在 count 兜底时还会补调分类器救兜底，
         该分支属模型行为，由人工冒烟 T2/T3 覆盖）
      ②③ 同域合并（规格快照）
      ④ original_query 保持首轮原话 —— 重建时不回写拼接串，直接断言
    """

    @pytest.mark.parametrize(
        "prev_subtype,query,expect_subtype,expect_date_raw",
        [
            # C5：显式切换 —— 上轮 enter，本轮「那出去的呢」以本轮 leave 为准
            ("enter", "那出去的呢", "leave", None),
            # C6：省略继承 —— 上轮 enter，本轮「昨天呢」继承 enter、时间用本轮
            ("enter", "昨天呢", "enter", "昨天"),
        ],
    )
    def test_ellipsis_turn_rebuilds_state(
        self, prev_subtype, query, expect_subtype, expect_date_raw
    ):
        handler = PersonStatusHandler()
        # 上轮完成后留在 session 的状态（与 app.py 会话态同构）。
        # 上轮时间用「上周」做标记：无时间词的本轮抽取默认落「今天」
        # （parse_time_slot 兜底），标记词不能与默认值撞车，否则时间
        # 不继承断言无法区分"默认今天"和"继承了上轮"
        prev_slots = {"query_type": prev_subtype, "person_name": "",
                      "date": {"time_type": "span", "raw": "上周"}}

        # ① 本轮输入单独抽槽（拼接串整体抽槽会把实体查成上轮的 —— app.py 注释红线）
        fresh = extract_person_status_slots(query)
        # ②③ 同域合并
        merged = _merge_ellipsis_slots(prev_slots, fresh, same_domain=True)
        # ④ 重建 IntentState：original_query 保持首轮原话，不回写拼接串
        state = IntentState(
            module="person_status",
            slots=merged,
            missing_params=handler._get_missing_params(merged),
            ask_count=0,
            unrelated_count=0,
            original_query="查一下园区上周进了多少人",
            done=False,
        )

        assert state.original_query == "查一下园区上周进了多少人"
        assert state.slots["query_type"] == expect_subtype, \
            f"{query!r} 应落子类型 {expect_subtype}，实际 {state.slots['query_type']!r}"
        assert state.missing_params == [], state.missing_params
        if expect_date_raw is None:
            # C5：本轮没说时间 -> date 是本轮抽取的默认值（今天），不得带上轮的「上周」
            assert "上周" not in str((state.slots.get("date") or {}).get("raw", ""))
        else:
            # C6：本轮「昨天呢」的时间胜出，上轮「上周」不得残留
            assert expect_date_raw in str(state.slots["date"].get("raw", ""))
            assert "上周" not in str(state.slots["date"].get("raw", ""))


# ============================================================
# 情况 1.5 省略式追问白名单门（规格快照）
# 快照自 app.py:1461-1493 的 _ELLIPSIS_FOLLOWUP_RE / is_ellipsis_followup：
# other 且上轮 done 的输入，只有「时间词/域动词 + 语气词」的省略句
# （昨天呢/那上周呢/出去的呢）才拼接上轮问题重新分类；其余 other
# （今天日期/你好/你是谁…）直接走 other 兜底，不被上轮问题拽回业务域
# （2026-09-23 用户裁定；背景：车辆完成后问「今天是哪天」被拼成停车
# 查询重答一遍）。app.py 改动该正则后须人工同步本快照。
# ============================================================
_ELLIPSIS_FOLLOWUP_SNAPSHOT = re.compile(
    r"^(?:那|再|就)?"
    r"(?:"
    r"(?:大前天|大后天|前天|后天|今天|明天|昨天|上周|本周|下周|上个月|上月|这个月|本月|下个月|下月|今年|去年|前年|刚才|现在|(?:今天|昨天|明天)?(?:早上|上午|中午|下午|晚上))"
    r"(?:的时候)?的?(?:呢|吗|么|怎么样|咋样|又如何|再看看|看看|又是多少|有多少)"
    r"|(?:出去|进来|进入|离开|离园|入园|来了?|走了?)(?:的人)?的?(?:呢|人数|有多少|多少)"
    r"|(?:大前天|大后天|前天|后天|今天|明天|昨天|上周|本周|下周|上个月|上月|这个月|本月|下个月|下月|今年|去年|前年|刚才|现在|(?:今天|昨天|明天)?(?:早上|上午|中午|下午|晚上))的?(?:出去|进来|进入|离开|离园|入园|来了?|走了?)(?:的人)?的?(?:呢|人数|有多少|多少)"
    r")"
    r"[吧呢吗啊呀]?$"
)


def _is_ellipsis_followup_snapshot(query: str) -> bool:
    if not query or len(query.strip()) > 12:
        return False
    q = re.sub(r"[\s，,。.!！?？~～]+$", "", query.strip())
    return bool(_ELLIPSIS_FOLLOWUP_SNAPSHOT.fullmatch(q))


class TestEllipsisGateSnapshot:
    @pytest.mark.parametrize(
        "query",
        [
            "昨天呢",
            "那上周呢",
            "今天呢",
            "上个月呢",
            "本月呢",
            "那去年呢",
            "昨天的呢",
            "出去的呢",
            "那出去的呢",
            "走了呢",
            "昨天出去的呢",
            "今天早上呢",
            "现在怎么样",
            "今天有多少",
            "昨天呢？",       # 带标点归一后放行
        ],
    )
    def test_ellipsis_shaped_passes(self, query):
        """省略形状 → 放行进 1.5 拼接继承"""
        assert _is_ellipsis_followup_snapshot(query) is True, f"{query!r} 应放行"

    @pytest.mark.parametrize(
        "query",
        [
            "今天是哪天",         # 冒烟翻车句：曾被拼成停车查询重答
            "今天日期",
            "今天几号",
            "今天星期几",
            "现在几点",
            "你好",
            "你是谁",
            "讲个笑话",
            "今天天气不错",
            "张三现在在哪里",     # 判不进域的完整问法也不该被拽回
            "停车场还有多少空位",  # 完整业务问句走正常分类，与省略门无关
            "昨天的离开人数是多少",  # 完整问句不是省略形状
        ],
    )
    def test_non_ellipsis_goes_to_other(self, query):
        """非省略形状 → 门拦下，直接走 other 兜底，不拼接"""
        assert _is_ellipsis_followup_snapshot(query) is False, f"{query!r} 应被拦下"
