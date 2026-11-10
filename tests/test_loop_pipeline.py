"""三级分流管线（Pipeline）的离线测试。

不连 MCP、不调真实模型：Router / Executor / Composer / LoopAgent 全部用假实现，
只验证编排分支是否正确。
"""
import asyncio
from types import SimpleNamespace as NS

from agent.loop import ChatPipeline, Session, ToolOutcome
from agent.loop.router import Pending, RoutePlan


class _FakeRouter:
    def __init__(self, plan):
        self.plan = plan
        self.seen_pending = "unset"

    async def route(self, query, pending=None):
        self.seen_pending = pending
        return self.plan


class _FakeExecutor:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    async def execute(self, tool, args=None):
        self.calls.append((tool, args))
        return self.outcome


class _FakeComposer:
    async def compose(self, query, tool_text, history=None, on_delta=None):
        return f"回答：{tool_text}"


class _FakeLoop:
    def __init__(self):
        self.calls = []

    async def run(self, query, history=None, cancel=None, on_event=None):
        self.calls.append(query)
        self.on_event = on_event
        return NS(text="loop 回答", stop_reason="answered", tool_calls=[{"name": "loop_tool"}])


def _pipeline(plan, outcome=None, loop=None):
    return ChatPipeline(
        router=_FakeRouter(plan),
        executor=_FakeExecutor(outcome or ToolOutcome("工具结果")),
        composer=_FakeComposer(),
        loop_agent=loop,
    )


def test_l1_direct_hit_answers_without_loop():
    plan = RoutePlan(level="L1", domain="device_query", tool="device_query__list",
                     args={"query": "门禁"}, confidence=0.9)
    loop = _FakeLoop()
    pipeline = _pipeline(plan, ToolOutcome("共 3 台门禁"), loop)

    result = asyncio.run(pipeline.handle("3楼的门禁", Session()))

    assert result.stop_reason == "answered"
    assert result.level == "L1"
    assert result.text == "回答：共 3 台门禁"
    assert result.escalated is False
    assert loop.calls == []          # 没有走 loop


def test_missing_param_asks_and_sets_pending():
    plan = RoutePlan(level="L1", domain="device_query", tool="device_query__detail",
                     args={"query": "查设备详情"}, missing=["device_keyword"], confidence=0.9)
    session = Session()
    result = asyncio.run(_pipeline(plan).handle("查设备详情", session))

    assert result.stop_reason == "asked"
    assert "设备" in result.text
    assert isinstance(session.pending, Pending)
    assert session.pending.missing == ["device_keyword"]
    assert session.pending.original_query == "查设备详情"


def test_pending_is_passed_to_router_on_followup():
    plan = RoutePlan(level="L1", domain="device_query", tool="device_query__detail",
                     args={"query": "查设备详情 MH-001"}, confidence=1.0)
    session = Session()
    session.pending = Pending(
        tool="device_query__detail", args={}, missing=["device_keyword"],
        domain="device_query", original_query="查设备详情",
    )
    router = _FakeRouter(plan)
    pipeline = ChatPipeline(
        router=router,
        executor=_FakeExecutor(ToolOutcome("详情结果")),
        composer=_FakeComposer(),
    )

    result = asyncio.run(pipeline.handle("MH-001", session))

    assert router.seen_pending is not None
    assert result.stop_reason == "answered"
    assert session.pending is None       # 成功后被清掉


def test_escalation_runs_loop_and_marks_flag():
    plan = RoutePlan(level="L1", domain="person_status", tool="person_status__query",
                     args={"query": "人员"}, confidence=0.8)
    loop = _FakeLoop()
    pipeline = _pipeline(plan, ToolOutcome("后端不可用", is_error=True, escalate=True), loop)

    result = asyncio.run(pipeline.handle("人员情况", Session()))

    assert result.stop_reason == "answered"
    assert result.level == "L2"
    assert result.escalated is True
    assert loop.calls == ["人员情况"]


def test_no_plan_goes_to_loop_and_reports_unavailable_when_missing():
    pipeline = _pipeline(None, loop=None)
    result = asyncio.run(pipeline.handle("对比告警和设备在线率", Session()))

    assert result.stop_reason == "loop_unavailable"
    assert result.level == "L2"


def test_scene_command_hands_off():
    plan = RoutePlan(level="L0", domain="scene", tool=None, needs_confirm=True, reason="scene_command")
    result = asyncio.run(_pipeline(plan).handle("打开A栋大门", Session()))

    assert result.stop_reason == "handoff"
    assert result.level == "L0"


def _passthrough_pipeline(passthrough: bool, outcome=None):
    """真 Executor + 真 registry，用来验证 passthrough 分支是否绕过 Composer。"""
    from agent.loop import DomainSpec, ToolRegistry, ToolSpec, tool_schema
    from agent.loop.executor import Executor

    async def execute(args):
        return outcome or ToolOutcome(
            "3 台设备，楼层 3F：\n- A｜门禁\n- B｜门禁\n- C｜门禁"
        )

    registry = ToolRegistry()
    registry.add_domain(
        DomainSpec(
            name="device_query",
            description="设备台账",
            tools=[
                ToolSpec(
                    name="device_query__list",
                    description="设备清单",
                    parameters=tool_schema({"query": {"type": "string"}}, ["query"]),
                    execute=execute,
                    passthrough=passthrough,
                )
            ],
        )
    )

    class _CountingComposer:
        def __init__(self):
            self.calls = 0

        async def compose(self, query, tool_text, history=None, on_delta=None):
            self.calls += 1
            return f"回答：{tool_text}"

    composer = _CountingComposer()
    pipeline = ChatPipeline(
        router=_FakeRouter(
            RoutePlan(
                level="L1",
                domain="device_query",
                tool="device_query__list",
                args={"query": "3楼门禁"},
                confidence=0.9,
            )
        ),
        executor=Executor(registry),
        composer=composer,
        loop_agent=None,
    )
    return pipeline, composer


def test_passthrough_tool_answers_without_composer():
    """设备清单直接出：不让模型复述（省 1~2 秒，也省掉"以下是"这类客套）。"""
    pipeline, composer = _passthrough_pipeline(passthrough=True)
    deltas: list = []

    async def on_delta(text):
        deltas.append(text)

    result = asyncio.run(pipeline.handle("3楼门禁", Session(), on_delta=on_delta))

    assert composer.calls == 0
    assert result.text.startswith("3 台设备")
    assert result.stop_reason == "answered"
    # 仍然推给前端（一次全量），ws_bridge 的 streamed_chars 会照常记账
    assert deltas == [result.text]


def test_non_passthrough_tool_still_uses_composer():
    pipeline, composer = _passthrough_pipeline(passthrough=False)

    result = asyncio.run(pipeline.handle("3楼门禁", Session()))

    assert composer.calls == 1
    assert result.text.startswith("回答：")


def test_passthrough_error_is_not_shown_to_user():
    """错误文案是写给模型看的（"请让用户确认…"），直出会让用户看到内部话术。"""
    pipeline, composer = _passthrough_pipeline(
        passthrough=True,
        outcome=ToolOutcome(
            "缺少要查询的设备。请向用户确认设备名称或编号。", is_error=True
        ),
    )

    result = asyncio.run(pipeline.handle("3楼门禁", Session()))

    assert composer.calls == 1
    assert result.text.startswith("回答：")
