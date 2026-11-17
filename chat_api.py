"""会话历史的**只读** HTTP 接口（默认不挂载）。

## 现在为什么不暴露

这个接口没有任何鉴权，`user_id` 直接来自 URL —— 一旦挂到公网，谁都能拿别人的会话 id 读历史。
所以它默认**不注册路由**，只有显式打开 `CHAT_READ_API=1` 时才由 `app.py` 挂上，
用于本地联调 / 内网回填历史 / 将来接 sidecar 鉴权之后的正式入口。

## 接口

```
GET /chat/sessions/{user_id}/{session_id}/messages?limit=200&before_id=<int>
```

响应：

```json
{
  "user_id": "u1",
  "session_id": "s1",
  "messages": [
    {"id": 12, "role": "user",      "content": "5楼有哪些监控", "created_at": 1763000000.0},
    {"id": 13, "role": "assistant", "content": "5 楼监控 14 台…", "created_at": 1763000001.2}
  ],
  "count": 2,        // 本次返回条数
  "total": 27,       // 该会话总条数
  "has_more": true   // 还有更早的（拿 messages[0].id 当 before_id 往前翻）
}
```

设计取舍：

- **正序返回**（老的在前），前端直接从上往下渲染，不用再排一遍；
- 分页用 `before_id` 游标而不是 offset：翻页时新消息进来不会跳行；
- `limit` 服务端夹在 1~500；
- 与"喂给模型"的 `load()` 分开：那个只有 role/content（默认 24 条），这个带 id/时间（默认 200 条）。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Query

from memory.chat_store import get_store

logger = logging.getLogger(__name__)


def build_chat_router() -> APIRouter:
    """造一个只读路由。挂载与否由 `app.py` 按 `CHAT_READ_API` 决定。"""
    router = APIRouter(prefix="/chat", tags=["chat-history"])

    @router.get("/sessions/{user_id}/{session_id}/messages")
    def list_messages(
        user_id: str,
        session_id: str,
        limit: int = Query(200, ge=1, le=500),
        before_id: int | None = Query(None, ge=1),
    ) -> dict:
        """回显一段会话历史（正序）。会话不存在时返回空列表而不是 404 —— 前端好处理。"""
        store = get_store()
        messages, has_more = store.load_messages(
            user_id, session_id, limit=limit, before_id=before_id
        )
        total = store.count_messages(user_id, session_id)
        return {
            "user_id": user_id,
            "session_id": session_id,
            "messages": messages,
            "count": len(messages),
            "total": total,
            "has_more": has_more,
        }

    return router
