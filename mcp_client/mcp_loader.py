import yaml
from pathlib import Path
from typing import Dict

from langchain_mcp_adapters.client import MultiServerMCPClient
'''从YAML配置文件加载多个MCP服务器配置，
并获取这些服务器提供的工具列表，让LangChain Agent能够调用这些远程工具。
# 完整的调用流程：
# 1. load_mcp_config() → 读取YAML配置
# 2. MultiServerMCPClient() → 创建多服务器客户端
# 3. client.get_tools() → 异步获取所有工具
# 4. 返回工具列表给Agent使用
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

async def get_mcp_tools(yaml_path: str) -> list:
    """
    加载 YAML 中的多 MCP 配置，并返回 LangChain 兼容的工具列表
    """
    mcp_dict = load_mcp_config(yaml_path)# 1. 加载配置
    client = MultiServerMCPClient(mcp_dict) # 2. 创建客户端
    tools = await client.get_tools() # 3. 异步获取工具
    return tools
