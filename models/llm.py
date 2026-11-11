from langchain_openai import ChatOpenAI
from langchain_ollama import ChatOllama
from langchain_core.language_models import BaseLanguageModel
import os


# 关闭思考级别时允许的写法（不传 reasoning_effort，走模型默认）
_NO_EFFORT_VALUES = {"", "off", "none", "disable", "disabled", "false"}

# DeepSeek 思考模式开关（OpenAI 兼容格式：extra_body.thinking.type）
_THINKING_VALUES = {"enabled", "disabled"}


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
    def __init__(self):
        '''
        配置项说明（注释是关键，定义了模型配置的规范）：
        name-模型的key，在放到map的时候作为检索的名称（如'deepseek'/'local'）
        type-模型的api类型，openai/ollama或其他（区分不同客户端）
        model_url-模型api地址（OpenAI兼容API的base_url，或Ollama的地址）
        model_name-模型名称，调用模型时需要（如Qwen3-30B、glm-4.7）
        api_key-模型的api-key，调用需要认证的模型（Ollama无需真实密钥）
        reasoning_effort-思考级别（可选）：low/high/max；不填则读
                        环境变量 LLM_REASONING_EFFORT，再没有就不传
        thinking-思考模式开关（可选）：enabled/disabled；不填则读
                 环境变量 LLM_THINKING。DeepSeek 想真正关闭思考用 disabled
        '''
        model_confs = [
            {
                "name": "local",
                "type": "openai",
                "model_url": "http://192.168.1.188:1234/v1",
                "model_name": "qwen/qwen3.5-9b",
                "api_key": os.getenv("OPENAI_API_KEY")
            },
            {
                "name": "deepseek",
                "type": "openai",
                "model_url": "https://api.deepseek.com/v1", # 硅基流动API地址
                "model_name": "deepseek-flash",
                "reasoning_effort": "low",
                "thinking": "disabled",
                # 统一只读 OPENAI_API_KEY（SILICON_API_KEY 已废弃）
                "api_key": os.getenv("OPENAI_API_KEY")
            },
            # {
            #     "name": "local",
            #     "type": "ollama",
            #     "model_url": "http://host.docker.internal:11434",
            #     "model_name": "qwen3:8b",
            #     "api_key": "123"
            # },
        ]
        # 初始化llm列表
        self.llms = {}
        for conf in model_confs:
            llm = self.init_llm(conf)
            self.llms[conf['name']] = llm

    def init_llm(self, model_config) -> BaseLanguageModel:
          # 全局模型参数（统一配置，可根据需求调整）
        temperature = 0  # 温度：0=确定性输出，越高越随机
        max_tokens = 4096  # 最大生成token数
        if(model_config['type'] == 'openai'):
            kwargs = {
                "base_url": model_config['model_url'],  # 自定义API地址（非OpenAI官方）
                "model": model_config['model_name'],    # 具体模型名
                "api_key": model_config['api_key'],     # API密钥
                "temperature": temperature,             # 温度参数
                "max_tokens": max_tokens,               # 最大token数
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
        if(model_config['type'] == 'ollama'):
            return ChatOllama(
                base_url=model_config['model_url'],  # Ollama服务地址
                model=model_config['model_name'],    # Ollama本地模型名
                temperature=temperature,             # 温度参数
                reasoning=False,                     # Ollama专属参数（禁用推理模式）
            )