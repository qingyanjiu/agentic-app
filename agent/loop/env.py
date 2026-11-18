"""Loop 链路的环境变量读取工具。

超时类配置分散在 MCP 调用层、Executor、LoopAgent 三处，统一在这里解析，
避免同一个变量在不同模块里被解释成不同的默认值。
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def env_positive_float(name: str, default: float) -> float:
    """读正的浮点环境变量；未配置、非法或非正数时回落默认值。"""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning(f"[loop] 环境变量 {name}={raw!r} 不是数字，使用默认值 {default}s")
        return default
    if value <= 0:
        logger.warning(f"[loop] 环境变量 {name}={raw!r} 必须大于 0，使用默认值 {default}s")
        return default
    return value
