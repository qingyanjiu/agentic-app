"""LLM 客户端工厂：**只有一个**模型配置，全部由环境变量驱动。

## 支持的两种客户端

| LLM_TYPE | 用的类 | 说明 |
|---|---|---|
| `openai`（默认） | `ChatOpenAI` | OpenAI 兼容接口：DeepSeek / 硅基流动 / vLLM / LM Studio 等 |
| `ollama` | `ChatOllama` | 本地 Ollama，走它的原生接口（不是 /v1） |

两种类型的差异只体现在：用哪个客户端类、默认地址是什么、以及
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

from langchain_core.language_models import BaseLanguageModel
from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI

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


class CustomLLMFactory():
    """构造唯一的 LLM 客户端。配置来源见模块文档，没有第二套写死的配置表。"""

    def __init__(self):
        self.config = build_model_config()
        self.llm = self.init_llm(self.config)

    def init_llm(self, model_config: dict) -> BaseLanguageModel:
        if model_config["type"] == "openai":
            kwargs = {
                "base_url": model_config["model_url"],  # 自定义 API 地址（非 OpenAI 官方）
                "model": model_config["model_name"],    # 具体模型名
                "api_key": model_config["api_key"],     # API 密钥
                "temperature": TEMPERATURE,             # 温度参数
                "max_tokens": MAX_TOKENS,               # 最大 token 数
            }
            effort = resolve_reasoning_effort(model_config)
            if effort:
                # 会写进请求体的 reasoning_effort 字段（DeepSeek: low/high/max）
                kwargs["reasoning_effort"] = effort
            thinking = resolve_thinking(model_config)
            if thinking:
                # DeepSeek 关闭/开启思考：{"thinking": {"type": "disabled"}}
                kwargs["extra_body"] = {"thinking": {"type": thinking}}
            return ChatOpenAI(**kwargs)

        return ChatOllama(
            base_url=model_config["model_url"],  # Ollama 服务地址
            model=model_config["model_name"],    # 本地模型名
            temperature=TEMPERATURE,
            reasoning=False,  # Ollama 专属参数：禁用推理模式（与改造前保持一致）
        )
