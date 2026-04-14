# 获取dify配置   
import json
from langchain_core.messages import (
    HumanMessage,
    AIMessage,
    SystemMessage,
    ToolMessage,
    BaseMessage,
)

def get_config(config_file_path = 'global_config.json') -> dict:
    config = {}
    with open(config_file_path, 'r') as f:
        config = json.loads(f.read())
    return config