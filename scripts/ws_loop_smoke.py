"""Loop 链路 WebSocket 冒烟脚本。

用法（服务已启动）：

    python3 scripts/ws_loop_smoke.py                      # 跑全部用例
    python3 scripts/ws_loop_smoke.py -q "现在园区有多少人"   # 只跑一条
    python3 scripts/ws_loop_smoke.py --host 127.0.0.1 --port 8000

判定标准：
- 收到 type=answer 且 content 非空 → PASS
- 收到 type=ask → 视为 PASS（追问也是正常路径）
- 收到 type=error 或超时 → FAIL
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid

import websockets

# 13 个域各一条 + 跨域 + 追问两轮
CASES = [
    ("person_status", "现在园区有多少人"),
    ("security_status", "今天的安防告警列表"),
    ("canteen_status", "本周菜谱是什么"),
    ("vehicle_status", "还剩多少停车位"),
    ("information_status", "今天发了多少条信息"),
    ("energy_status", "今天总能耗多少"),
    ("meeting_status", "今天开了多少会"),
    ("emergency_fire", "消防设备有多少"),
    ("emergency_perimeter", "周界告警统计"),
    ("device_status", "设备健康度怎么样"),
    ("device_query", "3楼的门禁设备都列一下"),
    ("compositive_overview", "园区基本情况"),
    ("twins_inspection", "今天巡检完成了吗"),
    ("L2-cross", "对比一下今天和昨天的安防告警，再看看设备在线率"),
]


async def _drain(ws, timeout: float) -> list[dict]:
    events: list[dict] = []
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout)
        payload = json.loads(raw)
        if payload.get("status") == "done":
            return events
        events.append(payload)


def _summarize(events: list[dict]) -> tuple[str, str, str]:
    """返回 (type, content, level)。"""
    for event in events:
        data = event.get("data") or {}
        if event.get("event") == "custom" and data.get("type") in ("answer", "ask", "error"):
            return data["type"], data.get("content", ""), data.get("level", "")
    return "none", "", ""


async def run_case(ws, query: str, timeout: float) -> tuple[bool, str]:
    await ws.send(json.dumps({"query": query, "mode": "loop"}, ensure_ascii=False))
    try:
        events = await _drain(ws, timeout)
    except asyncio.TimeoutError:
        return False, f"超时（>{timeout}s）"
    kind, content, level = _summarize(events)
    if kind in ("answer", "ask"):
        prefix = f"[{level}] " if level else ""
        return True, f"{prefix}{content[:120]}"
    if kind == "error":
        return False, f"error: {content[:160]}"
    return False, f"没有拿到 answer/ask 事件：{events[:3]}"


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--user", default="smoke")
    parser.add_argument("-q", "--query", action="append", default=[])
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args()

    cases = [(args.query and "custom", q) for q in args.query] if args.query else CASES
    session = uuid.uuid4().hex[:8]
    url = f"ws://{args.host}:{args.port}/chat/{args.user}/{session}"
    print(f"连接 {url}\n")

    passed = failed = 0
    async with websockets.connect(url, max_size=None, ping_interval=None) as ws:
        for label, query in cases:
            print(f"── [{label}] {query}")
            ok, detail = await run_case(ws, query, args.timeout)
            print(f"   {'PASS' if ok else 'FAIL'}  {detail}\n")
            if ok:
                passed += 1
            else:
                failed += 1

    print(f"结果：{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
