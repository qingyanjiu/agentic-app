"""灰度开关的离线测试（纯函数，无 IO）。"""
from agent.loop.rollout import Rollout, get_rollout, reset_rollout, startup_mode


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


# ---------------- 启动级开关 AGENT_MODE ----------------

def test_startup_mode_aliases(monkeypatch):
    for raw, expected in (
        ("loop", "loop"), ("FULL", "loop"), ("1", "loop"),
        ("legacy", "legacy"), ("off", "legacy"), ("0", "legacy"),
        ("auto", "auto"), ("", "auto"), ("whatever", "auto"),
    ):
        monkeypatch.setenv("AGENT_MODE", raw)
        assert startup_mode() == expected


def test_agent_mode_loop_is_full_rollout(monkeypatch):
    """AGENT_MODE=loop：不看灰度变量，全部走新链路。"""
    monkeypatch.setenv("AGENT_MODE", "loop")
    monkeypatch.setenv("LOOP_ROLLOUT_ENABLED", "0")
    reset_rollout()
    try:
        rollout = get_rollout()
        assert rollout.startup_mode == "loop"
        assert rollout.decide("u1", "s1").enabled is True
        # 专用模式依旧不接管
        assert rollout.decide("u1", "s1", "kb").enabled is False
        # 单请求应急回滚仍然有效
        assert rollout.decide("u1", "s1", "legacy").enabled is False
    finally:
        reset_rollout()


def test_agent_mode_legacy_ignores_gray_config(monkeypatch):
    """AGENT_MODE=legacy：灰度开着也全部走旧逻辑。"""
    monkeypatch.setenv("AGENT_MODE", "legacy")
    monkeypatch.setenv("LOOP_ROLLOUT_ENABLED", "1")
    monkeypatch.setenv("LOOP_ROLLOUT_PERCENT", "100")
    monkeypatch.setenv("LOOP_ROLLOUT_USERS", "u1")
    reset_rollout()
    try:
        rollout = get_rollout()
        assert rollout.startup_mode == "legacy"
        assert rollout.decide("u1", "s1").enabled is False
        # 单请求仍可试用新链路
        assert rollout.decide("u1", "s1", "loop").enabled is True
    finally:
        reset_rollout()


def test_agent_mode_auto_keeps_gray_behavior(monkeypatch):
    monkeypatch.delenv("AGENT_MODE", raising=False)
    monkeypatch.setenv("LOOP_ROLLOUT_ENABLED", "1")
    monkeypatch.setenv("LOOP_ROLLOUT_PERCENT", "100")
    reset_rollout()
    try:
        rollout = get_rollout()
        assert rollout.startup_mode == "auto"
        assert rollout.decide("u1", "s1").enabled is True
        assert "auto" in rollout.describe()
    finally:
        reset_rollout()
