# -*- coding: utf-8 -*-
"""业务图回答流式输出测试（不连真实 LLM）。

验证 graph/stream_utils.py：
  1. 图内执行时，token 通过 custom 通道逐个推出；
  2. 非流式直接调用时，退化为累积文本；
  3. 流式中途失败但已有内容时，返回已生成部分（避免调用方再发完整回答）。
"""
from typing import TypedDict

from langgraph.graph import StateGraph, END

from conftest import FakeLLM, run
from graph.stream_utils import stream_llm_text


class StreamState(TypedDict):
    prompt: str
    text: str


def _build_graph(llm):
    async def answer_node(state: StreamState) -> dict:
        return {"text": await stream_llm_text(llm, state["prompt"])}

    workflow = StateGraph(StreamState)
    workflow.add_node("answer", answer_node)
    workflow.set_entry_point("answer")
    workflow.add_edge("answer", END)
    return workflow.compile()


def test_stream_llm_text_emits_custom_chunks():
    graph = _build_graph(FakeLLM("你好"))

    async def collect():
        chunks = []
        final = {}
        async for mode, chunk in graph.astream(
            {"prompt": "p", "text": ""}, stream_mode=["custom", "values"]
        ):
            if mode == "custom":
                chunks.append(chunk)
            else:
                final = chunk
        return chunks, final

    chunks, final = run(collect())

    assert [c["content"] for c in chunks] == ["你好"]
    assert all(c["type"] == "answer" for c in chunks)
    assert final["text"] == "你好"


def test_stream_llm_text_without_writer_still_returns_text():
    # 单测直接调用格式化函数时不在图内，拿不到 writer，也要正常工作
    text = run(stream_llm_text(FakeLLM("普通回答"), "p"))
    assert text == "普通回答"


class _PartialFailLLM(FakeLLM):
    async def astream(self, prompt, **kwargs):
        self.prompts.append(prompt)
        yield type("FakeLLMChunk", (), {"content": "已生成"})()
        raise RuntimeError("stream broken")


def test_stream_llm_text_returns_partial_when_stream_breaks():
    text = run(stream_llm_text(_PartialFailLLM(), "p"))
    assert text == "已生成"
