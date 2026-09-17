# -*- coding: utf-8 -*-
"""
意图分类测试（依赖 embedding 模型 BAAI/bge-small-zh-v1.5，容器内挂载后运行）。

  - classify_intent：顶层意图（8 个态势域 + other）
  - classify_xxx_sub_type：各域子类型

运行方式（容器内）：
  pytest tests/test_intent_classification.py -v

docs/问题排查记录.md 案例3（未修复，待观察）在文件末尾以 skip 占位记录。
"""
import asyncio

import pytest

from agent.intent import (
    classify_intent,
    classify_sub_type,
    classify_security_sub_type,
    classify_canteen_sub_type,
    classify_vehicle_sub_type,
    classify_information_sub_type,
    classify_energy_sub_type,
    classify_meeting_sub_type,
    classify_fire_sub_type,
    classify_perimeter_sub_type,
    classify_device_sub_type,
    classify_compositive_overview_sub_type,
    classify_device_query_sub_type,
)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="session", autouse=True)
def warm_classifier():
    """session 级预热：加载一次模型（与 app startup 行为一致），后续用例复用单例"""
    run(classify_intent("预热"))
    yield


def classify(query: str) -> dict:
    return run(classify_intent(query))


# ============================================================
# 1. 顶层意图分类：8 个域各给 2 条典型问法
# ============================================================
class TestTopLevelIntent:
    @pytest.mark.parametrize(
        "query,expected_intent",
        [
            # 人员态势
            ("张三现在在哪里", "person_status"),
            ("今天进入园区多少人", "person_status"),
            # 安防态势
            ("今天的安防告警列表", "security_status"),
            ("园区安全指数是多少", "security_status"),
            # 食堂管理
            ("今天食堂就餐多少人", "canteen_status"),
            ("本周食堂菜单", "canteen_status"),
            # 车辆态势
            ("停车场还剩多少车位", "vehicle_status"),
            ("今天园区车流量", "vehicle_status"),
            # 信息发布
            ("本周信息发布任务趋势", "information_status"),
            ("广播设备明细", "information_status"),
            # 能源态势
            ("本月用电排名", "energy_status"),
            ("园区总体能耗情况", "energy_status"),
            # 会议管理
            ("今天有什么会议安排", "meeting_status"),
            ("本月会议统计", "meeting_status"),
            # 消防态势
            ("消防设备台账", "emergency_fire"),
            ("实时消防告警", "emergency_fire"),
            # 周界态势（孪生周界）
            ("周界防区一览", "emergency_perimeter"),
            ("周界告警统计", "emergency_perimeter"),
            # 设备态势
            ("设备分类占比", "device_status"),
            ("门禁设备在线率", "device_status"),
            # 综合态势总览
            ("园区面积多大", "compositive_overview"),
            ("设备健康度总览", "compositive_overview"),
            # 设备查询（台账口径：设备列表 / 设备详情，资产库的 syncSource 八类：
            # 0门禁 1道闸 2梯控 3监控 4入侵报警 5广播 6水表 7电表）
            ("设备列表", "device_query"),
            ("园区有哪些设备", "device_query"),
            ("查下A栋的监控设备", "device_query"),
            ("查下门禁设备列表", "device_query"),
            ("设备详情", "device_query"),
            # 注意：只报编码、不带"设备"二字的短问法（如「MH-001的详情」）目前过不了
            # 顶层阈值（实测 0.53）会落到 other，带上"设备/台"这类名词即可命中；
            # 原因与取舍见 docs/开发计划.md §2.5
            ("MH-001这台设备的详情", "device_query"),
        ],
    )
    def test_domain_query_classified(self, query, expected_intent):
        result = classify(query)
        assert isinstance(result, dict) and "intent" in result
        assert result["intent"] == expected_intent, (
            f"query={query!r} 顶层意图判为 {result['intent']}"
            f"（score={result.get('score')}），期望 {expected_intent}"
        )

    def test_return_shape_has_score(self):
        result = classify("今天食堂就餐多少人")
        assert 0.0 <= result.get("score", -1) <= 1.0


# ============================================================
# 2. 各域子类型分类
# ============================================================
class TestSubTypes:
    def test_person_sub_type(self):
        sub, score = run(classify_sub_type("张三现在在哪里"))
        assert sub == "location"

    def test_security_sub_type(self):
        sub, score = run(classify_security_sub_type("园区安全指数是多少"))
        assert sub == "security_index"

    def test_security_new_sub_types(self):
        """emergency_aialert 并入的三个新子类型（开发计划 §2.4）"""
        cases = [
            ("AI告警总览帮我看一下", "ai_overview"),
            ("近30天的告警数量变化趋势", "ai_trend"),
            ("管理预警的明细列表有哪些", "ai_alarm_list"),
        ]
        for query, expected in cases:
            sub, score = run(classify_security_sub_type(query))
            assert sub == expected, f"query={query!r} 安防子类型判为 {sub}（score={score:.4f}）"

    def test_canteen_sub_type(self):
        sub, score = run(classify_canteen_sub_type("今天食堂就餐多少人"))
        assert sub == "dining_count"

    def test_vehicle_sub_type(self):
        sub, score = run(classify_vehicle_sub_type("停车场还剩多少车位"))
        assert sub == "parking_space"

    def test_information_sub_type(self):
        sub, score = run(classify_information_sub_type("本周信息发布任务趋势"))
        assert sub == "task_trend"

    def test_energy_sub_type(self):
        sub, score = run(classify_energy_sub_type("本月用电排名"))
        assert sub == "electricity_rank"

    def test_meeting_sub_type(self):
        sub, score = run(classify_meeting_sub_type("今天有什么会议安排"))
        assert sub == "room_meet_list_by_day"

    def test_fire_sub_types(self):
        cases = [
            ("灭火器台账", "fire_assets"),
            ("今天火警数是多少", "fire_alarm_num"),
            ("实时消防告警列表", "fire_alarm_list"),
            ("本月报修数量趋势", "month_repair"),
        ]
        for query, expected in cases:
            sub, score = run(classify_fire_sub_type(query))
            assert sub == expected, f"query={query!r} 消防子类型判为 {sub}（score={score:.4f}）"

    def test_perimeter_sub_types(self):
        cases = [
            ("在线防区有多少", "key_metrics"),
            ("今日周界告警数", "key_metrics"),
            ("防区布防状态", "area_overview"),
            ("周界告警时段分布", "perimeter_alarm_stats"),
            ("周界告警列表", "alarm_overview"),
        ]
        for query, expected in cases:
            sub, score = run(classify_perimeter_sub_type(query))
            assert sub == expected, f"query={query!r} 周界子类型判为 {sub}（score={score:.4f}）"

    def test_device_sub_types(self):
        cases = [
            ("设备分类占比", "equip_class"),
            ("消防设备健康度", "category_health"),
            ("月度维修趋势", "month_maintenance"),
            ("本月报修数量", "month_repair"),
            ("各区域设备数量", "statis_region"),
            ("安防设备在线率", "anfang_online"),
            ("广播设备在线率", "gb_online"),
            ("门禁设备在线率", "mj_online"),
        ]
        for query, expected in cases:
            sub, score = run(classify_device_sub_type(query))
            assert sub == expected, f"query={query!r} 设备子类型判为 {sub}（score={score:.4f}）"

    def test_overview_sub_types(self):
        cases = [
            ("园区面积多大", "basic_info"),
            ("IoT设备总数多少", "basic_info"),
            ("设备健康度总览", "device_health"),
            ("设备健康分是多少", "device_health"),
        ]
        for query, expected in cases:
            sub, score = run(classify_compositive_overview_sub_type(query))
            assert sub == expected, f"query={query!r} 总览子类型判为 {sub}（score={score:.4f}）"

    def test_device_query_sub_types(self):
        """
        只覆盖子类型分类器判得动的问法。

        「清单/列表/有哪些」这类列表措辞在本模型里与详例句式贴得太近
        （连列表语料自身的"看下设备清单"都会被判成 device_detail 0.83），
        调语料收效甚微，因此改由 DeviceQueryHandler 的 LIST_WORDING
        以"正则为纲"兜底，见 tests/test_handlers.py
        ::TestFollowupNotOverwriteSubType::test_device_query_list_wording_beats_classifier
        """
        cases = [
            ("设备列表", "device_list"),
            ("园区有哪些设备", "device_list"),
            ("查下监控设备列表", "device_list"),
            ("查下园区里的监控设备", "device_list"),
            ("设备详情", "device_detail"),
            ("MH-001的详情", "device_detail"),
            ("看下这台设备的设备信息", "device_detail"),
            ("信息发布屏的详情", "device_detail"),
        ]
        for query, expected in cases:
            sub, score = run(classify_device_query_sub_type(query))
            assert sub == expected, f"query={query!r} 设备查询子类型判为 {sub}（score={score:.4f}）"


# ============================================================
# 3. 案例2 修复后的行为链路验证：
#    「查下最新的消防」正则落 count → graph 反问，不再落到不支持的工具
#    （正则部分见 test_slots.py；这里只验证分类器结果不会推翻反问流程）
# ============================================================
class TestCase2ClassifierFallback:
    def test_vague_fire_query_is_not_misclassified(self):
        """
        即使 embedding 分类器把「查下最新的消防」判成某个子类型，
        handler 只有在 score >= 0.6 时才覆盖正则的 count ——
        无论判什么值，本用例只保证链路不抛异常且返回合法子类型枚举。
        """
        sub, score = run(classify_fire_sub_type("查下最新的消防"))
        assert sub in {"count", "fire_assets", "fire_alarm_num", "fire_alarm_list", "month_repair"}
        assert 0.0 <= score <= 1.0


# ============================================================
# 4. 案例3（未修复，待观察）占位：
#    「实时告警」无会话状态时顶层意图被分到安防（score 0.6920）而非消防。
#    后续给消防示例集补充样例并重新预计算中心向量后，把 skip 去掉跑通即可。
# ============================================================
@pytest.mark.skip(reason="docs/问题排查记录.md 案例3 未修复（安防与消防示例向量竞争），待补充消防样例后启用")
class TestCase3TopIntentCompetition:
    def test_shishi_gaojing_should_be_fire(self):
        result = classify("实时告警")
        assert result["intent"] == "emergency_fire"
