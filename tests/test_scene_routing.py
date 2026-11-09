# -*- coding: utf-8 -*-
"""
场景控制 vs 业务查询 分流策略测试（纯规则，不加载 embedding 模型）。

场景控制不再单独设选项卡，和业务查询共用同一个聊天入口，因此需要验证：
  1. 业务意图没命中（other）时，场景命中就分流；
  2. 业务域也命中时，只有明确场景操作词或场景分明显更高才分流；
  3. 「摄像头在线率」「各防区告警统计」这类业务问法不能被场景抢走；
  4. 业务追问中的简短补参（「3楼」）不会因为场景分高被截胡；
  5. 2026-11-04 口径：「查询/查看 xx 情况、xx 数据、xx 统计」和问句一律业务；
     「查看 xx 设备 / 查看 xx」「转到 / 切到 / 换成 / 换到」一律场景。
"""
from agent.intent.classifier import (
    has_strong_scene_cue,
    is_business_query,
    should_route_scene,
    should_route_scene_by_vector,
)


def _info(intent="other", score=0.0, scene_score=0.70, scene_matched=None,
          business_query_score=None):
    info = {
        "intent": intent,
        "score": score,
        "scene_score": scene_score,
        "scene_matched": (scene_score >= 0.60 if scene_matched is None else scene_matched),
    }
    if business_query_score is not None:
        info["business_query_score"] = business_query_score
    return info


def test_route_when_business_is_other():
    assert should_route_scene(_info("other", 0.40, 0.66), "切到3楼")


def test_keep_business_when_scene_not_matched():
    assert not should_route_scene(_info("person_status", 0.78, 0.55), "现在园区有多少人")


def test_keep_business_when_scene_score_only_slightly_higher():
    # 场景分没有拉开 SCENE_ROUTE_MARGIN(0.05)
    assert not should_route_scene(_info("security_status", 0.70, 0.72), "摄像头在线率怎么样")


def test_route_when_scene_score_clearly_higher():
    assert should_route_scene(_info("security_status", 0.66, 0.72), "把三楼的监控调出来")


def test_strong_scene_cue_overrides_weak_business_hit():
    info = _info("security_status", 0.70, 0.65)
    assert should_route_scene(info, "重置视角")


def test_display_meeting_room_goes_scene_despite_business_hit():
    # 业务 meeting_status 分数更高，但「显示…会议室」是明确的场景展示动作
    info = _info("meeting_status", 0.73, 0.66)
    assert should_route_scene(info, "显示三楼的会议室")
    assert not should_route_scene(info, "三楼会议室使用情况")


def test_data_query_is_not_routed_to_scene():
    # 2026-11-04 线上：业务分 0.6474 判 other，场景分 0.6309 刚过 0.60，
    # 「查询一楼监控数据」被当成显示点位下发 scene_show_device_category。
    assert not should_route_scene(_info("other", 0.6474, 0.6309), "查询一楼监控数据")
    assert not should_route_scene(_info("other", 0.6312, 0.6050), "一楼监控数据")
    assert not should_route_scene(_info("other", 0.60, 0.70), "各防区告警统计")
    assert not should_route_scene(_info("other", 0.60, 0.70), "一楼监控在线率")


def test_explicit_display_still_goes_scene_even_with_data_word():
    # 「显示三楼的监控数据」是明确的展示动作，不能被数据词否决
    assert should_route_scene(_info("other", 0.0, 0.70), "显示三楼的监控数据")


def test_camera_tag_after_monitor_is_scene_cue():
    # 2026-11-04：tag 在「监控」后面的说法（线上真实句式）也要识别成场景指令
    for query in (
        "查看监控 jk_zl_1f_0000",
        "看下监控 jk_zl_1f_0000",
        "打开监控 1F-咖啡厅向西",
        "调出摄像头 jk_zl_1f_0000 的画面",
    ):
        assert has_strong_scene_cue(query), query
    assert should_route_scene(_info("other", 0.61, 0.63), "查看监控 jk_zl_1f_0000")


def test_monitor_data_query_stays_business():
    # 「监控数据/监控在线率」是查数据，不能因为带「监控」就进场景
    assert not has_strong_scene_cue("查看监控数据")
    assert not has_strong_scene_cue("查看监控在线率")
    assert not should_route_scene(_info("other", 0.61, 0.70), "查看监控数据")


def test_strong_scene_cues():
    for query in (
        "切到3楼",
        "回到整栋楼",
        "重置视角",
        "清场",
        "显示所有烟感点位",
        "隐藏这个标记",
        "看CY-HIK-JK-001的监控",
        "打开这个摄像头的画面",
        "看下三楼的监控",
        "改成傍晚",
        "在场景里显示三楼的摄像头",
        "把设备融合到场景里",
        "显示三楼的会议室",
        "显示食堂位置",
        "显示周界防区总览",
    ):
        assert has_strong_scene_cue(query), query


def test_business_queries_are_not_strong_scene_cues():
    for query in (
        "现在园区有多少人",
        "今天有哪些安防告警",
        "摄像头在线率是多少",
        "各防区告警统计",
        "整栋楼有多少人",
        "打开停车场监控",
        "三楼会议室使用情况",
        "会议室今天有几场会",
        "今天食堂的菜谱是什么",
        "食堂今天多少人",
    ):
        assert not has_strong_scene_cue(query), query


# ------------------------------------------------------------------
# 2026-11-04 用户口径
#   业务：查询/查看 xx 情况、xx 数据、xx 统计；问句
#   场景：查看 xx 设备、查看 xx、转到、切到、换成、换到
# ------------------------------------------------------------------

def test_query_or_view_plus_data_word_is_business():
    for query in (
        "查询一楼监控情况",
        "查看设备数据",
        "查看告警统计",
        "查询各防区告警数量",
        "查看设备运行情况",
        "查看会议室使用记录",
        "查询三楼摄像头在线率",
    ):
        assert is_business_query(query), query
        assert not should_route_scene(_info("other", 0.62, 0.70), query), query


def test_question_sentences_are_business():
    for query in (
        "今天有哪些会议？",
        "三楼有摄像头吗",
        "现在园区有多少人",
        "设备现在在线吗",
        "今天食堂有什么菜",
        "监控画面正常吗",
        "告警处理完了没",
        "哪个防区还有告警",
        "会议室今天有几场会",
        "查看设备吗",
        "查看监控画面正常吗",
    ):
        assert is_business_query(query), query
        assert not should_route_scene(_info("other", 0.62, 0.70), query), query


def test_view_device_and_switch_actions_are_scene():
    for query in (
        "查看设备",
        "查看摄像头",
        "查看这个设备",
        "查看监控画面",
        "转到3楼",
        "切到B1",
        "换到三楼",
        "换成晴天",
        "换成下雨",
    ):
        assert not is_business_query(query), query
        assert should_route_scene(_info("security_status", 0.72, 0.61), query), query


def test_scene_state_question_still_goes_scene():
    for query in (
        "现在场景在第几层",
        "当前楼层是几楼",
        "场景现在是什么状态",
        "视角状态怎么样",
    ):
        assert not is_business_query(query), query
        assert should_route_scene(_info("other", 0.50, 0.55), query), query


def test_explicit_action_wins_over_question_mark():
    # 带「显示/打开」这类明确操作时，问号不作为业务否决条件
    assert should_route_scene(_info("other", 0.50, 0.55), "能显示三楼的摄像头吗？")
    # 但「查看 + 数据词」仍压过场景动作
    assert is_business_query("查看三楼的监控数据")
    assert not should_route_scene(_info("other", 0.50, 0.70), "查看三楼的监控数据")
    # 「显示 + 数据词」不是查询/查看句式，仍按场景展示
    assert not is_business_query("显示三楼的监控数据")


def test_weather_switch_phrasings_are_scene():
    """2026-11-05 线上：「切到晴天」场景分 0.5839 没过阈值、又没命中动作词，
    被业务兜底拒绝；「切换到晴天」靠 0.6428 侥幸过了阈值。"""
    low = _info("other", 0.46, 0.50, scene_matched=False)
    for query in (
        "切到晴天",
        "切换到晴天",
        "换成晴天",
        "改成晴天",
        "改为晴天",
        "调到晴天",
        "变成晴天",
        "设成晴天",
        "切到下雨",
        "切换到小雨",
        "把天气改成暴雨",
        "调到夜间",
    ):
        assert has_strong_scene_cue(query), query
        assert should_route_scene(low, query), query


def test_bare_weather_word_is_scene_command():
    """用户只回一个天气词（场景追问/指令）也按场景处理，场景分不用过阈值"""
    low = _info("other", 0.42, 0.47, scene_matched=False)
    for query in ("晴天", "下雨", "傍晚", "多云", "小雨", "晴天！"):
        assert should_route_scene(low, query), query


def test_weather_questions_stay_business():
    for query in ("今天晴天吗", "今天天气怎么样", "明天会不会下雨"):
        assert is_business_query(query), query
        assert not should_route_scene(_info("other", 0.51, 0.45), query), query
    # 不是整句天气词（「晴天霹雳」）不触发场景
    assert not has_strong_scene_cue("晴天霹雳")


# ------------------------------------------------------------------
# 2026-11-05 向量兜底：正则都没命中时，业务查询中心 vs 场景中心裁决
#   （业务域未命中 intent=other 时参照 business_query_score）
# ------------------------------------------------------------------

def test_vector_fallback_prefers_scene_only_when_clearly_ahead():
    # 场景 0.62 领先业务中心 0.55 → 场景
    assert should_route_scene_by_vector(
        _info("other", 0.40, 0.62, business_query_score=0.55)
    )
    # 场景 0.62 但业务中心 0.60，领先不足 SCENE_ROUTE_MARGIN → 维持业务
    assert not should_route_scene_by_vector(
        _info("other", 0.40, 0.62, business_query_score=0.60)
    )


def test_vector_fallback_weak_rescue_below_threshold():
    # 「切到晴天」补语料前：场景分 0.5839 低于 0.60，但业务中心只有 0.49
    assert should_route_scene_by_vector(
        _info("other", 0.4594, 0.5839, scene_matched=False,
              business_query_score=0.49)
    )
    # 与业务中心差距不足 0.08 → 不放行
    assert not should_route_scene_by_vector(
        _info("other", 0.50, 0.5839, scene_matched=False,
              business_query_score=0.52)
    )
    # 再低（< 0.55）即使差距大也不放行
    assert not should_route_scene_by_vector(
        _info("other", 0.30, 0.54, scene_matched=False,
              business_query_score=0.40)
    )


def test_vector_fallback_uses_domain_score_when_intent_matched():
    # 业务域命中时参照命中的业务分，business_query_score 不参与
    assert should_route_scene_by_vector(
        _info("security_status", 0.66, 0.72, business_query_score=0.30)
    )
    assert not should_route_scene_by_vector(
        _info("security_status", 0.70, 0.72, business_query_score=0.30)
    )


def test_vector_fallback_requires_scene_vector():
    # 场景中心未加载（scene.url 未配置）→ scene_score=-1，不分流
    assert not should_route_scene_by_vector(_info("other", 0.0, -1.0))
    assert not should_route_scene_by_vector({})


def test_business_center_blocks_ambiguous_other_query():
    # 业务域没命中（other），正则也不命中；业务中心分和场景分接近 → 不抢
    assert not should_route_scene(
        _info("other", 0.63, 0.64, business_query_score=0.65),
        "三楼",
    )
    # 场景明显领先才分流
    assert should_route_scene(
        _info("other", 0.40, 0.64, business_query_score=0.55),
        "三楼",
    )

