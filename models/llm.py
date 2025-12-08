from langchain_openai import ChatOpenAI
from langchain_core.language_models import BaseLanguageModel
import os

class CustomLLMFactory():
    def __init__(self):
        model_confs = [
            { 
                "name": "silicon",
                "model_url": "https://api.siliconflow.cn/v1",
                "model_name": "Qwen/Qwen3-30B-A3B",
                "api_key": os.getenv("SILICON_API_KEY") if os.getenv("SILICON_API_KEY") else os.getenv("OPENAI_API_KEY")
            },
            { 
                "name": "local",
                "model_url": "http://192.168.100.85:1234/v1",
                "model_name": "qwen/qwen3-vl-8b",
                "api_key": "123"
            },
        ]
        # 初始化llm列表
        self.llms = {}
        for conf in model_confs:
            llm = self.openai_llm(conf['model_url'], conf['model_name'], conf['api_key'])
            self.llms[conf['name']] = llm

    def openai_llm(self, model_url, model, api_key) -> BaseLanguageModel:
        return ChatOpenAI(
            base_url=model_url,
            model=model,
            api_key=api_key,
            temperature=0,
            max_tokens=4096
        )