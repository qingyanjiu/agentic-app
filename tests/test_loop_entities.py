"""结构化实体（list 事件）的单元测试。

前端不再猜"这段文本是不是设备清单"，而是认 `data.type == "list"` 的 kind。
这里锁定三件事：
1. 协议字段（kind / title / items / total / truncated / note）；
2. 设备清单工具确实产出 entities，且 id 就是场景要的 tag；
3. L1 直出和 L2 流式两条路都能把 entities 送到前端。
"""
import asyncio
import json
from types import SimpleNamespace as NS

from agent.loop.domains.device_query import MAX_ROWS, build_domain, clear_device_cache
from agent.loop.entities import KIND_DEVICE, Entity, EntityList
from agent.loop.pipeline import PipelineResult

# ---------------------------------------------------------------- 协议本身


def test_entity_dict_omits_empty_attrs():
    item = Entity(kind=KIND_DEVICE, id="jk_zl_5f_0006", label="5F客梯厅")
    assert item.to_dict() == {
        "kind": "device",
        "id": "jk_zl_5f_0006",
        "label": "5F客梯厅",
    }


def test_entity_dict_keeps_attrs():
    item = Entity(
        kind=KIND_DEVICE,
        id="jk_zl_5f_0006",
        label="5F客梯厅",
        attrs={"code": "jk_zl_5f_0006", "status": "启用"},
    )
    assert item.to_dict()["attrs"]["status"] == "启用"


def test_entity_link_format():
    """行内引用协议（第二阶段）：[label](entity:kind/id)。"""
    item = Entity(kind=KIND_DEVICE, id="jk_zl_5f_0006", label="5F客梯厅")
    assert item.to_link() == "[5F客梯厅](entity:device/jk_zl_5f_0006)"


def test_entity_list_dict_minimal():
    entities = EntityList(
        kind=KIND_DEVICE,
        title="2 台设备",
        items=[
            Entity(kind=KIND_DEVICE, id="a", label="A"),
            Entity(kind=KIND_DEVICE, id="b", label="B"),
        ],
    )
    data = entities.to_dict()
    assert data["kind"] == "device"
    assert data["title"] == "2 台设备"
    assert [item["id"] for item in data["items"]] == ["a", "b"]
    # 没给就不出现，避免前端拿 None 判空
    assert "total" not in data
    assert "truncated" not in data
    assert "note" not in data


def test_entity_list_dict_full():
    entities = EntityList(
        kind=KIND_DEVICE,
        title="60 台设备（园区共 268 台）",
        items=[Entity(kind=KIND_DEVICE, id="a", label="A")],
        total=268,
        truncated=True,
        note="其余 208 台未展示",
    )
    data = entities.to_dict()
    assert data["total"] == 268
    assert data["truncated"] is True
    assert data["note"] == "其余 208 台未展示"


def test_empty_entity_list_is_falsy():
    """空列表不该发给前端，也不该让 `if outcome.entities` 成立。"""
    assert not EntityList(kind=KIND_DEVICE, title="0 台设备")


# ---------------------------------------------------------------- 工具产出

ROWS_5F_MONITOR = [
    {
        "id": "1001",
        "code": "jk_zl_5f_0006",
        "name": "5F客梯厅",
        "rawName": "5F客梯厅",
        "spaceName": "园区/科研办公楼/5F",
        "status": "1",
        "syncSource": "3",
    },
    {
        "id": "1002",
        "code": "jk_zl_5f_0007",
        "name": "5F电梯厅东",
        "rawName": "5F电梯厅东",
        "spaceName": "园区/科研办公楼/5F",
        "status": "0",
        "syncSource": "3",
    },
]


class _FakeToolbox:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def call(self, server, tool, args=None):
        self.calls.append((server, tool, args))
        return json.dumps(self.rows)


def _list_tool(rows):
    toolbox = _FakeToolbox(rows)
    domain = build_domain(toolbox)
    tool = next(item for item in domain.tools if item.name == "device_query__list")
    return tool, toolbox


def test_list_devices_returns_entities():
    clear_device_cache()
    tool, _ = _list_tool(ROWS_5F_MONITOR)
    outcome = asyncio.run(tool.execute({"query": "5楼有哪些监控"}))

    assert outcome.is_error is False
    assert outcome.entities is not None
    data = outcome.entities.to_dict()
    assert data["kind"] == "device"
    # 标题和文本首行是同一句话
    assert data["title"] == outcome.text.splitlines()[0].rstrip("：")
    assert [item["id"] for item in data["items"]] == ["jk_zl_5f_0006", "jk_zl_5f_0007"]
    first = data["items"][0]
    assert first["label"] == "5F客梯厅"
    assert first["attrs"]["code"] == "jk_zl_5f_0006"
    assert first["attrs"]["status"] == "启用"
    assert first["attrs"]["space"] == "园区/科研办公楼/5F"


def test_list_devices_without_match_has_no_entities():
    clear_device_cache()
    tool, _ = _list_tool(ROWS_5F_MONITOR)
    outcome = asyncio.run(tool.execute({"query": "9楼的监控"}))
    assert outcome.entities is None


def test_entities_truncated_beyond_max_rows():
    clear_device_cache()
    rows = [
        {
            "id": str(index),
            "code": f"jk_zl_6f_{index:04d}",
            "name": f"6F点位{index}",
            "rawName": f"6F点位{index}",
            "spaceName": "园区/科研办公楼/6F",
            "status": "1",
            "syncSource": "3",
        }
        for index in range(MAX_ROWS + 10)
    ]
    tool, _ = _list_tool(rows)
    outcome = asyncio.run(tool.execute({"query": "6楼的监控"}))

    data = outcome.entities.to_dict()
    assert len(data["items"]) == MAX_ROWS
    assert data["truncated"] is True
    assert "剩余 10 台" in data["note"] or "其余 10 台" in data["note"]


# ---------------------------------------------------------------- 事件传递


class _FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(json.loads(text))


class _ScriptedPipeline:
    def __init__(self, events, result):
        self.events = events
        self.result = result

    async def handle(self, query, session, cancel=None, on_delta=None, on_event=None):
        for kind, payload in self.events:
            if kind == "entities":
                await on_event(NS(type="entities", data={"list": payload}))
            elif kind == "on_delta":
                await on_delta(payload)
        return self.result


def _sent(monkeypatch, pipeline):
    from agent.loop import ws_bridge

    monkeypatch.setattr(ws_bridge, "get_pipeline", lambda llm: pipeline)
    ws_bridge.reset_sessions()
    ws = _FakeWebSocket()
    handled = asyncio.run(ws_bridge.handle_chat(ws, "5楼有哪些监控", "u", "s", llm=None))
    assert handled is True
    return [item["data"] for item in ws.sent if item.get("event") == "custom"]


def test_l1_passthrough_sends_list_after_answer(monkeypatch):
    entities = EntityList(
        kind=KIND_DEVICE,
        title="2 台设备",
        items=[Entity(kind=KIND_DEVICE, id="jk_zl_5f_0006", label="5F客梯厅")],
    )
    result = PipelineResult(
        text="2 台设备：\n- 5F客梯厅｜编号 jk_zl_5f_0006",
        level="L1",
        stop_reason="answered",
        entities=entities,
    )
    pipeline = _ScriptedPipeline([("on_delta", result.text)], result)
    types_and_data = _sent(monkeypatch, pipeline)

    assert [item["type"] for item in types_and_data] == ["answer", "list"]
    listing = types_and_data[1]
    assert listing["kind"] == "device"
    assert listing["title"] == "2 台设备"
    assert listing["items"][0]["id"] == "jk_zl_5f_0006"


def test_l2_entities_event_becomes_list(monkeypatch):
    payload = EntityList(
        kind=KIND_DEVICE,
        title="1 台设备",
        items=[Entity(kind=KIND_DEVICE, id="jk_zl_5f_0007", label="5F电梯厅东")],
    ).to_dict()
    result = PipelineResult(text="已查到 1 台。", level="L2", stop_reason="answered")
    pipeline = _ScriptedPipeline([("entities", payload)], result)
    types_and_data = _sent(monkeypatch, pipeline)

    lists = [item for item in types_and_data if item["type"] == "list"]
    assert len(lists) == 1
    assert lists[0]["items"][0]["id"] == "jk_zl_5f_0007"


def test_pipeline_result_dict_carries_entities():
    entities = EntityList(
        kind=KIND_DEVICE,
        title="1 台设备",
        items=[Entity(kind=KIND_DEVICE, id="a", label="A")],
    )
    data = PipelineResult(
        text="x", level="L1", stop_reason="answered", entities=entities
    ).to_dict()
    assert data["entities"]["items"][0]["id"] == "a"

    plain = PipelineResult(text="x", level="L1", stop_reason="answered").to_dict()
    assert "entities" not in plain
