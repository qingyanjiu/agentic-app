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

退出码：
    全部通过 → 0；存在失败 → 1（可接入 CI / 批次验收）

用例标签说明：
    normal    各域常规问法，扩写前就应当大部分通过
    boundary  边界锁：2026-09-17 拍板的 8 条跨域归属规则（裸报修归设备域、
              裸设备清单归 device_query、实时告警归消防、今日用电归 overall 等）。
              这些用例代表「目标状态」——边界手术/扩写完成前失败是预期内的，
              手术完成后必须全绿，之后任何语料改动不得使其回退
    incident  线上真实翻车句（如 2026-09-16 「今天园区能耗情况怎么样？」0.6471
              掉兜底），扩写完成后目标分数 >= 0.70

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

    for tag in ("boundary", "incident", "normal"):
        sub_set = [r for r in results if r[3] == tag]
        if sub_set:
            p = sum(1 for r in sub_set if r[7])
            print(f"[{tag:<8}] {p}/{len(sub_set)} 通过")

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

    print("\n说明：boundary / incident 用例代表目标状态，边界手术与语料扩写完成前失败属预期；")
    print("      手术/扩写完成后必须全绿。normal 用例若大量失败，说明改动伤到了原有能力。")

    sys.exit(0 if not failed else 1)


if __name__ == "__main__":
    main()
