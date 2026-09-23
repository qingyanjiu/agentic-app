import json
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

'''
知识库 MCP 极简直连客户端（Streamable HTTP）。

不复用 langchain_mcp_adapters：知识库只需要"握手一次 + 反复调一个工具"，
直接按 sidecar /mcp/knowledge 联调验证过的协议口径实现（与 Postman 调试脚本一致），
少一层客户端库的协议协商不确定性：

    ① POST initialize（protocolVersion 2025-03-26）→ 响应头拿 Mcp-Session-Id
    ② POST notifications/initialized（带会话头）
    ③ POST tools/call ragflow:searchKnowledge（带会话头）

响应兼容两种形态：application/json 单体，或 text/event-stream（逐行 data: 的
JSON-RPC 消息里找匹配 id 的那条）。会话失效（400/404）时自动重握手重试一次。
'''

JSONRPC_VERSION = "2.0"
# 与 sidecar（Spring AI MCP）联调通过的协议版本；服务端升级时同步改这里
PROTOCOL_VERSION = "2025-03-26"
# Streamable HTTP 要求的 Accept，两样都必须带
ACCEPT_HEADER = "application/json, text/event-stream"


class KnowledgeMcpClient:
    """单个知识库 MCP 端点对应一个客户端实例，非线程安全（每条 WS 连接一个，够用）"""

    def __init__(self, url: str, timeout: float = 30.0, client_name: str = "ai-agentic-app"):
        self.url = url
        self.timeout = timeout
        self.client_name = client_name
        # 注：属性/返回值注解不用 `X | None` 写法，容器 Python 可能是 3.9（运行时求值会 TypeError）
        self._client = None        # Optional[httpx.AsyncClient]
        self._session_id = None    # Optional[str]
        self._next_id = 2  # id=1 固定留给 initialize

    # ---------- 对外接口 ----------

    async def call_tool(self, name: str, arguments: dict) -> str:
        """
        调用 MCP 工具并返回拼接后的文本内容。
        连接未建立时懒握手；会话失效时重握手重试一次；
        网络异常向上抛，由调用方（检索节点）兜底成友好文案。
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
                    "params": {"name": name, "arguments": arguments},
                }
                resp = await self._client.post(self.url, json=body, headers=self._headers())

                # 会话被服务端回收（重启 sidecar 等）：重建会话再来一次
                if resp.status_code in (400, 404) and attempt == 1:
                    logger.warning(f"MCP会话失效(status={resp.status_code})，重握手后重试")
                    await self._connect()
                    continue

                if resp.status_code != 200:
                    raise RuntimeError(f"MCP tools/call HTTP {resp.status_code}: {resp.text[:200]}")

                payload = self._extract_payload(resp, msg_id)
                if payload is None:
                    raise RuntimeError("MCP响应里没找到匹配 id 的 JSON-RPC 消息")
                if "error" in payload:
                    err = payload["error"]
                    raise RuntimeError(f"MCP错误 {err.get('code')}: {err.get('message')}")

                return self._result_to_text(payload.get("result", {}))
            except Exception as e:
                last_err = e
                # 第二次失败不再重试，直接抛给上层兜底
        raise last_err

    async def aclose(self):
        """释放底层 HTTP 连接，WS 会话结束时调用"""
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception as e:
                logger.warning(f"关闭MCP HTTP客户端失败(忽略): {e}")
            self._client = None
        self._session_id = None

    # ---------- 握手 ----------

    async def _ensure_connected(self):
        if self._client is None or self._session_id is None:
            await self._connect()

    async def _connect(self):
        await self.aclose()
        self._client = httpx.AsyncClient(timeout=self.timeout)

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
            raise RuntimeError(f"MCP initialize HTTP {resp.status_code}: {resp.text[:200]}")

        payload = self._extract_payload(resp, 1)
        if payload is None or "result" not in payload:
            raise RuntimeError(f"MCP initialize 响应异常: {str(resp.text)[:200]}")

        # httpx 的 header 取值不区分大小写
        self._session_id = resp.headers.get("Mcp-Session-Id")
        if not self._session_id:
            raise RuntimeError("MCP initialize 响应头缺少 Mcp-Session-Id")

        server_info = payload["result"].get("serverInfo", {})
        logger.info(f"MCP握手成功: {self.url} serverInfo={server_info} sessionId={self._session_id[:8]}...")

        # ② initialized 通知（无 id，服务端一般回 202，不校验响应体）
        try:
            await self._client.post(
                self.url,
                json={"jsonrpc": JSONRPC_VERSION, "method": "notifications/initialized"},
                headers=self._headers(),
            )
        except Exception as e:
            # 通知失败不致命，tools/call 真失败还有重试兜底
            logger.warning(f"发送 notifications/initialized 失败(忽略): {e}")

    # ---------- 响应解析 ----------

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json", "Accept": ACCEPT_HEADER}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    @staticmethod
    def _extract_payload(resp: httpx.Response, msg_id: int) -> Optional[dict]:
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
        texts = [c.get("text", "") for c in contents if isinstance(c, dict) and c.get("type") == "text"]
        text = "\n".join(t for t in texts if t)
        # Java 侧工具方法内已把异常兜底成中文文案，isError=True 时文本同样是模型可读的提示
        return text
