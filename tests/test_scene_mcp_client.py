# -*- coding: utf-8 -*-
"""
SceneMcpClient 单元测试（不连真实 sidecar）。

用 http_client_factory 注入假 httpx.AsyncClient，断言三件事：
  1. 握手顺序与 protocolVersion 口径（initialize → initialized → tools/list）
  2. X-Session-Id 逐请求注入，且与协议层 Mcp-Session-Id 同时存在
  3. SSE / JSON 两种响应形态都能解析，会话失效(404)会重握手重试一次
"""
import json

import pytest

from conftest import run
from mcp_client.scene_mcp_client import (
    PROTOCOL_VERSION,
    SceneMcpClient,
    SceneToolError,
)


class FakeResponse:
    def __init__(self, status_code=200, headers=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text


class FakeHttpClient:
    """按 MCP 方法分发的假 httpx.AsyncClient"""

    def __init__(self, timeout=None):
        self.timeout = timeout
        self.requests = []
        self.closed = False
        # 第 N 次 tools/call 是否先返回 404（模拟 sidecar 重启后会话失效）
        self.fail_calls_before_success = 0
        # tools/call 返回 isError=true（模拟动作被拒绝/禁用）
        self.tool_error = None
        self._call_count = 0

    async def post(self, url, json=None, headers=None):
        body = json or {}
        self.requests.append({"url": url, "body": body, "headers": headers or {}})
        method = body.get("method")

        if method == "initialize":
            return FakeResponse(
                headers={"Mcp-Session-Id": "mcp-sess-001", "content-type": "application/json"},
                text=json.dumps({
                    "jsonrpc": "2.0",
                    "id": body.get("id"),
                    "result": {
                        "protocolVersion": PROTOCOL_VERSION,
                        "serverInfo": {"name": "scene-sidecar", "version": "1.0"},
                    },
                }),
            )

        if method == "notifications/initialized":
            return FakeResponse(status_code=202, text="")

        if method == "tools/list":
            return FakeResponse(
                headers={"content-type": "text/event-stream", "Mcp-Session-Id": "mcp-sess-001"},
                text="event: message\ndata: "
                     + json.dumps({
                         "jsonrpc": "2.0",
                         "id": body.get("id"),
                         "result": {"tools": [{"name": "scene_reset_view"}]},
                     })
                     + "\n\n",
            )

        if method == "tools/call":
            self._call_count += 1
            if self._call_count <= self.fail_calls_before_success:
                return FakeResponse(status_code=404, text="session not found")

            name = (body.get("params") or {}).get("name")
            if self.tool_error is not None:
                return FakeResponse(
                    headers={"content-type": "application/json"},
                    text=json.dumps({
                        "jsonrpc": "2.0",
                        "id": body.get("id"),
                        "result": {
                            "isError": True,
                            "content": [{"type": "text", "text": self.tool_error}],
                        },
                    }),
                )
            return FakeResponse(
                headers={"content-type": "application/json"},
                text=json.dumps({
                    "jsonrpc": "2.0",
                    "id": body.get("id"),
                    "result": {
                        "content": [
                            {"type": "text", "text": f"已下发动作 {name}"},
                            {"type": "text", "text": "注意：UE 不回执"},
                        ]
                    },
                }),
            )

        return FakeResponse(status_code=400, text="unknown method")

    async def aclose(self):
        self.closed = True


def _client(fake):
    return SceneMcpClient(
        "http://10.0.0.1:2222/mcp/scene",
        http_client_factory=lambda **kwargs: fake,
    )


def test_handshake_then_list_tools():
    fake = FakeHttpClient()
    client = _client(fake)

    tools = run(client.list_tools())

    assert tools == [{"name": "scene_reset_view"}]
    methods = [r["body"].get("method") for r in fake.requests]
    assert methods == ["initialize", "notifications/initialized", "tools/list"]

    init = fake.requests[0]
    assert init["url"].endswith("/mcp/scene")
    assert init["body"]["params"]["protocolVersion"] == PROTOCOL_VERSION
    assert "Mcp-Session-Id" not in init["headers"]  # 握手前还没有会话

    # 握手后：协议会话头必须带上
    assert fake.requests[1]["headers"]["Mcp-Session-Id"] == "mcp-sess-001"
    assert fake.requests[2]["headers"]["Mcp-Session-Id"] == "mcp-sess-001"


def test_list_tools_cached():
    fake = FakeHttpClient()
    client = _client(fake)

    run(client.list_tools())
    run(client.list_tools())

    assert [r["body"].get("method") for r in fake.requests].count("tools/list") == 1


def test_call_tool_injects_x_session_id_per_request():
    fake = FakeHttpClient()
    client = _client(fake)

    text = run(client.call_tool("scene_switch_floor", {"floor": 3, "focus": True}, "scene-abc"))

    # content 里的多个 text 块按行拼接
    assert text == "已下发动作 scene_switch_floor\n注意：UE 不回执"

    call_req = [r for r in fake.requests if r["body"].get("method") == "tools/call"][-1]
    assert call_req["body"]["params"] == {
        "name": "scene_switch_floor",
        "arguments": {"floor": 3, "focus": True},
    }
    assert call_req["headers"]["X-Session-Id"] == "scene-abc"
    assert call_req["headers"]["Mcp-Session-Id"] == "mcp-sess-001"
    # Streamable HTTP 必须同时接受 json 和 event-stream
    assert "text/event-stream" in call_req["headers"]["Accept"]


def test_x_session_id_is_per_call_not_persisted():
    """同一客户端服务多个用户：上一个请求的 session 绝不能残留到下一个"""
    fake = FakeHttpClient()
    client = _client(fake)

    run(client.call_tool("scene_reset_view", {}, "user-a"))
    run(client.call_tool("scene_reset_view", {}, "user-b"))
    run(client.call_tool("get_scene_state", {}, None))

    call_reqs = [r for r in fake.requests if r["body"].get("method") == "tools/call"]
    assert call_reqs[0]["headers"]["X-Session-Id"] == "user-a"
    assert call_reqs[1]["headers"]["X-Session-Id"] == "user-b"
    # 不传就完全不带该头，让 sidecar 走单会话兜底
    assert "X-Session-Id" not in call_reqs[2]["headers"]


def test_call_tool_retries_after_session_expired():
    fake = FakeHttpClient()
    fake.fail_calls_before_success = 1
    client = _client(fake)

    text = run(client.call_tool("scene_reset_view", {}, "scene-abc"))

    assert "已下发动作 scene_reset_view" in text
    # 404 → 重新 initialize → 再调一次 tools/call
    methods = [r["body"].get("method") for r in fake.requests]
    assert methods.count("initialize") == 2
    assert methods.count("tools/call") == 2


def test_tool_level_error_raises_and_does_not_retry():
    """isError=true 是工具失败（可能已部分生效），不能当成功、也不能重试"""
    fake = FakeHttpClient()
    fake.tool_error = "调用 scene_set_area_state 失败：该工具默认禁用"
    client = _client(fake)

    with pytest.raises(SceneToolError):
        run(client.call_tool("scene_set_area_state", {"tag": "A-01", "state": "撤防"}, "s1"))

    assert [r["body"].get("method") for r in fake.requests].count("tools/call") == 1


def test_get_scene_state_uses_readonly_tool():
    fake = FakeHttpClient()
    client = _client(fake)

    run(client.get_scene_state("scene-abc"))

    call_req = [r for r in fake.requests if r["body"].get("method") == "tools/call"][-1]
    assert call_req["body"]["params"] == {"name": "get_scene_state", "arguments": {}}
    assert call_req["headers"]["X-Session-Id"] == "scene-abc"
