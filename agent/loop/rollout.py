"""启动级路由 + 灰度开关。

启动级开关（日常用这个）——环境变量 `AGENT_MODE`，改完重启生效：

| AGENT_MODE | 效果 |
|---|---|
| `loop`   | 全量走 LOOP 新链路（等价 enabled=1、percent=100）；单请求仍可用 `mode=legacy` 应急回滚 |
| `legacy` | 全部走旧逻辑（等价 enabled=0、percent=0）；单请求仍可用 `mode=loop` 试用 |
| `auto`（默认/留空） | 按下面的灰度变量判定 |

灰度变量（仅 `AGENT_MODE=auto` 时生效），命中优先级从高到低：

1. payload 显式：`mode=loop` 强制走新链路，`mode=legacy` 强制走旧链路；
2. 白名单：`LOOP_ROLLOUT_USERS` 里的 user_id / session_id；
3. 比例：按 `user_id:session_id` 的稳定哈希取模 100，小于 `LOOP_ROLLOUT_PERCENT` 命中。

没有独立的"灰度总开关"：配了 `LOOP_ROLLOUT_PERCENT` 或 `LOOP_ROLLOUT_USERS` 就算开，
两者都空等价于关闭（percent=0 且白名单为空时任何请求都不会命中）。
任何异常都由调用方回落到旧链路。

注意：`mode=kb` / `mode=scene` 这类专用模式不由本模块接管。
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

# 不由灰度接管的专用模式
RESERVED_MODES = {"kb", "scene", "gen_doc", "asr"}
# 启动级路由：loop（全量新链路）/ legacy（全走旧逻辑）/ auto（按灰度变量）
STARTUP_MODE_ENV = "AGENT_MODE"


@dataclass(slots=True)
class RolloutDecision:
    enabled: bool
    reason: str

    def __bool__(self) -> bool:  # 方便 if decision:
        return self.enabled



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
        startup_mode: str = "auto",
    ) -> None:
        self.enabled = enabled
        self.percent = max(0, min(100, percent))
        self.users = {u.strip() for u in (users or set()) if u.strip()}
        self.startup_mode = startup_mode

    def describe(self) -> str:
        """启动日志用的一行描述。"""
        if self.startup_mode == "loop":
            return "AGENT_MODE=loop → 全量走 LOOP 新链路（单请求 mode=legacy 仍可回滚）"
        if self.startup_mode == "legacy":
            return "AGENT_MODE=legacy → 全部走旧逻辑（单请求 mode=loop 仍可试用）"
        return (
            f"AGENT_MODE=auto → 灰度 enabled={int(self.enabled)} "
            f"percent={self.percent}% 白名单={len(self.users)} 个"
        )

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


def startup_mode() -> str:
    """读 `AGENT_MODE`，返回 loop / legacy / auto（无法识别按 auto）。"""
    raw = (os.getenv(STARTUP_MODE_ENV) or "").strip().lower()
    if raw in ("loop", "full", "all", "on", "1", "true", "yes"):
        return "loop"
    if raw in ("legacy", "old", "off", "0", "false", "no"):
        return "legacy"
    return "auto"


def get_rollout() -> Rollout:
    """进程级单例，读取环境变量。改配置后重启生效。"""
    global _instance
    if _instance is None:
        mode = startup_mode()
        if mode == "loop":
            # 全量新链路：不再看灰度变量，避免两个配置打架
            _instance = Rollout(enabled=True, percent=100, startup_mode=mode)
        elif mode == "legacy":
            # 全量旧逻辑：白名单/比例一并失效
            _instance = Rollout(enabled=False, percent=0, startup_mode=mode)
        else:
            users = set(filter(None, (os.getenv("LOOP_ROLLOUT_USERS") or "").split(",")))
            percent = _env_int("LOOP_ROLLOUT_PERCENT", 0)
            # 没有单独的灰度总开关：配了比例或白名单就算开。
            # 两者都空时等价于关闭——percent=0 且白名单为空，任何请求都不会命中。
            _instance = Rollout(
                enabled=bool(users) or percent > 0,
                percent=percent,
                users=users,
                startup_mode=mode,
            )
    return _instance


def reset_rollout() -> None:
    """测试用：清掉单例。"""
    global _instance
    _instance = None
