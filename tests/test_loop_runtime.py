"""MCP 接入层（官方 SDK）的离线测试。

不连真实 sidecar：用假 session 注入缓存，验证文本提取、错误语义和重连一次。
"""
import asyncio
from types import SimpleNamespace as NS

import pytest

from agent.loop.runtime import McpToolbox, extract_text
from agent.loop.types import ToolTimeoutError


class _FakeSession:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    async def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments))
        item = self.results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def list_tools(self):
        return NS(tools=[NS(name="t1", description="demo")])


def test_extract_text_handles_sdk_result():
    result = NS(content=[NS(text="第一段"), NS(text="第二段")])
    assert extract_text(result) == "第一段\n第二段"


def test_extract_text_handles_dict_and_list_and_str():
    assert extract_text({"content": [{"type": "text", "text": "hi"}]}) == "hi"
    assert extract_text([{"type": "text", "text": "hi"}]) == "hi"
    assert extract_text("plain") == "plain"


def test_call_returns_text():
    box = McpToolbox()
    box._sessions["s"] = _FakeSession([NS(content=[NS(text="ok")])])

    assert asyncio.run(box.call("s", "t1", {"a": 1})) == "ok"


def test_call_raises_on_is_error():
    box = McpToolbox()
    box._sessions["s"] = _FakeSession([NS(content=[NS(text="boom")], isError=True)])

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(box.call("s", "t1", {}))


def test_call_reconnects_once_on_transport_failure():
    box = McpToolbox()
    broken = _FakeSession([RuntimeError("connection lost")])
    healthy = _FakeSession([NS(content=[NS(text="recovered")])])
    box._sessions["s"] = broken

    async def fake_connect(server: str):
        box._sessions[server] = healthy
        return healthy

    box._connect = fake_connect  # type: ignore[method-assign]

    assert asyncio.run(box.call("s", "t1", {})) == "recovered"
    assert len(broken.calls) == 1
    assert len(healthy.calls) == 1


def test_tools_for_returns_name_description_map():
    box = McpToolbox()
    box._sessions["s"] = _FakeSession([])

    mapping = asyncio.run(box.tools_for("s"))

    assert mapping == {"t1": "demo"}


def test_timeouts_are_env_configurable(monkeypatch):
    monkeypatch.setenv("MCP_CALL_TIMEOUT", "42")
    monkeypatch.setenv("MCP_CONNECT_TIMEOUT", "7")

    box = McpToolbox()

    assert box.timeout == 42
    assert box.connect_timeout == 7


def test_timeout_env_falls_back_on_invalid_value(monkeypatch):
    monkeypatch.setenv("MCP_CALL_TIMEOUT", "not-a-number")
    monkeypatch.setenv("MCP_CONNECT_TIMEOUT", "-3")

    box = McpToolbox()

    assert box.timeout == 10.0
    assert box.connect_timeout == 10.0


def test_timeout_is_not_retried():
    """超时说明服务端可能还在执行，重试只会把同一个慢调用再发一遍。"""

    class _SlowSession:
        def __init__(self):
            self.calls = 0

        async def call_tool(self, name, arguments=None):
            self.calls += 1
            await asyncio.sleep(1)

    box = McpToolbox(timeout=0.01)
    slow = _SlowSession()
    box._sessions["s"] = slow

    with pytest.raises(ToolTimeoutError, match="超时"):
        asyncio.run(box.call("s", "t1", {}))

    assert slow.calls == 1
