"""WebSocket 侧的小编排：跑一轮的同时还能收「停止」。

为什么单独放一个模块：
    app.py 里这段逻辑一错就是**整条连接报废**（线上表现为每次都回
    「连接已断开，请稍后重试」），必须能单测。放这里没有任何重依赖，
    `tests/test_loop_ws_flow.py` 可以直接 import 验证。

关键约束（踩过的坑）：
    **绝不能 cancel 一个挂着的 `websocket.receive_text()`**。
    Starlette 的 receive 通道被 cancel 之后整条连接就废了，后续消息再也收不到。
    所以这里只等 `inbox.get()`——收包由独立 task 负责往队列里塞，
    取消 `Queue.get()` 是安全的，不会碰 socket。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def await_turn_with_stop(
    turn_task: "asyncio.Task",
    user_id: str,
    session_id: str,
    inbox: "asyncio.Queue[Any]",
    pending_payloads: list,
) -> Any:
    """等这一轮跑完，期间继续从 inbox 取消息。

    - `{"type": "stop"}` → 置该轮 cancel（LoopAgent 在下一个检查点退出）；
    - 别的消息（用户抢着又发一句）→ 进 `pending_payloads`，本轮结束后再处理；
    - `None`（收包 task 报告连接断开）→ 停止本轮并把 None 排进队列，让主循环退出。

    :return: `turn_task` 的结果
    """
    from .ws_bridge import request_stop

    while not turn_task.done():
        nxt = asyncio.create_task(inbox.get())
        done, _ = await asyncio.wait(
            {turn_task, nxt}, return_when=asyncio.FIRST_COMPLETED
        )
        if nxt in done:
            raw = nxt.result()
            if raw is None:
                # 客户端断开：尽力停掉本轮，并让主循环知道该收尾了
                request_stop(user_id, session_id)
                pending_payloads.append(None)
                continue
            try:
                payload = json.loads(raw or "{}")
            except Exception:
                payload = {}
            if str(payload.get("type") or "").lower() == "stop":
                handled = request_stop(user_id, session_id)
                logger.info(
                    f"[loop] 收到停止请求 handled={handled} -- {user_id}:{session_id}"
                )
                continue
            pending_payloads.append(payload)
            continue

        # 本轮先结束：取消的只是 Queue.get()，不碰 socket
        nxt.cancel()
        try:
            await nxt
        except BaseException:
            pass

    return turn_task.result()
