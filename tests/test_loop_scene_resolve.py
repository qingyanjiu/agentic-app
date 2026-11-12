"""场景话术 → 点位 tag 的解析测试。

用户说「打开7F西通道监控」，`scene_show_camera` 要的却是设备编号。
这里验证：能唯一定位就给 tag；多台就列候选并引导补设备类型；
用户下一轮说「第三个」「这个」时按候选解析。
"""
import asyncio
import json

import agent.loop.domains.device_query as dq
from agent.loop.domains.device_query import clear_device_cache
from agent.loop.scene_resolve import resolve_scene_device_tag


class _FakeToolbox:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    async def call(self, server, tool, args):
        self.calls += 1
        return json.dumps(self.rows, ensure_ascii=False)


def _row(name, code, space="园区/科研办公楼/7F"):
    return {
        "id": f"id-{code}", "name": name, "rawName": name, "code": code,
        "spaceName": space, "status": "1", "syncSource": "3",
    }


def _resolve(box, query, candidates=None):
    return asyncio.run(resolve_scene_device_tag(box, query, candidates))


def _fresh(monkeypatch):
    monkeypatch.setattr(dq, "_CACHE_TTL", 300.0)
    clear_device_cache()


# ------------------------------------------------------------
# 名称 → tag
# ------------------------------------------------------------
def test_unique_match_returns_tag(monkeypatch):
    _fresh(monkeypatch)
    box = _FakeToolbox([_row("7F西通道西向东", "jk_zl_7f_0002")])

    tag, clarify, _ = _resolve(box, "打开监控7F西通道西向东")

    assert tag == "jk_zl_7f_0002"
    assert clarify is None


def test_trailing_type_word_is_extracted(monkeypatch):
    """「打开7F西通道监控」（类型词在句末）也要能定位。"""
    _fresh(monkeypatch)
    box = _FakeToolbox([_row("7F西通道西向东", "jk_zl_7f_0002")])

    tag, clarify, _ = _resolve(box, "打开7F西通道监控")

    assert tag == "jk_zl_7f_0002"
    assert clarify is None


def test_multiple_matches_return_candidates(monkeypatch):
    """多台命中：给候选（供下轮按序号选），并引导补设备类型。"""
    _fresh(monkeypatch)
    box = _FakeToolbox([
        _row("7F西通道西向东", "jk_zl_7f_0002"),
        _row("7F西通道东向西", "jk_zl_7f_0003"),
    ])

    tag, clarify, candidates = _resolve(box, "打开7F西通道监控")

    assert tag is None
    assert "补充设备类型" in clarify
    assert [c["code"] for c in candidates] == ["jk_zl_7f_0002", "jk_zl_7f_0003"]


def test_no_match_says_not_found(monkeypatch):
    _fresh(monkeypatch)
    box = _FakeToolbox([_row("别处的监控", "jk_zl_1f_0001", space="园区/科研办公楼/1F")])

    tag, clarify, _ = _resolve(box, "打开监控9F不存在的点位")

    assert tag is None
    assert "没有找到" in clarify


def test_missing_code_asks_for_code(monkeypatch):
    _fresh(monkeypatch)
    row = _row("7F西通道西向东", "")
    row.pop("code")
    box = _FakeToolbox([row])

    tag, clarify, _ = _resolve(box, "打开监控7F西通道西向东")

    assert tag is None
    assert "没有设备编号" in clarify


# ------------------------------------------------------------
# 候选记忆：序号 / 指代
# ------------------------------------------------------------
def _six_candidates():
    return [
        _row("7F西通道西向东", "jk_zl_7f_0002"),
        _row("7F中通道西向东", "jk_zl_7f_0003"),
        _row("7F中通道东向西", "jk_zl_7f_0001"),
    ]


def test_ordinal_picks_from_candidates(monkeypatch):
    """用户回「第三个」→ 取候选里的第 3 个，不再查台账。"""
    _fresh(monkeypatch)
    box = _FakeToolbox([])          # 台账根本不用查

    tag, clarify, _ = _resolve(box, "第三个", _six_candidates())

    assert tag == "jk_zl_7f_0001"
    assert clarify is None
    assert box.calls == 0


def test_ordinal_accepts_arabic(monkeypatch):
    _fresh(monkeypatch)
    box = _FakeToolbox([])

    tag, _, _ = _resolve(box, "第2个", _six_candidates())

    assert tag == "jk_zl_7f_0003"


def test_pronoun_with_single_candidate(monkeypatch):
    """候选只有一个时，「这个」可以直接用。"""
    _fresh(monkeypatch)
    box = _FakeToolbox([])

    tag, clarify, _ = _resolve(box, "打开这个监控", [_row("7F西通道西向东", "jk_zl_7f_0002")])

    assert tag == "jk_zl_7f_0002"
    assert clarify is None


def test_pronoun_with_multiple_candidates_asks_again(monkeypatch):
    """候选多台时「这个」有歧义，再问一次，不猜。"""
    _fresh(monkeypatch)
    box = _FakeToolbox([])

    tag, clarify, candidates = _resolve(box, "打开这个监控", _six_candidates())

    assert tag is None
    assert "哪一台" in clarify
    assert len(candidates) == 3


def test_ordinal_out_of_range_falls_through(monkeypatch):
    """序号超出候选数时不要瞎取，回落正常解析（这里查到 0 台 → 追问）。"""
    _fresh(monkeypatch)
    box = _FakeToolbox([])

    tag, clarify, _ = _resolve(box, "第九个", _six_candidates())

    assert tag is None
    assert clarify


def test_reuses_device_cache(monkeypatch):
    _fresh(monkeypatch)
    box = _FakeToolbox([
        _row("7F西通道西向东", "jk_zl_7f_0002"),
        _row("7F中通道东向西", "jk_zl_7f_0003"),
    ])

    _resolve(box, "打开监控7F西通道西向东")
    _resolve(box, "打开监控7F中通道东向西")

    assert box.calls == 1
