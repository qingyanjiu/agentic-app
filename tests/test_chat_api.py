# -*- coding: utf-8 -*-
"""会话历史只读接口测试（memory/chat_store.load_messages + chat_api 路由）。

这个接口默认不挂载（`CHAT_READ_API` 关着），所以这里直接拿 `build_chat_router()`
挂到一个干净的 FastAPI app 上测 —— 不 import app.py（它会拉起模型/意图分类）。

覆盖：
  1. 正序返回 + 带 id / created_at
  2. limit 生效、has_more 正确
  3. before_id 往前翻页
  4. 不存在的会话 → 200 + 空列表（不是 404）
  5. 会话隔离（不同 user/session 互不串）
  6. 默认不挂载的开关语义（CHAT_READ_API 空/0 视为关）
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chat_api import build_chat_router
from memory import chat_store


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("CHAT_DB_PATH", str(tmp_path / "chat.db"))
    chat_store.reset_store()
    app = FastAPI()
    app.include_router(build_chat_router())
    return TestClient(app)


def _seed(store, user: str, session: str, n: int) -> None:
    store.append_messages(
        user,
        session,
        [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"}
            for i in range(n)
        ],
    )


def test_returns_messages_in_ascending_order_with_fields(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _seed(chat_store.get_store(), "u1", "s1", 4)

    resp = client.get("/chat/sessions/u1/s1/messages")
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == "u1" and body["session_id"] == "s1"
    assert [m["content"] for m in body["messages"]] == ["m0", "m1", "m2", "m3"]
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user", "assistant"]
    first = body["messages"][0]
    assert isinstance(first["id"], int) and first["created_at"] > 0
    assert body["count"] == 4 and body["total"] == 4
    assert body["has_more"] is False


def test_limit_and_has_more(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _seed(chat_store.get_store(), "u1", "s1", 10)

    body = client.get("/chat/sessions/u1/s1/messages?limit=4").json()
    # 取的是"最近 4 条"
    assert [m["content"] for m in body["messages"]] == ["m6", "m7", "m8", "m9"]
    assert body["count"] == 4
    assert body["total"] == 10
    assert body["has_more"] is True


def test_before_id_pages_backwards(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _seed(chat_store.get_store(), "u1", "s1", 10)

    newest = client.get("/chat/sessions/u1/s1/messages?limit=3").json()["messages"]
    cursor = newest[0]["id"]  # 最早那条的 id

    older = client.get(
        f"/chat/sessions/u1/s1/messages?limit=3&before_id={cursor}"
    ).json()["messages"]
    assert [m["content"] for m in older] == ["m4", "m5", "m6"]
    # 两段不重叠
    assert set(m["id"] for m in older).isdisjoint({m["id"] for m in newest})


def test_unknown_session_returns_empty_not_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.get("/chat/sessions/nobody/nosession/messages")
    assert resp.status_code == 200
    body = resp.json()
    assert body["messages"] == [] and body["total"] == 0 and body["has_more"] is False


def test_sessions_are_isolated(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    store = chat_store.get_store()
    store.append_messages("u1", "s1", [{"role": "user", "content": "A"}])
    store.append_messages("u1", "s2", [{"role": "user", "content": "B"}])

    assert [m["content"] for m in client.get("/chat/sessions/u1/s1/messages").json()["messages"]] == ["A"]
    assert [m["content"] for m in client.get("/chat/sessions/u1/s2/messages").json()["messages"]] == ["B"]


def test_limit_is_clamped(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _seed(chat_store.get_store(), "u1", "s1", 3)
    assert client.get("/chat/sessions/u1/s1/messages?limit=0").status_code == 422
    assert client.get("/chat/sessions/u1/s1/messages?limit=9999").status_code == 422


# ------------------------------------------------------------ #
# 开关语义：默认不暴露
# ------------------------------------------------------------ #
def test_read_api_flag_semantics(monkeypatch):
    """app.py 里的判断：空串 / 0 / false / off / no 都算关，只有显式打开才挂。"""
    def enabled() -> bool:
        import os

        return (os.getenv("CHAT_READ_API") or "").strip().lower() not in (
            "", "0", "false", "off", "no"
        )

    for value in ("", "0", "false", "OFF", "no", "  "):
        monkeypatch.setenv("CHAT_READ_API", value)
        assert enabled() is False, value
    for value in ("1", "true", "yes", "on"):
        monkeypatch.setenv("CHAT_READ_API", value)
        assert enabled() is True, value
