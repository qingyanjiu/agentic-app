import yaml
from pathlib import Path
from typing import Dict

from langchain_mcp_adapters.client import MultiServerMCPClient

def load_mcp_config(file_path: str) -> Dict:
    """从 YAML 加载 MCP 配置"""
    path = Path(file_path)
    with open(path, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    return config.get("mcp_servers", {})

async def get_mcp_tools(yaml_path: str) -> list:
    """
    加载 YAML 中的多 MCP 配置，并返回 LangChain 兼容的工具列表
    """
    mcp_dict = load_mcp_config(yaml_path)
    client = MultiServerMCPClient(mcp_dict)
    tools = await client.get_tools()
    return tools
