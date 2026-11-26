"""安防接口不支持按楼层筛选：必须先说清楚，不能拿园区结果冒充楼层结果。"""
import asyncio

from conftest import answers_of, get_graph_module, graph_input
from agent.loop.domains.security_status import build_domain


class _Toolbox:
    def __init__(self):
        self.calls = 0
        self.args = []

    async def call(self, server, tool, args):
        self.calls += 1
        self.args.append((server, tool, args))
        return '{"code":200,"rows":[]}'


def _security_tool(box):
    domain = build_domain(box)
    return next(t for t in domain.tools if t.name == "security_status__query")


def test_floor_security_query_is_rejected_without_calling_mcp():
    box = _Toolbox()

    out = asyncio.run(_security_tool(box).execute({"query": "7楼安防告警"}))

    assert box.calls == 0
    assert out.is_error
    assert "不能按楼层" in out.text


def test_floor_phrases_are_all_rejected():
    for query in ("这层楼安防数据", "当前层有多少告警", "各楼层告警统计", "7F 的安防指数"):
        box = _Toolbox()
        out = asyncio.run(_security_tool(box).execute({"query": query}))
        assert box.calls == 0, query
        assert "不能按楼层" in out.text, query


def test_park_security_query_still_calls_mcp():
    box = _Toolbox()

    out = asyncio.run(_security_tool(box).execute({"query": "今天的园区安防告警"}))

    assert box.calls == 1
    assert "不能按楼层" not in out.text


def test_old_security_graph_rejects_floor_query():
    """旧链路（LangGraph）同样不能把园区结果说成楼层结果。"""
    graph = get_graph_module("security_status").build_security_status_graph({}, llm=None)

    final = asyncio.run(graph.ainvoke(
        graph_input({}, original_query="7楼安防告警")
    ))

    assert final["done"] is True
    assert "不能按楼层" in answers_of(final)[0]
