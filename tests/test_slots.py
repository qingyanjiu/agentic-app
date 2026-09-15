# -*- coding: utf-8 -*-
"""
全意图槽位抽取测试（正则层，不依赖 embedding 模型）。

覆盖 8 个域的 extract_xxx_slots：
  人员 / 安防 / 食堂 / 车辆 / 信息发布 / 能源 / 会议 / 消防
以及公共的 parse_time_slot（时间范围 / 模糊时间 / 未来时间）。
"""
import pytest

from agent.intent.slots import (
    parse_time_slot,
    extract_person_status_slots,
    extract_security_status_slots,
    extract_canteen_status_slots,
    extract_vehicle_status_slots,
    extract_information_status_slots,
    extract_energy_status_slots,
    extract_emergency_fire_slots,
    extract_meeting_status_slots,
    extract_device_status_slots,
    extract_compositive_overview_slots,
)


# ============================================================
# 人员态势
# ============================================================
class TestPersonSlots:
    EXTRACT = staticmethod(extract_person_status_slots)

    @pytest.mark.parametrize(
        "query,expected",
        [
            ("张三现在在哪里", "location"),
            ("李四的位置", "location"),
            ("看看张三上午的轨迹", "trace"),
            ("今天进入多少人", "enter"),
            ("今天离开多少人", "leave"),
            ("最近人员流动趋势怎么样", "flow"),
            ("园区人员结构分布", "structure"),
            ("现在园区有多少人", "realtime"),
            ("有没有异常人员", "abnormal"),
        ],
    )
    def test_query_type(self, query, expected):
        assert self.EXTRACT(query)["query_type"] == expected

    def test_person_name_extracted(self):
        slots = self.EXTRACT("张三现在在哪里")
        assert slots["person_name"] == "张三"

    def test_vague_query_falls_back_to_count(self):
        assert self.EXTRACT("查下人员")["query_type"] == "count"


# ============================================================
# 安防态势（槽位键是 event_type）
# ============================================================
class TestSecuritySlots:
    EXTRACT = staticmethod(extract_security_status_slots)

    @pytest.mark.parametrize(
        "query,expected",
        [
            ("园区安全指数是多少", "security_index"),
            ("今天的AI告警分布情况", "ai_alert"),
            ("本周巡查任务有哪些", "patrol"),
            ("这条告警的详情是什么", "alarm_detail"),
            ("摄像头设备状态怎么样", "device"),
            ("本周巡查趋势", "inspection_trend"),
            # emergency_aialert 并入的新子类型（开发计划 §2.4）
            ("AI告警总览帮我看一下", "ai_overview"),
            ("园区累计告警有多少条", "ai_overview"),
            ("系统识别准确率是多少", "ai_overview"),
            ("近30天的告警数量变化趋势", "ai_trend"),
            ("最近一周的告警走势如何", "ai_trend"),
            ("管理预警的明细列表有哪些", "ai_alarm_list"),
            ("环境预警最近有哪些记录", "ai_alarm_list"),
        ],
    )
    def test_event_type(self, query, expected):
        assert self.EXTRACT(query)["event_type"] == expected

    def test_default_is_alarm_list(self):
        # 兜底默认告警列表
        assert self.EXTRACT("看看安防情况")["event_type"] == "alarm_list"

    def test_alarm_id_extracted(self):
        assert self.EXTRACT("查一下告警 123 的详情")["alarm_id"] == "123"


# ============================================================
# 食堂管理（槽位键是 event_type）
# ============================================================
class TestCanteenSlots:
    EXTRACT = staticmethod(extract_canteen_status_slots)

    @pytest.mark.parametrize(
        "query,expected",
        [
            ("今天食堂就餐多少人", "dining_count"),
            ("本月菜品热度排行", "dish_rank"),
            ("本周菜单", "week_menu"),
            ("今天食堂有什么菜", "week_menu"),
        ],
    )
    def test_event_type(self, query, expected):
        assert self.EXTRACT(query)["event_type"] == expected

    def test_meal_slot(self):
        assert self.EXTRACT("本周午餐菜单")["meal"] == "午餐"

    def test_date_parsed_from_query(self):
        slots = self.EXTRACT("本周菜单")
        assert slots["date"]["time_type"] == "span"


# ============================================================
# 车辆态势
# ============================================================
class TestVehicleSlots:
    EXTRACT = staticmethod(extract_vehicle_status_slots)

    @pytest.mark.parametrize(
        "query,expected",
        [
            ("停车场还有多少空车位", "parking_space"),
            ("今天车流量多大", "traffic_flow"),
            ("园区停车结构分布", "parking_structure"),
            ("公车有多少辆", "official_vehicle"),
            ("本月停车时长排名", "parking_duration_rank"),
        ],
    )
    def test_query_type(self, query, expected):
        assert self.EXTRACT(query)["query_type"] == expected


# ============================================================
# 信息发布（槽位键是 event_type）
# ============================================================
class TestInformationSlots:
    EXTRACT = staticmethod(extract_information_status_slots)

    @pytest.mark.parametrize(
        "query,expected",
        [
            ("本周信息发布任务趋势", "task_trend"),
            ("广播设备明细", "broadcast_equip"),
            ("信息发布设备明细", "info_equip"),
            ("本月节目数量趋势", "program_count"),
            ("广播在线情况", "broadcast_view"),
            ("信息发布统计", "info_view"),
        ],
    )
    def test_event_type(self, query, expected):
        assert self.EXTRACT(query)["event_type"] == expected


# ============================================================
# 能源态势
# ============================================================
class TestEnergySlots:
    EXTRACT = staticmethod(extract_energy_status_slots)

    @pytest.mark.parametrize(
        "query,expected",
        [
            ("水表有多少个", "metering_equipment"),
            ("能耗设备在线状态", "device_status"),
            ("今日实时用电曲线", "realtime_electricity"),
            ("今日实时用水曲线", "realtime_water"),
            ("本月各楼层用电排名", "electricity_rank"),
            ("本月用水排名", "water_rank"),
            ("园区总体能耗情况", "overall_energy"),
        ],
    )
    def test_query_type(self, query, expected):
        assert self.EXTRACT(query)["query_type"] == expected


# ============================================================
# 会议管理（槽位键是 event_type）
# ============================================================
class TestMeetingSlots:
    EXTRACT = staticmethod(extract_meeting_status_slots)

    @pytest.mark.parametrize(
        "query,expected",
        [
            ("今天有什么会议安排", "room_meet_list_by_day"),
            ("哪个会议室使用最多", "high_freq_meeting_rooms"),
            ("会议室使用状态", "meeting_room_status"),
            ("会议室一览", "meeting_room_overview"),
            ("本月会议数量趋势", "number_of_meetings"),
            ("平均会议时长是多少", "meeting_statistics"),
        ],
    )
    def test_event_type(self, query, expected):
        assert self.EXTRACT(query)["event_type"] == expected


# ============================================================
# 消防态势
# docs/问题排查记录.md 案例2 的关键路径：
#   笼统问法在正则层必须落到 count 兜底（再由 graph 反问）
# ============================================================
class TestFireSlots:
    EXTRACT = staticmethod(extract_emergency_fire_slots)

    @pytest.mark.parametrize(
        "query,expected",
        [
            # 案例2 的触发问法：正则抽不到子类型，落 count 兜底
            ("查下最新的消防", "count"),
            ("实时告警", "fire_alarm_list"),
            ("最新的消防告警列表", "fire_alarm_list"),
            ("有多少火警", "fire_alarm_num"),
            ("本月故障数是多少", "fire_alarm_num"),
            ("灭火器台账", "fire_assets"),
            ("消防水压情况", "fire_assets"),
            ("本月报修数量", "month_repair"),
        ],
    )
    def test_query_type(self, query, expected):
        assert self.EXTRACT(query)["query_type"] == expected

    def test_aska_option_words_all_hit_regex(self):
        """
        案例2 教训：反问的选项措辞要与 slots.py 正则关键词对齐，
        用户照着念就能被识别。反问句里的 4 个选项词逐一验证。
        """
        from graph.emergency_fire_langgraph import _JAVA_TOOL_MAP


        for option in ["设备台账", "告警统计", "实时告警", "月度报修"]:
            qt = self.EXTRACT(option)["query_type"]
            assert qt in _JAVA_TOOL_MAP, (
                f"反问选项「{option}」抽到 query_type={qt}，不在消防 _JAVA_TOOL_MAP 中，"
                f"用户回复该选项将无法识别"
            )


# ============================================================
# 设备态势
# ============================================================
class TestDeviceSlots:
    EXTRACT = staticmethod(extract_device_status_slots)

    @pytest.mark.parametrize(
        "query,expected",
        [
            ("设备分类占比", "equip_class"),
            ("各类设备数量占比", "equip_class"),
            ("设备健康度怎么样", "category_health"),
            ("消防设备健康度", "category_health"),
            ("月度维修趋势", "month_maintenance"),
            ("本月维修数量", "month_maintenance"),
            ("月度报修趋势", "month_repair"),
            ("本月报修数量", "month_repair"),
            ("各区域设备数量", "statis_region"),
            ("设备区域分布", "statis_region"),
            ("安防设备在线率", "anfang_online"),
            ("监控在线率多少", "anfang_online"),
            ("广播设备在线率", "gb_online"),
            ("门禁设备在线率", "mj_online"),
            ("门禁在线率", "mj_online"),
        ],
    )
    def test_query_type(self, query, expected):
        assert self.EXTRACT(query)["query_type"] == expected


# ============================================================
# 综合态势总览
# 语料红线（docs/开发计划.md §2.3）：
#   人车/能耗/会议/安全指数问法不进本模块，由既有域承接；
#   device_health 只收"健康度/健康分"总览口径
# ============================================================
class TestCompositiveOverviewSlots:
    EXTRACT = staticmethod(extract_compositive_overview_slots)

    @pytest.mark.parametrize(
        "query,expected",
        [
            ("园区面积多大", "basic_info"),
            ("园区占地面积多少", "basic_info"),
            ("园区概况", "basic_info"),
            ("IoT设备总数多少", "basic_info"),
            ("园区设备总数", "basic_info"),
            ("设备健康度总览", "device_health"),
            ("设备健康分是多少", "device_health"),
            ("设备整体健康情况", "device_health"),
        ],
    )
    def test_query_type(self, query, expected):
        assert self.EXTRACT(query)["query_type"] == expected


# ============================================================
# 公共时间解析 parse_time_slot
# ============================================================
class TestTimeSlot:
    def test_near_n_days(self):
        slots = parse_time_slot("近两天")
        assert slots["time_type"] == "span"
        assert slots["raw"] == "近2天"

    def test_this_week(self):
        slots = parse_time_slot("本周")
        assert slots["time_type"] == "span"
        assert slots["raw"] == "本周"

    def test_yesterday(self):
        slots = parse_time_slot("昨天")
        assert slots["time_type"] == "span"
        assert slots["raw"] == "昨天"

    def test_vague_time(self):
        slots = parse_time_slot("最近的人员流动")
        assert slots["time_type"] == "vague"
        assert slots["start_time"] is None
        assert "近三天" in slots["options"]

    def test_future_time(self):
        slots = parse_time_slot("明天")
        assert slots["time_type"] == "future"

    def test_no_time_falls_back_to_today(self):
        slots = parse_time_slot("实时告警")
        assert slots["time_type"] == "span"
        assert slots["raw"] == "今天"
