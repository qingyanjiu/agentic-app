"""LLM 客户端：**只有一个**模型配置，全部由环境变量驱动。

本模块不依赖 langchain —— loop 链路直接用 openai SDK，场景规划只要
「发一段话、收一段话」，所以这里只提供一个最小客户端 `ChatClient`，
对外接口就三样：`ainvoke(prompt) -> Completion`、`.model_name`、
`.openai_api_base` / `.openai_api_key`（后三个属性供
`agent/loop/ws_bridge.build_pipeline` 复用同一份配置）。

## 支持的两种客户端

| LLM_TYPE | 走哪个接口 | 说明 |
|---|---|---|
| `openai`（默认） | `openai.AsyncOpenAI` 的 chat.completions | OpenAI 兼容接口：DeepSeek / 硅基流动 / vLLM / LM Studio 等 |
| `ollama` | `POST {base}/api/chat`（httpx） | 本地 Ollama 原生接口（不是 /v1） |

两种类型的差异只体现在：调哪个接口、默认地址是什么、以及
`reasoning_effort` / `thinking` 这两个 DeepSeek 专有参数（ollama 忽略）。

## 环境变量

| 变量 | 取值 | 默认值 |
|---|---|---|
| `LLM_TYPE` | `openai` / `ollama` | `openai` |
| `LLM_BASE_URL` | 接口地址 | openai: `https://api.deepseek.com/v1`<br>ollama: `http://localhost:11434` |
| `LLM_MODEL` | 模型名 | openai: `deepseek-flash`<br>ollama: `qwen3:8b` |
| `LLM_API_KEY` | 密钥 | 缺省回退 `OPENAI_API_KEY`；ollama 可留空 |
| `LLM_REASONING_EFFORT` | `low` / `high` / `max` | `low` |
| `LLM_THINKING` | `enabled` / `disabled` | `disabled` |

## 两条容易踩的规则

1. **空字符串按"未设置"处理。** docker-compose 里 `${LLM_THINKING:-}` 会传空串，
   如果让空串覆盖默认值，`thinking` 会变成"不传该字段"，DeepSeek 反而**开启**思考，
   耗时和费用都会上去。所以这里统一 `strip()` 后判空再回退默认值。

2. **`LLM_REASONING_EFFORT` / `LLM_THINKING` 填 `off` 表示"不传该字段"**，
   而不是"传一个 off 值"。DeepSeek 没有 `reasoning_effort=off`，
   真正关闭思考要传 `extra_body={"thinking": {"type": "disabled"}}`。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import AsyncIterator

# 全局采样参数（与具体模型无关）
TEMPERATURE = 0  # 0=确定性输出
MAX_TOKENS = 4096

# 关闭思考级别时允许的写法（不传 reasoning_effort，走模型默认）
_NO_EFFORT_VALUES = {"", "off", "none", "disable", "disabled", "false"}

# DeepSeek 思考模式开关（OpenAI 兼容格式：extra_body.thinking.type）
_THINKING_VALUES = {"enabled", "disabled"}

# 每种客户端类型的默认地址与模型名，只在对应环境变量为空时使用
_TYPE_DEFAULTS = {
    "openai": {
        "model_url": "https://api.deepseek.com/v1",
        "model_name": "deepseek-flash",
    },
    "ollama": {
        "model_url": "http://localhost:11434",
        "model_name": "qwen3:8b",
    },
}


def _env(name: str) -> str:
    """读环境变量并去掉首尾空白；空串视为未设置（compose 的 ${VAR:-} 会传空串）。"""
    return (os.getenv(name) or "").strip()


def build_model_config() -> dict:
    """按环境变量组装唯一的模型配置。

    `reasoning_effort` / `thinking` 在这里就把默认值填进去（而不是留空交给
    resolve_* 去读环境变量），保证"环境变量 > 默认值"的语义只有一层，
    不会出现"配置项写死导致环境变量失效"的老问题。
    """
    kind = (_env("LLM_TYPE") or "openai").lower()
    if kind not in _TYPE_DEFAULTS:
        raise ValueError(
            f"LLM_TYPE 只支持 {sorted(_TYPE_DEFAULTS)}，收到 {kind!r}"
        )
    defaults = _TYPE_DEFAULTS[kind]
    return {
        "type": kind,
        "model_url": _env("LLM_BASE_URL") or defaults["model_url"],
        "model_name": _env("LLM_MODEL") or defaults["model_name"],
        # 统一只读 LLM_API_KEY，再回退 OPENAI_API_KEY（SILICON_API_KEY 已废弃）
        "api_key": _env("LLM_API_KEY") or _env("OPENAI_API_KEY"),
        # 以下两项只对 openai 类型生效，ollama 分支忽略
        "reasoning_effort": _env("LLM_REASONING_EFFORT") or "low",
        "thinking": _env("LLM_THINKING") or "disabled",
    }


def resolve_reasoning_effort(model_config: dict) -> str:
    """
    解析思考级别（OpenAI 兼容接口的 reasoning_effort）

    优先级：模型配置项 > 环境变量 LLM_REASONING_EFFORT。
    DeepSeek 支持 low / high / max（medium 会被映射成 high）。
    设为 off/none/disabled/false 或空串表示**不传该字段**，但注意：
    不传不等于关闭思考——DeepSeek 思考模式默认开启（effort=high），
    真正关闭要用 thinking={"type": "disabled"}（见 resolve_thinking）。
    """
    effort = model_config.get("reasoning_effort")
    if effort is None:
        effort = os.getenv("LLM_REASONING_EFFORT")
    effort = str(effort or "").strip().lower()
    return "" if effort in _NO_EFFORT_VALUES else effort


def resolve_thinking(model_config: dict) -> str:
    """
    解析思考模式开关：enabled / disabled

    优先级：模型配置项 > 环境变量 LLM_THINKING。
    DeepSeek 的 OpenAI 兼容接口没有 reasoning_effort="off"，
    要关闭思考必须传 extra_body={"thinking": {"type": "disabled"}}。
    """
    value = model_config.get("thinking")
    if value is None:
        value = os.getenv("LLM_THINKING")
    value = str(value or "").strip().lower()
    return value if value in _THINKING_VALUES else ""


@dataclass
class Completion:
    """最小返回对象：只有 `.content`。

    与原来的 AIMessage 对齐到同一个取法：调用方 `getattr(resp, "content", resp)`
    照样能用（见 agent/scene_control.py 的 `_resp_text`）。
    """

    content: str = ""


class ChatClient:
    """一个模型 = 一次 HTTP 请求。对外只有 `ainvoke(prompt) -> Completion`。

    属性 `model_name` / `openai_api_base` / `openai_api_key` 是给
    `agent/loop/ws_bridge.build_pipeline` 复用的：它拿这三个值自己建
    AsyncOpenAI 客户端，不依赖本类。
    """

    def __init__(self, config: dict):
        self.config = config
        self.type = config["type"]
        self.model_name = config["model_name"]
        self.openai_api_base = config["model_url"]
        self.openai_api_key = config["api_key"]
        self.temperature = TEMPERATURE
        self.max_tokens = MAX_TOKENS
        # 只对 openai 类型生效（ollama 分支不发这两个参数）
        self.reasoning_effort = resolve_reasoning_effort(config)
        self.thinking = resolve_thinking(config)
        self.extra_body = {"thinking": {"type": self.thinking}} if self.thinking else None
        self._client = None  # 懒建：import 期不连网络，也不用时就付连接成本

    # ------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------ #
    async def ainvoke(self, prompt: str) -> Completion:
        """发一段话，收一段话。prompt 只支持字符串（调用方都是拼好的提示词）。"""
        if self.type == "ollama":
            return Completion(content=await self._chat_ollama(prompt))
        return Completion(content=await self._chat_openai(prompt))

    async def astream(self, prompt: str) -> AsyncIterator[str]:
        """流式返回文本增量（KB 问答这类要边生成边上屏的场景用）。

        与 `ainvoke` 同一份配置：openai 走 SDK 的 stream，ollama 走
        /api/chat 的 NDJSON 流。调用方负责拼提示词和收尾。
        """
        if self.type == "ollama":
            async for piece in self._stream_ollama(prompt):
                yield piece
            return
        async for piece in self._stream_openai(prompt):
            yield piece

    async def _stream_openai(self, prompt: str) -> AsyncIterator[str]:
        kwargs = {
            "model": self.model_name,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": True,
        }
        if self.reasoning_effort:
            kwargs["reasoning_effort"] = self.reasoning_effort
        if self.extra_body:
            kwargs["extra_body"] = self.extra_body

        stream = await self._openai_client().chat.completions.create(**kwargs)
        async for chunk in stream:
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            delta = getattr(choices[0], "delta", None)
            text = getattr(delta, "content", None) if delta is not None else None
            if text:
                yield text

    async def _stream_ollama(self, prompt: str) -> AsyncIterator[str]:
        import json as _json

        import httpx

        url = f"{self.openai_api_base.rstrip('/')}/api/chat"
        payload = {
            "model": self.model_name,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
            "think": False,
            "options": {"temperature": self.temperature, "num_predict": self.max_tokens},
        }
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream("POST", url, json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    line = (line or "").strip()
                    if not line:
                        continue
                    try:
                        data = _json.loads(line)
                    except ValueError:
                        continue
                    text = (data.get("message") or {}).get("content") or ""
                    if text:
                        yield text
                    if data.get("done"):
                        break

    # ------------------------------------------------------------ #
    # 两种后端
    # ------------------------------------------------------------ #
    def _openai_client(self):
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(
                base_url=self.openai_api_base,
                api_key=self.openai_api_key or "not-needed",
            )
        return self._client

    async def _chat_openai(self, prompt: str) -> str:
        kwargs = {
            "model": self.model_name,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        # DeepSeek: low/high/max。不传 = 走服务端默认（默认是开启思考，见模块注释）
        if self.reasoning_effort:
            kwargs["reasoning_effort"] = self.reasoning_effort
        # {"thinking": {"type": "disabled"}} —— 真正关闭思考靠这个字段
        if self.extra_body:
            kwargs["extra_body"] = self.extra_body

        resp = await self._openai_client().chat.completions.create(**kwargs)
        return (resp.choices[0].message.content or "") if resp.choices else ""

    async def _chat_ollama(self, prompt: str) -> str:
        import httpx

        url = f"{self.openai_api_base.rstrip('/')}/api/chat"
        payload = {
            "model": self.model_name,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "think": False,  # 与改造前 ChatOllama(reasoning=False) 一致
            "options": {"temperature": self.temperature, "num_predict": self.max_tokens},
        }
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json() or {}
        return ((data.get("message") or {}).get("content") or "")


class CustomLLMFactory():
    """构造唯一的 LLM 客户端。配置来源见模块文档，没有第二套写死的配置表。"""

    def __init__(self):
        self.config = build_model_config()
        self.llm = self.init_llm(self.config)

    def init_llm(self, model_config: dict) -> ChatClient:
        return ChatClient(model_config)
