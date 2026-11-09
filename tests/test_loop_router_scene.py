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
