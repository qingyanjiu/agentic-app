"""对话历史持久化（SQLite，标准库 sqlite3，零新依赖）。

## 为什么是 SQLite 而不是 Redis

现状是**单实例部署**（容器里一个 uvicorn），先要的是"重启不失忆"，
而不是"多副本共享"。SQLite 是标准库、无外部服务、单文件可备份，正好够用。

将来上多副本（TODO 里的 T6 / T15）时，只需要再实现一个同样接口的
Redis 版 `SessionStore`，`agent/loop/ws_bridge.py` 不用动——
它只依赖下面 `get_store()` 返回对象上的四个方法。

## 存什么

只存**给模型的对话历史**（user / assistant 纯文本，和 `Session.history` 同构）：

| 列 | 说明 |
|---|---|
| `user_id` / `session_id` | 会话键，与 WS 路径里的 `{user_id}/{session_id}` 一致 |
| `role` | `user` / `assistant` |
| `content` | 消息正文 |
| `created_at` | unix 时间戳，用于过期清理 |

追问态（`Session.pending`、场景的 `IntentState.slots`）**还没落库**：
它们含候选设备列表这类结构化状态，序列化要单独设计，见 TODO T15 第 3 步。

## 环境变量

| 变量 | 作用 | 默认 |
|---|---|---|
| `CHAT_DB_PATH` | 数据库文件路径；`:memory:` = 不落盘（进程内临时库，重启即丢） | `<项目根>/chat_history.db` |
| `CHAT_RETENTION_DAYS` | 超过多少天的消息在启动时清掉；`0` = 不清理 | `30` |
| `CHAT_HISTORY_MESSAGES` | 重启后还原并喂给模型的**消息条数**上限（1 轮 = 2 条） | `24` |

空字符串按"未设置"处理（compose 的 `${VAR:-}` 会传空串），与 `models/llm.py` 一致。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

# 项目根目录（本文件在 <root>/memory/ 下）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(_ROOT, "chat_history.db")

DEFAULT_RETENTION_DAYS = 30
# 还原并喂给模型的条数上限（约等于轮数 × 2）；与 Session.max_history 对齐
DEFAULT_HISTORY_MESSAGES = 24

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT NOT NULL,
    session_id TEXT NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_session
    ON chat_messages (user_id, session_id, id);
CREATE INDEX IF NOT EXISTS idx_chat_created
    ON chat_messages (created_at);
"""


def _env(name: str) -> str:
    """读环境变量并去空白；空串视为未设置。"""
    return (os.getenv(name) or "").strip()


def resolve_db_path(path: Optional[str] = None) -> str:
    """决定数据库文件路径：显式入参 > 环境变量 > 项目根默认值。"""
    return (path or "").strip() or _env("CHAT_DB_PATH") or DEFAULT_DB_PATH


def _resolve_retention_days() -> int:
    raw = _env("CHAT_RETENTION_DAYS")
    if not raw:
        return DEFAULT_RETENTION_DAYS
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("[chat-store] CHAT_RETENTION_DAYS=%r 不是整数，按默认 %s 天",
                       raw, DEFAULT_RETENTION_DAYS)
        return DEFAULT_RETENTION_DAYS


def _resolve_history_messages() -> int:
    """还原/喂给模型的条数上限：CHAT_HISTORY_MESSAGES，默认 24（≈12 轮）。"""
    raw = _env("CHAT_HISTORY_MESSAGES")
    if not raw:
        return DEFAULT_HISTORY_MESSAGES
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("[chat-store] CHAT_HISTORY_MESSAGES=%r 不是整数，按默认 %s 条",
                       raw, DEFAULT_HISTORY_MESSAGES)
        return DEFAULT_HISTORY_MESSAGES

class ChatStore:
    """SQLite 版会话历史存储。方法都是同步的（单条 insert/select，微秒级）。"""

    def __init__(
        self,
        path: Optional[str] = None,
        *,
        retention_days: Optional[int] = None,
        history_messages: Optional[int] = None,
    ):
        self.path = resolve_db_path(path)
        self.retention_days = (
            _resolve_retention_days() if retention_days is None else max(0, retention_days)
        )
        self.history_messages = (
            _resolve_history_messages()
            if history_messages is None
            else max(0, history_messages)
        )
        self._lock = threading.Lock()  # sqlite3 连接不跨线程共享，写入串行化
        self._pruned = False
        self._init_schema()

    # ------------------------------------------------------------ #
    # 底层
    # ------------------------------------------------------------ #
    def _connect(self) -> sqlite3.Connection:
        # 每次操作新建连接：ws_bridge 的写入是在线程池里跑的，跨线程共用一个连接不安全
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        try:
            with self._lock, self._connect() as conn:
                conn.executescript(_SCHEMA)
        except sqlite3.Error as exc:
            # 存储不可用不该拖垮对话：降级成"只记内存"，每轮会打一次 warning 由调用方决定
            logger.error("[chat-store] 初始化失败，历史将不会落盘: %s", exc)

    # ------------------------------------------------------------ #
    # 对外接口（ws_bridge 只用这四个）
    # ------------------------------------------------------------ #
    def load(self, user_id: str, session_id: str, limit: Optional[int] = None) -> list[dict]:
        """取最近 limit 条消息，按时间正序返回（直接喂给模型）。

        limit 不传就用 `CHAT_HISTORY_MESSAGES`（默认 24 条 ≈ 12 轮）：
        条数可配是为了控制上下文预算，跟"跨不跨会话"是两件事。
        """
        limit = self.history_messages if limit is None else limit
        if limit <= 0:
            return []
        try:
            with self._lock, self._connect() as conn:
                rows = conn.execute(
                    "SELECT role, content FROM chat_messages "
                    "WHERE user_id = ? AND session_id = ? "
                    "ORDER BY id DESC LIMIT ?",
                    (user_id, session_id, limit),
                ).fetchall()
        except sqlite3.Error as exc:
            logger.warning("[chat-store] 读取历史失败（按空历史继续）: %s", exc)
            return []
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    def append_messages(self, user_id: str, session_id: str, messages: list[dict]) -> int:
        """批量追加消息，返回写入条数。content 为空的消息跳过。"""
        rows = [
            (user_id, session_id, str(m.get("role") or ""), str(m.get("content") or ""),
             time.time())
            for m in messages
            if (m.get("content") or "").strip() and m.get("role")
        ]
        if not rows:
            return 0
        try:
            with self._lock, self._connect() as conn:
                conn.executemany(
                    "INSERT INTO chat_messages (user_id, session_id, role, content, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    rows,
                )
        except sqlite3.Error as exc:
            logger.warning("[chat-store] 写入历史失败（本轮只留内存）: %s", exc)
            return 0
        return len(rows)

    def delete_session(self, user_id: str, session_id: str) -> int:
        """删掉某个会话的全部历史（前端「新对话」不需要调，session_id 本来就换了）。"""
        try:
            with self._lock, self._connect() as conn:
                cur = conn.execute(
                    "DELETE FROM chat_messages WHERE user_id = ? AND session_id = ?",
                    (user_id, session_id),
                )
            return cur.rowcount or 0
        except sqlite3.Error as exc:
            logger.warning("[chat-store] 删除会话失败: %s", exc)
            return 0

    def prune(self, days: Optional[int] = None) -> int:
        """清理超过 N 天的消息；days<=0 表示不清理。返回删除条数。"""
        days = self.retention_days if days is None else days
        if days <= 0:
            return 0
        cutoff = time.time() - days * 86400
        try:
            with self._lock, self._connect() as conn:
                cur = conn.execute("DELETE FROM chat_messages WHERE created_at < ?", (cutoff,))
            return cur.rowcount or 0
        except sqlite3.Error as exc:
            logger.warning("[chat-store] 过期清理失败: %s", exc)
            return 0


class NullChatStore(ChatStore):
    """不落盘的空实现：`CHAT_DB_PATH=:memory:` 或初始化失败时用。"""

    def __init__(self) -> None:  # 故意不调 super().__init__，不建库
        self.path = ":memory:"
        self.retention_days = 0
        # 不落盘，但"喂多少条给模型"的预算仍然生效（内存会话的轮数上限跟着它走）
        self.history_messages = _resolve_history_messages()
        self._lock = threading.Lock()
        self._pruned = True

    def load(self, user_id: str, session_id: str, limit: Optional[int] = None) -> list[dict]:
        return []

    def append_messages(self, user_id: str, session_id: str, messages: list[dict]) -> int:
        return 0

    def delete_session(self, user_id: str, session_id: str) -> int:
        return 0

    def prune(self, days: Optional[int] = None) -> int:
        return 0


_store: Optional[ChatStore] = None


def build_store(path: Optional[str] = None) -> ChatStore:
    """按路径/环境变量造一个 store。`:memory:` 返回不落盘实现。"""
    if resolve_db_path(path) == ":memory:":
        return NullChatStore()
    return ChatStore(path)


def get_store() -> ChatStore:
    """进程单例；第一次取用时顺手做一次过期清理。"""
    global _store
    if _store is None:
        _store = build_store()
        removed = _store.prune()
        if removed:
            logger.info("[chat-store] 启动清理 %s 条过期历史（保留 %s 天）",
                        removed, _store.retention_days)
        logger.info("[chat-store] 对话历史落盘: %s", _store.path)
    return _store


def reset_store() -> None:
    """测试用：丢掉单例（下次 get_store 重新按环境变量建）。"""
    global _store
    _store = None
