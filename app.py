# uvicorn app:app --host 0.0.0.0 --port 8000 --reload

import json
from typing import Optional
from fastapi import FastAPI, WebSocket
from agent.executor import AgentExecutorWrapper
from langchain_core.messages import HumanMessage, SystemMessage, BaseMessage, AIMessage, AIMessageChunk
from models.llm import CustomLLMFactory
# from graph.graph_pipeline import LangGraphPipeline
from graph.reactive_pipeline import InfoDoubleCheckPipeline
from graph.gen_doc_pipeline import GenDocPipeline
from tools.load_tools import load_tools
import logging
import uuid

# docker开发环境
# docker run -d -v /Users/louisliu/dev/AI_projects/agentic-app:/root/agentic-app --name langchain-agent-dev qingyanjiu/langchain:1.0.3 tail -f /dev/null

#日志
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
# llm = llm_factory.llms['zp']


def _safe_serialize(obj):
    """递归将 BaseMessage 转为 dict（解决WebSocket传输序列化问题）"""
    if isinstance(obj, BaseMessage):
        return obj.model_dump()
    elif isinstance(obj, list):
        return [_safe_serialize(i) for i in obj]
    elif isinstance(obj, dict):
        return {k: _safe_serialize(v) for k, v in obj.items()}
    else:
        return obj

'''
对话智能体
user_id - 用户id，必填
session_id - 会话id，可以为空，为空就新建session
'''
@app.websocket("/chat/{user_id}/{session_id}")
async def agent_ws(websocket: WebSocket, user_id: str, session_id: Optional[str] = None):
    await websocket.accept()# 接受客户端WebSocket连接
    
    # 新对话，生成新的sessionid
    if (not session_id):
        session_id = uuid.uuid4()
        
    thread_id = f'{user_id}-{session_id}'# 会话唯一标识（用户+会话，隔离不同对话上下文）
    
    tools = await load_tools()# 异步加载Agent工具集（如搜索、计算器、数据库等）
    
    '''
    @@@@@ # 创建LangGraph核心流水线（信息核验Agent）
    '''
    try:
        rag_pipeline = await InfoDoubleCheckPipeline.create(
            llm=llm,
            tools=tools,
            user_id=user_id,
            session_id=session_id,
            use_evaluator=False # 是否启用结果评估器（可选
        )
        logging.info("GenDocPipeline创建成功")
    except Exception as e:
        error_msg = f"Pipeline创建失败：{str(e)}"
        logging.error(error_msg)
        await websocket.send_text(json.dumps({
            "type": "error",
            "message": error_msg,
            "code": "PIPELINE_CREATE_FAILED"
        }))
        await websocket.close()
        return
 # 持续监听客户端消息（WebSocket长连接循环）
    while True:
        try:
            # 接收客户端发送的JSON数据（格式：{"query": "用户问题"}）
            data = await websocket.receive_text()
            query = json.loads(data).get("query")
             # 校验用户输入：空查询直接返回错误
            if not query:
                await websocket.send_text(json.dumps({"error": "empty query"}))
                continue
            # 核心：流式运行LangGraph流水线，返回Agent执行过程
            # 假设 agent 是通过 create_agent 创建的，并且支持 astream
            
            async for chunk in rag_pipeline.astream_run(query, thread_id):
                 # 序列化chunk（解决LangChain对象无法JSON化问题）
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
                # 过滤掉token级流式输出（只返回阶段型结果，减少传输量）
                if(text['event'] != 'token'):
                     # 向客户端发送JSON数据（ensure_ascii=False支持中文）
                    await websocket.send_text(json.dumps(text, ensure_ascii=False))
             # 流水线执行完成：发送结束标识    
            await websocket.send_text(json.dumps({"status": "done"}))
             # 记录日志：会话完成
            logging.info(f"answer done -- {user_id}-{session_id}")
        # 异常处理：捕获所有错误，返回给客户端
        except Exception as e:
            await websocket.send_text(json.dumps({"error": str(e)}))



'''
统计文档生成智能体
user_id - 用户id，必填
session_id - 会话id，可以为空，为空就新建session
'''
@app.websocket("/gen_doc/{user_id}/{session_id}")
async def agent_ws(websocket: WebSocket, user_id: str, session_id: Optional[str] = None):
    # 1. 接受WebSocket连接
    await websocket.accept()
    
    # 新对话，生成新的sessionid
    if (not session_id):
        session_id = uuid.uuid4()
        
    thread_id = f'{user_id}-{session_id}'
    
    # 工具加载添加异常处理
    try:
        tools = await load_tools()
        logging.info(f"成功加载{len(tools)}个工具")
    except Exception as e:
        error_msg = f"工具加载失败：{str(e)}"
        logging.error(error_msg)
        await websocket.send_text(json.dumps({
            "type": "error",
            "message": error_msg,
            "code": "TOOL_LOAD_FAILED"
        }))
        await websocket.close()
        return

  
    '''
    @@@@@ 创建langgraph pipeline
    '''
    try:
        doc_gen_pipeline = await GenDocPipeline.create(
            llm=llm,
            tools=tools,
            user_id=user_id,
            session_id=session_id,
        )
        logging.info("GenDocPipeline创建成功")
    except Exception as e:
        error_msg = f"Pipeline创建失败：{str(e)}"
        logging.error(error_msg)
        await websocket.send_text(json.dumps({
            "type": "error",
            "message": error_msg,
            "code": "PIPELINE_CREATE_FAILED"
        }))
        await websocket.close()
        return

    while True:
        try:
            # 接收前端传入的JSON数据
            data = await websocket.receive_text()
            query = json.loads(data).get("query")
            style= json.loads(data).get("style")
            if not query:
                await websocket.send_text(json.dumps({"error": "empty query"}))
                continue
            
# ========== 核心：调用pipeline的流式接口 ==========
            # try:
            #     # 调用astream_run，传入query和thread_id
            #     async for result in doc_gen_pipeline.astream_run(
            #         query=query,
            #         thread_id=thread_id,
                   
            #     ):
            #         # 流式返回结果给前端（确保JSON序列化）
            #         await websocket.send_text(json.dumps({
            #             "type": "stream",
            #             "data": result,
            #             "thread_id": thread_id
            #         }, ensure_ascii=False))  # 关键：处理中文
                
            #     # 流式调用完成后，发送结束标识
            #     await websocket.send_text(json.dumps({
            #         "type": "complete",
            #         "message": "文档生成完成",
            #         "thread_id": thread_id
            #     }, ensure_ascii=False))
                
            # except Exception as e:
            #     error_msg = f"文档生成失败：{str(e)}"
            #     logging.error(error_msg, exc_info=True)
            #     await websocket.send_text(json.dumps({
            #         "type": "error",
            #         "message": error_msg,
            #         "code": "GEN_DOC_FAILED",
            #         "thread_id": thread_id
            #     }, ensure_ascii=False))
            # 假设 agent 是通过 create_agent 创建的，并且支持 astream
            async for chunk in doc_gen_pipeline.astream_run(query,style, thread_id):
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
