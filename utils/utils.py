# 获取dify配置   
import json
import logging
from langchain_core.messages import (
    HumanMessage,
    AIMessage,
    SystemMessage,
    ToolMessage,
    BaseMessage,
)

def get_config(config_file_path = 'global_config.json') -> dict:
    """读配置文件；文件缺失或解析失败时返回空 dict 并告警，不抛异常。

    部署包里不带 global_config.json（pack.sh 当密钥文件排除，里面是
    redis 密码 / dify key），调用方必须能容忍它不存在，
    否则 import 期就 FileNotFoundError 直接起不来。
    """
    try:
        with open(config_file_path, 'r', encoding='utf-8') as f:
            return json.loads(f.read())
    except FileNotFoundError:
        logging.warning(
            "[config] %s 不存在，使用默认配置（部署包通常不带该文件）", config_file_path
        )
    except Exception as exc:
        logging.warning("[config] %s 读取失败：%s，使用默认配置", config_file_path, exc)
    return {}