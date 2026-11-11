import asyncio
import logging

import yaml
from pathlib import Path
from typing import Dict

from .mcp_config import apply_base_url

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
    """从 YAML 加载 MCP 配置。

    支持顶层 `mcp_base_url`：各 server 的 url 只写路径（如 /personStatus）时
    用 base 补全；url 已是完整 http(s) 地址则原样返回，老配置不受影响。
    """
    # 把字符串路径转为 Path 对象（跨平台兼容）
    path = Path(file_path)
    # 打开文件（指定utf-8编码，避免中文乱码）
    with open(path, encoding="utf-8") as f:
        # 安全加载YAML内容（yaml.safe_load避免执行恶意代码）
        config = yaml.safe_load(f) or {}
    # 只处理 "mcp_servers" 段（核心配置），并补全相对 url
    servers = config.get("mcp_servers", {}) or {}
    return apply_base_url(servers, config.get("mcp_base_url"))
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
    # direct_client: true 的条目由专属直连客户端接管（如知识库 KnowledgeMcpClient），
    # 不走 langchain_mcp_adapters，也避免其工具混进业务图的工具列表
    mcp_dict = {
        name: conf for name, conf in mcp_dict.items()
        if not (isinstance(conf, dict) and conf.get("direct_client"))
    }

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


async def get_mcp_tools_for_server(
    yaml_path: str,
    server_name: str,
    timeout: float = 15.0,
) -> list:
    """只连接 YAML 里指定的一个 server 并返回它的工具列表。

    单个业务域（如设备查询）只需要自己的端点时用它：
    - 不会被其它 server 的连接/超时拖垮（get_mcp_tools 是全部加载后
      统一 wait_for，任何一个慢端点都会让整个结果超时、本域工具为空）；
    - 失败时能打出具体是哪个 URL，便于定位 sidecar 地址/网络问题。

    :param yaml_path: MCP 配置文件
    :param server_name: `mcp_servers` 下的 key
    :param timeout: 连接 + tools/list 的总超时
    :return: 工具列表；配置缺失/直连条目/失败/超时都返回 []
    """
    mcp_dict = load_mcp_config(yaml_path)
    conf = mcp_dict.get(server_name)
    if not isinstance(conf, dict):
        logger.error(f"MCP 配置中不存在 server [{server_name}]：{yaml_path}")
        return []
    if conf.get("direct_client"):
        logger.warning(f"MCP server [{server_name}] 由专属直连客户端接管，跳过")
        return []

    try:
        tools = await asyncio.wait_for(
            _load_server_tools(server_name, conf),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.error(
            f"MCP server [{server_name}] 工具加载超时（>{timeout}s）："
            f"{conf.get('url')}，请检查 sidecar 是否启动、地址是否可达"
        )
        return []
    except Exception as e:
        logger.error(
            f"MCP server [{server_name}] 工具加载失败：{conf.get('url')}: {e}",
            exc_info=True,
        )
        return []

    logger.info(f"MCP server [{server_name}] 加载到 {len(tools)} 个工具")
    return tools
