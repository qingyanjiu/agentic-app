#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
意图分类评测脚本（不启动 app，直接考分类器）
================================================
用途：
    语料扩写/边界手术的验收标尺。直接加载 agent/intent/classifier.py 里的
    模型与语料，对一组带「期望意图」的用例逐条分类，输出通过率报告。

用法（容器内，项目根目录）：
    # 模型路径与 app 保持一致（app 怎么设这里就怎么设）
    export INTENT_MODEL_PATH=/root/agentic-app/agent/intent/models/BAAI_bge-small-zh-v1.5

    python tools/eval_intent.py              # 全量跑
    python tools/eval_intent.py energy       # 只跑 intent 名含 "energy" 的用例
    python tools/eval_intent.py boundary     # 只跑边界锁用例（按标签过滤）
    python tools/eval_intent.py incident     # 只跑翻车句用例
    python tools/eval_intent.py negative     # 只跑域外负样本（应判 other）
    python tools/eval_intent.py adversarial  # 只跑对抗鲁棒用例

退出码（2026-09-23 起分级门禁，见 docs/测试方案.md §2）：
    normal/boundary/incident  逐条硬门禁：任一失败 → 1
    negative                  误响应率 <= NEG_MAX_FAIL_RATE（默认 0.05）→ 0，否则 1
    adversarial               通过率   >= ADV_MIN_PASS_RATE（默认 0.90）→ 0，否则 1
    门槛可用环境变量覆盖，如：NEG_MAX_FAIL_RATE=0.08 python tools/eval_intent.py negative

用例标签说明：
    normal      各域常规问法，扩写前就应当大部分通过
    boundary    边界锁：2026-09-17 拍板的 8 条跨域归属规则（裸报修归设备域、
                裸设备清单归 device_query、实时告警归消防、今日用电归 overall 等）。
                这些用例代表「目标状态」——边界手术/扩写完成前失败是预期内的，
                手术完成后必须全绿，之后任何语料改动不得使其回退
    incident    线上真实翻车句（如 2026-09-16 「今天园区能耗情况怎么样？」0.6471
                掉兜底），扩写完成后目标分数 >= 0.70
    negative    域外负样本：闲聊/域外业务/开放问答/控制指令/易混操作话术，
                期望全部判 other。失败即「误响应」（系统把管不了的问题接进了
                某个业务域）。首跑先拿基线，超 5% 再决定压阈值还是补兜底策略
    adversarial 对抗鲁棒：错别字/标点空格/语气词/中英混杂/长度极端的加噪变体，
                期望仍判原意图。失败说明域中心对词面噪声敏感，考虑补同形变体语料

注意：
    预期子类型（sub）只在用例里显式给出时才校验；没给就只看顶层意图。
"""

import os
import sys
import logging
import importlib.util
from collections import defaultdict

# ------------------------------------------------------------
# 加载 classifier.py（按文件路径加载，绕开 agent.intent 包级 __init__，
# 避免连带导入 handlers/slots 等无关重依赖）
# ------------------------------------------------------------
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLASSIFIER_PATH = os.path.join(ROOT, "agent", "intent", "classifier.py")


def load_classifier():
    if not os.path.exists(CLASSIFIER_PATH):
        print(f"[ERROR] 找不到分类器文件: {CLASSIFIER_PATH}")
        sys.exit(2)
    spec = importlib.util.spec_from_file_location("intent_classifier_eval", CLASSIFIER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------
# 评测集
# c(问法, 期望顶层意图, 期望子类型=None, 标签="normal")
# ------------------------------------------------------------
def c(query, intent, sub=None, tag="normal"):
    return (query, intent, sub, tag)


CASES = [
    # ================= 人员态势 person_status =================
    c("张三现在在哪", "person_status", "location"),
    c("查一下李四今天的轨迹", "person_status", "trace"),
    c("现在园区有多少人", "person_status", "realtime"),
    c("今天进入园区多少人", "person_status", "enter"),
    c("今天离开多少人", "person_status", "leave"),
    c("人员流动趋势怎么样", "person_status", "flow"),
    c("园区人员结构占比", "person_status", "structure"),
    c("最近有没有异常人员", "person_status", "abnormal"),

    # ================= 安防态势 security_status =================
    c("今天园区有哪些告警", "security_status", "alarm_list", "boundary"),  # 规则4：泛告警归安防
    c("帮我查一下上周的安防告警记录", "security_status", "alarm_list"),
    c("保安现在巡逻情况怎么样", "security_status", "patrol"),
    c("能不能调一下园区大门口的监控", "security_status", "video"),
    c("今天都有哪些人刷了门禁", "security_status", "access"),
    c("园区今天的综合安全指数是多少", "security_status", "security_index"),
    c("最近AI巡查发现了哪些异常", "security_status", "ai_inspection"),
    c("近一周的告警趋势怎么样", "security_status", "ai_trend"),

    # ================= 食堂管理 canteen_status =================
    c("本周食堂菜谱有哪些", "canteen_status", "week_menu"),
    c("这周中午吃什么", "canteen_status", "week_menu"),
    c("本月菜品热度排行", "canteen_status", "dish_rank"),
    c("哪个菜最受欢迎", "canteen_status", "dish_rank"),
    c("今天食堂就餐人数多少", "canteen_status", "dining_count"),
    c("今天有多少人吃饭", "canteen_status", "dining_count"),  # 与人员在园人数的混淆对
    c("食堂人多吗", "canteen_status", "dining_count"),

    # ================= 车辆态势 vehicle_status =================
    c("停车场还有多少空位", "vehicle_status", "parking_space"),
    c("今天园区车流量多大", "vehicle_status", "traffic_flow"),
    c("停车结构分布", "vehicle_status", "parking_structure"),
    c("公务车有多少", "vehicle_status", "official_vehicle"),
    c("停车时长排名", "vehicle_status", "parking_duration_rank"),
    c("园区车辆统计", "vehicle_status", "count"),
    c("停车场监控画面能看吗", "vehicle_status", "parking_space"),

    # ================= 信息发布 information_status =================
    c("信息发布情况怎么样", "information_status", "info_view"),
    c("广播设备一览", "information_status", "broadcast_view"),
    c("信息发布任务执行趋势", "information_status", "task_trend"),
    c("节目数量统计", "information_status", "program_count"),
    c("信息发布屏列表", "information_status", "info_equip"),
    c("查一下广播终端", "information_status", "broadcast_equip"),

    # ================= 能源态势 energy_status =================
    c("今天园区能耗情况怎么样？", "energy_status", None, "incident"),  # 2026-09-16 翻车句 0.6471
    c("今日用电量", "energy_status", "overall_energy", "incident"),    # 用户实测走进 realtime 的翻车句
    c("今天用水多少", "energy_status", "overall_energy", "boundary"),  # 规则8：今日归 overall
    c("园区年度用电情况", "energy_status", "overall_energy"),
    c("用电排名前十的单位", "energy_status", "electricity_rank"),
    c("有多少个智能水表", "energy_status", "metering_equipment"),
    c("能耗设备在线情况", "energy_status", "device_status", "boundary"),  # 规则7：带能耗限定
    c("本周和上周用电对比", "energy_status", "realtime_electricity", "boundary"),  # 规则8：周对比归 realtime
    c("本周用水趋势怎么样", "energy_status", "realtime_water", "boundary"),

    # ================= 会议管理 meeting_status =================
    c("今天有多少会议", "meeting_status", "number_of_meetings"),
    c("会议预约统计", "meeting_status", "meeting_statistics"),
    c("哪个会议室用得最多", "meeting_status", "high_freq_meeting_rooms"),
    c("会议室一览", "meeting_status", "meeting_room_overview"),
    c("现在有哪些空会议室", "meeting_status", "meeting_room_status"),
    c("今日会议安排", "meeting_status", "room_meet_list_by_day"),

    # ================= 消防态势 emergency_fire =================
    c("实时告警", "emergency_fire", "fire_alarm_list", "incident"),  # 案例3：裸实时告警归消防（规则4）
    c("消防设备台账", "emergency_fire", "fire_assets"),
    c("本月火警数量统计", "emergency_fire", "fire_alarm_num"),
    c("消防告警列表", "emergency_fire", "fire_alarm_list"),
    c("消防本月报修了多少", "emergency_fire", "month_repair", "boundary"),  # 规则2：带消防限定归消防
    c("消防态势怎么样", "emergency_fire", "count"),

    # ================= 周界态势 emergency_perimeter =================
    c("周界关键指标", "emergency_perimeter", "key_metrics"),
    c("防区一览", "emergency_perimeter", "area_overview"),
    c("本周周界告警统计", "emergency_perimeter", "perimeter_alarm_stats"),
    c("最新的周界告警", "emergency_perimeter", "alarm_overview"),
    c("有多少防区在线", "emergency_perimeter", "key_metrics"),
    c("周界态势怎么样", "emergency_perimeter", "count"),

    # ================= 设备态势 device_status =================
    c("园区设备分类占比", "device_status", "equip_class", "boundary"),  # 规则1：统计口径归设备态势
    c("消防设备健康度怎么样", "device_status", "category_health", "boundary"),  # 规则3：带类别词
    c("月度报修趋势", "device_status", "month_repair", "boundary"),  # 规则2：裸报修归设备域
    c("本月维修了多少次", "device_status", "month_maintenance"),
    c("各区域设备数量统计", "device_status", "statis_region"),
    c("安防设备在线率", "device_status", "anfang_online"),
    c("门禁设备在线率", "device_status", "mj_online"),
    c("广播设备在线率", "device_status", "gb_online", "boundary"),  # 规则6：在线率归设备域
    c("设备态势整体情况", "device_status", "count"),

    # ================= 综合态势总览 compositive_overview =================
    c("园区面积多大", "compositive_overview", "basic_info"),
    c("园区概况", "compositive_overview", "basic_info"),
    c("设备健康度怎么样", "compositive_overview", "device_health", "boundary"),  # 规则3：裸健康问法归总览
    c("设备健康分是多少", "compositive_overview", "device_health", "boundary"),
    c("IoT设备总数多少", "compositive_overview", "basic_info"),

    # ================= 孪生巡检 twins_inspection =================
    c("今日巡检完成率", "twins_inspection", "today_inspection", "boundary"),  # 规则5：巡检归孪生
    c("今天的巡检任务有哪些", "twins_inspection", "today_tasks"),
    c("近一月巡检统计", "twins_inspection", "inspection_statistics"),
    c("各巡检员执行情况", "twins_inspection", "inspection_execution_status"),
    c("孪生巡检总览", "twins_inspection", "today_inspection"),

    # ================= 设备查询 device_query =================
    c("园区有哪些设备", "device_query", "device_list", "boundary"),  # 规则1：裸清单归设备查询
    c("查一下A栋的设备", "device_query", "device_list"),
    c("监控设备列表", "device_query", "device_list"),
    c("门禁设备清单", "device_query", "device_list"),
    c("查一下设备档案", "device_query", "device_detail"),
    c("看下B栋道闸的详情", "device_query", "device_detail"),

    # ================= 域外负样本 negative（期望 other，2026-09-23 测试方案 §2） =================
    # —— 闲聊寒暄 ——
    c("你好", "other", None, "negative"),
    c("您好呀", "other", None, "negative"),
    c("在吗", "other", None, "negative"),
    c("讲个笑话", "other", None, "negative"),
    c("你叫什么名字", "other", None, "negative"),
    c("你能做什么", "other", None, "negative"),
    c("早上好", "other", None, "negative"),
    c("晚安", "other", None, "negative"),
    c("谢谢你的帮助", "other", None, "negative"),
    c("你真聪明", "other", None, "negative"),
    c("今天心情不错", "other", None, "negative"),
    c("陪我聊聊天", "other", None, "negative"),
    c("你几岁了", "other", None, "negative"),
    c("你是机器人吗", "other", None, "negative"),
    c("给我唱首歌", "other", None, "negative"),
    c("你喜欢什么颜色", "other", None, "negative"),
    c("会说方言吗", "other", None, "negative"),
    c("你吃饭了吗", "other", None, "negative"),
    c("给我讲个冷笑话", "other", None, "negative"),
    c("辛苦啦", "other", None, "negative"),

    # —— 域外业务（生活/办公场景，园区态势系统管不了） ——
    c("今天天气怎么样", "other", None, "negative"),
    c("明天会下雨吗", "other", None, "negative"),
    c("帮我订一张去北京的机票", "other", None, "negative"),
    c("附近有什么好吃的餐厅", "other", None, "negative"),
    c("帮我叫一辆出租车", "other", None, "negative"),
    c("最近有什么电影推荐", "other", None, "negative"),
    c("帮我查一下快递单号", "other", None, "negative"),
    c("现在美元汇率是多少", "other", None, "negative"),
    c("帮我设个明天早上八点的闹钟", "other", None, "negative"),
    c("附近哪里有加油站", "other", None, "negative"),
    c("帮我写一篇周报", "other", None, "negative"),
    c("把这段话翻译成英文", "other", None, "negative"),
    c("今天股市行情怎么样", "other", None, "negative"),
    c("帮我查一下火车票", "other", None, "negative"),
    c("推荐几本好书", "other", None, "negative"),
    c("帮我预约一个牙医", "other", None, "negative"),
    c("我的外卖到哪了", "other", None, "negative"),
    c("帮我充一下话费", "other", None, "negative"),
    c("查一下我的银行卡余额", "other", None, "negative"),
    c("帮我抢一张演唱会门票", "other", None, "negative"),
    c("最近的新闻头条有哪些", "other", None, "negative"),
    c("今天恒生指数收盘多少", "other", None, "negative"),
    c("帮我导航到市中心", "other", None, "negative"),
    c("明天限行尾号是多少", "other", None, "negative"),

    # —— 开放问答（知识通识，不属于园区态势域） ——
    c("床前明月光的作者是谁", "other", None, "negative"),
    c("地球到月球有多远", "other", None, "negative"),
    c("长城有多长", "other", None, "negative"),
    c("人的正常体温是多少", "other", None, "negative"),
    c("一年有多少天", "other", None, "negative"),
    c("中国有多少个省份", "other", None, "negative"),
    c("光速是多少", "other", None, "negative"),
    c("水的沸点是多少", "other", None, "negative"),
    c("大熊猫吃什么", "other", None, "negative"),
    c("圆周率是多少", "other", None, "negative"),
    c("世界上最高的山是哪座", "other", None, "negative"),
    c("恐龙为什么灭绝了", "other", None, "negative"),
    c("太阳系有几大行星", "other", None, "negative"),
    c("量子力学是什么", "other", None, "negative"),
    c("怎么快速学英语", "other", None, "negative"),
    c("蜂蜜会不会过期", "other", None, "negative"),
    c("企鹅生活在哪里", "other", None, "negative"),
    c("为什么先看到闪电后听到雷声", "other", None, "negative"),

    # —— 控制指令（系统只查不控） ——
    c("打开音乐", "other", None, "negative"),
    c("把空调调到二十六度", "other", None, "negative"),
    c("帮我开灯", "other", None, "negative"),
    c("关闭走廊的灯", "other", None, "negative"),
    c("把大屏关掉", "other", None, "negative"),
    c("放一首歌", "other", None, "negative"),
    c("把音量调大一点", "other", None, "negative"),
    c("帮我打开窗户", "other", None, "negative"),
    c("把摄像头转个方向", "other", None, "negative"),
    c("把门禁打开", "other", None, "negative"),
    c("帮我关掉广播", "other", None, "negative"),
    c("把空调打开", "other", None, "negative"),
    c("帮我把周界报警声音关一下", "other", None, "negative"),

    # —— 相邻域易混操作（词面沾业务域，但系统无此能力，最难的陷阱） ——
    c("帮我把这条告警处理掉", "other", None, "negative"),
    c("把这条告警标记为已处理", "other", None, "negative"),
    c("帮我预定明天下午的会议室", "other", None, "negative"),
    c("帮我预约下周三的会议室", "other", None, "negative"),
    c("给张三发一条消息", "other", None, "negative"),
    c("帮我通知所有人开会", "other", None, "negative"),
    c("帮我导出全部报表", "other", None, "negative"),
    c("把周报发给领导", "other", None, "negative"),
    c("帮我审批一下请假申请", "other", None, "negative"),
    c("帮我报修一下空调", "other", None, "negative"),
    c("给我开个门禁权限", "other", None, "negative"),
    c("帮新员工登记一下入职信息", "other", None, "negative"),
    c("帮我充值饭卡", "other", None, "negative"),
    c("取消今天的会议室预订", "other", None, "negative"),
    c("帮我修改会议室预订时间", "other", None, "negative"),
    c("把这周的巡检任务改到明天", "other", None, "negative"),
    c("帮我查一下社保缴纳记录", "other", None, "negative"),
    c("附近药店哪家近", "other", None, "negative"),
    c("今天穿什么衣服合适", "other", None, "negative"),
    c("帮我算一下房贷利率", "other", None, "negative"),
    c("提醒我下午三点开会", "other", None, "negative"),
    c("写一首关于春天的诗", "other", None, "negative"),
    c("晚上一起打游戏吗", "other", None, "negative"),
    c("最近房价走势如何", "other", None, "negative"),
    c("帮我查一下航班动态", "other", None, "negative"),
    c("帮我代订一份午餐", "other", None, "negative"),
    c("帮我点一份外卖送到园区", "other", None, "negative"),
    c("帮我申请一个固定车位", "other", None, "negative"),

    # ================= 对抗鲁棒 adversarial（期望仍判原意图，2026-09-23 测试方案 §2） =================
    # —— 错别字/同音字 ——
    c("张三现在再哪", "person_status", "location", "adversarial"),          # 在→再
    c("查下李四的轨际", "person_status", "trace", "adversarial"),           # 轨迹→轨际
    c("今天园区油多少人", "person_status", "realtime", "adversarial"),      # 有→油
    c("停车场还有多少控位", "vehicle_status", "parking_space", "adversarial"),  # 空→控
    c("本月菜品热渡排行", "canteen_status", "dish_rank", "adversarial"),    # 度→渡
    c("消防舍备台账", "emergency_fire", "fire_assets", "adversarial"),      # 设备→舍备
    c("用电排明前十的单位", "energy_status", "electricity_rank", "adversarial"),  # 名→明
    c("门禁设备再线率", "device_status", "mj_online", "adversarial"),       # 在→再
    c("园区概阔", "compositive_overview", "basic_info", "adversarial"),     # 况→阔
    c("今日寻检完成率", "twins_inspection", "today_inspection", "adversarial"),  # 巡→寻
    c("申防设备在线率", "device_status", "anfang_online", "adversarial"),   # 安→申
    c("会仪室一览", "meeting_status", "meeting_room_overview", "adversarial"),  # 议→仪
    c("今日能牦情况", "energy_status", None, "adversarial"),                # 耗→牦

    # —— 标点/空格/全半角 ——
    c("今 天 园 区 有 多 少 人", "person_status", "realtime", "adversarial"),
    c("停车场还有多少空位？", "vehicle_status", "parking_space", "adversarial"),
    c("用电排名。", "energy_status", "electricity_rank", "adversarial"),
    c("！实时告警", "emergency_fire", "fire_alarm_list", "adversarial"),
    c("园区概况～", "compositive_overview", "basic_info", "adversarial"),
    c("今日会议安排?", "meeting_status", "room_meet_list_by_day", "adversarial"),
    c(",,人员流动趋势", "person_status", "flow", "adversarial"),
    c("消防设备台账!!", "emergency_fire", "fire_assets", "adversarial"),
    c("车位监控。。", "vehicle_status", "parking_space", "adversarial"),
    c("信息发布一览——", "information_status", "info_view", "adversarial"),

    # —— 语气词后缀 ——
    c("现在有多少人呀", "person_status", "realtime", "adversarial"),
    c("帮我看下能耗哈", "energy_status", None, "adversarial"),
    c("用电排名呗", "energy_status", "electricity_rank", "adversarial"),
    c("会议室一览嘛", "meeting_status", "meeting_room_overview", "adversarial"),
    c("消防态势哦", "emergency_fire", "count", "adversarial"),
    c("园区概况呢", "compositive_overview", "basic_info", "adversarial"),
    c("巡检完成率呀", "twins_inspection", "today_inspection", "adversarial"),
    c("广播一览啦", "information_status", "broadcast_view", "adversarial"),
    c("车流量多大啊", "vehicle_status", "traffic_flow", "adversarial"),
    c("就餐人数多少呢", "canteen_status", "dining_count", "adversarial"),
    c("安防告警记录看一下哈", "security_status", "alarm_list", "adversarial"),

    # —— 中英混杂 ——
    c("查一下 meeting room 有几间空", "meeting_status", "meeting_room_status", "adversarial"),
    c("iot设备总数多少", "compositive_overview", "basic_info", "adversarial"),
    c("帮我看下 energy 能耗情况", "energy_status", None, "adversarial"),
    c("停车场 parking 还有多少空位", "vehicle_status", "parking_space", "adversarial"),
    c("今天的 AI 告警有多少", "security_status", None, "adversarial"),
    c("看下 park 停车时长排名", "vehicle_status", "parking_duration_rank", "adversarial"),
    c("设备 health 健康度怎么样", "compositive_overview", "device_health", "adversarial"),
    c("今日 attendance 就餐人数", "canteen_status", "dining_count", "adversarial"),

    # —— 长度极端 ——
    c("能耗", "energy_status", None, "adversarial"),
    c("巡检", "twins_inspection", None, "adversarial"),
    c("麻烦你帮我看看今天就是我们园区里面这个用电量大概是多少然后情况怎么样", "energy_status", None, "adversarial"),
    c("我想了解一下就是今天上午园区这边人员进来的数量大概是多少人来", "person_status", "enter", "adversarial"),
    c("帮我看看停车场现在还有没有空着的位置可以用来说一下剩余车位情况", "vehicle_status", "parking_space", "adversarial"),
    c("请帮我查询一下本周食堂的菜谱安排都有哪些菜可以吃", "canteen_status", "week_menu", "adversarial"),
    c("麻烦帮我看一下现在这个时间点园区里边的实时人数大概是多少人在线", "person_status", "realtime", "adversarial"),
]

# 顶层意图 -> 子类型判定方法名
SUB_TYPE_METHOD = {
    "person_status": "classify_sub_type",
    "security_status": "classify_security_sub_type",
    "canteen_status": "classify_canteen_sub_type",
    "vehicle_status": "classify_vehicle_sub_type",
    "information_status": "classify_information_sub_type",
    "energy_status": "classify_energy_sub_type",
    "meeting_status": "classify_meeting_sub_type",
    "emergency_fire": "classify_fire_sub_type",
    "emergency_perimeter": "classify_perimeter_sub_type",
    "device_status": "classify_device_sub_type",
    "compositive_overview": "classify_compositive_overview_sub_type",
    "twins_inspection": "classify_inspection_sub_type",
    "device_query": "classify_device_query_sub_type",
}


def run_filter(case, filter_key):
    """filter_key 同时匹配意图名与标签，如 energy / boundary / incident"""
    if not filter_key:
        return True
    _, intent, _, tag = case
    return filter_key in intent or filter_key == tag


def main():
    logging.basicConfig(level=logging.WARNING)  # 压掉 classifier 每条 query 的 INFO 日志
    filter_key = sys.argv[1] if len(sys.argv) > 1 else None

    mod = load_classifier()
    print(f"[eval] 加载模型与语料中（首次约 40~90 秒）...")
    clf = mod.PersonStatusClassifier()

    cases = [x for x in CASES if run_filter(x, filter_key)]
    if not cases:
        print(f"[eval] 过滤条件 '{filter_key}' 没有命中任何用例")
        sys.exit(2)

    results = []  # (query, expected_intent, expected_sub, tag, got_intent, got_sub, score, passed)
    for query, exp_intent, exp_sub, tag in cases:
        intent, score = clf.classify_top_intent(query)
        got_sub = None
        if intent in SUB_TYPE_METHOD:
            # 子类型方法返回 (子类型名, 分数)
            got_sub, _ = getattr(clf, SUB_TYPE_METHOD[intent])(query)
        passed = (intent == exp_intent) and (exp_sub is None or got_sub == exp_sub)
        results.append((query, exp_intent, exp_sub, tag, intent, got_sub, score, passed))

    # ---------- 逐条输出 ----------
    print("\n" + "=" * 78)
    print(f"{'问法':<24}{'预期':<22}{'实际':<28}{'分数':<8}{'结果'}")
    print("-" * 78)
    for query, exp_intent, exp_sub, tag, intent, got_sub, score, passed in results:
        exp_str = exp_intent if exp_sub is None else f"{exp_intent}/{exp_sub}"
        got_str = intent if got_sub is None else f"{intent}/{got_sub}"
        mark = "PASS" if passed else "FAIL"
        flag = f"({tag})" if tag != "normal" else ""
        print(f"{query:<24}{exp_str:<22}{got_str:<28}{score:<8.4f}{mark} {flag}")

    # ---------- 汇总 ----------
    total = len(results)
    passed_n = sum(1 for r in results if r[7])
    print("=" * 78)
    print(f"\n[总计] {passed_n}/{total} 通过（{passed_n / total:.0%}）")

    for tag in ("normal", "boundary", "incident", "negative", "adversarial"):
        sub_set = [r for r in results if r[3] == tag]
        if sub_set:
            p = sum(1 for r in sub_set if r[7])
            print(f"[{tag:<11}] {p}/{len(sub_set)} 通过")

    # 按域统计，找最弱域
    by_intent = defaultdict(lambda: [0, 0])
    for r in results:
        by_intent[r[1]][1] += 1
        if r[7]:
            by_intent[r[1]][0] += 1
    ranked = sorted(by_intent.items(), key=lambda kv: kv[1][0] / kv[1][1])
    print("\n[各域正确率]（由低到高）")
    for intent, (p, n) in ranked:
        print(f"  {intent:<24}{p}/{n}")

    # 失败清单（方便逐条改语料）
    failed = [r for r in results if not r[7]]
    if failed:
        print(f"\n[失败用例 {len(failed)} 条]")
        for query, exp_intent, exp_sub, tag, intent, got_sub, score, _ in failed:
            note = ""
            if intent == "other":
                note = f"(最高分 {score:.4f} 未过阈值，判为 other)"
            print(f"  - {query}  期望 {exp_intent}"
                  f"{'' if exp_sub is None else '/' + exp_sub}，实际 {intent}"
                  f"{'' if got_sub is None else '/' + got_sub} {note}")

    # 误判流向（预期 → 实际 计数），定位跷跷板域对
    confuse = defaultdict(int)
    for r in results:
        if not r[7]:
            confuse[(r[1], r[4])] += 1
    if confuse:
        print("\n[误判流向]（预期 → 实际：条数，由高到低）")
        for (exp_i, got_i), n in sorted(confuse.items(), key=lambda kv: -kv[1]):
            print(f"  {exp_i}  →  {got_i}: {n}")

    print("\n说明：boundary / incident 用例代表目标状态，边界手术与语料扩写完成前失败属预期；")
    print("      手术/扩写完成后必须全绿。normal 用例若大量失败，说明改动伤到了原有能力。")

    # ---------- 分级门禁（docs/测试方案.md §2.2） ----------
    # normal/boundary/incident：逐条硬门禁，任一失败即不通过
    # negative：误响应率 <= NEG_MAX_FAIL_RATE（默认 5%）即通过
    # adversarial：通过率 >= ADV_MIN_PASS_RATE（默认 90%）即通过
    neg = [r for r in results if r[3] == "negative"]
    adv = [r for r in results if r[3] == "adversarial"]
    hard_failed = [r for r in failed if r[3] in ("normal", "boundary", "incident")]
    neg_fail_rate = (sum(1 for r in neg if not r[7]) / len(neg)) if neg else 0.0
    adv_pass_rate = (sum(1 for r in adv if r[7]) / len(adv)) if adv else 1.0
    neg_max = float(os.getenv("NEG_MAX_FAIL_RATE", "0.05"))
    adv_min = float(os.getenv("ADV_MIN_PASS_RATE", "0.90"))

    print("\n[门禁判定]")
    ok = not hard_failed
    if hard_failed:
        print(f"  normal/boundary/incident  ✗ {len(hard_failed)} 条失败")
    else:
        print("  normal/boundary/incident  ✓ 全部通过")
    if neg:
        neg_ok = neg_fail_rate <= neg_max
        ok = ok and neg_ok
        mark = "✓" if neg_ok else "✗"
        print(f"  negative 误响应率 {neg_fail_rate:.1%} {mark}"
              f"（上限 {neg_max:.0%}，误响应 {sum(1 for r in neg if not r[7])}/{len(neg)} 条）")
    if adv:
        adv_ok = adv_pass_rate >= adv_min
        ok = ok and adv_ok
        mark = "✓" if adv_ok else "✗"
        print(f"  adversarial 通过率 {adv_pass_rate:.1%} {mark}"
              f"（下限 {adv_min:.0%}，失败 {sum(1 for r in adv if not r[7])}/{len(adv)} 条）")

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
