# uvicorn app:app --host 0.0.0.0 --port 8000 --reload
import json
import os 
from typing import Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from agent.executor import AgentExecutorWrapper
from langchain_core.messages import HumanMessage, SystemMessage, BaseMessage, AIMessage, AIMessageChunk
from models.llm import CustomLLMFactory
# from graph.graph_pipeline import LangGraphPipeline
from graph.reactive_pipeline import InfoDoubleCheckPipeline
from graph.gen_doc_pipeline import GenDocPipeline
from tools.load_tools import load_tools
import logging
import uuid
import time

from asr.voice_asr import get_recognizer, VoiceRecognizer
from asr.text_corrector import get_corrector, TextCorrector

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
logger = logging.getLogger(__name__)
app = FastAPI()
# 添加 CORS 支持（解决跨域问题）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# 挂载报告文件目录为静态文件服务
REPORT_DIR = "/root/agentic-app/reports"
os.makedirs(REPORT_DIR, exist_ok=True)
# 将 reports 目录挂载为静态目录，前端可直接访问 /reports/文件名.docx 下载
app.mount("/reports", StaticFiles(directory=REPORT_DIR), name="reports")
from fastapi.staticfiles import StaticFiles

# 挂载静态文件目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


# ==================== 初始化语音和纠错模块 ====================
try:
    # 初始化语音识别器（使用Whisper后端）
    voice_recognizer = get_recognizer(
        backend="whisper",      # 可选: "whisper", "custom"
        model_name="medium",      # 可选: tiny, base, small, medium, large
        use_stream=True         # 启用流式处理
    )
    logger.info("语音识别器初始化成功")
except Exception as e:
    logger.error(f"语音识别器初始化失败: {e}")
    voice_recognizer = None

try:
    # 初始化文本纠错器
    text_corrector = get_corrector(use_advanced=False)  # 使用高级纠错
    logger.info("文本纠错器初始化成功")
except Exception as e:
    logger.error(f"文本纠错器初始化失败: {e}")
    text_corrector = None

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
async def safe_send_message(websocket: WebSocket, message: dict):
    """安全地发送WebSocket消息，处理连接断开的情况"""
    try:
        await websocket.send_text(json.dumps(message, ensure_ascii=False))
        return True
    except (WebSocketDisconnect, RuntimeError) as e:
        logging.info(f"WebSocket send failed: {str(e)}")
        return False
    except Exception as e:
        logging.error(f"Unexpected error in safe_send_message: {str(e)}")
        return False
'''
语音识别WebSocket接口
    支持实时语音转文字 + 文本纠错
user_id - 用户id，必填
session_id - 会话id，可以为空，为空就新建session
'''
@app.websocket("/asr/{user_id}/{session_id}")
async def agent_ws(websocket: WebSocket, user_id: str, session_id: Optional[str] = None):
    await websocket.accept()
    
    # 直接使用全局已初始化的识别器（删除重复初始化！）
    if not voice_recognizer or not voice_recognizer.is_available:
        logger.error("语音识别器不可用")
        await safe_send_message(websocket, {
            "event": "error",
            "error": "语音识别服务不可用，请检查Whisper安装"
        })
        await websocket.close()
        return
    # 生成session_id
    if not session_id:
        session_id = str(uuid.uuid4())
    
    thread_id = f'{user_id}-{session_id}'
    logger.info(f"ASR会话已建立: {thread_id}")
    
    # 累积的完整文本
    accumulated_text = ""
    
    # 发送会话开始消息
    await safe_send_message(websocket, {
        "event": "session_started",
        "session_id": session_id,
        "message": "实时语音识别已启动，请开始说话",
        "backend": voice_recognizer.backend
    })
    
    try:
        while True:
            try:
                # 接收音频数据
                data = await websocket.receive_text()
                payload = json.loads(data)
                
                voice_base64 = payload.get("voice_base64", "")
                action = payload.get("action", "")
                
                # 控制命令
                if action == "reset":
                    voice_recognizer.remove_session(session_id)
                    accumulated_text = ""
                    await safe_send_message(websocket, {
                        "event": "session_reset",
                        "message": "会话已重置"
                    })
                    continue
                
                if action == "end":
                   # ✅ 强制保存到绝对路径
                    RECORD_DIR = "/root/agentic-app"
                    os.makedirs(RECORD_DIR, exist_ok=True)
                    # 调用保存（会从 full_webm 转完整音频）
                    saved_path = voice_recognizer.save_recording(session_id, save_dir=RECORD_DIR)
                    logger.info(f"✅ 最终保存路径: {saved_path}")
                    await safe_send_message(websocket, {
                        "event": "session_ended",
                        "final_text": accumulated_text,
                        "message": "会话已结束"
                    })
                    break
                
                # 实时语音识别
                if voice_base64:
                    # 异步识别
                    recognized_text = await voice_recognizer.transcribe_stream_async(
                        session_id, 
                        voice_base64
                    )
                    
                    if recognized_text:
                        logger.info(f"识别到: {recognized_text}")
                        # ===== 在这里添加纠错 =====
                        if text_corrector:
                            corrected_text, _ = text_corrector.correct(recognized_text)
                        else:
                            corrected_text = recognized_text
                        # ==========================
                        # 发送临时结果
                        await safe_send_message(websocket, {
                            "event": "asr_interim",
                            "text": recognized_text,
                            "corrected": corrected_text,        # 纠错后
                            "timestamp": time.time()
                        })
                        
                        # 判断句子是否结束
                        if voice_recognizer.is_sentence_end(recognized_text):
                            accumulated_text += (accumulated_text and " " or "") + recognized_text
                            await safe_send_message(websocket, {
                                "event": "asr_final",
                                "text": recognized_text,
                                "accumulated": accumulated_text
                            })
                
            except json.JSONDecodeError:
                await safe_send_message(websocket, {
                    "event": "error",
                    "error": "无效的JSON格式"
                })
            except Exception as e:
                logger.error(f"处理消息出错: {e}")
                await safe_send_message(websocket, {
                    "event": "error",
                    "error": str(e)
                })
                
    except WebSocketDisconnect:
        logger.info(f"ASR会话断开: {thread_id}")
    except Exception as e:
        logger.error(f"WebSocket异常: {e}")
    finally:
        voice_recognizer.remove_session(session_id)
        logger.info(f"ASR会话已清理: {thread_id}")

            

        



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
            payload = json.loads(data)
             # ======================== 【多模态核心：统一入口解析】 ========================
            query = payload.get("query", "")
          
            # 最终交给你Agent的文本（融合所有模态）
            query = query.strip()
            # ============================================================================

             # 校验用户输入：空查询直接返回错误
            if not query:
                await websocket.send_text(json.dumps({"error": "empty query"}))
                continue
            # 核心：流式运行LangGraph流水线，返回Agent执行过程
            # 假设 agent 是通过 create_agent 创建的，并且支持 astream
            
            async for chunk in rag_pipeline.astream_run(query, user_id, session_id):
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
 
            # 假设 agent 是通过 create_agent 创建的，并且支持 astream
            async for chunk in doc_gen_pipeline.astream_run(query,style, user_id, session_id):
                text = _safe_serialize(chunk)
              
                # 把 AIMessageChunk 信息过滤掉
                if(text['event'] != 'token'):
                    # 如果是完成事件，添加下载URL
                    if (text.get('event') == 'custom' and 
                        text.get('data', {}).get('type') == 'final_file'):
                        # 添加静态文件访问URL
                        file_name = text['data'].get('file_name')
                       
                    
                    if not await safe_send_message(websocket, text):
                        return
                
            await websocket.send_text(json.dumps({"status": "done"}))
            
            logging.info(f"answer done -- {user_id}-{session_id}")

        except Exception as e:
            await websocket.send_text(json.dumps({"error": str(e)}))
