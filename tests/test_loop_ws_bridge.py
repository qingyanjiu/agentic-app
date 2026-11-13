"""ws_bridge 的事件协议测试。

前端只认这几种 data.type，发错一个字段用户就看到"没有流式"：
    answer  追加（data.content）
    tool    工具状态（data.name）
    ask     追问（data.question，不是 content）
    error   错误（data.content）
    done    {"status": "done"}，可带 level / escalated
"""
import asyncio
import json
import logging
from types import SimpleNamespace as NS

import pytest

from agent.loop import ws_bridge
from agent.loop.entities import KIND_DEVICE, Entity, EntityList
from agent.loop.pipeline import PipelineResult


class _FakeWebSocket:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))


class _FakePipeline:
    """按脚本回放 loop 内部事件，模拟真实的 L2 流式过程。"""

    def __init__(self, events, result):
        self.events = events
        self.result = result

    async def handle(self, query, session, cancel=None, on_delta=None, on_event=None):
        for kind, payload in self.events:
            if kind == "tool_start":
                await on_event(NS(type="tool_start", data={"name": payload}))
            elif kind == "delta":
                await on_event(NS(type="assistant_delta", data={"text": payload}))
            elif kind == "on_delta":
                # L1 走 Composer，直接调 on_delta
                await on_delta(payload)
        return self.result


def _run(monkeypatch, pipeline) -> list[dict]:
    monkeypatch.setattr(ws_bridge, "get_pipeline", lambda llm: pipeline)
    ws_bridge.reset_sessions()
    ws = _FakeWebSocket()
    handled = asyncio.run(ws_bridge.handle_chat(ws, "查一下门禁", "u", "s", llm=None))
    assert handled is True
    return ws.sent


def _custom(sent: list[dict]) -> list[dict]:
    return [item["data"] for item in sent if item.get("event") == "custom"]


def test_l2_stream_sends_answer_chunks_and_tool_events(monkeypatch):
    result = PipelineResult(text="共有 3 台门禁设备。", level="L2", stop_reason="answered")
    pipeline = _FakePipeline(
        [
            ("delta", "我先查"),
            ("delta", "一下。"),
            ("tool_start", "device_query__list"),
            ("delta", "共有 3 台"),
            ("delta", "门禁设备。"),
        ],
        result,
    )

    sent = _run(monkeypatch, pipeline)
    custom = _custom(sent)

    # 分块追加，前端 currentContent 才能逐字渲染
    assert custom[0] == {"type": "answer", "content": "我先查"}
    assert custom[1] == {"type": "answer", "content": "一下。"}
    # 工具状态：前端据此把刚才那段话落成"过程稿"气泡
    assert custom[2] == {"type": "tool", "name": "device_query__list"}
    assert custom[3:] == [
        {"type": "answer", "content": "共有 3 台"},
        {"type": "answer", "content": "门禁设备。"},
    ]

    # 已经流式发过，就不能再补一整段，否则前端会看到重复内容
    joined = "".join(c["content"] for c in custom if c["type"] == "answer")
    assert joined == "我先查一下。共有 3 台门禁设备。"

    done = [item for item in sent if item.get("status") == "done"]
    assert done == [{"status": "done", "level": "L2", "escalated": False}]


def test_non_stream_level_falls_back_to_single_answer(monkeypatch):
    """一个字符都没流过（L1 关流式 / 模型直接给结论）→ 一次性发完整回答。"""
    result = PipelineResult(text="一层共 12 台门禁。", level="L1", stop_reason="answered")
    pipeline = _FakePipeline([], result)

    sent = _run(monkeypatch, pipeline)
    custom = _custom(sent)

    assert custom == [{"type": "answer", "content": "一层共 12 台门禁。"}]
    assert sent[-1] == {"status": "done", "level": "L1", "escalated": False}


def test_l1_on_delta_is_forwarded_as_answer(monkeypatch):
    result = PipelineResult(text="318 个空位", level="L1", stop_reason="answered")
    pipeline = _FakePipeline([("on_delta", "318 "), ("on_delta", "个空位")], result)

    custom = _custom(_run(monkeypatch, pipeline))

    assert custom == [
        {"type": "answer", "content": "318 "},
        {"type": "answer", "content": "个空位"},
    ]


def test_ask_uses_question_field(monkeypatch):
    """追问气泡前端读的是 question 字段，发 content 会变成空气泡。"""
    result = PipelineResult(text="请问查询哪个楼层？", level="L1", stop_reason="asked")
    pipeline = _FakePipeline([], result)

    custom = _custom(_run(monkeypatch, pipeline))

    assert custom == [{"type": "ask", "question": "请问查询哪个楼层？"}]


def test_handoff_returns_false_and_sends_nothing(monkeypatch):
    result = PipelineResult(text="场景控制", level="L0", stop_reason="handoff")
    pipeline = _FakePipeline([], result)
    monkeypatch.setattr(ws_bridge, "get_pipeline", lambda llm: pipeline)
    ws_bridge.reset_sessions()
    ws = _FakeWebSocket()

    handled = asyncio.run(ws_bridge.handle_chat(ws, "打开空调", "u", "s", llm=None))

    assert handled is False
    assert ws.sent == []


def test_internal_error_event_is_not_forwarded_as_answer(monkeypatch):
    """loop 内部事件（tool_end 等）不应漏成前端可见的消息。"""
    result = PipelineResult(text="好了", level="L2", stop_reason="answered")
    pipeline = _FakePipeline(
        [("delta", "好了"), ("tool_start", "x")],
        result,
    )

    custom = _custom(_run(monkeypatch, pipeline))

    assert all(c["type"] in {"answer", "tool"} for c in custom)


def test_send_failure_does_not_break_the_turn(monkeypatch):
    """客户端断开后服务端还在推事件：send 失败要被吞掉，不能把整轮打断。

    线上：前端刷新 → 服务端 send_text 抛 → ASGI 报
    `Unexpected ASGI message 'websocket.send'`，还被上层当成"灰度链路异常"重走旧链路。
    """
    class _ClosedSocket:
        def __init__(self):
            self.attempts = 0

        async def send_text(self, text):
            self.attempts += 1
            raise RuntimeError("Unexpected ASGI message 'websocket.send'")

    result = PipelineResult(
        text="2 台设备", level="L1", stop_reason="answered",
        entities=EntityList(
            kind=KIND_DEVICE, title="2 台设备",
            items=[Entity(kind=KIND_DEVICE, id="dz_park_0001", label="南门道闸进口")],
        ),
    )
    pipeline = _FakePipeline([("delta", "2 台设备")], result)
    monkeypatch.setattr(ws_bridge, "get_pipeline", lambda llm: pipeline)
    ws_bridge.reset_sessions()

    ws = _ClosedSocket()
    handled = asyncio.run(ws_bridge.handle_chat(ws, "有哪些道闸设备", "u", "s", llm=None))

    assert handled is True          # 仍然算"已处理"，不回落旧链路
    assert ws.attempts >= 2         # 试过 answer 和 list，都失败但都没抛


def test_request_stop_interrupts_running_turn(monkeypatch):
    """停止按钮：本轮还在跑时置 cancel，loop 在下一个检查点退出。"""

    class _StuckPipeline:
        """模拟"卡住"的一轮：只有收到 cancel 才结束。"""

        async def handle(self, query, session, cancel=None, on_delta=None, on_event=None):
            for _ in range(300):
                if cancel is not None and cancel.is_set():
                    return PipelineResult(
                        text="已取消本轮查询。", level="L2", stop_reason="aborted"
                    )
                await asyncio.sleep(0.01)
            return PipelineResult(text="超时", level="L2", stop_reason="answered")

    monkeypatch.setattr(ws_bridge, "get_pipeline", lambda llm: _StuckPipeline())
    ws_bridge.reset_sessions()
    ws = _FakeWebSocket()

    async def scenario():
        task = asyncio.create_task(
            ws_bridge.handle_chat(ws, "查一下设备", "u", "s", llm=None)
        )
        await asyncio.sleep(0.05)                 # 等 handle_chat 注册好 cancel
        stopped = ws_bridge.request_stop("u", "s")
        handled = await task
        return stopped, handled

    stopped, handled = asyncio.run(scenario())

    assert stopped is True                        # 确实通知到了
    assert handled is True                        # 仍算"已处理"，不回落旧链路
    assert any(d.get("content") == "已取消本轮查询。" for d in _custom(ws.sent))
    # 本轮结束后要摘掉，否则停止会打到已结束的轮次
    assert ws_bridge.request_stop("u", "s") is False


def test_request_stop_without_running_turn():
    ws_bridge.reset_sessions()
    assert ws_bridge.request_stop("nobody", "nothing") is False


def test_query_is_logged(monkeypatch, caplog):
    """排查全靠这行：谁、哪个会话、问了什么。"""
    result = PipelineResult(text="ok", level="L1", stop_reason="answered")
    pipeline = _FakePipeline([("on_delta", "ok")], result)
    monkeypatch.setattr(ws_bridge, "get_pipeline", lambda llm: pipeline)
    ws_bridge.reset_sessions()

    with caplog.at_level(logging.INFO, logger="agent.loop.ws_bridge"):
        asyncio.run(
            ws_bridge.handle_chat(_FakeWebSocket(), "园区有哪些监控", "u1", "s1", llm=None)
        )

    assert "园区有哪些监控" in caplog.text
    assert "[loop] 开始处理提问" in caplog.text
