# -*- coding: utf-8 -*-
"""对话历史持久化测试（memory/chat_store.py + ws_bridge 的读写接点）。

覆盖：
  1. 写入 / 读取的顺序与 role
  2. limit 取最近 N 条
  3. 模拟重启：换一个 ChatStore 实例还能读到
  4. 会话隔离（同库不同 session 互不串）
  5. delete_session / prune 清理
  6. CHAT_DB_PATH 空串按未设置、`:memory:` 不落盘
  7. ws_bridge：restore_history 补水、handle_chat 首轮带上历史并把本轮写盘
"""
import asyncio
import json
import sqlite3
import time

from agent.loop import ws_bridge
from agent.loop.pipeline import PipelineResult, Session
from memory import chat_store


def _store(tmp_path, monkeypatch, name="chat.db") -> chat_store.ChatStore:
    """造一个指向 tmp_path 的真 SQLite store，并接管进程单例。"""
    path = str(tmp_path / name)
    monkeypatch.setenv("CHAT_DB_PATH", path)
    chat_store.reset_store()
    store = chat_store.get_store()
    assert store.path == path
    return store


# ============================================================
# 1. 基本读写
# ============================================================
def test_append_then_load_keeps_order_and_roles(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    store.append_messages("u1", "s1", [
        {"role": "user", "content": "现在园区有多少人"},
        {"role": "assistant", "content": "实时在园人数 375 人"},
    ])
    store.append_messages("u1", "s1", [
        {"role": "user", "content": "那今天进了多少"},
        {"role": "assistant", "content": "今日进入 472 人"},
    ])

    history = store.load("u1", "s1")
    assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant"]
    assert history[0]["content"] == "现在园区有多少人"
    assert history[-1]["content"] == "今日进入 472 人"


def test_empty_content_is_skipped(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    written = store.append_messages("u1", "s1", [
        {"role": "user", "content": "   "},
        {"role": "", "content": "没有角色"},
        {"role": "user", "content": "有效"},
    ])
    assert written == 1
    assert [m["content"] for m in store.load("u1", "s1")] == ["有效"]


def test_load_limit_returns_latest(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    store.append_messages("u1", "s1", [
        {"role": "user", "content": f"q{i}"} for i in range(10)
    ])
    assert [m["content"] for m in store.load("u1", "s1", limit=3)] == ["q7", "q8", "q9"]
    assert store.load("u1", "s1", limit=0) == []


def test_sessions_are_isolated(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    store.append_messages("u1", "s1", [{"role": "user", "content": "会话一"}])
    store.append_messages("u1", "s2", [{"role": "user", "content": "会话二"}])
    store.append_messages("u2", "s1", [{"role": "user", "content": "别人的会话"}])

    assert [m["content"] for m in store.load("u1", "s1")] == ["会话一"]
    assert [m["content"] for m in store.load("u1", "s2")] == ["会话二"]
    assert [m["content"] for m in store.load("u2", "s1")] == ["别人的会话"]


# ============================================================
# 2. 重启：换实例还能读到（这是这次改动的核心目的）
# ============================================================
def test_history_survives_new_store_instance(tmp_path, monkeypatch):
    path = str(tmp_path / "chat.db")
    first = chat_store.ChatStore(path)
    first.append_messages("u1", "s1", [
        {"role": "user", "content": "上一轮的问题"},
        {"role": "assistant", "content": "上一轮的回答"},
    ])

    second = chat_store.ChatStore(path)  # 模拟进程重启
    assert [m["content"] for m in second.load("u1", "s1")] == ["上一轮的问题", "上一轮的回答"]


# ============================================================
# 3. 清理
# ============================================================
def test_delete_session_only_removes_that_session(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    store.append_messages("u1", "s1", [{"role": "user", "content": "a"}])
    store.append_messages("u1", "s2", [{"role": "user", "content": "b"}])

    assert store.delete_session("u1", "s1") == 1
    assert store.load("u1", "s1") == []
    assert [m["content"] for m in store.load("u1", "s2")] == ["b"]


def test_prune_removes_old_rows(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    store.append_messages("u1", "s1", [{"role": "user", "content": "新消息"}])
    store.append_messages("u1", "s1", [{"role": "user", "content": "老消息"}])

    # 把老消息的 created_at 改成 40 天前
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE chat_messages SET created_at = ? WHERE content = ?",
            (time.time() - 40 * 86400, "老消息"),
        )

    assert store.prune(days=30) == 1
    assert [m["content"] for m in store.load("u1", "s1")] == ["新消息"]


def test_prune_disabled_with_zero_days(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    store.append_messages("u1", "s1", [{"role": "user", "content": "x"}])
    assert store.prune(days=0) == 0
    assert len(store.load("u1", "s1")) == 1


# ============================================================
# 4. 配置
# ============================================================
def test_empty_env_falls_back_to_default_path(monkeypatch):
    monkeypatch.setenv("CHAT_DB_PATH", "   ")
    assert chat_store.resolve_db_path() == chat_store.DEFAULT_DB_PATH


def test_memory_path_means_no_persist(monkeypatch):
    monkeypatch.setenv("CHAT_DB_PATH", ":memory:")
    store = chat_store.build_store()
    assert isinstance(store, chat_store.NullChatStore)
    assert store.append_messages("u", "s", [{"role": "user", "content": "x"}]) == 0
    assert store.load("u", "s") == []


def test_retention_days_bad_value_falls_back(monkeypatch):
    monkeypatch.setenv("CHAT_RETENTION_DAYS", "abc")
    assert chat_store._resolve_retention_days() == chat_store.DEFAULT_RETENTION_DAYS
    monkeypatch.setenv("CHAT_RETENTION_DAYS", "")
    assert chat_store._resolve_retention_days() == chat_store.DEFAULT_RETENTION_DAYS
    monkeypatch.setenv("CHAT_RETENTION_DAYS", "7")
    assert chat_store._resolve_retention_days() == 7


# ============================================================
# 5. ws_bridge 接点
# ============================================================
def test_restore_history_fills_empty_session(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    store.append_messages("u1", "s1", [
        {"role": "user", "content": "上午问过的设备"},
        {"role": "assistant", "content": "南门门禁 8 台"},
    ])

    session = Session(user_id="u1", session_id="s1")
    assert ws_bridge.restore_history(session) == 2
    assert [m["content"] for m in session.history] == ["上午问过的设备", "南门门禁 8 台"]

    # 已有历史时不重复补
    assert ws_bridge.restore_history(session) == 0
    assert len(session.history) == 2


def test_persist_messages_writes_rows(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    session = Session(user_id="u1", session_id="s1")
    session.add_turn("问题", "回答")

    assert ws_bridge.persist_messages(session, session.history) == 2
    assert [m["role"] for m in store.load("u1", "s1")] == ["user", "assistant"]


class _RecordingPipeline:
    """记录喂进来的 history，并真的调 session.add_turn（模拟真实 pipeline）。"""

    def __init__(self):
        self.seen_history: list[dict] = []

    async def handle(self, query, session, cancel=None, on_delta=None, on_event=None):
        self.seen_history = list(session.history)
        session.add_turn(query, "本轮回答")
        return PipelineResult(text="本轮回答", level="L1", stop_reason="answered")


class _FakeWebSocket:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))


def test_handle_chat_restores_then_persists(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    # 上一轮（重启前）的对话已经在库里
    store.append_messages("u1", "s1", [
        {"role": "user", "content": "上一轮问题"},
        {"role": "assistant", "content": "上一轮回答"},
    ])

    pipeline = _RecordingPipeline()
    monkeypatch.setattr(ws_bridge, "get_pipeline", lambda llm: pipeline)
    ws_bridge.reset_sessions()

    ws = _FakeWebSocket()
    handled = asyncio.run(ws_bridge.handle_chat(ws, "第三个", "u1", "s1", llm=None))
    assert handled is True

    # 模型这次看到了恢复出来的历史
    assert [m["content"] for m in pipeline.seen_history] == ["上一轮问题", "上一轮回答"]
    # 本轮也写盘了，且没有重复写历史
    assert [m["content"] for m in store.load("u1", "s1")] == [
        "上一轮问题", "上一轮回答", "第三个", "本轮回答",
    ]
    ws_bridge.reset_sessions()


def test_handle_chat_handoff_does_not_persist(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)

    class _HandoffPipeline:
        async def handle(self, query, session, cancel=None, on_delta=None, on_event=None):
            return PipelineResult(text="", level="L0", stop_reason="handoff")

    monkeypatch.setattr(ws_bridge, "get_pipeline", lambda llm: _HandoffPipeline())
    ws_bridge.reset_sessions()

    handled = asyncio.run(ws_bridge.handle_chat(_FakeWebSocket(), "切到 3 楼", "u1", "s1", llm=None))
    assert handled is False
    assert store.load("u1", "s1") == []
    ws_bridge.reset_sessions()


# ============================================================
# 6. 条数可配（CHAT_HISTORY_MESSAGES）
# 条数上限与「跨不跨会话」是两个维度：这里只管喂给模型多少条
# ============================================================
def test_history_messages_env_controls_load(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_HISTORY_MESSAGES", "4")
    store = _store(tmp_path, monkeypatch)
    store.append_messages("u1", "s1", [
        {"role": "user", "content": f"m{i}"} for i in range(10)
    ])
    assert store.history_messages == 4
    assert [m["content"] for m in store.load("u1", "s1")] == ["m6", "m7", "m8", "m9"]
    # 显式传 limit 仍然覆盖环境变量
    assert [m["content"] for m in store.load("u1", "s1", limit=2)] == ["m8", "m9"]


def test_history_messages_default_and_bad_value(monkeypatch):
    monkeypatch.delenv("CHAT_HISTORY_MESSAGES", raising=False)
    assert chat_store._resolve_history_messages() == chat_store.DEFAULT_HISTORY_MESSAGES
    monkeypatch.setenv("CHAT_HISTORY_MESSAGES", "")
    assert chat_store._resolve_history_messages() == chat_store.DEFAULT_HISTORY_MESSAGES
    monkeypatch.setenv("CHAT_HISTORY_MESSAGES", "abc")
    assert chat_store._resolve_history_messages() == chat_store.DEFAULT_HISTORY_MESSAGES


def test_session_max_history_follows_env(tmp_path, monkeypatch):
    """内存保留轮数要跟着条数走：6 条 = 3 轮，否则还原回来第一轮就被裁。"""
    monkeypatch.setenv("CHAT_HISTORY_MESSAGES", "6")
    _store(tmp_path, monkeypatch)
    ws_bridge.reset_sessions()
    session = ws_bridge.get_session("u1", "s1")
    assert session.max_history == 3
    ws_bridge.reset_sessions()


def test_restore_history_uses_configured_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_HISTORY_MESSAGES", "4")
    store = _store(tmp_path, monkeypatch)
    store.append_messages("u1", "s1", [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"h{i}"} for i in range(10)
    ])

    session = Session(user_id="u1", session_id="s1")
    assert ws_bridge.restore_history(session) == 4
    assert [m["content"] for m in session.history] == ["h6", "h7", "h8", "h9"]
