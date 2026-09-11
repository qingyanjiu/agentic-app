import asyncio
import logging

import yaml
from pathlib import Path
from typing import Dict

from langchain_mcp_adapters.client import MultiServerMCPClient

logger = logging.getLogger(__name__)

'''从YAML配置文件加载多个MCP服务器配置，
并获取这些服务器提供的工具列表，让LangChain Agent能够调用这些远程工具。
# 完整的调用流程：
# 1. load_mcp_config() → 读取YAML配置
# 2. get_mcp_tools() → 逐个 server 独立连接并收集工具
# 3. 返回工具列表给Agent使用
'''
def load_mcp_config(file_path: str) -> Dict:
    """从 YAML 加载 MCP 配置"""
    # 把字符串路径转为 Path 对象（跨平台兼容）
    path = Path(file_path)
    # 打开文件（指定utf-8编码，避免中文乱码）
    with open(path, encoding="utf-8") as f:
         # 安全加载YAML内容（yaml.safe_load避免执行恶意代码）
        config = yaml.safe_load(f)
         # 只返回配置中 "mcp_servers" 字段的内容（核心配置），默认返回空字典
    return config.get("mcp_servers", {})


async def _load_server_tools(name: str, server_conf: dict) -> list:
    """连接单个 MCP server 并返回其工具列表"""
    client = MultiServerMCPClient({name: server_conf})
    return await client.get_tools()


async def get_mcp_tools(yaml_path: str) -> list:
    """
    加载 YAML 中的多 MCP 配置，并返回 LangChain 兼容的工具列表

    每个 server 独立加载、失败只跳过该 server：
    之前所有 server 塞进一个 MultiServerMCPClient，
    TaskGroup 里任何一个连不上（如某模块后端未部署返回 404）
    会导致整体失败、所有域都拿不到工具。
    """
    mcp_dict = load_mcp_config(yaml_path)  # 1. 加载配置

    # 2. 并发加载各 server，单个失败不影响其他
    results = await asyncio.gather(
        *(_load_server_tools(name, conf) for name, conf in mcp_dict.items()),
        return_exceptions=True,
    )

    # 3. 汇总成功的结果，失败的记日志跳过
    tools = []
    for name, result in zip(mcp_dict.keys(), results):
        if isinstance(result, BaseException):
            logger.warning(f"MCP server [{name}] 工具加载失败，跳过该服务: {result}")
            continue
        logger.info(f"MCP server [{name}] 加载到 {len(result)} 个工具")
        tools.extend(result)

    return tools
