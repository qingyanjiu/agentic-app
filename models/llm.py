from langchain_openai import ChatOpenAI
from langchain_ollama import ChatOllama
from langchain_core.language_models import BaseLanguageModel
import os

class CustomLLMFactory():
    def __init__(self):
        '''
        配置项说明（注释是关键，定义了模型配置的规范）：
        name-模型的key，在放到map的时候作为检索的名称（如'silicon'/'zp'）
        type-模型的api类型，openai/ollama或其他（区分不同客户端）
        model_url-模型api地址（OpenAI兼容API的base_url，或Ollama的地址）
        model_name-模型名称，调用模型时需要（如Qwen3-30B、glm-4.7）
        api_key-模型的api-key，调用需要认证的模型（Ollama无需真实密钥）
        '''
        model_confs = [
            { 
                "name": "silicon",
                "type": "openai",
                "model_url": "https://api.siliconflow.cn/v1", # 硅基流动API地址
                "model_name": "Qwen/Qwen3-30B-A3B-Instruct-2507",
                # 优先级：先读SILICON_API_KEY，没有则读OPENAI_API_KEY
                "api_key": os.getenv("SILICON_API_KEY") if os.getenv("SILICON_API_KEY") else os.getenv("OPENAI_API_KEY")
            },
            { 
                "name": "zp",
                "type": "openai",
                "model_url": "https://open.bigmodel.cn/api/coding/paas/v4",
                "model_name": "glm-4.7",
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
            return ChatOpenAI(
                base_url=model_config['model_url'],  # 自定义API地址（非OpenAI官方）
                model=model_config['model_name'],    # 具体模型名
                api_key=model_config['api_key'],     # API密钥
                temperature=temperature,             # 温度参数
                max_tokens=max_tokens                # 最大token数
            )
        if(model_config['type'] == 'ollama'):
            return ChatOllama(
                base_url=model_config['model_url'],  # Ollama服务地址
                model=model_config['model_name'],    # Ollama本地模型名
                temperature=temperature,             # 温度参数
                reasoning=False,                     # Ollama专属参数（禁用推理模式）
            )