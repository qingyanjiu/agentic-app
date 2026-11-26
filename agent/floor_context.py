"""「这层 / 当前层」→ 具体楼层的解析工具。

UE 场景页知道用户当前在看哪一层；业务查询（尤其安防）本身没有这个上下文。
聊天入口在进入意图分流前，用这里的纯函数把「这层楼」补成「3楼」；
拿不到楼层就保持原话，由业务侧追问或明确说明取不到。
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

# 「这层 / 当前层 / 本层 / 该层（楼）」
FLOOR_CONTEXT_RE = re.compile(r"(?:这|当前|本|该)(?:一)?层(?:楼)?")

# 前端可能使用的字段名
FLOOR_PAYLOAD_KEYS = (
    "floor", "current_floor", "currentFloor", "floorNum", "currentFloorNum",
)

_SCENE_FLOOR_KEYS = ("floor", "currentFloor", "floorNum", "current_floor")


def floor_label(value: Any) -> Optional[str]:
    """把 3 / 3F / B1 / -1 / 99 收敛成替换「这层」用的短语前缀。"""
    text = str(value or "").strip()
    if not text:
        return None
    if text.upper() == "B1" or text == "-1":
        return "B1"
    match = re.fullmatch(r"(-?\d+)\s*[Ff]?", text)
    if not match:
        return text
    number = int(match.group(1))
    if number in (11, 99):
        return "整栋楼"
    if number == -1:
        return "B1"
    return str(number)


def payload_floor(payload: dict) -> Optional[str]:
    """从聊天 payload 里取前端声明的当前楼层。"""
    if not isinstance(payload, dict):
        return None
    for key in FLOOR_PAYLOAD_KEYS:
        value = payload.get(key)
        if value not in (None, ""):
            label = floor_label(value)
            if label:
                return label
    return None


def parse_scene_floor(raw: Any) -> Optional[str]:
    """从 get_scene_state 的返回里取当前楼层（兼容 dict / JSON 文本）。"""
    data = raw
    if not isinstance(data, dict):
        text = str(raw or "")
        try:
            data = json.loads(text)
        except Exception:
            match = re.search(
                r'["\']?(?:floor|currentFloor|floorNum|current_floor)["\']?'
                r"\s*[:：]\s*(-?\d+)",
                text,
            )
            return floor_label(match.group(1)) if match else None
    if not isinstance(data, dict):
        return None
    for key in _SCENE_FLOOR_KEYS:
        if data.get(key) not in (None, ""):
            return floor_label(data[key])
    for key in ("data", "state", "scene"):
        nested = data.get(key)
        if isinstance(nested, dict):
            label = parse_scene_floor(nested)
            if label:
                return label
    return None


def replace_floor_context(query: str, floor: Optional[str]) -> str:
    """把「这层楼」替换成「3楼」；floor 为空时原样返回。"""
    text = query or ""
    if not floor:
        return text
    replacement = floor if floor.endswith("楼") else f"{floor}楼"
    return FLOOR_CONTEXT_RE.sub(replacement, text)
