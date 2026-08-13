# uvicorn app:app --host 0.0.0.0 --port 8000 --reload
import json
import os 
from typing import Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from agent.executor import AgentExecutorWrapper
from langchain_core.messages import HumanMessage, SystemMessage, BaseMessage, AIMessage, AIMessageChunk
from models.llm import CustomLLMFactory
# from graph.graph_pipeline import LangGraphPipeline
from graph.reactive_pipeline import InfoDoubleCheckPipeline
from graph.gen_doc_pipeline import GenDocPipeline
from graph.asr_pipeline import TextCorrectorPipeline
from tools.load_tools import load_tools
# from skills.mcp_agent import McpSkillAgent
import logging
import uuid
import time
import asyncio

# 新增：人员态势/安防态势意图识别相关导入
# classify_security_sub_type：安防态势子类型（event_type）判定，供安防 handler 追问/兜底使用
from agent.intent import classify_intent, classify_security_sub_type, PersonStatusHandler, SecurityStatusHandler, CanteenStatusHandler
from graph.person_status_langgraph import build_person_status_graph, load_person_status_tools
from graph.security_status_langgraph import build_security_status_graph, load_security_tools
from graph.canteen_status_langgraph import build_canteen_status_graph, load_canteen_tools
from memory.session_state import session_state, IntentState
# from asr.voice_asr import get_recognizer, VoiceRecognizer
# from asr.text_corrector import get_corrector, TextCorrector

# docker开发环境
# docker run -d -v /Users/louisliu/dev/AI_projects/agentic-app:/root/agentic-app --name langchain-agent-dev qingyanjiu/langchain:1.0.3 tail -f /dev/null

#日志
# logging.basicConfig(
#     filename='app.log',
#     # 追加模式 'a'，覆盖模式 'w' 
#     filemode='w',
#     level=logging.DEBUG,
#     format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
# )
logging.basicConfig(
    filename='app.log',
    filemode='a',  # 改成 'a'
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
REPORT_DIR = "./reports"
os.makedirs(REPORT_DIR, exist_ok=True)
# 将 reports 目录挂载为静态目录，前端可直接访问 /reports/文件名.docx 下载
app.mount("/reports", StaticFiles(directory=REPORT_DIR), name="reports")
from fastapi.staticfiles import StaticFiles

# 挂载静态文件目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

# 图片上传目录
UPLOAD_FOLDER = "/root/agentic-app/uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# 挂载图片目录
app.mount("/uploads", StaticFiles(directory=UPLOAD_FOLDER), name="uploads")
# 前端访问图片的地址（你的服务地址）
BASE_URL = "http://127.0.0.1:8001"
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
@app.on_event("startup")
async def startup():
    logger.info("[startup] 开始预加载意图识别模型...")
    from agent.intent import get_classifier
    await get_classifier()
    logger.info("[startup] 意图识别模型预加载完成")

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

# ============================================================
# 人员态势 LangGraph 执行助手
# build_person_status_graph 需要传入 MCP tools，加载会 fork 子进程，故模块级缓存
# ============================================================
_person_status_graph = None

async def get_person_status_graph():
    """懒加载：首次调用时从 MCP 加载人员态势工具并编译图，之后复用"""
    global _person_status_graph
    if _person_status_graph is None:
        tools = await load_person_status_tools()
        # 传入 llm，让 call_tool 节点用 LLM 根据 query_type 分析 MCP 返回生成回答
        _person_status_graph = build_person_status_graph(tools, llm=llm)
    return _person_status_graph


async def run_person_status_graph(websocket, state, user_id, session_id):
    """
    用 LangGraph 图执行人员态势流程：
      1. 把 IntentState(dataclass) 转成图需要的 PersonStatusGraphState(TypedDict)
      2. ainvoke 跑图（call_tool 是异步节点，必须用 ainvoke）
      3. 把图更新后的 slots/ask_count/last_question 回写到会话状态
      4. 发送 events；有 ask 事件则保留状态等下一轮，否则清空
    """
    graph = await get_person_status_graph()

    # 组装喂给图的输入状态
    graph_input = {
        "slots": state.slots,
        "missing_params": state.missing_params,
        "ask_count": state.ask_count,
        "unrelated_count": state.unrelated_count,
        "last_question": state.last_question,
        "original_query": state.original_query,
        "answer": None,
        "error": None,
        "done": state.done,
        "events": [],
    }
    print(f"[GRAPH INPUT] user={user_id}, session={session_id}")
    print(json.dumps(graph_input, ensure_ascii=False, default=str))

    final_state = await graph.ainvoke(graph_input)

    print(f"[GRAPH OUTPUT] user={user_id}, session={session_id}")
    print(json.dumps(final_state, ensure_ascii=False, default=str))

    # 图内多轮追问会更新这些字段，回写供下一轮 handle_reply 使用
    state.slots = final_state["slots"]
    state.missing_params = final_state["missing_params"]
    state.ask_count = final_state["ask_count"]
    state.last_question = final_state["last_question"]

    # 发送事件；存在 ask 事件说明进入追问，保留状态等待用户补充
    keep_state = False
    for chunk in final_state["events"]:
        text = _safe_serialize(chunk)
        await websocket.send_text(json.dumps(text, ensure_ascii=False))
        if chunk.get("event") == "custom" and chunk.get("data", {}).get("type") == "ask":
            keep_state = True

    if keep_state:
        session_state.set(user_id, session_id, state)
    else:
        session_state.clear(user_id, session_id)


# ============================================================
# 安防态势 LangGraph 执行助手
# build_security_status_graph 需要传入 MCP tools，加载会 fork 子进程，故模块级缓存
# ============================================================
_security_status_graph = None

async def get_security_status_graph():
    """懒加载：首次调用时从 MCP 加载安防工具并编译图，之后复用"""
    global _security_status_graph
    if _security_status_graph is None:
        tools = await load_security_tools()
        # 传入 llm，让 call_tool 节点用 LLM 组织 MCP 返回生成回答
        _security_status_graph = build_security_status_graph(tools, llm=llm)
    return _security_status_graph


async def run_security_status_graph(websocket, state, user_id, session_id):
    """
    用 LangGraph 图执行安防态势流程（与人员态势对称）：
      1. 把 IntentState(dataclass) 转成图需要的 SecurityStatusGraphState(TypedDict)
      2. ainvoke 跑图（call_tool 是异步节点，必须用 ainvoke）
      3. 把图更新后的 slots/ask_count/last_question 回写到会话状态
      4. 发送 events；有 ask 事件则保留状态等下一轮，否则清空
    """
    graph = await get_security_status_graph()

    # 组装喂给图的输入状态
    graph_input = {
        "slots": state.slots,
        "missing_params": state.missing_params,
        "ask_count": state.ask_count,
        "unrelated_count": state.unrelated_count,
        "last_question": state.last_question,
        "original_query": state.original_query,
        "answer": None,
        "error": None,
        "done": state.done,
        "events": [],
    }
    print(f"[GRAPH INPUT] user={user_id}, session={session_id}")
    print(json.dumps(graph_input, ensure_ascii=False, default=str))

    final_state = await graph.ainvoke(graph_input)

    print(f"[GRAPH OUTPUT] user={user_id}, session={session_id}")
    print(json.dumps(final_state, ensure_ascii=False, default=str))

    # 图内多轮追问会更新这些字段，回写供下一轮 handle_reply 使用
    state.slots = final_state["slots"]
    state.missing_params = final_state["missing_params"]
    state.ask_count = final_state["ask_count"]
    state.last_question = final_state["last_question"]

    # 发送事件；存在 ask 事件说明进入追问，保留状态等待用户补充
    keep_state = False
    for chunk in final_state["events"]:
        text = _safe_serialize(chunk)
        await websocket.send_text(json.dumps(text, ensure_ascii=False))
        if chunk.get("event") == "custom" and chunk.get("data", {}).get("type") == "ask":
            keep_state = True

    if keep_state:
        session_state.set(user_id, session_id, state)
    else:
        session_state.clear(user_id, session_id)


# ============================================================
# 食堂管理 LangGraph 执行助手
# 与安防态势对称：懒加载图，用图执行食堂管理流程
# ============================================================
_canteen_status_graph = None

async def get_canteen_status_graph():
    """懒加载：首次调用时从 MCP 加载食堂工具并编译图，之后复用"""
    global _canteen_status_graph
    if _canteen_status_graph is None:
        tools = await load_canteen_tools()
        # 传入 llm，让 call_tool 节点用 LLM 组织 MCP 返回生成回答
        _canteen_status_graph = build_canteen_status_graph(tools, llm=llm)
    return _canteen_status_graph


async def run_canteen_status_graph(websocket, state, user_id, session_id):
    """
    用 LangGraph 图执行食堂管理流程（与人员/安防态势对称）：
      1. 把 IntentState(dataclass) 转成图需要的 CanteenStatusGraphState(TypedDict)
      2. ainvoke 跑图（call_tool 是异步节点，必须用 ainvoke）
      3. 把图更新后的 slots/ask_count/last_question 回写到会话状态
      4. 发送 events；有 ask 事件则保留状态等下一轮，否则清空
    """
    graph = await get_canteen_status_graph()

    # 组装喂给图的输入状态
    graph_input = {
        "slots": state.slots,
        "missing_params": state.missing_params,
        "ask_count": state.ask_count,
        "unrelated_count": state.unrelated_count,
        "last_question": state.last_question,
        "original_query": state.original_query,
        "answer": None,
        "error": None,
        "done": state.done,
        "events": [],
    }
    print(f"[GRAPH INPUT] user={user_id}, session={session_id}")
    print(json.dumps(graph_input, ensure_ascii=False, default=str))

    final_state = await graph.ainvoke(graph_input)

    print(f"[GRAPH OUTPUT] user={user_id}, session={session_id}")
    print(json.dumps(final_state, ensure_ascii=False, default=str))

    # 图内多轮追问会更新这些字段，回写供下一轮 handle_reply 使用
    state.slots = final_state["slots"]
    state.missing_params = final_state["missing_params"]
    state.ask_count = final_state["ask_count"]
    state.last_question = final_state["last_question"]

    # 发送事件；存在 ask 事件说明进入追问，保留状态等待用户补充
    keep_state = False
    for chunk in final_state["events"]:
        text = _safe_serialize(chunk)
        await websocket.send_text(json.dumps(text, ensure_ascii=False))
        if chunk.get("event") == "custom" and chunk.get("data", {}).get("type") == "ask":
            keep_state = True

    if keep_state:
        session_state.set(user_id, session_id, state)
    else:
        session_state.clear(user_id, session_id)

async def safe_send_message(websocket: WebSocket, message: dict):
    """安全地发送WebSocket消息，处理连接断开的情况"""
    try:
        # 最重要：先判断连接状态
        if not websocket.client_state.CONNECTED:
            return False
        
        await websocket.send_text(json.dumps(message, ensure_ascii=False))
        return True
    except (WebSocketDisconnect, RuntimeError, OSError):
        # 静默失败，不打冗余日志
        return False
    except Exception as e:
        logger.error(f"消息发送失败: {str(e)}")
        return False
'''
语音识别WebSocket接口
    支持实时语音转文字 + 文本纠错
user_id - 用户id，必填
session_id - 会话id，可以为空，为空就新建session
'''
@app.websocket("/asr/{user_id}/{session_id}")
async def asr_ws(websocket: WebSocket, user_id: str, session_id: Optional[str] = None):
    """
    WebSocket 语音识别接口
    
    客户端协议:
    1. 发送音频帧: {"voice_base64": "base64_audio_data","action": "end"}
    2. 重置会话: {"action": "reset"}
    """
    await websocket.accept()
    connected = True
    session_state = {"error_count": 0}
    # ========== 1. 初始化检查 ==========
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
    
    
    # 发送会话开始消息
    await safe_send_message(websocket, {
        "event": "session_started",
        "session_id": session_id,
        "user_id": user_id,
        "message": "实时语音识别已启动，请开始说话",
        "backend": voice_recognizer.backend
    })
    try:
        correctorPipeline = TextCorrectorPipeline(llm, user_id=user_id, session_id=session_id)
    except Exception as e:
        logger.error(f"初始化纠错器失败: {e}")
        await websocket.close()
        return
    
    try:
        while connected:
            try:

                # 接收音频数据
                # 设置接收超时（可选，避免长时间阻塞）
                data = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=300.0  # 5分钟超时
                )
                # 解析消息
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError as e:
                    logger.error(f"JSON解析失败: {e}, 原始数据: {repr(data)}")
                    await safe_send_message(websocket, {
                        "event": "error",
                        "error": f"无效的JSON格式: {str(e)}"
                        
                    })
                    continue
                
                voice_base64 = payload.get("voice_base64", "")
                action = payload.get("action", "")
                
                # 控制命令
                if action == "reset":
                    voice_recognizer.remove_session(session_id)
                    await safe_send_message(websocket, {
                        "event": "session_reset",
                        "session_id": session_id,
                        "message": "会话已重置"
                    })
                    continue
                
                if action == "end" and voice_base64:
                    try:
                   
                #    # ✅ 强制保存到绝对路径
                #     RECORD_DIR = "/root/agentic-app"
                #     os.makedirs(RECORD_DIR, exist_ok=True)
                #     # 调用保存（会从 full_webm 转完整音频）
                #     saved_path = voice_recognizer.save_recording(session_id, save_dir=RECORD_DIR)
                #     logger.info(f"✅ 最终保存路径: {saved_path}")
                
            
                        # 异步识别
                        recognized_text = await voice_recognizer.transcribe_stream_async(
                            session_id, 
                            voice_base64
                        )
                        
                        if recognized_text:
                            logger.info(f"识别到: {recognized_text}")
                            # ===== 使用大模型进行文本纠错 =====
                            async def stream_writer(data):
                                if connected and websocket.client_state.CONNECTED:
                                    await safe_send_message(websocket, data)

                            corrected_text = await correctorPipeline.correct(recognized_text,stream_writer)
                            # # ===== 在这里添加纠错 =====
                            # if text_corrector:
                            #     corrected_text, _ = text_corrector.correct(recognized_text)
                            # else:
                            #     corrected_text = recognized_text
                            # ==========================
                            # 发送临时结果
                            await safe_send_message(websocket, {
                                "event": "asr_final",
                                "text": recognized_text,      # 纠错后
                                "timestamp": time.time()
                            })
                            connected = False
                    except Exception as e:
                        logger.error(f"识别/纠错失败: {e}")
                        continue
                
            # -------------------- 捕获断开 --------------------
            except WebSocketDisconnect:
                logger.info(f"客户端主动断开: {thread_id}")
                connected = False
                break

            # -------------------- 捕获超时 --------------------
            except asyncio.TimeoutError:
                logger.info(f"WebSocket超时: {thread_id}")
                connected = False
                break

            # -------------------- 其他错误 --------------------
            except Exception as e:
                logger.error(f"处理消息出错: {e}")
                connected = False
                break
                
    # 最外层捕获（兜底）
    except Exception as e:
        logger.error(f"WebSocket全局异常: {e}")
    finally:
        connected = False
        voice_recognizer.remove_session(session_id)
        logger.info(f"ASR会话已清理: {thread_id}")
        return

            

        

# 上传图片接口
@app.post("/upload/image")
async def upload_image(file: UploadFile = File(...)):
    # 生成唯一文件名，防止重名
    ext = file.filename.split(".")[-1]
    filename = f"{uuid.uuid4()}.{ext}"
    save_path = os.path.join(UPLOAD_FOLDER, filename)

    # 保存图片
    with open(save_path, "wb") as f:
        f.write(await file.read())

    # 返回可访问的URL
    image_url = f"{BASE_URL}/uploads/{filename}"
    return {
        "image_url": image_url,
        "filename": filename
    }

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
        logging.info("chatPipeline创建成功")
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
            # 从 payload 中获取用户输入
            query = payload.get("query", "")

            # ============================================================
            # 人员态势 / 安防态势 MVP 分支
            # 先判断是否有正在进行的追问状态
            # 再判断是否为新的人员/安防态势意图
            # 否则走原有 pipeline
            # ============================================================
            active_state = session_state.get(user_id, session_id)

            # 预计算顶层意图（人员态势 / 安防态势 / 食堂管理 / other），只算一次
            # 有进行中任务时不调用模型，避免追问轮重复编码
            if active_state and active_state.module in ("person_status", "security_status", "canteen_status"):
                top_intent = None
            else:
                top_intent = (await classify_intent(query))["intent"]

            # 情况 1：当前有进行中的追问状态（人员态势 / 安防态势 / 食堂管理）
            if active_state and active_state.module in ("person_status", "security_status", "canteen_status"):
                print("[DEBUG] 进入追问分支, module:", active_state.module)

                # 按模块选择对应 handler（人员态势 / 安防态势 / 食堂管理）
                if active_state.module == "person_status":
                    handler = PersonStatusHandler()
                elif active_state.module == "security_status":
                    handler = SecurityStatusHandler()
                else:
                    handler = CanteenStatusHandler()
                
                # 让 handler 判断用户回复是否相关，并更新状态
                result = await handler.handle_reply(active_state, query, llm)
                
                # 情况 1.1：连续不相关超过阈值，放弃任务
                if result["action"] == "give_up":
                    # 清空会话状态
                    session_state.clear(user_id, session_id)
                    
                    await websocket.send_text(json.dumps({
                        "event": "custom",
                        "data": {"type": "answer", "content": result["answer"]}
                    }, ensure_ascii=False))
                    continue  # 跳过原有 pipeline
                
                # 情况 1.2：用户回复不相关，但未超限，再次追问
                elif result["action"] == "re_ask":
                    # 保存更新后的状态
                    session_state.set(user_id, session_id, result["state"])
                    
                    await websocket.send_text(json.dumps({
                        "event": "custom",
                        "data": {
                            "type": "ask",
                            "question": result["answer"],
                            "missing_params": result["state"].missing_params,
                            "unrelated_count": result["state"].unrelated_count
                        }
                    }, ensure_ascii=False))
                    continue  # 跳过原有 pipeline
                
                # 情况 1.3：用户回复相关，继续执行 pipeline
                elif result["action"] == "continue":
                    # 保存更新后的状态
                    session_state.set(user_id, session_id, result["state"])
                    
                    if active_state.module == "person_status":
                        # 用 LangGraph 图执行人员态势流程（内部会回写/清理会话状态）
                        await run_person_status_graph(websocket, result["state"], user_id, session_id)
                    elif active_state.module == "security_status":
                        # 用 LangGraph 图执行安防态势流程（内部会回写/清理会话状态）
                        await run_security_status_graph(websocket, result["state"], user_id, session_id)
                    else:
                        # 用 LangGraph 图执行食堂管理流程（内部会回写/清理会话状态）
                        await run_canteen_status_graph(websocket, result["state"], user_id, session_id)

                    continue  # 跳过原有 pipeline

            # 情况 2：没有进行中状态，但新意图属于人员态势
            elif top_intent == "person_status":
                print("[DEBUG] 进入新意图分支, query:", query)
                
                handler = PersonStatusHandler()
                
                # 从用户输入中抽取初始 slots
                slots = await handler.extract_slots(query)
                
                # 判断初始 slots 是否完整
                missing = handler._get_missing_params(slots)
                
                # 创建新的会话状态
                state = IntentState(
                    module="person_status",
                    slots=slots,
                    missing_params=missing,
                    ask_count=0,
                    unrelated_count=0,
                    original_query=query,
                    done=False
                )
                session_state.set(user_id, session_id, state)
                
                # 用 LangGraph 图执行人员态势流程（内部会回写/清理会话状态）
                await run_person_status_graph(websocket, state, user_id, session_id)

                continue  # 跳过原有 pipeline

            # 情况 2.1：没有进行中状态，但新意图属于安防态势
            elif top_intent == "security_status":
                print("[DEBUG] 进入安防态势新意图分支, query:", query)

                handler = SecurityStatusHandler()

                # 从用户输入中抽取初始 slots
                slots = await handler.extract_slots(query)

                # 判断初始 slots 是否完整
                missing = handler._get_missing_params(slots)

                # 创建新的会话状态
                state = IntentState(
                    module="security_status",
                    slots=slots,
                    missing_params=missing,
                    ask_count=0,
                    unrelated_count=0,
                    original_query=query,
                    done=False
                )
                session_state.set(user_id, session_id, state)

                # 用 LangGraph 图执行安防态势流程（内部会回写/清理会话状态）
                await run_security_status_graph(websocket, state, user_id, session_id)

                continue  # 跳过原有 pipeline

            # 情况 2.2：没有进行中状态，但新意图属于食堂管理
            elif top_intent == "canteen_status":
                print("[DEBUG] 进入食堂管理新意图分支, query:", query)

                handler = CanteenStatusHandler()

                # 从用户输入中抽取初始 slots
                slots = await handler.extract_slots(query)

                # 判断初始 slots 是否完整
                missing = handler._get_missing_params(slots)

                # 创建新的会话状态
                state = IntentState(
                    module="canteen_status",
                    slots=slots,
                    missing_params=missing,
                    ask_count=0,
                    unrelated_count=0,
                    original_query=query,
                    done=False
                )
                session_state.set(user_id, session_id, state)

                # 用 LangGraph 图执行食堂管理流程（内部会回写/清理会话状态）
                await run_canteen_status_graph(websocket, state, user_id, session_id)

                continue  # 跳过原有 pipeline

             # ======================== 【多模态核心：统一入口解析】 ========================
            query = payload.get("query", "")
            image_url = payload.get("image_url", "")        # 新增：url图片

            # 构建多模态消息内容（兼容文本+图片）
            content = []
            # 1. 添加文本内容
            if query:
                content.append({"type": "text", "text": query})
            
            # 2. 添加图片（优先base64，其次url）
            if image_url:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": image_url}
                })
            # 如果最终没有任何内容，返回错误
            if not content:
                await websocket.send_text(json.dumps({"error": "empty query or image"}))
                continue
           
            
            # 核心：流式运行LangGraph流水线，返回Agent执行过程
            # 假设 agent 是通过 create_agent 创建的，并且支持 astream
            
            async for chunk in rag_pipeline.astream_run(content, user_id, session_id):
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
            error_msg = f"处理消息异常: {str(e)}"
            logger.error(error_msg, exc_info=True)  # 关键：打印完整堆栈
            try:
                await websocket.send_text(json.dumps({"error": error_msg}))
            except Exception:
                logger.warning("客户端已断开，无法发送错误消息")
            break
        # except Exception as e:
        #     await websocket.send_text(json.dumps({"error": str(e)}))



'''
统计文档生成智能体
user_id - 用户id，必填
session_id - 会话id，可以为空，为空就新建session
'''
@app.websocket("/gen_doc/{user_id}/{session_id}")
async def doc_ws(websocket: WebSocket, user_id: str, session_id: Optional[str] = None):
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


'''
MCP Skill Agent — 单Agent智能调用MCP工具
用户只需用自然语言描述想查什么，Agent自动判断用哪个MCP工具、提取参数、调用并返回结果
'''
# @app.websocket("/mcp_skill/{user_id}/{session_id}")
# async def mcp_skill_ws(websocket: WebSocket, user_id: str, session_id: Optional[str] = None):
#     await websocket.accept()

#     if not session_id:
#         session_id = str(uuid.uuid4())

#     # 初始化 Agent（连接MCP服务器、创建AgentExecutor）
#     try:
#         agent = McpSkillAgent(
#             llm=llm,
#             mcp_yaml_path="mcp_client/mcp_server_config.yaml",
#         )
#         await agent.initialize()
#         logging.info("McpSkillAgent 初始化成功，可用工具: %s", agent.available_tools)
#     except Exception as e:
#         error_msg = f"McpSkillAgent 初始化失败: {str(e)}"
#         logging.error(error_msg)
#         await websocket.send_text(json.dumps({
#             "type": "error",
#             "message": error_msg,
#             "code": "AGENT_INIT_FAILED"
#         }))
#         await websocket.close()
#         return

#     # 对话循环
#     while True:
#         try:
#             data = await websocket.receive_text()
#             payload = json.loads(data)
#             query = payload.get("query", "")

#             if not query:
#                 await websocket.send_text(json.dumps({"type": "error", "message": "empty query"}))
#                 continue

#             # 流式执行 Agent
#             async for event in agent.stream_chat(query):
#                 await websocket.send_text(json.dumps(event, ensure_ascii=False))

#             await websocket.send_text(json.dumps({"type": "done"}))

#         except WebSocketDisconnect:
#             logging.info("客户端断开连接: %s-%s", user_id, session_id)
#             break
#         except Exception as e:
#             logging.exception("对话异常")
#             await websocket.send_text(json.dumps({"type": "error", "message": str(e)}))
