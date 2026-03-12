from tools.rag_tools import TOOLS as RAG_TOOLS
from tools.system_tools import genSystemTools
from dynamic_tools.file_dynamic_tool import FileDynamicTool
from mcp_client.mcp_loader import get_mcp_tools

'''
@@@@@@@@@@@@@@@@@@@@@@@@
获取工具
# 不同的工具来源对应不同的功能域
- RAG工具 → 知识检索能力
- 系统工具 → 基础操作能力  
- 动态工具 → 文件处理能力
- MCP工具 → 外部服务集成能力
@@@@@@@@@@@@@@@@@@@@@@@@
'''
async def load_tools():
    # # 动态工具
    # fileDynamicTool = FileDynamicTool(call_tool_token='dataset-3dwC5VAiVum9GooOuN3ZlKpE')
    # tools = fileDynamicTool.generate_tools()
    
    # # 基础工具
    # tools = genSystemTools()

    # mcp工具
    tools = await get_mcp_tools('mcp_client/mcp_server_config.yaml')
    return tools