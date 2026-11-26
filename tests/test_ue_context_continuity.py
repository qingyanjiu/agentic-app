# -*- coding: utf-8 -*-
"""UE 交互：跨通道上下文连续性 / 楼层词表回归（只写，未运行）。

对应 [docs/UE交互测试失败记录.md](../docs/UE交互测试失败记录.md)：

- UE-FAIL-001：「切2楼」被设备解析拦截（应进场景图，不查台账）
- UE-FAIL-004：「切回主楼」没有映射到整栋楼 `floor=99`
- UE-FAIL-005：列表之后「打开其中一个」，无上下文时查全表

标记约定：

- 带 `xfail` 的用例是**已知失败、修复后应转绿**的需求，不是当前行为的背书；
- 不带 `xfail` 的用例是现有正确行为，作为对照，防止修复时误伤。

注意：本次只新增用例，没有运行任何测试或服务。
"""
import asyncio
import json

import pytest

import agent.loop.domains.device_query as dq
from agent.loop.domains.device_query import clear_device_cache
from agent.loop.scene_resolve import has_device_reference, resolve_scene_device_tag


class _FakeToolbox:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    async def call(self, server, tool, args):
        self.calls += 1
        return json.dumps(self.rows, ensure_ascii=False)


def _row(name, code, space="园区/科研办公楼/1F"):
    return {
        "id": f"id-{code}",
        "name": name,
        "rawName": name,
        "code": code,
        "spaceName": space,
        "status": "1",
        "syncSource": "3",
    }


def _resolve(box, query, candidates=None):
    return asyncio.run(resolve_scene_device_tag(box, query, candidates))


def _fresh(monkeypatch):
    monkeypatch.setattr(dq, "_CACHE_TTL", 300.0)
    clear_device_cache()


def _three_candidates():
    return [
        _row("1F-西电梯MJ", "DEFAULT-0178"),
        _row("1F-主楼梯MJ2", "DEFAULT-0176"),
        _row("1F-后厨东MJ", "DEFAULT-0177"),
    ]


# ------------------------------------------------------------
# UE-FAIL-005：列表之后的指代
# ------------------------------------------------------------
def test_selection_without_context_does_not_query_ledger(monkeypatch):
    """「打开其中一个」没有上一层列表时，应追问要哪台，而不是查台账。"""
    _fresh(monkeypatch)
    box = _FakeToolbox(_three_candidates())

    tag, clarify, candidates = _resolve(box, "打开其中一个")

    assert box.calls == 0, "不应调用设备台账"
    assert tag is None
    assert clarify and "匹配到" not in clarify
    assert not candidates


def test_selection_with_candidates_uses_them_instead_of_ledger(monkeypatch):
    """上一轮有候选时，「其中一个」应围绕候选追问/解析，不能再查台账。"""
    _fresh(monkeypatch)
    box = _FakeToolbox(_three_candidates())

    tag, clarify, candidates = _resolve(box, "打开其中一个", _three_candidates())

    assert box.calls == 0, "候选已存在，不应重新查台账"
    assert tag is None
    assert clarify
    assert "匹配到" not in clarify
    # 期望话术明确指向上一轮候选，而不是让用户重说完整要求
    assert any(word in clarify for word in ("上一轮", "上方", "哪一台", "第几个"))


def test_bare_pronoun_without_context_does_not_query_ledger(monkeypatch):
    """「打开这个监控」没有候选、也没有具体名称 → 追问，不查全表。"""
    _fresh(monkeypatch)
    box = _FakeToolbox(_three_candidates())

    tag, clarify, candidates = _resolve(box, "打开这个监控")

    assert box.calls == 0
    assert tag is None
    assert clarify and "哪一台" in clarify
    assert not candidates


def test_explicit_ordinal_without_context_does_not_query_ledger(monkeypatch):
    """「第100个」没有列表时不能拿序号去查台账。"""
    _fresh(monkeypatch)
    box = _FakeToolbox(_three_candidates())

    tag, clarify, candidates = _resolve(box, "第100个")

    assert box.calls == 0
    assert tag is None
    assert clarify and "设备清单" in clarify
    assert not candidates


def test_ordinal_out_of_range_keeps_candidates(monkeypatch):
    """序号超出当前列表时追问，并保留候选供下一次说正确序号。"""
    _fresh(monkeypatch)
    box = _FakeToolbox([])

    tag, clarify, candidates = _resolve(box, "第10个", _three_candidates())

    assert box.calls == 0
    assert tag is None
    assert clarify and "前 3 台" in clarify
    assert [c["code"] for c in candidates] == [c["code"] for c in _three_candidates()]


def test_recent_entities_roundtrip():
    """list 快照能还原成候选行；非 device 清单忽略。"""
    from agent.loop.recent_entities import (
        get_recent_entities,
        recent_entities_to_rows,
        remember_entities,
        reset_recent_entities,
    )

    reset_recent_entities()
    payload = {
        "kind": "device",
        "title": "122 台设备（园区共 579 台），楼层 1F",
        "items": [
            {"kind": "device", "id": "DEFAULT-0178", "label": "1F-西电梯MJ",
             "attrs": {"code": "DEFAULT-0178", "space": "园区/科研办公楼/1F"}},
        ],
        "total": 122,
    }
    remember_entities("u-recent", "s-recent", payload)

    snapshot = get_recent_entities("u-recent", "s-recent")
    assert snapshot and snapshot["total"] == 122
    rows = recent_entities_to_rows(snapshot)
    assert rows == [{
        "name": "1F-西电梯MJ", "rawName": "1F-西电梯MJ",
        "code": "DEFAULT-0178", "spaceName": "园区/科研办公楼/1F",
    }]
    assert recent_entities_to_rows({"kind": "person", "items": payload["items"]}) == []


def test_candidate_title_keeps_total():
    from agent.loop.scene_resolve import candidate_entities

    data = candidate_entities(_three_candidates(), total=122).to_dict()
    assert "匹配到 122 台设备" in data["title"]
    assert "先列出前 3 台" in data["title"]
    assert data["total"] == 122


def test_ordinal_with_candidates_still_picks(monkeypatch):
    """对照：有候选时「第三个」仍应直接选中第 3 个（现有正确行为）。"""
    _fresh(monkeypatch)
    box = _FakeToolbox([])

    tag, clarify, _ = _resolve(box, "第三个", _three_candidates())

    assert tag == "DEFAULT-0177"
    assert clarify is None
    assert box.calls == 0


def test_explicit_device_name_still_resolves(monkeypatch):
    """对照：直接说设备名仍应正常解析成 tag（T12）。"""
    _fresh(monkeypatch)
    box = _FakeToolbox([_row("1F-西电梯MJ", "DEFAULT-0178")])

    tag, clarify, _ = _resolve(box, "打开1F-西电梯MJ")

    assert tag == "DEFAULT-0178"
    assert clarify is None


# ------------------------------------------------------------
# UE-FAIL-001：楼层话术不能被当成设备线索
# ------------------------------------------------------------
@pytest.mark.parametrize("query", ["切2楼", "去2楼", "转2楼", "上2楼", "回2楼", "一层一层"])
def test_floor_only_phrases_are_not_device_references(query):
    assert has_device_reference(query) is False


def test_floor_with_type_word_is_device_reference():
    """对照：「2楼的监控」明确在说设备，仍要查台账。"""
    assert has_device_reference("2楼的监控") is True


# ------------------------------------------------------------
# UE-FAIL-004：「主楼」应等价于整栋楼
# ------------------------------------------------------------
@pytest.mark.parametrize("query", ["切回主楼", "回到主楼", "展示主楼", "切回科研办公楼"])
def test_main_building_maps_to_whole_building(query):
    from graph.scene_control_langgraph import _extract_floor

    assert _extract_floor(query) == 99


def test_main_staircase_is_not_whole_building():
    """设备名「主楼梯」不能被整栋楼规则吞掉。"""
    from graph.scene_control_langgraph import _extract_floor

    assert _extract_floor("3F-主楼梯MJ") == 3
    assert _extract_floor("主楼梯") is None


def test_main_building_is_not_device_reference():
    """「主楼 / 科研办公楼」是整栋楼视角，不能拿去查设备台账。"""
    for query in ("切回主楼", "回到主楼", "切回科研办公楼"):
        assert has_device_reference(query) is False, query
    # 真·设备线索仍要保留
    assert has_device_reference("打开主楼梯的监控") is True


def test_whole_building_synonyms_still_work():
    """对照：现有整栋同义词不能被修复误伤。"""
    from graph.scene_control_langgraph import _extract_floor

    for query in ("展示整栋楼", "切回整栋楼", "恢复楼栋", "回到全楼"):
        assert _extract_floor(query) == 99, query
