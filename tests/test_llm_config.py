# -*- coding: utf-8 -*-
"""LLM 思考级别（reasoning_effort）/ 思考开关（thinking）配置测试（纯函数，不发起请求）。"""
from models.llm import resolve_reasoning_effort, resolve_thinking


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


def test_factory_deepseek_configs_disable_thinking(monkeypatch):
    """当前 deepseek 系配置：思考关闭 + effort=low（effort 在关闭时无实际作用）"""
    from models.llm import CustomLLMFactory

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("SILICON_API_KEY", raising=False)

    factory = CustomLLMFactory()
    for name in ("silicon", "deepseek"):
        llm = factory.llms[name]
        assert llm.extra_body == {"thinking": {"type": "disabled"}}
        assert llm.reasoning_effort == "low"
