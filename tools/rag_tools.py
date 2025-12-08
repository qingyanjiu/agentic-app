from tools.dify_datasets_controller import DifyKnowledgeBaseController
from langchain_core.tools import tool
from pydantic import BaseModel, Field
import json
import logging

logging.basicConfig(
    filename='app.log',
    # 追加模式 'a'，覆盖模式 'w' 
    filemode='w',
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
)

# -------------------------------
# RAG工具
# -------------------------------

kb_controller = DifyKnowledgeBaseController(
    base_url="http://host.docker.internal",
    dataset_id="7f0d26c3-dbe8-44a3-9ee0-541430244052"
)

class QueryKBParams(BaseModel):
    query: str
@tool(args_schema=QueryKBParams, description="根据问题在知识库中进行语义检索，返回候选片段列表。参数是一个由数字、字母、-组成的字符串，不包含任何其他字符。")
def query_knowledge_base(query: str) -> str:
    """根据问题在知识库中进行语义检索，返回候选片段列表"""
    try:
        results = kb_controller.search(query)
        # 包含文本过短的，过滤掉
        results = [r for r in results if len(r['segment']['content']) > 10]
        logging.info(f"query_knowledge_base检索知识库结果：{results}")
    except Exception as e:
        logging.error(f"Error querying knowledge base: {e}")
        return "查询知识库时出错，请稍后再试"
    return json.dumps(results, ensure_ascii=False, indent=2)

@tool(description="获取知识库列表")
def list_datasets() -> str:
    """获取知识库列表"""
    try:
        results = kb_controller.list_datasets()
    except Exception as e:
        logging.error(f"Error listing datasets: {e}")
        return "获取知识库列表时出错，请稍后再试"
    return json.dumps(results, ensure_ascii=False, indent=2)

class ReadFilesegmentsParams(BaseModel):
    doc_id: str
    segment_start: int
    segment_end: int
@tool(args_schema=ReadFilesegmentsParams, description='''
    读取指定文档的所有分段内容，作用是从文档中检索复数个分段的内容。
    doc_id为文档ID；
    segment_start为要检索的文档的开始分段编号，
    segment_end为要检索的文档的结束分段编号
    ''')
def get_document_segments(doc_id: str, segment_start: int, segment_end: int) -> str:
    """读取指定文档的所有分段内容"""
    if not doc_id:
        return "请提供文档ID"
    results = {"messages": [{"role": "system", "content": ""}]}
    try:
        for segment_no in range(segment_start, segment_end + 1):
            segment_data = kb_controller.get_document_segments(doc_id, page=segment_no)
            segment_content = segment_data[0]['content']
            results["messages"][0]['content'] += f'{segment_content}\n'
        logging.info(f'get_document_segments：{results["messages"][0]["content"][:100]}...')
    except Exception as e:
        logging.error(f"Error reading file segments: {e}")
        return "读取文档分段内容时出错，请稍后再试"
    return json.dumps(results, ensure_ascii=False, indent=2)


class ListFilesParams(BaseModel):
    page: int = 1
    page_size: int = 10

@tool(args_schema=ListFilesParams, description="列出当前知识库中的文档，返回文档ID、文档名和segment数")
def list_documents(page: int = 1, page_size: int = 10) -> str:
    """列出当前知识库中的文档，返回文档ID、文档名和segment数"""
    try:
        results = kb_controller.list_documents(page, page_size)
    except Exception as e:
        logging.error(f"Error listing files: {e}")
        return "列出文档时出错，请稍后再试"
    return json.dumps(results, ensure_ascii=False, indent=2)


TOOLS = [query_knowledge_base, list_datasets, get_document_segments, list_documents]
TOOL_NAMES = ['query_knowledge_base', 'list_datasets', 'get_document_segments', 'list_documents']