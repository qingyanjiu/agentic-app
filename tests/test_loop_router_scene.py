"""场景控制的 L0 判定测试。

线上问题：灰度到 loop 链路后，「打开监控视频」之后说「关闭监控窗口」，
回答是"我没有这个能力"。

根因：`_SCENE_STRONG_CUES` 的动词表里只有「打开/查看/显示/展示/调出」，
没有「关闭/关掉/退出」。于是 has_strong_scene_cue 给 False，router 不判 L0，
`intent=other` 又直接转 L2；而 L2 手上只有查询类工具，模型只能如实说自己
没这个能力。

旧链路一直用 should_route_scene 判定（正则 + 场景向量兜底），
实测「关闭监控窗口」scene=0.687、业务分 0.594，能救回来。
所以 router 的 L0 必须补上同一套兜底，否则灰度链路会改变存量行为。
"""
import asyncio

from agent.loop.router import Router
from agent.loop.types import DomainPlan


def _router(intent: str, score: float, scene: float) -> Router:
    async def classify(query: str) -> dict:
        return {"intent": intent, "score": score, "scene_score": scene}

    return Router(classify=classify)


def test_close_monitor_window_hands_off_to_legacy_scene():
    # 实测：正则不命中，但场景分 0.687 明显领先业务分 0.594
    plan = asyncio.run(_router("other", 0.594, 0.687).route("关闭监控窗口"))

    assert plan is not None
    assert plan.level == "L0"
    assert plan.domain == "scene"
    assert plan.tool is None          # tool=None → pipeline 走 handoff 交回旧链路
    assert plan.reason == "scene_vector"


def test_scene_verbs_missing_from_regex_still_hand_off():
    """「退出监控页面」「关掉监控」同样不在强场景词表里，靠向量兜住。"""
    for query, scene, business in (
        ("退出监控页面", 0.688, 0.592),
        ("关掉监控", 0.678, 0.622),
    ):
        plan = asyncio.run(_router("other", business, scene).route(query))
        assert plan is not None and plan.level == "L0", query


def test_regex_cue_still_wins_without_classifier():
    """明确场景动作走快路径，不该等分类器（也不该被它改判）。"""
    calls: list[str] = []

    async def classify(query: str) -> dict:
        calls.append(query)
        return {"intent": "other", "score": 0.0, "scene_score": 0.0}

    plan = asyncio.run(Router(classify=classify).route("关闭弹窗"))

    assert plan is not None and plan.level == "L0"
    assert calls == []                # 正则命中就不调分类器


def test_business_query_is_not_hijacked_by_scene_vector():
    """「查看 xx 在线率」属于业务问法，场景分再高也不能抢走。"""
    plan = asyncio.run(_router("device_status", 0.94, 0.70).route("查看设备在线率"))

    assert plan is None or plan.level != "L0"


def test_confident_business_domain_still_direct_connects(monkeypatch):
    from agent.loop import router as router_mod

    monkeypatch.setitem(
        router_mod.DOMAIN_PLANNERS,
        "person_status",
        lambda q: DomainPlan(tool="person_status__count", args={"query": q}),
    )

    plan = asyncio.run(_router("person_status", 0.93, 0.20).route("园区人员情况"))

    assert plan is not None
    assert plan.level == "L1"
    assert plan.tool == "person_status__count"


def test_question_form_monitor_command_hands_off():
    """「能打开监控吗」：存量正则漏 `|` 导致 has_strong_scene_cue=False、
    is_business_query=True，会掉进业务查询只能答"没有能力"。②.5 补上这条。"""
    plan = asyncio.run(_router("security_status", 0.80, 0.40).route("能打开监控吗"))

    assert plan is not None
    assert plan.level == "L0"
    assert plan.reason == "scene_monitor"


def test_monitor_cue_variants_all_hand_off():
    for query in (
        "查看监控",
        "打开监控",
        "显示摄像头",
        "调出监控画面",
        "看一下监控",
        "切到监控画面",
    ):
        plan = asyncio.run(_router("other", 0.50, 0.30).route(query))
        assert plan is not None and plan.level == "L0", query


def test_data_query_about_camera_is_not_hijacked():
    """「看摄像头在线率」问的是数据，不能被「看+摄像头」送进场景。"""
    plan = asyncio.run(_router("device_status", 0.93, 0.70).route("看摄像头在线率"))

    assert plan is None or plan.level != "L0"


# ------------------------------------------------------------
# 场景 vs 设备：类型词后面跟具体设备名，是问设备不是开关画面
# ------------------------------------------------------------
def test_device_name_after_monitor_is_not_scene():
    """「查看 监控5F客梯厅」问的是这台设备，不能被判成场景指令。

    线下实测：这条曾被 `_SCENE_MONITOR_CUE` 的 `.{0,4}` 吞掉，
    判成 L0 → handoff 回旧链路 → 拉 268 台设备全量灌给模型。
    """
    for query in ("查看 监控5F客梯厅", "查看 监控 jk_zl_5f_0006"):
        plan = asyncio.run(_router("other", 0.50, 0.70).route(query))
        assert plan is None or plan.level != "L0", query


def test_scene_command_ending_with_type_word_still_hands_off():
    """「打开5F客梯厅的监控」类型词在句末，仍然是开画面的场景指令。"""
    plan = asyncio.run(_router("other", 0.50, 0.70).route("打开5F客梯厅的监控"))

    assert plan is not None and plan.level == "L0"


def test_monitor_window_words_are_still_scene():
    """「画面/窗口」这类泛称不能被当成设备名。"""
    for query in ("调出监控画面", "关闭监控窗口"):
        plan = asyncio.run(_router("other", 0.50, 0.70).route(query))
        assert plan is not None and plan.level == "L0", query


def test_open_monitor_with_device_name_is_scene_not_query():
    """「打开监控7F西通道西向东」是开画面，不能因为带了设备名被判成查询。

    回归点：`_asks_specific_device` 只应对「查看类」生效。线上实测过
    「打开监控X」掉进 L2，模型只能回答"没有控制类工具"。
    这里把场景分压到 0.30（向量兜不住），确保命中靠的是打开类正则。

    注意：场景动作最终由 app.py 的 SceneMcpClient 下发（scene 是 direct_client，
    需要逐请求 X-Session-Id），loop 这边只负责把话认出来并 handoff。
    """
    plan = asyncio.run(_router("other", 0.50, 0.30).route("打开监控7F西通道西向东"))

    assert plan is not None
    assert plan.level == "L0"
    assert plan.domain == "scene"


def test_open_monitor_without_device_still_hands_off():
    """只说「打开监控」、没给设备 → 同样 handoff 给场景通道。"""
    plan = asyncio.run(_router("other", 0.50, 0.30).route("打开监控"))

    assert plan is not None and plan.level == "L0"


def test_open_monitor_with_trailing_type_word_hands_off():
    """「打开7F西通道监控」（类型词在句末）也要走场景通道。

    线上实测过这条掉进 L2：那里只有查询工具，模型只能回答"没有控制类工具"。
    句末式说法比「打开监控X」更自然，两种都要认。
    """
    plan = asyncio.run(_router("other", 0.50, 0.30).route("打开7F西通道监控"))

    assert plan is not None
    assert plan.level == "L0"
    assert plan.domain == "scene"
    assert plan.reason == "scene_monitor"
