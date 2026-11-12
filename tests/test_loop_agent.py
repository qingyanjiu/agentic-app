"""Loop Agent 的离线单元测试。

不连 MCP、不调真实模型，用假 client 验证三件事：
1. loop 会把模型给的 tool_calls 真正执行，并把结果回填；
2. 没有 tool_calls 时正常收尾；
3. 输出治理会截断并落盘。
"""
import asyncio
from types import SimpleNamespace as NS

from agent.loop import (
    DomainSpec,
    LoopAgent,
    ToolOutcome,
    ToolRegistry,
    ToolSpec,
    tool_schema,
)
from agent.loop.govern import govern
from agent.loop.validate import compress_tool_messages, validate_args


def _tool_call(name: str, arguments: str, call_id: str = "call_1"):
    return NS(id=call_id, function=NS(name=name, arguments=arguments))


class _FakeCompletions:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0)
        # 脚本项可以是 message，也可以是 (message, finish_reason)
        message, finish_reason = item if isinstance(item, tuple) else (item, None)
        return NS(choices=[NS(message=message, finish_reason=finish_reason)])


class _FakeClient:
    def __init__(self, script):
        self.chat = NS(completions=_FakeCompletions(script))


def _demo_registry(executed: list):
    async def execute(args):
        executed.append(args)
        return ToolOutcome("共 3 台门禁设备")

    registry = ToolRegistry()
    registry.add_domain(
        DomainSpec(
            name="demo",
            description="演示域",
            tools=[
                ToolSpec(
                    name="demo__query",
                    description="查演示数据",
                    parameters=tool_schema({"query": {"type": "string"}}, ["query"]),
                    execute=execute,
                )
            ],
        )
    )
    return registry


def test_loop_executes_tool_then_answers():
    executed: list = []
    events: list = []
    script = [
        NS(content="", tool_calls=[_tool_call("demo__query", '{"query":"门禁"}')]),
        NS(content="共有 3 台门禁设备。", tool_calls=None),
    ]
    agent = LoopAgent(
        client=_FakeClient(script),
        model="fake",
        registry=_demo_registry(executed),
        on_event=lambda event: events.append(event.type),
        stream=False,
    )

    result = asyncio.run(agent.run("查一下门禁"))

    assert result.stop_reason == "answered"
    assert result.text == "共有 3 台门禁设备。"
    assert executed == [{"query": "门禁"}]
    assert result.steps == 2
    assert "tool_start" in events and "tool_end" in events and "done" in events


def test_unknown_tool_is_reported_back_to_model():
    executed: list = []
    script = [
        NS(content="", tool_calls=[_tool_call("no_such_tool", "{}")]),
        NS(content="工具不可用，请稍后再试。", tool_calls=None),
    ]
    agent = LoopAgent(
        client=_FakeClient(script),
        model="fake",
        registry=_demo_registry(executed),
        stream=False,
    )

    result = asyncio.run(agent.run("查一下"))

    assert result.stop_reason == "answered"
    assert executed == []
    # 第二条 tool message 里应包含"工具不存在"
    tool_messages = [m for m in result.messages if m.get("role") == "tool"]
    assert tool_messages and "工具不存在" in tool_messages[0]["content"]


def test_govern_truncates_and_spills(tmp_path):
    text = "\n".join(f"line-{i}" for i in range(3000))
    out = govern(text, label="demo", spill_dir=str(tmp_path), max_lines=100)

    assert "输出已截断" in out
    assert len(list(tmp_path.iterdir())) == 1
    assert out.count("\n") <= 102  # 100 行 + 空行 + 提示


def test_length_finish_skips_tool_execution():
    """finish_reason=length 时，参数可能被截断，工具不应执行。"""
    executed: list = []
    script = [
        (NS(content="", tool_calls=[_tool_call("demo__query", '{"query":"门禁"}')]), "length"),
        (NS(content="刚才输出太长了，我重新来。", tool_calls=None), "stop"),
    ]
    agent = LoopAgent(
        client=_FakeClient(script),
        model="fake",
        registry=_demo_registry(executed),
        stream=False,
    )

    result = asyncio.run(agent.run("查一下门禁"))

    assert result.stop_reason == "answered"
    assert executed == []                      # 工具一次都没执行
    tool_messages = [m for m in result.messages if m.get("role") == "tool"]
    assert tool_messages and "未执行" in tool_messages[0]["content"]


def test_tool_timeout_is_reported_back_to_model():
    executed: list = []

    async def slow(args):
        executed.append(args)
        await asyncio.sleep(0.5)
        return ToolOutcome("太晚了")

    registry = ToolRegistry()
    registry.add_domain(
        DomainSpec(
            name="demo",
            description="慢工具",
            tools=[
                ToolSpec(
                    name="demo__slow",
                    description="很慢",
                    parameters=tool_schema({"query": {"type": "string"}}, ["query"]),
                    execute=slow,
                )
            ],
        )
    )
    script = [
        (NS(content="", tool_calls=[_tool_call("demo__slow", '{"query":"x"}')]), None),
        (NS(content="工具超时了，请稍后再试。", tool_calls=None), "stop"),
    ]
    agent = LoopAgent(
        client=_FakeClient(script),
        model="fake",
        registry=registry,
        tool_timeout=0.05,
        stream=False,
    )

    result = asyncio.run(agent.run("查一下"))

    assert result.stop_reason == "answered"
    tool_messages = [m for m in result.messages if m.get("role") == "tool"]
    assert tool_messages and "超时" in tool_messages[0]["content"]


def test_cancel_aborts_before_model_call():
    executed: list = []
    client = _FakeClient([])          # 脚本为空：一旦调用模型就会 IndexError
    agent = LoopAgent(
        client=client, model="fake", registry=_demo_registry(executed), stream=False
    )
    cancel = asyncio.Event()
    cancel.set()

    result = asyncio.run(agent.run("查一下", cancel=cancel))

    assert result.stop_reason == "aborted"
    assert result.text.startswith("已取消")
    assert executed == []


def test_validate_args_reports_missing_and_type():
    schema = tool_schema({"query": {"type": "string"}}, ["query"])
    assert validate_args(schema, {"query": "x"}) == []
    assert "缺少必填参数 query" in " ".join(validate_args(schema, {}))
    assert "应为 string" in " ".join(validate_args(schema, {"query": 1}))


def test_before_tool_can_block_execution():
    executed: list = []

    async def execute(args):
        executed.append(args)
        return ToolOutcome("真的执行了")

    async def guard(name, args):
        return ToolOutcome("无权限调用该工具", is_error=True)

    registry = ToolRegistry()
    registry.add_domain(
        DomainSpec(
            name="demo",
            description="受保护",
            tools=[
                ToolSpec(
                    name="demo__query",
                    description="查",
                    parameters=tool_schema({"query": {"type": "string"}}, ["query"]),
                    execute=execute,
                )
            ],
        )
    )
    script = [
        (NS(content="", tool_calls=[_tool_call("demo__query", '{"query":"x"}')]), None),
        (NS(content="这个工具我现在用不了。", tool_calls=None), "stop"),
    ]
    agent = LoopAgent(
        client=_FakeClient(script),
        model="fake",
        registry=registry,
        before_tool=guard,
        stream=False,
    )

    result = asyncio.run(agent.run("查一下"))

    assert executed == []                       # 被拦截，工具没执行
    tool_messages = [m for m in result.messages if m.get("role") == "tool"]
    assert "无权限" in tool_messages[0]["content"]


def test_compress_tool_messages_shrinks_old_results():
    messages = [
        {"role": "tool", "content": "x" * 5000},
        {"role": "tool", "content": "y" * 5000},
    ]
    compressed = compress_tool_messages(messages, budget_chars=6000)

    assert compressed >= 1
    assert "已压缩" in messages[0]["content"]
    assert sum(len(m["content"]) for m in messages) <= 6000


class _FakeStreamCompletions:
    """假流式实现：每次 create 返回一段 chunk 序列。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        chunks = self.script.pop(0)

        async def gen():
            for item in chunks:
                yield item

        return gen()


class _FakeStreamClient:
    def __init__(self, script):
        self.chat = NS(completions=_FakeStreamCompletions(script))


def _chunk(content=None, tool_calls=None, finish_reason=None):
    delta = NS(content=content, tool_calls=tool_calls)
    return NS(choices=[NS(delta=delta, finish_reason=finish_reason)])


def _tc_chunk(index=0, call_id=None, name=None, arguments=None):
    tc = NS(index=index, id=call_id, function=NS(name=name, arguments=arguments))
    return _chunk(tool_calls=[tc])


def test_stream_emits_deltas_and_stitches_tool_arguments():
    """流式下 arguments 是分片增量，必须按 index 拼接后才能执行工具。"""
    executed: list = []
    deltas: list = []
    script = [
        [
            _chunk(content="我先查一下。"),
            _tc_chunk(0, "call_1", "demo__query", '{"qu'),
            _tc_chunk(0, None, None, 'ery":"门禁"}'),
            _chunk(finish_reason="tool_calls"),
        ],
        [
            _chunk(content="共有 3 台门禁设备。"),
            _chunk(finish_reason="stop"),
        ],
    ]

    def collect(event):
        if event.type == "assistant_delta":
            deltas.append(event.data.get("text") or "")

    agent = LoopAgent(
        client=_FakeStreamClient(script),
        model="fake",
        registry=_demo_registry(executed),
        on_event=collect,
    )

    result = asyncio.run(agent.run("查一下门禁"))

    assert executed == [{"query": "门禁"}]
    assert result.text == "共有 3 台门禁设备。"
    assert result.stop_reason == "answered"
    # 过程稿和最终回答都是逐块吐出来的，前端才能做到"边想边说"
    assert "".join(deltas) == "我先查一下。共有 3 台门禁设备。"


def _boom_registry(exc: BaseException):
    """构造一个 execute 直接抛指定异常的工具域。"""

    async def boom(args):
        raise exc

    registry = ToolRegistry()
    registry.add_domain(
        DomainSpec(
            name="demo",
            description="会中断的工具",
            tools=[
                ToolSpec(
                    name="demo__boom",
                    description="中断",
                    parameters=tool_schema({"query": {"type": "string"}}, ["query"]),
                    execute=boom,
                )
            ],
        )
    )
    return registry


def test_tool_cancelled_error_becomes_is_error():
    """mcp SDK 连接超时抛的是 CancelledError（BaseException 子类），必须转成工具错误。

    早期只接 asyncio.TimeoutError，CancelledError 会一路穿透到 uvicorn，
    把整个 WebSocket 打断（前端表现是"发完消息连接就断了"）。
    """
    agent = LoopAgent(
        client=_FakeClient([]),
        model="fake",
        registry=_boom_registry(asyncio.CancelledError("simulated mcp cancel scope")),
        stream=False,
    )

    outcome = asyncio.run(agent._execute("demo__boom", '{"query":"x"}'))

    assert outcome.is_error is True
    assert "中断" in outcome.text


def test_executor_cancelled_error_becomes_is_error():
    """L1 直连路径同样要兜住 CancelledError，且不 escalate（服务不可用时转 L2 无意义）。"""
    from agent.loop.executor import Executor

    executor = Executor(_boom_registry(asyncio.CancelledError("simulated cancel")))

    outcome = asyncio.run(executor.execute("demo__boom", {"query": "x"}))

    assert outcome.is_error is True
    assert outcome.escalate is False
    assert "中断" in outcome.text


# ------------------------------------------------------------------
# 清单类工具（passthrough）：产出了结构化清单就直接收尾
#
# 线上问题：L2 里模型照 system prompt 第 7 条把工具结果又逐行列了一遍，
# 前端"清单一份 + 文本一份"，用户看到同一份设备列表出现两遍。
# ------------------------------------------------------------------
def _list_registry(executed: list):
    from agent.loop.entities import KIND_DEVICE, Entity, EntityList

    async def execute(args):
        executed.append(args)
        return ToolOutcome(
            "2 台设备：\n- 南门道闸出口｜编号 dz_park_0002\n- 南门道闸进口｜编号 dz_park_0001",
            entities=EntityList(
                kind=KIND_DEVICE,
                title="2 台设备",
                items=[
                    Entity(kind=KIND_DEVICE, id="dz_park_0002", label="南门道闸出口"),
                    Entity(kind=KIND_DEVICE, id="dz_park_0001", label="南门道闸进口"),
                ],
            ),
        )

    registry = ToolRegistry()
    registry.add_domain(
        DomainSpec(
            name="demo",
            description="演示域",
            tools=[
                ToolSpec(
                    name="demo__list",
                    description="列设备",
                    parameters=tool_schema({"query": {"type": "string"}}, ["query"]),
                    execute=execute,
                    passthrough=True,          # 关键：清单结果就是成品
                )
            ],
        )
    )
    return registry


def test_passthrough_tool_ends_run_without_model_restating():
    executed: list = []
    events: list = []
    client = _FakeClient([
        NS(
            content="",
            tool_calls=[_tool_call("demo__list", '{"query":"道闸设备有哪些"}')],
        ),
        # 这个分支不该被用到：清单已经直出，不需要模型再总结
        NS(content="根据查询结果，共有 2 台道闸设备……", tool_calls=None),
    ])
    agent = LoopAgent(
        client=client, model="fake", registry=_list_registry(executed), stream=False
    )

    result = asyncio.run(agent.run("道闸设备有哪些", on_event=events.append))

    assert len(executed) == 1
    # 只调了一次模型（第二次是复述清单）
    assert len(client.chat.completions.calls) == 1
    assert result.stop_reason == "answered"
    assert result.text.startswith("2 台设备")
    kinds = [getattr(e, "type", None) for e in events]
    assert "entities" in kinds and "done" in kinds


def test_passthrough_does_not_end_run_on_error():
    """出错时不能直出（错误文案是写给模型看的），仍应回到模型。"""
    async def execute(args):
        return ToolOutcome("工具执行失败：连接超时", is_error=True)

    registry = ToolRegistry()
    registry.add_domain(
        DomainSpec(
            name="demo",
            description="演示域",
            tools=[
                ToolSpec(
                    name="demo__list",
                    description="列设备",
                    parameters=tool_schema({"query": {"type": "string"}}, ["query"]),
                    execute=execute,
                    passthrough=True,
                )
            ],
        )
    )
    client = _FakeClient([
        NS(content="", tool_calls=[_tool_call("demo__list", '{"query":"道闸"}')]),
        NS(content="设备查询服务暂时不可用。", tool_calls=None),
    ])
    agent = LoopAgent(client=client, model="fake", registry=registry, stream=False)

    result = asyncio.run(agent.run("道闸"))

    assert len(client.chat.completions.calls) == 2      # 仍回到模型
    assert "不可用" in result.text
