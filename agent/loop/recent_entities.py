"""最近一次结构化清单的跨通道快照。

loop L1 产出的设备列表只发给了前端，场景通道下一轮解析「其中一个 /
第三个」时看不到它；反过来，场景通道列出的候选，业务/loop 也看不到。

这里按 `user_id:session_id` 存一份**精简快照**（kind/title/items/total），
TTL 与会话一致。它只服务于“指代上一轮列表”，不替代设备台账：
用户说出设备名称/编号时仍然走 `find_devices` 查台账。

限制与现有 loop 会话池一样：进程内存储，多副本部署需要换 Redis。
"""
from __future__ import annotations

import os
import time
from typing import Any

TTL_SECONDS = float(os.getenv("SESSION_TTL_SECONDS") or 1800)

_RECENT: dict[str, tuple[float, dict]] = {}


def _key(user_id: str, session_id: Any) -> str:
    return f"{user_id}:{session_id}"


def remember_entities(user_id: str, session_id: Any, entities: dict | None) -> None:
    """记录本轮发给前端的 list 事件（只存有 items 的清单）。"""
    if not isinstance(entities, dict) or not entities.get("items"):
        return
    _RECENT[_key(user_id, session_id)] = (time.time(), dict(entities))


def get_recent_entities(user_id: str, session_id: Any) -> dict | None:
    """取最近一次清单；过期返回 None。"""
    key = _key(user_id, session_id)
    cached = _RECENT.get(key)
    if cached is None:
        return None
    touched, data = cached
    if time.time() - touched > TTL_SECONDS:
        _RECENT.pop(key, None)
        return None
    return data


def clear_recent_entities(user_id: str, session_id: Any) -> None:
    _RECENT.pop(_key(user_id, session_id), None)


def reset_recent_entities() -> None:
    """测试用。"""
    _RECENT.clear()


def recent_entities_to_rows(snapshot: dict | None) -> list[dict]:
    """把 list 快照还原成 `find_devices` 风格的候选行。

    场景解析只需要 name/rawName/code/spaceName 四个字段；`code` 是点位
    tag 的来源，没有 code 的条目跳过（内部 id 不能直接下发给场景）。
    """
    if not isinstance(snapshot, dict) or snapshot.get("kind") != "device":
        return []
    rows: list[dict] = []
    for item in snapshot.get("items") or []:
        if not isinstance(item, dict):
            continue
        attrs = item.get("attrs") if isinstance(item.get("attrs"), dict) else {}
        code = str(attrs.get("code") or item.get("id") or "").strip()
        if not code:
            continue
        label = str(item.get("label") or code)
        rows.append({
            "name": label,
            "rawName": label,
            "code": code,
            "spaceName": str(attrs.get("space") or ""),
        })
    return rows
