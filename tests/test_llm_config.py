# -*- coding: utf-8 -*-
"""LLM 配置测试：环境变量覆盖、默认值回退、思考开关（纯函数/构造，不发起请求）。"""
from models.llm import build_model_config, resolve_reasoning_effort, resolve_thinking


def test_model_config_wins(monkeypatch):
    monkeypatch.setenv("LLM_REASONING_EFFORT", "high")
    assert resolve_reasoning_effort({"reasoning_effort": "low"}) == "low"


def test_env_fallback(monkeypatch):
    monkeypatch.setenv("LLM_REASONING_EFFORT", "medium")
    assert resolve_reasoning_effort({}) == "medium"


def test_default_is_empty(monkeypatch):
    monkeypatch.delenv("LLM_REASONING_EFFORT", raising=False)
    assert resolve_reasoning_effort({}) == ""


def test_off_values_disable(monkeypatch):
    monkeypatch.setenv("LLM_REASONING_EFFORT", "off")
    assert resolve_reasoning_effort({}) == ""
    assert resolve_reasoning_effort({"reasoning_effort": "disabled"}) == ""
    assert resolve_reasoning_effort({"reasoning_effort": ""}) == ""


def test_effort_is_normalized(monkeypatch):
    monkeypatch.delenv("LLM_REASONING_EFFORT", raising=False)
    assert resolve_reasoning_effort({"reasoning_effort": " LOW "}) == "low"


# ------------------------------------------------------------
# DeepSeek 的 reasoning_effort 没有 off；关闭思考要走 thinking.type
# ------------------------------------------------------------
def test_thinking_model_config_wins(monkeypatch):
    monkeypatch.setenv("LLM_THINKING", "enabled")
    assert resolve_thinking({"thinking": "disabled"}) == "disabled"


def test_thinking_env_fallback(monkeypatch):
    monkeypatch.setenv("LLM_THINKING", "DISABLED")
    assert resolve_thinking({}) == "disabled"


def test_thinking_default_is_empty(monkeypatch):
    monkeypatch.delenv("LLM_THINKING", raising=False)
    assert resolve_thinking({}) == ""


def test_thinking_rejects_effort_style_values(monkeypatch):
    # off/low 不是 thinking 的合法值，直接忽略
    monkeypatch.delenv("LLM_THINKING", raising=False)
    assert resolve_thinking({"thinking": "off"}) == ""
    assert resolve_thinking({"thinking": "low"}) == ""


_ALL_MODEL_ENVS = (
    "LLM_TYPE",
    "LLM_BASE_URL",
    "LLM_MODEL",
    "LLM_API_KEY",
    "LLM_REASONING_EFFORT",
    "LLM_THINKING",
)


def test_factory_default_matches_production(monkeypatch):
    """环境变量全空时的默认值：openai / deepseek-flash / 思考关闭 / effort=low。"""
    from models.llm import CustomLLMFactory

    for name in _ALL_MODEL_ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    factory = CustomLLMFactory()
    assert factory.config["type"] == "openai"
    assert factory.config["model_url"] == "https://api.deepseek.com/v1"
    assert factory.config["model_name"] == "deepseek-flash"
    # 改造前这两项写死在配置表里，现在由默认值给出，行为不变
    assert factory.llm.extra_body == {"thinking": {"type": "disabled"}}
    assert factory.llm.reasoning_effort == "low"


def test_env_overrides_model_and_url(monkeypatch):
    """LLM_BASE_URL / LLM_MODEL 现在真的能覆盖默认值（改造前完全无效）。"""
    from models.llm import CustomLLMFactory

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.siliconflow.cn/v1")
    monkeypatch.setenv("LLM_MODEL", "Qwen/Qwen3-30B-A3B")

    factory = CustomLLMFactory()
    assert factory.config["model_url"] == "https://api.siliconflow.cn/v1"
    assert factory.config["model_name"] == "Qwen/Qwen3-30B-A3B"


def test_llm_api_key_falls_back_to_openai_key(monkeypatch):
    from models.llm import build_model_config

    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "from-openai")
    assert build_model_config()["api_key"] == "from-openai"

    monkeypatch.setenv("LLM_API_KEY", "dedicated")
    assert build_model_config()["api_key"] == "dedicated"


def test_empty_env_falls_back_to_default(monkeypatch):
    """compose 的 ${VAR:-} 会传空串，空串必须按未设置处理，不能冲掉默认值。"""
    monkeypatch.setenv("LLM_MODEL", "")
    monkeypatch.setenv("LLM_BASE_URL", "   ")  # 只有空白也算空
    monkeypatch.setenv("LLM_THINKING", "")
    monkeypatch.setenv("LLM_REASONING_EFFORT", "")

    config = build_model_config()
    assert config["model_name"] == "deepseek-flash"
    assert config["model_url"] == "https://api.deepseek.com/v1"
    assert config["thinking"] == "disabled"
    assert config["reasoning_effort"] == "low"


def test_thinking_env_can_enable(monkeypatch):
    """LLM_THINKING=enabled 能真正把思考打开（改造前被硬编码遮蔽）。"""
    monkeypatch.setenv("LLM_THINKING", "enabled")
    assert build_model_config()["thinking"] == "enabled"


def test_llm_type_ollama_defaults(monkeypatch):
    """LLM_TYPE=ollama 时用 Ollama 的默认地址与模型，且不需要 api_key。"""
    monkeypatch.setenv("LLM_TYPE", "ollama")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)

    config = build_model_config()
    assert config["type"] == "ollama"
    assert config["model_url"] == "http://localhost:11434"
    assert config["model_name"] == "qwen3:8b"


def test_llm_type_invalid_raises(monkeypatch):
    """拼错类型要立刻报错，而不是悄悄退回 openai。"""
    import pytest

    monkeypatch.setenv("LLM_TYPE", "vllm")
    with pytest.raises(ValueError):
        build_model_config()
