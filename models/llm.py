from langchain_openai import ChatOpenAI
from langchain_ollama import ChatOllama
from langchain_core.language_models import BaseLanguageModel
import os

class CustomLLMFactory():
    def __init__(self):
        '''
        name-模型的key，在放到map的时候作为检索的名称
        type-模型的api类型，openai/ollama或其他
        model_url-模型api地址
        model_name-模型名称，调用模型时需要
        api_key-模型的api-key，调用需要认证的模型
        '''
        model_confs = [
            { 
                "name": "silicon",
                "type": "openai",
                "model_url": "https://api.siliconflow.cn/v1",
                "model_name": "Qwen/Qwen3-30B-A3B",
                "api_key": os.getenv("SILICON_API_KEY") if os.getenv("SILICON_API_KEY") else os.getenv("OPENAI_API_KEY")
            },
            # { 
            #     "name": "local",
            #     "type": "openai",
            #     "model_url": "http://192.168.100.85:1234/v1",
            #     "model_name": "qwen/qwen3-8b",
            #     "api_key": "123"
            # },
            { 
                "name": "local",
                "type": "ollama",
                "model_url": "http://host.docker.internal:11434",
                "model_name": "qwen3:8b",
                "api_key": "123"
            },
        ]
        # 初始化llm列表
        self.llms = {}
        for conf in model_confs:
            llm = self.init_llm(conf)
            self.llms[conf['name']] = llm

    def init_llm(self, model_config) -> BaseLanguageModel:
        temperature = 0
        max_tokens = 4096
        if(model_config['type'] == 'openai'):
            return ChatOpenAI(
                base_url=model_config['model_url'],
                model=model_config['model_name'],
                api_key=model_config['api_key'],
                temperature=temperature,
                max_tokens=max_tokens
            )
        if(model_config['type'] == 'ollama'):
            return ChatOllama(
                base_url=model_config['model_url'],
                model=model_config['model_name'],
                temperature=temperature,
                reasoning=False,
            )