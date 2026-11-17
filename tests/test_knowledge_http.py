# -*- coding: utf-8 -*-
"""知识库 HTTP 直连客户端测试（不连真实检索服务，用 httpx.MockTransport）。

覆盖：
  1. 请求口径：URL / Bearer 头 / body 字段（inputs、dataset_ids、retrieval_mode、
     multiple_retrieval_config.top_k / score_threshold / reranking / weights）
  2. 响应归一化：`{"data":[{title,metadata,content}]}` → 带来源的多段文本
  3. 空结果 → 「未检索到相关内容。」
  4. HTTP 5xx / 网络异常 → 抛 RuntimeError（由管线兜底成友好文案）
  5. 环境变量：未配置时 build_from_env() 返回 None；配置后各字段解析正确
"""
import asyncio
import json

import httpx
import pytest

from mcp_client import knowledge_http_client as kb

BASE = "http://kb.local:8082"
KEY = "dataset-test-key"


def _client(handler, **kwargs) -> kb.KnowledgeHttpClient:
    return kb.KnowledgeHttpClient(
        BASE, KEY, transport=httpx.MockTransport(handler), **kwargs
    )


def _retrieve(client: kb.KnowledgeHttpClient, question: str = "外来人员入园需要什么手续") -> str:
    async def run():
        try:
            return await client.retrieve(question)
        finally:
            await client.aclose()

    return asyncio.run(run())


# ============================================================
# 1. 请求口径
# ============================================================
def test_request_shape_matches_platform_contract():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"data": [], "inputs": "x"})

    client = _client(
        handler,
        dataset_ids=["1213360", "1210129"],
        top_k=4,
        score_threshold=0.13,
        rerank_enable=True,
        rerank_provider="tong_yi",
        rerank_model="gte-rerank",
    )
    _retrieve(client)

    assert seen["url"] == f"{BASE}/v1/api_key/datasets/draft"
    assert seen["auth"] == f"Bearer {KEY}"
    body = seen["body"]
    assert body["inputs"] == "外来人员入园需要什么手续"
    assert body["dataset_ids"] == ["1213360", "1210129"]
    assert body["retrieval_mode"] == "multiple"
    assert body["retry_config"] == {"max_retries": 0, "retry_interval": 0, "retry_enabled": False}
    multiple = body["multiple_retrieval_config"]
    assert multiple["top_k"] == 4
    assert multiple["score_threshold"] == 0.13
    assert multiple["reranking_mode"] == "reranking_model"
    assert multiple["reranking_enable"] is True
    assert multiple["reranking_model"] == {"provider": "tong_yi", "model": "gte-rerank"}
    assert multiple["weights"] == {
        "vector_setting": {"vector_weight": 1.0},
        "keyword_setting": {"keyword_weight": 0.0},
    }


def test_rerank_off_and_no_threshold_keeps_body_minimal():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"data": []})

    _retrieve(_client(handler, rerank_enable=False))
    multiple = seen["body"]["multiple_retrieval_config"]
    assert "reranking_mode" not in multiple
    assert "reranking_model" not in multiple
    assert "score_threshold" not in multiple
    assert multiple["reranking_enable"] is False


def test_empty_dataset_ids_sends_empty_list():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"data": []})

    _retrieve(_client(handler))
    assert seen["body"]["dataset_ids"] == []


# ============================================================
# 2. 响应归一化
# ============================================================
def _doc_payload():
    return {
        "data": [
            {
                "metadata": {
                    "dataset_name": "园区制度",
                    "document_name": "外来人员管理办法.docx",
                    "score": 0.61,
                },
                "title": "外来人员管理办法.docx",
                "content": "入园需提前一天报备，携带身份证登记。",
                "position": 1,
            },
            {
                "metadata": {"document_name": "门岗操作规范.pdf"},
                "title": "门岗操作规范.pdf",
                "content": "门岗对外来人员进行身份核验。",
                "position": 2,
            },
        ],
        "inputs": "外来人员入园需要什么手续",
    }


def test_formats_segments_with_source():
    text = _retrieve(_client(lambda req: httpx.Response(200, json=_doc_payload())))
    assert "【来源：外来人员管理办法.docx（园区制度）】" in text
    assert "入园需提前一天报备" in text
    assert "【来源：门岗操作规范.pdf】" in text
    # 两段之间空行分隔，顺序与接口返回一致
    assert text.index("外来人员管理办法") < text.index("门岗操作规范")


def test_long_segment_is_truncated():
    payload = {"data": [{"title": "长文档.docx", "content": "字" * 5000}]}
    text = _retrieve(
        _client(lambda req: httpx.Response(200, json=payload), segment_max_chars=100)
    )
    assert text.endswith("…")
    assert len(text) < 300


def test_empty_data_returns_hint():
    assert _retrieve(_client(lambda req: httpx.Response(200, json={"data": []}))) == kb.EMPTY_HINT
    assert _retrieve(_client(lambda req: httpx.Response(200, json={}))) == kb.EMPTY_HINT


def test_row_without_content_is_skipped():
    payload = {"data": [{"title": "空文档", "content": "  "}]}
    assert _retrieve(_client(lambda req: httpx.Response(200, json=payload))) == kb.EMPTY_HINT


# ============================================================
# 3. 异常
# ============================================================
def test_http_error_raises_with_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="invalid api key")

    with pytest.raises(RuntimeError) as exc:
        _retrieve(_client(handler))
    assert "400" in str(exc.value) and "invalid api key" in str(exc.value)


def test_network_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(RuntimeError):
        _retrieve(_client(handler))


def test_not_configured_raises():
    client = kb.KnowledgeHttpClient("", "", transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    assert client.available is False
    with pytest.raises(RuntimeError):
        _retrieve(client)


def test_empty_question_short_circuits():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"data": []})

    assert _retrieve(_client(handler), question="   ") == kb.EMPTY_HINT
    assert calls == []  # 空问题不打网络


# ============================================================
# 4. 环境变量
# ============================================================
def test_build_from_env_requires_base_and_key(monkeypatch):
    for name in ("KB_BASE_URL", "KB_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert kb.build_from_env() is None

    monkeypatch.setenv("KB_BASE_URL", "http://kb.local:8082/")
    assert kb.build_from_env() is None  # 缺 key 仍不启用

    monkeypatch.setenv("KB_API_KEY", "dataset-test-key")
    client = kb.build_from_env()
    assert client is not None
    assert client.base_url == "http://kb.local:8082"  # 末尾斜杠被去掉
    assert client.available is True


def test_build_from_env_parses_options(monkeypatch):
    monkeypatch.setenv("KB_BASE_URL", "http://kb.local:8082")
    monkeypatch.setenv("KB_API_KEY", "k")
    monkeypatch.setenv("KB_DATASET_IDS", " 1213360 , 1210129 ,, ")
    monkeypatch.setenv("KB_TOP_K", "8")
    monkeypatch.setenv("KB_SCORE_THRESHOLD", "0.2")
    monkeypatch.setenv("KB_RERANK_ENABLE", "0")
    monkeypatch.setenv("KB_RETRIEVAL_PATH", "v1/custom/retrieve")
    monkeypatch.setenv("KB_SEGMENT_MAX_CHARS", "600")

    client = kb.build_from_env()
    assert client.dataset_ids == ["1213360", "1210129"]
    assert client.top_k == 8
    assert client.score_threshold == pytest.approx(0.2)
    assert client.rerank_enable is False
    assert client.retrieval_path == "/v1/custom/retrieve"  # 自动补前导斜杠
    assert client.segment_max_chars == 600


def test_build_from_env_ignores_bad_numbers(monkeypatch):
    monkeypatch.setenv("KB_BASE_URL", "http://kb.local:8082")
    monkeypatch.setenv("KB_API_KEY", "k")
    monkeypatch.setenv("KB_TOP_K", "abc")
    monkeypatch.setenv("KB_SCORE_THRESHOLD", "high")
    client = kb.build_from_env()
    assert client.top_k == kb.DEFAULT_TOP_K
    assert client.score_threshold is None
