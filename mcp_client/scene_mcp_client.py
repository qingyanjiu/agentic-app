import asyncio
import json
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

'''
UE 场景控制 MCP 直连客户端（Streamable HTTP）。

为什么不走 langchain_mcp_adapters / MultiServerMCPClient：
    场景服务的业务路由键 X-Session-Id 必须**逐请求**注入（决定动作帧推给
    哪个场景页面），而 MultiServerMCPClient 的 header 是启动时静态配置的，
    拿不到当前用户的场景会话。这里参考 knowledge_mcp_client.py 的直连口径：
        ① POST initialize（protocolVersion 2025-03-26）→ 响应头拿 Mcp-Session-Id
        ② POST notifications/initialized
        ③ POST tools/list（握手后缓存）
        ④ POST tools/call（带 Mcp-Session-Id + 本次请求的 X-Session-Id）

注意：
    - X-Session-Id 绝不能由 LLM 填，必须由 agent 从聊天入口透传；
    - sidecar 动作超时默认 15s，本客户端超时默认 25s，必须大于服务端；
    - 动作返回 dispatched 只代表帧已送到页面，UE 不回执，不能当成"已完成"。
'''

JSONRPC_VERSION = "2.0"
# 与 sidecar（Spring AI MCP）联调口径一致；服务端升级时同步改这里
PROTOCOL_VERSION = "2025-03-26"
# Streamable HTTP 要求的 Accept，两样都必须带
ACCEPT_HEADER = "application/json, text/event-stream"
# 必须大于 sidecar 的 scene.action-timeout-ms（默认 15000ms）
DEFAULT_TIMEOUT = 25.0

# 业务路由头：决定动作帧推给哪个场景页面（前端 WS hello 里的 sessionId）
SCENE_SESSION_HEADER = "X-Session-Id"


class SceneToolError(RuntimeError):
    """MCP 工具级失败（isError=true / JSON-RPC error）。

    这不是会话失效，重试也不会变好，而且动作类工具重试有重复下发风险，
    所以必须直接抛出，不能被 call_tool 的会话重连重试逻辑吞掉。
    """


class SceneMcpClient:
    """单个 /mcp/scene 端点对应一个客户端实例。

    X-Session-Id 是逐请求参数，因此实例可以被多个用户共享；
    Mcp-Session-Id 是协议层会话，握手一次后复用，失效时自动重连。
    """

    def __init__(self, url: str, timeout: float = DEFAULT_TIMEOUT,
                 client_name: str = "ai-agentic-app", http_client_factory=None):
        self.url = url
        self.timeout = timeout
        self.client_name = client_name
        # 测试可注入假的 httpx.AsyncClient；生产用默认实现
        self._http_client_factory = http_client_factory or httpx.AsyncClient
        # 注：属性/返回值注解不用 `X | None` 写法，容器 Python 可能是 3.9
        self._client = None        # Optional[httpx.AsyncClient]
        self._session_id = None    # Optional[str]
        self._tools = None         # Optional[list]
        self._next_id = 2  # id=1 固定留给 initialize
        # 多用户并发时只允许一个协程重建会话，避免互相 aclose 掉对方正在用的 HTTP 客户端
        self._connect_lock = None  # Optional[asyncio.Lock]

    # ---------- 对外接口 ----------

    async def list_tools(self) -> list:
        """获取并缓存 sidecar 暴露的工具定义（tools/list）"""
        if self._tools is not None:
            return self._tools

        last_err = None
        for attempt in (1, 2):
            try:
                await self._ensure_connected()
                msg_id = self._next_id
                self._next_id += 1
                body = {
                    "jsonrpc": JSONRPC_VERSION,
                    "id": msg_id,
                    "method": "tools/list",
                    "params": {},
                }
                resp = await self._client.post(self.url, json=body, headers=self._headers())

                if resp.status_code in (400, 404) and attempt == 1:
                    logger.warning(f"场景 MCP 会话失效(status={resp.status_code})，重握手后重试")
                    await self._connect()
                    continue

                if resp.status_code != 200:
                    raise RuntimeError(f"场景 MCP tools/list HTTP {resp.status_code}: {resp.text[:200]}")

                payload = self._extract_payload(resp, msg_id)
                if payload is None:
                    raise RuntimeError("场景 MCP tools/list 响应里没找到匹配 id 的 JSON-RPC 消息")
                if "error" in payload:
                    err = payload["error"]
                    raise RuntimeError(f"场景 MCP 错误 {err.get('code')}: {err.get('message')}")

                tools = (payload.get("result") or {}).get("tools") or []
                self._tools = tools
                logger.info(f"场景 MCP 工具加载完成: {len(tools)} 个")
                return tools
            except Exception as e:
                last_err = e
        raise last_err

    async def call_tool(self, name: str, arguments: dict,
                        x_session_id: Optional[str] = None) -> str:
        """调用场景工具并返回拼接后的文本内容。

        x_session_id：当前用户的场景页面会话 id（前端 WS hello 里声明），
        逐请求注入到 X-Session-Id；拿不到时传 None，sidecar 会走单会话兜底。
        """
        last_err = None
        for attempt in (1, 2):
            try:
                await self._ensure_connected()
                msg_id = self._next_id
                self._next_id += 1
                body = {
                    "jsonrpc": JSONRPC_VERSION,
                    "id": msg_id,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments or {}},
                }
                resp = await self._client.post(
                    self.url, json=body, headers=self._headers(x_session_id)
                )

                # 会话被服务端回收（sidecar 重启等）：重建会话再来一次
                if resp.status_code in (400, 404) and attempt == 1:
                    logger.warning(f"场景 MCP 会话失效(status={resp.status_code})，重握手后重试")
                    await self._connect()
                    continue

                if resp.status_code != 200:
                    raise RuntimeError(f"场景 MCP tools/call HTTP {resp.status_code}: {resp.text[:200]}")

                payload = self._extract_payload(resp, msg_id)
                if payload is None:
                    raise RuntimeError("场景 MCP 响应里没找到匹配 id 的 JSON-RPC 消息")
                if "error" in payload:
                    err = payload["error"]
                    raise SceneToolError(f"场景 MCP 错误 {err.get('code')}: {err.get('message')}")

                result = payload.get("result", {})
                text = self._result_to_text(result)
                # MCP 工具级失败：sidecar 把异常兜底成中文文案并置 isError=true。
                # 必须当错误抛出去，否则图里会把它当成功回答（"已下发"）。
                if result.get("isError"):
                    raise SceneToolError(text or "场景动作执行失败")
                return text
            except SceneToolError:
                # 工具级失败：不重试（动作可能已经部分生效，重试会重复下发）
                raise
            except Exception as e:
                last_err = e
        raise last_err

    async def get_scene_state(self, x_session_id: Optional[str] = None) -> str:
        """查询场景页面当前状态快照（只读缓存，不推送动作）"""
        return await self.call_tool("get_scene_state", {}, x_session_id)

    async def aclose(self):
        """释放底层 HTTP 连接（进程退出时调用）"""
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception as e:
                logger.warning(f"关闭场景 MCP HTTP 客户端失败(忽略): {e}")
            self._client = None
        self._session_id = None

    # ---------- 握手 ----------

    def _lock(self) -> "asyncio.Lock":
        # 延迟创建：客户端可能在事件循环外实例化
        if self._connect_lock is None:
            self._connect_lock = asyncio.Lock()
        return self._connect_lock

    async def _ensure_connected(self):
        if self._client is not None and self._session_id is not None:
            return
        async with self._lock():
            # 双重检查：等锁期间别的协程可能已经建好会话
            if self._client is None or self._session_id is None:
                await self._do_connect()

    async def _connect(self):
        """强制重建会话（会话被 sidecar 回收时使用）"""
        async with self._lock():
            await self._do_connect()

    async def _do_connect(self):
        await self.aclose()
        self._client = self._http_client_factory(timeout=self.timeout)

        # ① initialize 握手，拿 Mcp-Session-Id
        init_body = {
            "jsonrpc": JSONRPC_VERSION,
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": self.client_name, "version": "1.0"},
            },
        }
        resp = await self._client.post(self.url, json=init_body, headers=self._headers())
        if resp.status_code != 200:
            raise RuntimeError(f"场景 MCP initialize HTTP {resp.status_code}: {resp.text[:200]}")

        payload = self._extract_payload(resp, 1)
        if payload is None or "result" not in payload:
            raise RuntimeError(f"场景 MCP initialize 响应异常: {str(resp.text)[:200]}")

        # httpx 的 header 取值不区分大小写
        self._session_id = resp.headers.get("Mcp-Session-Id")
        if not self._session_id:
            raise RuntimeError("场景 MCP initialize 响应头缺少 Mcp-Session-Id")

        server_info = payload["result"].get("serverInfo", {})
        logger.info(
            f"场景 MCP 握手成功: {self.url} serverInfo={server_info} "
            f"sessionId={self._session_id[:8]}..."
        )

        # ② initialized 通知（无 id，服务端一般回 202，不校验响应体）
        try:
            await self._client.post(
                self.url,
                json={"jsonrpc": JSONRPC_VERSION, "method": "notifications/initialized"},
                headers=self._headers(),
            )
        except Exception as e:
            # 通知失败不致命，tools/call 真失败还有重试兜底
            logger.warning(f"发送场景 MCP notifications/initialized 失败(忽略): {e}")

    # ---------- 响应解析 ----------

    def _headers(self, x_session_id: Optional[str] = None) -> dict:
        headers = {"Content-Type": "application/json", "Accept": ACCEPT_HEADER}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if x_session_id:
            headers[SCENE_SESSION_HEADER] = str(x_session_id)
        return headers

    @staticmethod
    def _extract_payload(resp, msg_id: int) -> Optional[dict]:
        """从响应体里找出 id 匹配的 JSON-RPC 消息，兼容 json 单体和 SSE 两种形态"""
        content_type = resp.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            for line in resp.text.splitlines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    msg = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if msg.get("id") == msg_id:
                    return msg
            return None
        # application/json 单体
        return json.loads(resp.text)

    @staticmethod
    def _result_to_text(result: dict) -> str:
        """MCP tools/call 的 result.content 是 [{type:'text', text:'...'}] 数组，拼接文本部分"""
        contents = result.get("content") or []
        texts = [
            c.get("text", "")
            for c in contents
            if isinstance(c, dict) and c.get("type") == "text"
        ]
        text = "\n".join(t for t in texts if t)
        # sidecar 已把异常兜底成中文文案，isError=True 时文本同样是模型/用户可读的提示
        return text
