# -*- coding: utf-8 -*-
"""
性能基准测试（pytest -m performance 单独运行）。

覆盖：
  1. 正则槽位抽取延迟（8 域）
  2. 顶层意图分类延迟（embedding 模型）
  3. 消防子类型分类延迟
  4. 单张图端到端执行延迟（FakeTool + 无 LLM，衡量图本身开销）
  5. 并发吞吐：30 路图并发执行
  6. 追问轮 handler.handle_reply 延迟（正则路径）
  7. MCP loader 单点隔离：慢/挂的后端不阻塞其他域的加载时间

所有阈值可通过环境变量覆盖（容器 CPU 性能差异大，默认值给得宽松）：
  PERF_SLOTS_AVG_MS / PERF_SLOTS_P95_MS
  PERF_CLASSIFY_AVG_MS / PERF_CLASSIFY_P95_MS
  PERF_SUBTYPE_AVG_MS
  PERF_GRAPH_AVG_MS / PERF_GRAPH_P95_MS
  PERF_CONCURRENCY_MAX_S
"""
import asyncio
import os
import statistics
import time

import pytest

from agent.intent import classify_intent, classify_fire_sub_type
from agent.intent.slots import (
    extract_person_status_slots,
    extract_security_status_slots,
    extract_canteen_status_slots,
    extract_vehicle_status_slots,
    extract_information_status_slots,
    extract_energy_status_slots,
    extract_emergency_fire_slots,
    extract_meeting_status_slots,
    extract_device_status_slots,
    extract_device_query_slots,
)
from agent.intent.handlers.emergency_fire_handler import EmergencyFireHandler
from conftest import SPAN_DATE, default_tools, graph_input

pytestmark = pytest.mark.performance


def run(coro):
    return asyncio.run(coro)


def _env_ms(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _pct(values, p: float) -> float:
    """百分位（毫秒），p=0.95 即 P95"""
    values = sorted(values)
    idx = min(int(len(values) * p), len(values) - 1)
    return values[idx]


def _report(name: str, ms_values: list) -> dict:
    stat = {
        "n": len(ms_values),
        "avg_ms": round(statistics.mean(ms_values), 2),
        "p50_ms": round(_pct(ms_values, 0.50), 2),
        "p95_ms": round(_pct(ms_values, 0.95), 2),
        "max_ms": round(max(ms_values), 2),
    }
    print(f"[PERF] {name}: {stat}")
    return stat


# ============================================================
# 1. 正则槽位抽取延迟（8 个域）
# ============================================================
SLOT_EXTRACTORS = [
    ("person", extract_person_status_slots, "张三现在在哪里"),
    ("security", extract_security_status_slots, "本周巡查任务有哪些"),
    ("canteen", extract_canteen_status_slots, "今天食堂就餐多少人"),
    ("vehicle", extract_vehicle_status_slots, "停车场还剩多少车位"),
    ("information", extract_information_status_slots, "本周信息发布任务趋势"),
    ("energy", extract_energy_status_slots, "本月各楼层用电排名"),
    ("meeting", extract_meeting_status_slots, "今天有什么会议安排"),
    ("fire", extract_emergency_fire_slots, "查下最新的消防"),
    ("device", extract_device_status_slots, "设备分类占比"),
    ("device_query", extract_device_query_slots, "A栋3楼有哪些消防设备"),
]


def test_perf_slot_extraction_latency():
    """正则抽取应在毫秒级（含 jionlp 时间解析）"""
    rounds = int(os.getenv("PERF_SLOTS_ROUNDS", "40"))
    all_ms = []
    for _ in range(rounds):
        for name, fn, query in SLOT_EXTRACTORS:
            t0 = time.perf_counter()
            fn(query)
            all_ms.append((time.perf_counter() - t0) * 1000)

    stat = _report(f"slot_extraction({rounds}x8域)", all_ms)
    assert stat["avg_ms"] < _env_ms("PERF_SLOTS_AVG_MS", 10), "槽位抽取平均延迟过高"
    assert stat["p95_ms"] < _env_ms("PERF_SLOTS_P95_MS", 50), "槽位抽取 P95 延迟过高"


# ============================================================
# 2. 顶层意图分类延迟（embedding 模型）
# ============================================================
CLASSIFY_QUERIES = [
    "张三现在在哪里", "今天食堂就餐多少人", "停车场还剩多少车位",
    "今天有什么会议安排", "本月用电排名", "消防设备台账",
    "今天的安防告警列表", "本周信息发布任务趋势",
]


def test_perf_classify_intent_latency():
    """预热后单次顶层意图分类延迟（bge-small-zh CPU 量级应 < 秒）"""
    run(classify_intent("预热"))  # 预热：模型加载 + 首次编码

    ms_values = []
    for q in CLASSIFY_QUERIES:
        t0 = time.perf_counter()
        run(classify_intent(q))
        ms_values.append((time.perf_counter() - t0) * 1000)

    stat = _report("classify_intent", ms_values)
    assert stat["avg_ms"] < _env_ms("PERF_CLASSIFY_AVG_MS", 1500), "意图分类平均延迟过高"
    assert stat["p95_ms"] < _env_ms("PERF_CLASSIFY_P95_MS", 3000), "意图分类 P95 延迟过高"


def test_perf_fire_sub_type_latency():
    """消防子类型分类延迟（追问轮反问后补槽依赖它）"""
    run(classify_fire_sub_type("预热"))

    ms_values = []
    for q in ["灭火器台账", "今天火警数是多少", "实时消防告警列表", "本月报修数量趋势"] * 2:
        t0 = time.perf_counter()
        run(classify_fire_sub_type(q))
        ms_values.append((time.perf_counter() - t0) * 1000)

    stat = _report("classify_fire_sub_type", ms_values)
    assert stat["avg_ms"] < _env_ms("PERF_SUBTYPE_AVG_MS", 1500)


# ============================================================
# 3. 图端到端执行延迟（FakeTool + 无 LLM，纯图调度开销）
# ============================================================
def test_perf_graph_e2e_latency():
    from conftest import build_graph

    graph = build_graph("emergency_fire", default_tools("emergency_fire"))
    slots = {"query_type": "fire_alarm_list"}

    run(graph.ainvoke(graph_input(slots)))  # 预热

    ms_values = []
    for _ in range(30):
        t0 = time.perf_counter()
        final = run(graph.ainvoke(graph_input(slots)))
        ms_values.append((time.perf_counter() - t0) * 1000)
        assert final.get("done") is True

    stat = _report("graph_e2e(emergency_fire)", ms_values)
    assert stat["avg_ms"] < _env_ms("PERF_GRAPH_AVG_MS", 100), "图端到端平均延迟过高"
    assert stat["p95_ms"] < _env_ms("PERF_GRAPH_P95_MS", 300)


# ============================================================
# 4. 并发吞吐：30 路混合域并发执行
# ============================================================
def test_perf_graph_concurrency():
    from conftest import build_graph

    graphs = {
        key: build_graph(key, default_tools(key))
        for key in ["person_status", "canteen_status", "energy_status", "emergency_fire"]
    }
    cases = [
        ("person_status", {"query_type": "realtime"}),
        ("canteen_status", {"event_type": "dining_count", "date": SPAN_DATE}),
        ("energy_status", {"query_type": "overall_energy"}),
        ("emergency_fire", {"query_type": "fire_alarm_list"}),
    ]
    n = int(os.getenv("PERF_CONCURRENCY_N", "30"))

    async def _bench():
        tasks = []
        for i in range(n):
            key, slots = cases[i % len(cases)]
            tasks.append(graphs[key].ainvoke(graph_input(slots, original_query=f"并发{i}")))
        t0 = time.perf_counter()
        results = await asyncio.gather(*tasks)
        return results, (time.perf_counter() - t0)

    results, wall_s = run(_bench())
    ok = sum(1 for f in results if f.get("done") is True and f.get("error") is None)

    print(f"[PERF] 并发 {n} 路图执行: wall={wall_s:.2f}s, 成功={ok}/{n}, "
          f"吞吐={n / wall_s:.1f} req/s")
    assert ok == n, f"并发执行有失败: {ok}/{n}"
    assert wall_s < _env_ms("PERF_CONCURRENCY_MAX_S", 60), "并发执行总耗时过高"


# ============================================================
# 5. 追问轮 handle_reply 延迟（正则路径，不触发分类器）
# ============================================================
def test_perf_handler_reply_latency():
    handler = EmergencyFireHandler()
    from memory.session_state import IntentState

    async def _one():
        state = IntentState(
            module="emergency_fire",
            slots={"query_type": "count"},
            missing_params=["query_type"],
        )
        t0 = time.perf_counter()
        await handler.handle_reply(state, "实时告警", llm=None)
        return (time.perf_counter() - t0) * 1000

    ms_values = [run(_one()) for _ in range(30)]

    stat = _report("fire_handle_reply(正则路径)", ms_values)
    assert stat["avg_ms"] < _env_ms("PERF_REPLY_AVG_MS", 20)


# ============================================================
# 6. MCP loader 单点隔离：fire 后端慢 0.5s 且 404，其他域不受阻
# ============================================================
def test_perf_loader_isolation(monkeypatch):
    """复现案例1 场景：一个后端慢且挂 -> 其他域工具加载总耗时应接近正常水平"""
    import mcp_client.mcp_loader as mcp_loader
    from conftest import FakeTool

    class SlowFailClient:
        def __init__(self, config):
            (name, _), = config.items()
            self.name = name

        async def get_tools(self):
            if self.name == "fire":
                await asyncio.sleep(0.5)   # 模拟网络悬置
                raise RuntimeError("HTTP/1.1 404")
            return [FakeTool(f"{self.name}:mockTool", "{}")]

    monkeypatch.setattr(mcp_loader, "MultiServerMCPClient", SlowFailClient)

    t0 = time.perf_counter()
    tools = run(mcp_loader.get_mcp_tools("mcp_client/mcp_server_config.yaml"))
    wall_s = time.perf_counter() - t0

    # 7 个正常域的工具都应拿到（每个域 mock 1 个工具）
    assert len(tools) >= 7, f"单点失败不应影响其他域工具数量, got {len(tools)}"
    print(f"[PERF] loader 单点隔离: fire 慢0.5s且404, 总耗时={wall_s:.2f}s, "
          f"其余域工具 {len(tools)} 个正常返回")
    # 总耗时约等于最慢 server 的 0.5s（并发 gather），不应被失败 server 显著放大
    assert wall_s < 3.0, f"单点失败拖垮了整体加载耗时: {wall_s:.2f}s"
