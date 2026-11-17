"""知识库检索的 **HTTP 直连** 客户端（走平台封装的检索接口，不经 sidecar）。

## 为什么加这条通道

`/mcp/knowledge` 那条链路（`KnowledgeMcpClient` → sidecar → RAGFlow `/api/v1/*`）
依赖 sidecar 容器里能访问 RAGFlow。实测线上 sidecar 里配的是
`http://localhost:9380/api/v1/...`，容器内连不上 → 检索恒返回
「知识库服务暂时不可用」。平台另给了一套检索接口（下面这套），Python 侧直连更快也更好排查。

两条通道的关系：

| 顺序 | 通道 | 何时用 |
|---|---|---|
| 1 | **HTTP 直连（本文件）** | 配了 `KB_BASE_URL` + `KB_API_KEY` 时优先用 |
| 2 | MCP `/mcp/knowledge` | 没配 HTTP 配置时退回（现状行为）；HTTP 调用报错时兜底一次 |

## 请求（平台口径）

```
POST {KB_BASE_URL}/v1/api_key/datasets/draft
Authorization: Bearer {KB_API_KEY}
Content-Type: application/json

{
  "inputs": "外来人员入园需要什么手续",
  "dataset_ids": ["1213360", "1210129"],
  "retrieval_mode": "multiple",
  "retry_config": {"max_retries": 0, "retry_interval": 0, "retry_enabled": false},
  "multiple_retrieval_config": {
    "top_k": 5,
    "score_threshold": 0.13,
    "reranking_mode": "reranking_model",
    "reranking_enable": true,
    "reranking_model": {"provider": "tong_yi", "model": "gte-rerank"},
    "weights": {
      "vector_setting":  {"vector_weight": 1.0},
      "keyword_setting": {"keyword_weight": 0.0}
    }
  }
}
```

响应：`{"data": [{"metadata": {...}, "title": "...", "content": "...", "position": 1}], "inputs": "..."}`

## 环境变量（都支持"空串按未设置"）

| 变量 | 作用 | 默认 |
|---|---|---|
| `KB_BASE_URL` | 检索服务地址，如 `http://10.204.146.44:8082`；**不配则本通道不启用** | 空 |
| `KB_API_KEY` | `Authorization: Bearer <key>`，平台给的 `dataset-xxx` | 空 |
| `KB_RETRIEVAL_PATH` | 检索路径（平台版本不同可能变） | `/v1/api_key/datasets/draft` |
| `KB_DATASET_IDS` | 知识库 ID，多个用英文逗号分隔；留空 = 不指定范围（全库） | 空 |
| `KB_TOP_K` | 召回条数 | `5` |
| `KB_SCORE_THRESHOLD` | 分数阈值；留空则不传该字段 | 空 |
| `KB_RERANK_ENABLE` | 是否开启重排 | `1` |
| `KB_RERANK_PROVIDER` / `KB_RERANK_MODEL` | 重排模型 | `tong_yi` / `gte-rerank` |
| `KB_TIMEOUT` | 请求超时（秒） | `20` |
| `KB_SEGMENT_MAX_CHARS` | 单条片段喂给模型时的最大字符数（防止撑爆上下文） | `800` |

## 故意不做的

- 不在这里组织答案：本类只负责"问题进、检索文本出"，prompt 与流式作答留在
  `graph/knowledge_qa_langgraph.py`；
- 不重试：检索慢时重试只会让首字更慢，失败直接抛给上层兜底文案。
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_RETRIEVAL_PATH = "/v1/api_key/datasets/draft"
DEFAULT_TOP_K = 5
DEFAULT_TIMEOUT = 20.0
DEFAULT_SEGMENT_MAX_CHARS = 800
EMPTY_HINT = "未检索到相关内容。"


def _env(name: str) -> str:
    """读环境变量并去空白；空串视为未设置。"""
    return (os.getenv(name) or "").strip()


def _env_float(name: str, default: Optional[float] = None) -> Optional[float]:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("[kb-http] %s=%r 不是数字，按默认 %s", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("[kb-http] %s=%r 不是整数，按默认 %s", name, raw, default)
        return default


def _env_true(name: str, default: bool = True) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw not in ("0", "false", "off", "no")


class KnowledgeHttpClient:
    """平台检索接口的极简客户端：一次请求换一段可读文本。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        retrieval_path: str = DEFAULT_RETRIEVAL_PATH,
        dataset_ids: Optional[list[str]] = None,
        top_k: int = DEFAULT_TOP_K,
        score_threshold: Optional[float] = None,
        rerank_enable: bool = True,
        rerank_provider: str = "tong_yi",
        rerank_model: str = "gte-rerank",
        timeout: float = DEFAULT_TIMEOUT,
        segment_max_chars: int = DEFAULT_SEGMENT_MAX_CHARS,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.retrieval_path = retrieval_path if retrieval_path.startswith("/") else f"/{retrieval_path}"
        self.dataset_ids = list(dataset_ids or [])
        self.top_k = max(1, top_k)
        self.score_threshold = score_threshold
        self.rerank_enable = rerank_enable
        self.rerank_provider = rerank_provider
        self.rerank_model = rerank_model
        self.timeout = timeout
        self.segment_max_chars = max(80, segment_max_chars)
        # transport 只在测试里注入（httpx.MockTransport），线上走默认
        self._transport = transport
        self._client: Optional[httpx.AsyncClient] = None

    # ------------------------------------------------------------ #
    # 对外
    # ------------------------------------------------------------ #
    @property
    def available(self) -> bool:
        return bool(self.base_url and self.api_key)

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def build_payload(
        self,
        question: str,
        dataset_ids: Optional[list[str]] = None,
        rerank_enable: Optional[bool] = None,
    ) -> dict:
        """按平台口径拼请求体（单独抽出来，便于单测断言字段）。

        `rerank_enable` 显式传入时覆盖实例默认值：平台不支持重排模型会回
        HTTP 500 Retrieval failed，retrieve() 用这个参数做一次「去重排」降级。
        """
        ids = self.dataset_ids if dataset_ids is None else list(dataset_ids)
        use_rerank = self.rerank_enable if rerank_enable is None else bool(rerank_enable)
        multiple: dict[str, Any] = {
            "top_k": self.top_k,
            "reranking_enable": use_rerank,
            "weights": {
                "vector_setting": {"vector_weight": 1.0},
                "keyword_setting": {"keyword_weight": 0.0},
            },
        }
        if self.score_threshold is not None:
            multiple["score_threshold"] = self.score_threshold
        if use_rerank:
            multiple["reranking_mode"] = "reranking_model"
            multiple["reranking_model"] = {
                "provider": self.rerank_provider,
                "model": self.rerank_model,
            }
        return {
            "inputs": question,
            "dataset_ids": ids,
            "retrieval_mode": "multiple",
            "retry_config": {"max_retries": 0, "retry_interval": 0, "retry_enabled": False},
            "multiple_retrieval_config": multiple,
        }

    async def retrieve(self, question: str, dataset_ids: Optional[list[str]] = None) -> str:
        """检索并返回可直接拼进 prompt 的文本；失败抛异常（由调用方兜底）。"""
        question = (question or "").strip()
        if not question:
            return EMPTY_HINT
        if not self.available:
            raise RuntimeError("知识库检索未配置（KB_BASE_URL / KB_API_KEY 为空）")

        url = f"{self.base_url}{self.retrieval_path}"
        payload = self.build_payload(question, dataset_ids)
        ids = payload.get("dataset_ids") or []
        logger.info(
            "[kb-http] 检索请求: POST %s | dataset_ids=%s | q=%r",
            url,
            ids if ids else "[]（空！多数平台=不检索任何库）",
            question[:40],
        )
        started = time.monotonic()

        async def _request(body: dict) -> tuple[Any, Any]:
            resp = await self._client_or_new().post(url, json=body, headers=self._headers())
            resp.raise_for_status()
            return resp, resp.json()

        try:
            resp, data = await _request(payload)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            # 把服务端返回体带上，排查 key/数据集不对时最有用
            body = (exc.response.text or "")[:200] if exc.response is not None else ""
            if self.rerank_enable and status >= 500:
                # 实测：部分平台实例不支持 reranking_model（如 tong_yi/gte-rerank），
                # 带 reranking_enable=true 直接回 HTTP 500 Retrieval failed。
                # 这不是重试同一请求，而是换成平台能接受的参数形态再检一次。
                logger.warning(
                    "[kb-http] 重排不可用（HTTP %s body=%s），本次去掉 reranking 再检索一次",
                    status,
                    body,
                )
                try:
                    resp, data = await _request(
                        self.build_payload(question, dataset_ids, rerank_enable=False)
                    )
                    logger.info(
                        "[kb-http] 去掉重排后检索成功: HTTP %s", getattr(resp, "status_code", "?")
                    )
                except httpx.HTTPStatusError as exc2:
                    status2 = exc2.response.status_code if exc2.response is not None else 0
                    body2 = (exc2.response.text or "")[:200] if exc2.response is not None else ""
                    logger.error(
                        "[kb-http] 去掉重排后仍失败: HTTP %s body=%s", status2, body2
                    )
                    raise RuntimeError(f"知识库接口返回 {status2}: {body2}") from exc2
                except httpx.HTTPError as exc2:
                    logger.error(
                        "[kb-http] 去掉重排后仍失败: %s: %s", type(exc2).__name__, exc2
                    )
                    raise RuntimeError(f"知识库接口调用失败: {exc2}") from exc2
            else:
                hint = ""
                if status in (401, 403):
                    hint = "（鉴权失败：检查 KB_API_KEY 是否为 dataset-xxx 形式）"
                elif status == 404:
                    hint = "（路径不存在：检查 KB_RETRIEVAL_PATH）"
                elif status >= 500:
                    hint = "（服务端异常：检查平台日志）"
                logger.error(
                    "[kb-http] 检索失败: HTTP %s 用时 %.2fs%s body=%s",
                    status,
                    time.monotonic() - started,
                    hint,
                    body,
                )
                raise RuntimeError(f"知识库接口返回 {status}: {body}") from exc
        except httpx.TimeoutException as exc:
            logger.error(
                "[kb-http] 检索超时: %s 用时 %.2fs（KB_TIMEOUT=%ss）",
                url,
                time.monotonic() - started,
                self.timeout,
            )
            raise RuntimeError(f"知识库接口超时: {exc}") from exc
        except httpx.HTTPError as exc:
            logger.error(
                "[kb-http] 检索失败: %s 用时 %.2fs %s: %s "
                "（连接类失败请确认 KB_BASE_URL 在容器内可达、端口已开放）",
                url,
                time.monotonic() - started,
                type(exc).__name__,
                exc,
            )
            raise RuntimeError(f"知识库接口调用失败: {exc}") from exc

        rows = self._rows(data)
        text = self._format(data)
        elapsed = time.monotonic() - started
        status_code = getattr(resp, "status_code", "?")
        if text == EMPTY_HINT:
            logger.warning(
                "[kb-http] 检索成功但结果为空: HTTP %s 用时 %.2fs | data 条数=%s | dataset_ids=%s",
                status_code,
                elapsed,
                len(rows),
                ids if ids else "[]",
            )
        else:
            logger.info(
                "[kb-http] 检索成功: HTTP %s 用时 %.2fs | data 条数=%s（去重后 %s 段）| 文本 %s 字",
                status_code,
                elapsed,
                len(rows),
                len(text.split("【来源：")) - 1,
                len(text),
            )
        return text

    # ------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------ #
    def _client_or_new(self) -> httpx.AsyncClient:
        if self._client is None:
            kwargs: dict[str, Any] = {"timeout": self.timeout}
            if self._transport is not None:
                kwargs["transport"] = self._transport
            self._client = httpx.AsyncClient(**kwargs)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def _rows(data: Any) -> list:
        """从响应体里取出结果行列表（`{"data":[...]}` 与裸数组都兼容）。"""
        if isinstance(data, dict):
            rows = data.get("data") or []
        elif isinstance(data, list):
            rows = data
        else:
            return []
        return rows if isinstance(rows, list) else []

    def _format(self, data: Any) -> str:
        """把 `{"data":[{title,content,...}]}` 归一化成带来源的多段文本。"""
        rows = self._rows(data)
        if not rows:
            return EMPTY_HINT

        parts: list[str] = []
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            content = str(row.get("content") or "").strip()
            if not content:
                continue
            # 平台按滑窗切分，同一段目录/标题会在多条结果里重复出现；
            # 不去重会让 top_k 名额全被重复片段占满，正文片段永远挤不进来。
            if content in seen:
                continue
            seen.add(content)
            if len(content) > self.segment_max_chars:
                content = content[: self.segment_max_chars] + "…"
            meta = row.get("metadata") or {}
            title = str(row.get("title") or meta.get("document_name") or "未命名文档").strip()
            dataset = str(meta.get("dataset_name") or "").strip()
            source = f"{title}（{dataset}）" if dataset else title
            parts.append(f"【来源：{source}】\n{content}")

        if not parts:
            return EMPTY_HINT
        return "\n\n".join(parts)


def build_from_env(transport: Optional[httpx.AsyncBaseTransport] = None) -> Optional[KnowledgeHttpClient]:
    """按环境变量造客户端；`KB_BASE_URL` 或 `KB_API_KEY` 为空时返回 None（= 不启用该通道）。"""
    base_url = _env("KB_BASE_URL")
    api_key = _env("KB_API_KEY")
    if not base_url or not api_key:
        logger.warning(
            "[kb-http] 未启用平台直连：KB_BASE_URL %s、KB_API_KEY %s；知识库问答将走 sidecar /mcp/knowledge",
            "已配置" if base_url else "为空",
            "已配置" if api_key else "为空",
        )
        return None
    raw_ids = _env("KB_DATASET_IDS")
    dataset_ids = [i.strip() for i in raw_ids.split(",") if i.strip()] if raw_ids else []
    client = KnowledgeHttpClient(
        base_url,
        api_key,
        retrieval_path=_env("KB_RETRIEVAL_PATH") or DEFAULT_RETRIEVAL_PATH,
        dataset_ids=dataset_ids,
        top_k=_env_int("KB_TOP_K", DEFAULT_TOP_K),
        score_threshold=_env_float("KB_SCORE_THRESHOLD"),
        rerank_enable=_env_true("KB_RERANK_ENABLE", True),
        rerank_provider=_env("KB_RERANK_PROVIDER") or "tong_yi",
        rerank_model=_env("KB_RERANK_MODEL") or "gte-rerank",
        timeout=_env_float("KB_TIMEOUT", DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT,
        segment_max_chars=_env_int("KB_SEGMENT_MAX_CHARS", DEFAULT_SEGMENT_MAX_CHARS),
        transport=transport,
    )
    logger.info(
        "[kb-http] 平台直连就绪: POST %s%s | 数据集=%s | top_k=%s | threshold=%s "
        "| rerank=%s(%s/%s) | timeout=%ss | API key=已配置",
        client.base_url,
        client.retrieval_path,
        ",".join(client.dataset_ids) or "(未指定)",
        client.top_k,
        client.score_threshold if client.score_threshold is not None else "(不传)",
        client.rerank_enable,
        client.rerank_provider,
        client.rerank_model,
        client.timeout,
    )
    if not dataset_ids:
        logger.warning(
            "[kb-http] KB_DATASET_IDS 为空，请求体里 dataset_ids=[]；"
            "很多平台的实现会把空数组当作「不检索任何库」而返回空结果，建议显式配置库 ID"
        )
    return client
