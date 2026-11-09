"""灰度开关。

三种命中方式，优先级从高到低：

1. payload 显式：`mode=loop` 强制走新链路，`mode=legacy` 强制走旧链路；
2. 白名单：`LOOP_ROLLOUT_USERS` 里的 user_id / session_id；
3. 比例：按 `user_id:session_id` 的稳定哈希取模 100，小于 `LOOP_ROLLOUT_PERCENT` 命中。

总开关 `LOOP_ROLLOUT_ENABLED=0` 时，只有 `mode=loop` 能命中（方便联调），
其余全部走旧链路。任何异常都由调用方回落到旧链路。

注意：`mode=kb` / `mode=scene` 这类专用模式不由本模块接管。
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

TRUTHY = {"1", "true", "yes", "on"}

# 不由灰度接管的专用模式
RESERVED_MODES = {"kb", "scene", "gen_doc", "asr"}


@dataclass(slots=True)
class RolloutDecision:
    enabled: bool
    reason: str

    def __bool__(self) -> bool:  # 方便 if decision:
        return self.enabled


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in TRUTHY


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _bucket(user_id: str, session_id: str) -> int:
    digest = hashlib.md5(f"{user_id}:{session_id}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 100


class Rollout:
    def __init__(
        self,
        *,
        enabled: bool = False,
        percent: int = 0,
        users: set[str] | None = None,
    ) -> None:
        self.enabled = enabled
        self.percent = max(0, min(100, percent))
        self.users = {u.strip() for u in (users or set()) if u.strip()}

    def decide(
        self,
        user_id: str,
        session_id: str,
        payload_mode: str | None = None,
    ) -> RolloutDecision:
        mode = (payload_mode or "").strip().lower()

        # 1) 显式开关
        if mode == "loop":
            return RolloutDecision(True, "mode=loop")
        if mode == "legacy":
            return RolloutDecision(False, "mode=legacy")
        if mode in RESERVED_MODES:
            return RolloutDecision(False, f"reserved mode={mode}")

        # 2) 总开关
        if not self.enabled:
            return RolloutDecision(False, "disabled")

        # 3) 白名单
        if self.users and (user_id in self.users or session_id in self.users):
            return RolloutDecision(True, "whitelist")

        # 4) 比例
        bucket = _bucket(user_id, session_id)
        if bucket < self.percent:
            return RolloutDecision(True, f"bucket={bucket}<{self.percent}")
        return RolloutDecision(False, f"bucket={bucket}>={self.percent}")


_instance: Rollout | None = None


def get_rollout() -> Rollout:
    """进程级单例，读取环境变量。改配置后重启生效。"""
    global _instance
    if _instance is None:
        users = set(filter(None, (os.getenv("LOOP_ROLLOUT_USERS") or "").split(",")))
        _instance = Rollout(
            enabled=_env_bool("LOOP_ROLLOUT_ENABLED", False),
            percent=_env_int("LOOP_ROLLOUT_PERCENT", 0),
            users=users,
        )
    return _instance


def reset_rollout() -> None:
    """测试用：清掉单例。"""
    global _instance
    _instance = None
