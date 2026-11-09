"""灰度开关的离线测试（纯函数，无 IO）。"""
from agent.loop.rollout import Rollout


def test_default_is_disabled():
    decision = Rollout().decide("u1", "s1")
    assert decision.enabled is False
    assert decision.reason == "disabled"


def test_explicit_loop_mode_overrides_disabled():
    decision = Rollout().decide("u1", "s1", "loop")
    assert decision.enabled is True
    assert decision.reason == "mode=loop"


def test_explicit_legacy_wins_over_enabled():
    decision = Rollout(enabled=True, percent=100).decide("u1", "s1", "legacy")
    assert decision.enabled is False


def test_reserved_modes_are_not_taken_over():
    rollout = Rollout(enabled=True, percent=100)
    for mode in ("kb", "scene", "gen_doc", "asr"):
        assert rollout.decide("u1", "s1", mode).enabled is False
        assert "reserved" in rollout.decide("u1", "s1", mode).reason


def test_whitelist_hits_even_with_zero_percent():
    rollout = Rollout(enabled=True, percent=0, users={"vip"})
    assert rollout.decide("vip", "s1").enabled is True
    assert rollout.decide("vip", "s1").reason == "whitelist"
    assert rollout.decide("other", "s1").enabled is False


def test_percent_boundaries_and_stability():
    assert Rollout(enabled=True, percent=100).decide("u1", "s1").enabled is True
    assert Rollout(enabled=True, percent=0).decide("u1", "s1").enabled is False

    same = [
        Rollout(enabled=True, percent=50).decide("u1", "s1").enabled
        for _ in range(3)
    ]
    assert len(set(same)) == 1          # 同一 user/session 结果稳定


def test_percent_is_clamped():
    rollout = Rollout(enabled=True, percent=999)
    assert rollout.percent == 100
