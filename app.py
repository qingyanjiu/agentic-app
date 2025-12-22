# uvicorn app:app --host 0.0.0.0 --port 8000 --reload

import json
from typing import Optional
from fastapi import FastAPI, WebSocket
from agent.executor import AgentExecutorWrapper
from langchain_core.messages import HumanMessage, SystemMessage, BaseMessage, AIMessage, AIMessageChunk
from models.llm import CustomLLMFactory
from tools.rag_tools import TOOLS as RAG_TOOLS
from tools.system_tools import genTools
# from graph.graph_pipeline import LangGraphPipeline
from graph.reactive_pipeline import InfoDoubleCheckPipeline
from dynamic_tools.file_dynamic_tool import FileDynamicTool
import logging
import uuid

# docker开发环境
# docker run -d -v /Users/louisliu/dev/AI_projects/agentic-app:/root/agentic-app --name langchain-agent-dev qingyanjiu/langchain:1.0.3 tail -f /dev/null

logging.basicConfig(
    filename='app.log',
    # 追加模式 'a'，覆盖模式 'w' 
    filemode='w',
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
)

app = FastAPI()

# 全局模型和工具
llm_factory = CustomLLMFactory()
llm = llm_factory.llms['silicon']


def _safe_serialize(obj):
    """递归将 BaseMessage 转为 dict"""
    if isinstance(obj, BaseMessage):
        return obj.model_dump()
    elif isinstance(obj, list):
        return [_safe_serialize(i) for i in obj]
    elif isinstance(obj, dict):
        return {k: _safe_serialize(v) for k, v in obj.items()}
    else:
        return obj
    


'''
@@@@@@@@@@@@@@@@@@@@@@@@
动态获取工具 dynamic_tools
@@@@@@@@@@@@@@@@@@@@@@@@
'''
fileDynamicTool = FileDynamicTool(call_tool_token='dataset-3dwC5VAiVum9GooOuN3ZlKpE')
tools = fileDynamicTool.generate_tools()
# @@@ 测试工具
tools = genTools()

'''
发起聊天对话
user_id - 用户id，必填
session_id - 会话id，可以为空，为空就新建session
'''
@app.websocket("/agentic_rag_query/{user_id}/{session_id}")
async def agent_ws(websocket: WebSocket, user_id: str, session_id: Optional[str] = None):
    await websocket.accept()
    
    # 新对话，生成新的sessionid
    if (not session_id):
        session_id = uuid.uuid4()
        
    thread_id = f'{user_id}-{session_id}'
    
    '''
    @@@@@ 创建langgraph pipeline
    '''
    rag_pipeline = InfoDoubleCheckPipeline(
        llm=llm,
        tools=tools,
        user_id=user_id,
        session_id=session_id,
        use_evaluator=False
    )

    while True:
        try:
            data = await websocket.receive_text()
            query = json.loads(data).get("query")
            if not query:
                await websocket.send_text(json.dumps({"error": "empty query"}))
                continue

            # 假设 agent 是通过 create_agent 创建的，并且支持 astream
            async for chunk in rag_pipeline.astream_run(query, thread_id):
                text = _safe_serialize(chunk)
                ##################################
                # 如果直接用agentWrapper，就用这个逻辑
                ##################################
                # 如果是最后结束的消息，直接拿message
                # if(text['event'] == 'on_chain_end'
                #     and 'output' in text['data'] 
                #     and text['name'] == 'executor_agent'):
                #     # 取最后 messagetext['name'] == 'executor_agent'):
                #     output_json = {
                #         "event": "final_answer", 
                #         "data": text['data']['output']['messages'][-1]['content']
                #     }
                # await websocket.send_text(json.dumps(text, ensure_ascii=False))
            
                ##################################
                # 如果是用langgraph，就用这个逻辑
                ##################################
                # 把 AIMessageChunk 信息过滤掉
                if(text['event'] != 'token'):
                    await websocket.send_text(json.dumps(text, ensure_ascii=False))
                
            await websocket.send_text(json.dumps({"status": "done"}))
            
            logging.info(f"answer done -- {user_id}-{session_id}")

        except Exception as e:
            await websocket.send_text(json.dumps({"error": str(e)}))
