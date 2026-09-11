"""
从《项目结构与接口文档.md》抽取的二级路由意图种子。

用途：
    1. 供 generate_intent_examples.py 无种子生成示例语料。
    2. 作为所有业务模块意图/子类型的权威定义。

结构约定：
    - 每个顶层 key 对应一个二级路由（即一个意图分类），命名以 _STATUS_EXAMPLES 结尾。
    - _desc 为该意图的总体描述。
    - 其他 key 为子类型（对应页面小模块/接口函数口径），value 为子类型描述。
"""

# ============================================================
# 1. 综合态势 /compositive
# ============================================================
COMPOSITIVE_OVERVIEW_STATUS_EXAMPLES = {
    "_desc": "综合态势总览：园区概况、设备健康度、人车数量、能耗趋势、会议统计、安全指数",
    "basic_info": "园区面积、IoT设备总数等基本信息",
    "device_health": "每类设备健康分、在线率、维修率、寿命率",
    "people_vehicle_num": "实时在园人数、车位总数/空闲/已占",
    "energy_consumption": "电/水当日值与趋势折线",
    "today_meet": "会议室总数/已预约/空闲/使用中的今日会议统计",
    "security_index": "综合安全指数分与各分项指标",
}

COMPOSITIVE_PERSONNEL_STATUS_EXAMPLES = {
    "_desc": "人员态势：今日态势、人员流动、人员结构、今日访客、来访时间段、被访部门、门禁通行记录",
    "today_affairs": "实时在场/进入/离开人数的今日态势",
    "person_flow": "人员流入/流出两条趋势曲线",
    "personnel_structure": "人员构成占比饼图",
    "today_visitor": "今日离开/预约/已到访/异常访客数",
    "visiting_period": "各时段预约到访量分布",
    "target_dep": "各部门被访量占比",
    "access_record": "门禁点位弹窗：进出方向、开门成败、时间设备",
}

COMPOSITIVE_VEHICLE_STATUS_EXAMPLES = {
    "_desc": "车辆态势：停车位、车流量、停车结构、公车统计、停车时长排名",
    "parking_space": "车位状态占比与车流量数据",
    "traffic_volume": "进出车流量趋势",
    "parking_structure": "停车类型/时长结构占比",
    "car_statistic": "公车总数/使用中/在司/外出，按公司切Tab",
    "parking_rank": "超时停留率、平均停车时长与车牌排名表",
}

COMPOSITIVE_ENERGY_STATUS_EXAMPLES = {
    "_desc": "能源态势：总体能耗、表具设备、用电/用水排名、设备状态、实时用电/用水",
    "overall_energy": "电/水年度累计与今日用量",
    "metering_equipment": "电表/水表设备数量",
    "electricity_rank": "用电单位/区域排名",
    "water_rank": "用水单位/区域排名",
    "device_status": "能耗设备在线/离线数",
    "realtime_electricity": "今日vs昨日用电曲线",
    "realtime_water": "今日vs昨日用水曲线",
}

COMPOSITIVE_EVENT_STATUS_EXAMPLES = {
    "_desc": "事件态势：事件工单、事件一览、工单统计、工单趋势、工单效率",
    "event_ticket": "事件工单列表：状态/标题/时间/内容/处理人",
    "event_overview": "事件总览，可带抓拍图/位置/设备",
    "work_statis": "日/周/月工单量统计卡",
    "work_trend": "工单热力图与趋势线",
    "work_efficiency": "各工单类型处理效率占比",
}

# ============================================================
# 2. 安全应急 /emergency
# ============================================================
EMERGENCY_SECURITY_STATUS_EXAMPLES = {
    "_desc": "安防态势：综合安全指数、视频监控、AI告警态势、巡查任务、巡查趋势、AI巡查事件、设备详情",
    "security_index": "综合安全指数分与各分项指标",
    "video": "监控视频宫格，点击/POI弹窗播放",
    "ai_alert_situation": "各AI告警类别数量与占比",
    "patrol_mission": "近一周巡查任务：待处理/已处理/异常",
    "inspection_trend": "巡查正常/异常趋势折线",
    "ai_inspection_events": "AI巡查事件列表：类型/等级/图片/位置",
    "device_detail": "摄像头/设备档案详情",
}

EMERGENCY_FIRE_STATUS_EXAMPLES = {
    "_desc": "消防态势：消防设备台账、设备告警统计、实时告警、月度报修",
    "fire_assets": "消防设备台账：状态/压力液位/电量/倾角",
    "fire_alarm_num": "火警/故障/隐患/漏报/离人告警分类统计",
    "fire_alarm_list": "实时消防告警列表",
    "month_repair": "月度报修趋势",
}

EMERGENCY_PERIMETER_STATUS_EXAMPLES = {
    "_desc": "孪生周界：关键指标、防区一览、周界告警统计、告警一览",
    "key_metrics": "在线/离线防区设备数、今日告警数",
    "area_overview": "防区列表与布防状态",
    "perimeter_alarm_stats": "告警按区域/时段统计与小时曲线",
    "alarm_overview": "周界告警列表，点击看抓拍照片",
}

EMERGENCY_COMMAND_STATUS_EXAMPLES = {
    "_desc": "孪生指挥：应急等级、应急资源、实时告警、门禁/广播控制",
    "emergency_level": "在园人数/今日报警/实时人数/资源可用性",
    "emergency_resource": "应急资源清单与消防设施可用/已用",
    "real_time_warn": "实时告警列表（消防/安防Tab）",
    "access_broadcast_control": "一键开门禁、广播喊话，联动3D动画",
}

EMERGENCY_AIALERT_STATUS_EXAMPLES = {
    "_desc": "AI预警：告警总览、分类告警统计、告警态势、告警列表",
    "alarm_view": "今日/累计告警、算法类型数、准确率",
    "class_alarm_statistic": "各分类告警占比饼图",
    "alert_situation": "近7/近30天告警趋势",
    "alarm_list_with_type": "安防/管理/环境预警分类告警明细",
}

# ============================================================
# 3. 智能运营 /operation
# ============================================================
OPERATION_DINING_STATUS_EXAMPLES = {
    "_desc": "食堂管理：菜谱、菜品热度排行、就餐人数统计",
    "recipes": "本周早中晚餐谱（菜名+价格）",
    "dish_popularity": "菜品销量/好评/点赞TOP5",
    "dining_count": "三餐各时段就餐人数曲线",
}

OPERATION_MEETING_STATUS_EXAMPLES = {
    "_desc": "会议管理：会议统计、高频会议室、会议室一览、会议数量、会议室状态、当日会议",
    "meeting_statistics": "预约数/平均时长与环比涨跌",
    "high_freq_meeting_rooms": "使用频率最高的会议室排行TOP5",
    "meeting_room_overview": "各会议室使用情况饼图",
    "number_of_meetings": "会议数量趋势与状态/类型分布",
    "meeting_room_status": "各会议室使用/空闲状态",
    "room_meet_list_by_day": "点击3D会议室弹当日会议安排",
}

OPERATION_TWINVISITORS_STATUS_EXAMPLES = {
    "_desc": "孪生访客：今日访客、来访时间段、被访部门、今日访客详情、访客总览",
    "day_visitor": "当日到访/未到访人数",
    "visiting_period": "各时段来访预约量",
    "visitor_depart": "单位→部门访客占比（动态Tab）",
    "day_visitor_detail": "访客明细：姓名/单位/时间/被访人/电话",
    "visitor_overview": "预约总量与部门被访分布",
}

# ============================================================
# 4. 孪生应用 /twins
# ============================================================
TWINS_INSPECTION_STATUS_EXAMPLES = {
    "_desc": "孪生巡检：今日巡检、今日任务列表、巡检统计、巡检执行状态",
    "today_inspection": "今日任务数/点位/完成率与图表",
    "today_tasks": "今日巡检任务：人员/类型/状态/班组/时间",
    "inspection_statistics": "平均时长/点位/隐患数（近1月/3月/1年）",
    "inspection_execution_status": "按人巡检正常/异常情况",
}

TWINS_MONITOR_STATUS_EXAMPLES = {
    "_desc": "孪生监控：设备类型统计、分区域统计、实/虚设备对比、监控总览",
    "equip_type": "各类型设备数量占比饼图",
    "statis_region": "分区域设备数量统计柱状图",
    "virtual_real_compare": "实/虚设备对比",
    "monitor_overview": "区域监控总览",
}

TWINS_TRACE_STATUS_EXAMPLES = {
    "_desc": "轨迹回溯：人脸搜索、人员模糊查询、人员详情",
    "face_search": "上传照片/选人人脸比对，返回相似记录",
    "user_fuzzy_query": "按姓名模糊搜人员列表",
    "user_info": "人员信息含照片，用于人脸搜索",
}

# ============================================================
# 5. 基础服务 /services
# ============================================================
SERVICES_INFORMATION_STATUS_EXAMPLES = {
    "_desc": "信息发布：信息发布一览、广播一览、任务执行趋势、节目数量趋势、信息发布设备、广播设备",
    "info_view": "信息发布一览：发布设备统计、类型占比、设备列表",
    "broadcast_view": "广播一览：广播设备在线/离线/占用统计",
    "task_trend": "发布任务执行量趋势",
    "program_count": "信息发布节目数量趋势",
    "info_publish_equip": "信息发布设备明细表",
    "broadcast_equip": "广播设备（近七天）明细，含当前任务",
}

SERVICES_DEVICE_STATUS_EXAMPLES = {
    "_desc": "设备态势：设备分类、类别健康度、统计区域、月度维修/报修、安防/广播/门禁在线率",
    "equip_class": "各设备类别数量占比",
    "category_health": "消防/空调/能耗等类别健康度",
    "statis_region": "分区域设备统计柱状图",
    "month_maintenance": "月度维修量趋势",
    "month_repair": "月度报修量趋势",
    "anfang_online": "安防设备在线率统计",
    "gb_online": "广播设备在线率统计",
    "mj_online": "门禁设备在线率统计",
}

SERVICES_NETWORK_STATUS_EXAMPLES = {
    "_desc": "网络管理：网络设备监控、AP监控、流量趋势、网络综合信息、网络告警、交换机流量、无线接入、上网行为",
    "device_monitor": "网络设备CPU/内存/温度",
    "wireless_ap": "AP状态/位置/信号",
    "traffic_trend": "流量趋势（日/周/月）",
    "network_basic_info": "网络设备在线/离线数",
    "network_alarm": "网络设备告警列表",
    "switch_flow": "核心交换机上/下行流量与总带宽",
    "wireless_access": "AP在线数/终端数/高负载/平均信号",
    "online_behavior": "在线用户数、设备接入数",
}

# ============================================================
# 6. 安全监控 /security/monitor（新增 · 未挂路由）
# ============================================================
SECURITY_MONITOR_STATUS_EXAMPLES = {
    "_desc": "安全监控：监控概览、实时告警列表、设备在线率、告警趋势、监控设备列表",
    "monitor_overview": "摄像头在线/离线/异常、告警总量/待处理",
    "alarm_list": "实时告警分页列表",
    "device_online_rate": "各区域设备在线/离线率",
    "alarm_trend": "告警量/已处理/未处理趋势",
    "monitor_device_list": "监控设备分页列表（搜索/筛选）",
}


# ============================================================
# 聚合导出（供生成脚本遍历）
# ============================================================
ALL_INTENT_SEEDS = {
    # 综合态势
    "COMPOSITIVE_OVERVIEW_STATUS_EXAMPLES": COMPOSITIVE_OVERVIEW_STATUS_EXAMPLES,
    "COMPOSITIVE_PERSONNEL_STATUS_EXAMPLES": COMPOSITIVE_PERSONNEL_STATUS_EXAMPLES,
    "COMPOSITIVE_VEHICLE_STATUS_EXAMPLES": COMPOSITIVE_VEHICLE_STATUS_EXAMPLES,
    "COMPOSITIVE_ENERGY_STATUS_EXAMPLES": COMPOSITIVE_ENERGY_STATUS_EXAMPLES,
    "COMPOSITIVE_EVENT_STATUS_EXAMPLES": COMPOSITIVE_EVENT_STATUS_EXAMPLES,
    # 安全应急
    "EMERGENCY_SECURITY_STATUS_EXAMPLES": EMERGENCY_SECURITY_STATUS_EXAMPLES,
    "EMERGENCY_FIRE_STATUS_EXAMPLES": EMERGENCY_FIRE_STATUS_EXAMPLES,
    "EMERGENCY_PERIMETER_STATUS_EXAMPLES": EMERGENCY_PERIMETER_STATUS_EXAMPLES,
    "EMERGENCY_COMMAND_STATUS_EXAMPLES": EMERGENCY_COMMAND_STATUS_EXAMPLES,
    "EMERGENCY_AIALERT_STATUS_EXAMPLES": EMERGENCY_AIALERT_STATUS_EXAMPLES,
    # 智能运营
    "OPERATION_DINING_STATUS_EXAMPLES": OPERATION_DINING_STATUS_EXAMPLES,
    "OPERATION_MEETING_STATUS_EXAMPLES": OPERATION_MEETING_STATUS_EXAMPLES,
    "OPERATION_TWINVISITORS_STATUS_EXAMPLES": OPERATION_TWINVISITORS_STATUS_EXAMPLES,
    # 孪生应用
    "TWINS_INSPECTION_STATUS_EXAMPLES": TWINS_INSPECTION_STATUS_EXAMPLES,
    "TWINS_MONITOR_STATUS_EXAMPLES": TWINS_MONITOR_STATUS_EXAMPLES,
    "TWINS_TRACE_STATUS_EXAMPLES": TWINS_TRACE_STATUS_EXAMPLES,
    # 基础服务
    "SERVICES_INFORMATION_STATUS_EXAMPLES": SERVICES_INFORMATION_STATUS_EXAMPLES,
    "SERVICES_DEVICE_STATUS_EXAMPLES": SERVICES_DEVICE_STATUS_EXAMPLES,
    "SERVICES_NETWORK_STATUS_EXAMPLES": SERVICES_NETWORK_STATUS_EXAMPLES,
    # 安全监控（新增）
    "SECURITY_MONITOR_STATUS_EXAMPLES": SECURITY_MONITOR_STATUS_EXAMPLES,
}
