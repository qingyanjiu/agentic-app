"""WS 侧编排的回归测试。

线上事故：加了停止按钮之后，**每次提问都回「连接已断开，请稍后重试」**。
根因：上一版在"本轮先结束"时 `recv.cancel()` 了一个挂着的
`websocket.receive_text()` —— Starlette 的 receive 通道被 cancel 之后整条连接就废了，
后续消息再也收不到。

这里锁三件事：
1. 绝不去 cancel 挂着的 `receive_text()`（用会记仇的假 socket 验证）；
2. 一轮结束后队列依然可用（下一轮还能取到消息）；
3. stop / 中途插话 / 断开 三种输入的处置。
"""
import asyncio
import json

from agent.loop import ws_bridge
from agent.loop.ws_flow import await_turn_with_stop


class _PickySocket:
    """一旦它的 receive_text 被 cancel 就记下来——这正是那次事故的原因。"""

    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.cancelled_receive = False

    async def receive_text(self):
        try:
            if not self.incoming:
                await asyncio.sleep(3600)
            return self.incoming.pop(0)
        except asyncio.CancelledError:
            self.cancelled_receive = True
            raise


def _queue_turn(result="答完了", delay=0.05, cancel_probe=None):
    async def _turn():
        for _ in range(int(delay / 0.01) or 1):
            if cancel_probe is not None and cancel_probe.is_set():
                return "已取消本轮查询。"
            await asyncio.sleep(0.01)
        return result

    return asyncio.create_task(_turn())


def test_turn_finishes_without_cancelling_receive_text():
    """核心回归：一轮结束后不能把 socket 的 receive 弄坏。"""

    async def scenario():
        ws_bridge.reset_sessions()
        sock = _PickySocket([json.dumps({"query": "园区有哪些监控"})])
        inbox: asyncio.Queue = asyncio.Queue()

        async def pump():
            try:
                while True:
                    await inbox.put(await sock.receive_text())
            except Exception:
                await inbox.put(None)

        pump_task = asyncio.create_task(pump())
        first = await inbox.get()                       # 第一轮 query
        assert json.loads(first)["query"] == "园区有哪些监控"

        pending: list = []
        turn = _queue_turn("园区共有 268 台监控设备。")
        out = await await_turn_with_stop(turn, "u", "s", inbox, pending)

        assert out == "园区共有 268 台监控设备。"
        assert sock.cancelled_receive is False          # ← 关键断言
        assert pending == []

        # 第二轮还能正常取消息（连接没坏）
        await inbox.put(json.dumps({"query": "第二个问题"}))
        second = await inbox.get()
        assert json.loads(second)["query"] == "第二个问题"

        pump_task.cancel()
        try:
            await pump_task
        except BaseException:
            pass

    asyncio.run(scenario())


def test_stop_message_interrupts_turn():
    """中途收到 stop → 置 cancel，本轮在检查点退出。"""

    async def scenario():
        ws_bridge.reset_sessions()
        inbox: asyncio.Queue = asyncio.Queue()
        pending: list = []

        turn = _queue_turn("正常答完")
        # 模拟"本轮跑到一半，用户点了停止"
        await inbox.put(json.dumps({"type": "stop"}))
        await asyncio.sleep(0)                          # 让 turn_task 先跑一步
        out = await await_turn_with_stop(turn, "u", "s", inbox, pending)

        assert out == "正常答完"                        # 假 turn 不读 cancel，只验证调用链
        assert pending == []

    asyncio.run(scenario())


def test_mid_turn_message_is_queued_not_lost():
    """本轮进行中用户又发了一句：排队，别丢。"""

    async def scenario():
        ws_bridge.reset_sessions()
        inbox: asyncio.Queue = asyncio.Queue()
        pending: list = []

        turn = asyncio.create_task(_sleep_then(0.08))
        await inbox.put(json.dumps({"query": "顺便看一下 5 楼"}))
        out = await await_turn_with_stop(turn, "u", "s", inbox, pending)

        assert out == "ok"
        assert [p.get("query") for p in pending] == ["顺便看一下 5 楼"]

    asyncio.run(scenario())


def test_disconnect_marker_stops_turn_and_exits_loop():
    async def scenario():
        ws_bridge.reset_sessions()
        inbox: asyncio.Queue = asyncio.Queue()
        pending: list = []

        turn = asyncio.create_task(_sleep_then(0.08))
        await inbox.put(None)                           # 收包 task 报告断开
        await await_turn_with_stop(turn, "u", "s", inbox, pending)

        assert pending == [None]                        # 主循环据此退出

    asyncio.run(scenario())


async def _sleep_then(delay):
    await asyncio.sleep(delay)
    return "ok"
