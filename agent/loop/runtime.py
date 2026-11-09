"""MCP 工具封装（官方 mcp SDK，不再依赖 langchain_mcp_adapters）。

为什么要换掉 langchain_mcp_adapters：
- 新链路已经不用 LangGraph 编排，没必要为了一个工具适配层继续拖着 langchain-core；
- 官方 mcp SDK 只依赖 httpx + anyio，体积和升级面都小得多；
- 逐请求 header、session 复用、重连这些能力在 SDK 里是原生的，不用绕。

设计：
- 每个 server 一个长连接（AsyncExitStack 持有），首次使用时懒建立；
- 同一 server 并发建连用 asyncio.Lock 保护，避免重复握手；
- 调用失败（连接失效）时丢弃缓存、重连一次；业务错误（isError）不重试；
- close() 供进程退出 / 测试使用。
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
import yaml

logger = logging.getLogger(__name__)

DEFAULT_MCP_CONFIG = "mcp_client/mcp_server_config.yaml"


def load_servers(yaml_path: str) -> dict:
    """读 mcp_server_config.yaml 的 mcp_servers 段。

    自己读而不是复用 mcp_client.mcp_loader：后者顶部 import 了
    langchain_mcp_adapters，会把 langchain 一起拉进来。
    """
    with open(yaml_path, encoding="utf-8") as handle:
        return (yaml.safe_load(handle) or {}).get("mcp_servers", {})


def extract_text(result: Any) -> str:
    """把 MCP 返回结构转成文本。

    兼容四种形态：
    - 官方 SDK 的 CallToolResult（.content 是 content block 列表）
    - {"content": [...]} 字典包装
    - [{type:"text", text:"..."}] / [TextContent(...)] 列表
    - 普通字符串
    """
    if isinstance(result, str):
        return result

    content = getattr(result, "content", None)
    if content is None and isinstance(result, dict):
        content = result.get("content")
    if content is None and isinstance(result, list):
        # 直接传 content block 列表的情况
        content = result

    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            text = getattr(block, "text", None)
            if text is None and isinstance(block, dict):
                text = block.get("text")
            if text:
                parts.append(str(text))
        if parts:
            return "\n".join(parts)
        return str(result)

    if content is not None and not isinstance(content, list):
        return str(content)

    return str(result)


class McpToolbox:
    """按 server 懒建立并复用 MCP 会话。"""

    def __init__(
        self,
        yaml_path: str = DEFAULT_MCP_CONFIG,
        timeout: float = 15.0,
        connect_timeout: float = 10.0,
    ) -> None:
        self.yaml_path = yaml_path
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self._sessions: dict[str, ClientSession] = {}
        self._stacks: dict[str, AsyncExitStack] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------ #
    # 连接管理
    # ------------------------------------------------------------------ #
    def _server_config(self, server: str) -> dict:
        conf = load_servers(self.yaml_path).get(server)
        if not isinstance(conf, dict):
            raise RuntimeError(f"MCP 配置中不存在 server [{server}]：{self.yaml_path}")
        if conf.get("direct_client"):
            raise RuntimeError(f"MCP server [{server}] 由专属直连客户端接管，不走 McpToolbox")
        if not conf.get("url"):
            raise RuntimeError(f"MCP server [{server}] 缺少 url 配置")
        return conf

    async def _connect(self, server: str) -> ClientSession:
        conf = self._server_config(server)
        url = conf["url"]
        headers = conf.get("headers")

        stack = AsyncExitStack()
        try:
            read, write, _ = await stack.enter_async_context(
                streamablehttp_client(
                    url,
                    headers=headers,
                    timeout=self.connect_timeout,
                )
            )
            session = await stack.enter_async_context(ClientSession(read, write))
            await asyncio.wait_for(session.initialize(), self.connect_timeout)
        except Exception:
            await stack.aclose()
            raise

        self._stacks[server] = stack
        self._sessions[server] = session
        logger.info(f"[mcp] server [{server}] 已连接：{url}")
        return session

    async def _session_for(self, server: str) -> ClientSession:
        cached = self._sessions.get(server)
        if cached is not None:
            return cached

        lock = self._locks.setdefault(server, asyncio.Lock())
        async with lock:
            # 双检：等锁期间可能已被别的协程连上
            cached = self._sessions.get(server)
            if cached is not None:
                return cached
            return await self._connect(server)

    async def _drop(self, server: str) -> None:
        session = self._sessions.pop(server, None)
        stack = self._stacks.pop(server, None)
        if session is not None:
            logger.info(f"[mcp] server [{server}] 连接已释放")
        if stack is not None:
            try:
                await stack.aclose()
            except Exception:
                logger.debug(f"[mcp] 关闭 [{server}] 连接时忽略异常", exc_info=True)

    async def close(self) -> None:
        for server in list(self._sessions):
            await self._drop(server)

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    async def tools_for(self, server: str) -> dict[str, str]:
        """返回 {工具名: 描述}，用于诊断和按需暴露。"""
        session = await self._session_for(server)
        result = await asyncio.wait_for(session.list_tools(), self.timeout)
        tools = getattr(result, "tools", None) or []
        mapping = {
            getattr(tool, "name", ""): getattr(tool, "description", "") or ""
            for tool in tools
            if getattr(tool, "name", "")
        }
        logger.info(f"[mcp] server [{server}] 可用工具 {len(mapping)} 个")
        return mapping

    async def call(self, server: str, tool_name: str, args: dict | None = None) -> str:
        """调用一个 MCP 工具并返回文本。

        - 连接失效（网络/会话过期）：丢弃连接、重连一次再试；
        - 业务错误（CallToolResult.isError）：不重试，抛 RuntimeError 交给上层转 is_error。
        """
        payload = args or {}
        session = await self._session_for(server)

        try:
            result = await asyncio.wait_for(
                session.call_tool(tool_name, payload), self.timeout
            )
        except Exception as exc:
            logger.warning(f"[mcp] {server}/{tool_name} 调用失败，尝试重连：{exc}")
            await self._drop(server)
            session = await self._session_for(server)
            result = await asyncio.wait_for(
                session.call_tool(tool_name, payload), self.timeout
            )

        text = extract_text(result)
        if getattr(result, "isError", False):
            raise RuntimeError(text or f"MCP 工具 {tool_name} 返回错误")
        return text
