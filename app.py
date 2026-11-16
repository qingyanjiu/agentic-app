# uvicorn app:app --host 0.0.0.0 --port 8000 --reload
import json
import os
import re
from typing import Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from langchain_core.messages import HumanMessage, SystemMessage, BaseMessage, AIMessage, AIMessageChunk
from models.llm import CustomLLMFactory
# from graph.graph_pipeline import LangGraphPipeline
from graph.knowledge_qa_langgraph import KnowledgeQAPipeline
from graph.asr_pipeline import TextCorrectorPipeline
# from skills.mcp_agent import McpSkillAgent
import logging
import uuid
import time
import asyncio

# 新增：人员态势/安防态势/食堂管理/车辆态势/信息发布/能源态势/会议管理意图识别相关导入
# classify_security_sub_type：安防态势子类型（event_type）判定，供安防 handler 追问/兜底使用
from agent.intent import (
    is_scene_control_intent,
    has_strong_scene_cue,
    classify_intent_with_scene,
    should_route_scene,
    classify_intent,
    classify_security_sub_type,
)
# UE 3D 场景控制：sidecar 的 /mcp/scene 是 direct_client（逐请求注入 X-Session-Id），
# 不走 langchain_mcp_adapters，由 SceneMcpClient 直连
from mcp_client.scene_mcp_client import SceneMcpClient
from graph.scene_control_langgraph import build_scene_control_graph
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
# 挂载报告文件目录为静态文件服务（绝对路径，避免启动目录不同导致下载 404）
REPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
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

# 全局模型：配置全部来自环境变量，见 models/llm.py 模块注释
llm_factory = CustomLLMFactory()
llm = llm_factory.llm
# 打一行日志，避免"改了 .env 却不知道实际用了哪个模型"
logger.info(
    "[startup] 模型 %s / %s @ %s",
    llm_factory.config["type"],
    llm_factory.config["model_name"],
    llm_factory.config["model_url"],
)
@app.on_event("startup")
async def startup():
    logger.info("[startup] 开始预加载意图识别模型...")
    from agent.intent import get_classifier
    await get_classifier()
    logger.info("[startup] 意图识别模型预加载完成")
    # 后台预热场景 MCP / 各业务域图，避免第一个用户承担握手和工具加载时间
    #
    # 动态发现参数简单的 MCP 工具（试点 devicequery 的统计类接口）。
    # 必须在首次构建 pipeline 之前完成：loop 的 system prompt 里的工具概览是
    # LoopAgent 构造时固化的，晚到的工具进不去。失败只告警，只用显式工具。
    # 出问题可以用 LOOP_DISCOVER=0 一键退回（只保留手写的 14 个工具）。
    if os.getenv("LOOP_DISCOVER", "1").strip().lower() not in ("0", "false", "off", "no"):
        try:
            from agent.loop.discover import warm_discovered

            found = await asyncio.wait_for(warm_discovered(), timeout=10)
            tool_count = sum(len(domain.tools) for domain in found)
            logger.info(
                f"[startup] MCP 工具发现完成：{len(found)} 个域 / {tool_count} 个工具"
            )
        except BaseException as e:  # noqa: BLE001
            # 必须是 BaseException：MCP 不可达时 wait_for 超时会抛 CancelledError，
            # 早期写 except Exception 接不住，直接把 startup 带崩（服务起不来）。
            # 工具发现是可选项，失败就该退回只用 14 个显式工具。
            logger.warning(
                f"[startup] MCP 工具发现跳过（仅使用显式工具）: {type(e).__name__} {e}"
            )
    else:
        logger.info("[startup] LOOP_DISCOVER=0，跳过 MCP 工具发现")
    # 启动级路由：AGENT_MODE=loop 全量走新链路 / legacy 全走旧逻辑 / auto 走灰度
    # 只影响路由判定，不改变任何链路内部行为；改完重启生效
    try:
        from agent.loop.rollout import get_rollout

        logger.info(f"[startup] 路由模式: {get_rollout().describe()}")
    except Exception as e:  # noqa: BLE001 日志不该带崩启动
        logger.warning(f"[startup] 读取路由模式失败: {type(e).__name__} {e}")
    _start_prewarm()
    # 会话池监控：与预热无关，PREWARM_ON_STARTUP=0 时也要开
    _prewarm_tasks.append(asyncio.create_task(_watch_session_pool()))


@app.on_event("shutdown")
async def shutdown():
    """进程退出时释放场景 MCP 直连的 HTTP 会话（全局单例，不能按连接关闭）"""
    global _scene_client, _scene_graph
    if _scene_client is not None:
        try:
            await _scene_client.aclose()
        except Exception:
            logger.warning("SceneMcpClient 释放失败(忽略)")
        _scene_client = None
        _scene_graph = None


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


async def _stream_graph_to_ws(websocket, graph, graph_input):
    """流式执行业务图，把 LLM 回答分块即时推给前端。

    业务图的回答节点通过 graph.stream_utils.stream_llm_text 走 LangGraph 的
    custom 通道；这里收到一个 chunk 就转发一个，首字不必等整段生成完。
    返回 (final_state, streamed_answer)：streamed_answer 为 True 时，调用方
    转发最终 events 要跳过完整 answer，避免前端把同一段回答显示两遍。
    """
    final_state = dict(graph_input)
    streamed_answer = False

    async for mode, chunk in graph.astream(
        graph_input, stream_mode=["custom", "values"]
    ):
        if mode == "custom":
            if not isinstance(chunk, dict) or chunk.get("type") != "answer":
                continue
            if not chunk.get("content"):
                continue
            streamed_answer = True
            event = _safe_serialize({"event": "custom", "data": chunk})
            await websocket.send_text(json.dumps(event, ensure_ascii=False))
        elif mode == "values":
            # values 是节点执行后的完整状态快照，最后一个就是 final_state
            final_state = chunk

    return final_state, streamed_answer

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

            

        

# ============================================================
# 各业务域的 handler / graph 执行器映射
# 供「追问态新意图接管」复用情况 2 的新意图启动流程
# ============================================================
# ============================================================
# 收尾/告别语识别（排查记录案例 15）
# 「请问您还有其他问题吗？」→「没有了」这类回复是对开放问题的礼貌收尾，
# 不是可分类的业务查询：give_up 清空状态后它必然分类成 other，若不拦住
# 会掉进旧 pipeline 回生硬的「无法解决你的问题」；done 态也会被情况 1.5
# 拼接上轮问题把已完成的查询重新执行一遍。故在意图分发前整句匹配拦截。
# 整句匹配 + 长度上限，避免把「没有告警吗」「没有什么新闻」这类真查询误吞。
# ============================================================
_CONVERSATION_CLOSE_RE = re.compile(
    r"^(?:我说|就是|那个|额)*"                       # 口头铺垫（可无）
    r"(?:"
    r"没有(?:了|什么|啥|(?:其他|别的)(?:问题|的事?)?)?"   # 没有了/没有其他问题/没什么…
    r"|没了"
    r"|不用了?"
    r"|暂时没有"
    r"|木有了?"
    r"|再见"
    r"|拜拜"
    r"|谢谢"
    r"|辛苦了"
    r"|好的"
    r")"
    r"(?:谢谢|多谢|辛苦了)?"                          # 后置致谢（没有了谢谢）
    r"(?:[吧啦呀哦哈的了~～!！。,，]*)$"
)


def is_conversation_close(query: str) -> bool:
    """无任务态下，整句命中收尾/告别语时返回 True"""
    if not query or len(query.strip()) > 12:
        return False
    return bool(_CONVERSATION_CLOSE_RE.match(query.strip()))


# 省略式追问形状白名单（情况 1.5 放行条件，2026-09-23 用户裁定）：
# other 且上轮 done 的输入，只有「时间词/域动词 + 语气词」这种真正的
# 省略句才拼接上轮问题重新分类；其余 other（「今天日期」「今天是哪天」
# 「你好」等）一律直接走 other 兜底，不能被上轮问题拽回业务域把上轮
# 查询重跑一遍。背景（2026-09-23 冒烟）：车辆查询完成后问「今天是哪天」，
# 拼接「停车场还有多少空位 今天是哪天」被判回车辆域、停车数据重答一遍。
_ELLIPSIS_FOLLOWUP_RE = re.compile(
    r"^(?:那|再|就)?"                              # 指示前缀（那上周呢）
    r"(?:"
    # ① 时间词省略：昨天呢 / 那上周呢 / 今天早上呢
    r"(?:大前天|大后天|前天|后天|今天|明天|昨天|上周|本周|下周|上个月|上月|这个月|本月|下个月|下月|今年|去年|前年|刚才|现在|(?:今天|昨天|明天)?(?:早上|上午|中午|下午|晚上))"
    r"(?:的时候)?的?(?:呢|吗|么|怎么样|咋样|又如何|再看看|看看|又是多少|有多少)"
    # ② 域动词省略：出去的呢 / 那走了呢
    r"|(?:出去|进来|进入|离开|离园|入园|来了?|走了?)(?:的人)?的?(?:呢|人数|有多少|多少)"
    # ③ 时间+动词：昨天出去的呢 / 上周进来的呢
    r"|(?:大前天|大后天|前天|后天|今天|明天|昨天|上周|本周|下周|上个月|上月|这个月|本月|下个月|下月|今年|去年|前年|刚才|现在|(?:今天|昨天|明天)?(?:早上|上午|中午|下午|晚上))的?(?:出去|进来|进入|离开|离园|入园|来了?|走了?)(?:的人)?的?(?:呢|人数|有多少|多少)"
    r")"
    r"[吧呢吗啊呀]?$"
)


def is_ellipsis_followup(query: str) -> bool:
    """整句是省略式追问形状（「昨天呢」「那上周呢」「出去的呢」）时返回 True。

    情况 1.5 的放行白名单：other 输入只有长得像省略追问才拼接上轮
    问题重新分类；其余 other 直接走 other 兜底（用户裁定：所有意图
    都不包含就直接进 other）。
    """
    if not query or len(query.strip()) > 12:
        return False
    q = re.sub(r"[\s，,。.!！?？~～]+$", "", query.strip())
    return bool(_ELLIPSIS_FOLLOWUP_RE.fullmatch(q))


# 各域抽取器的子类型兜底值（与 slots.py 各 extract_*_slots 的初始值对齐）：
# 省略式追问（情况 1.5）本轮抽到这些值说明输入本身不含子类型信息
# （如「昨天呢」），才继承上轮子类型；本轮明确抽出具体子类型
# （如「出去呢」→ leave）时以本轮为准（排查记录案例 13 / 案例 17）
_SUBTYPE_FALLBACKS = {
    "query_type": {"count", "basic_info", "device_list"},
    "event_type": {"count", "alarm_list", "week_menu", "info_view"},
}


# 业务域模块名集合。
# 原来这里是 {模块名: Handler 类} + {模块名: run_xxx_graph}，legacy 删除后
# handler 和 13 张图都不在了；只剩场景分流要判断"这个意图是不是业务域"，
# 所以退化成名字元组。
BUSINESS_MODULES = (
    "person_status",
    "security_status",
    "canteen_status",
    "vehicle_status",
    "information_status",
    "energy_status",
    "emergency_fire",
    "emergency_perimeter",
    "meeting_status",
    "device_status",
    "compositive_overview",
    "device_query",
    "twins_inspection",
)

# 兼容场景分流里 `in MODULE_HANDLERS` 的旧写法
MODULE_HANDLERS = {name: None for name in BUSINESS_MODULES}
# ============================================================
# UE 3D 场景控制（MCP sidecar /mcp/scene）
# ------------------------------------------------------------
# 链路：前端 WS hello 声明 scene session → 聊天请求 payload 带上该 id
#       → app.py 取出 → SceneMcpClient 以 X-Session-Id 头逐请求调用
#       → sidecar 通过自己的 WS 把动作帧推给对应场景页面。
# 红线：X-Session-Id 只能来自前端/会话状态，绝不能让 LLM 生成或补全。
# ============================================================
_scene_client = None
_scene_graph = None


def _extract_scene_session_id(payload: dict) -> Optional[str]:
    """从前端 payload 里取场景页面会话 id（即业务路由头 X-Session-Id 的值）。

    前端字段命名未完全统一，这里兼容常见几种驼峰/下划线写法；
    取不到返回 None，sidecar 会走单场景会话兜底。
    """
    for key in (
        "scene_session_id", "sceneSessionId", "sceneId", "scene_id",
        "X-Session-Id", "x_session_id",
    ):
        value = payload.get(key)
        if value:
            return str(value)
    return None


_scene_url_cache = None


def _scene_mcp_url() -> str:
    """场景服务地址：优先环境变量，其次 mcp_server_config.yaml 的 scene 条目

    结果缓存：场景控制现在参与每轮意图分流，避免每条消息都读一遍 yaml。
    没配置时返回空串，分流逻辑据此关闭场景自动识别。
    """
    global _scene_url_cache
    if _scene_url_cache is not None:
        return _scene_url_cache
    url = os.getenv("SCENE_MCP_URL")
    if not url:
        try:
            from mcp_client.mcp_loader import load_mcp_config
            conf = load_mcp_config(os.path.join(BASE_DIR, "mcp_client", "mcp_server_config.yaml"))
            url = ((conf.get("scene") or {}).get("url")) or ""
        except Exception as e:
            logger.warning(f"读取 scene MCP 配置失败(仅场景控制不可用): {e}")
            url = ""
    _scene_url_cache = url
    return url


async def get_scene_graph():
    """懒加载场景控制图（懒握手：sidecar 未就绪不影响服务启动）"""
    global _scene_graph, _scene_client
    if _scene_graph is None:
        url = _scene_mcp_url()
        if not url:
            logger.warning("未配置 scene MCP 地址，场景控制不可用")
            return None
        _scene_client = SceneMcpClient(url)
        _scene_graph = build_scene_control_graph(_scene_client, llm=llm)
        logger.info(f"SceneControlGraph创建成功: {url}")
    return _scene_graph


# ============================================================
# 启动预热：把首个用户要付的握手/工具加载成本挪到后台
#   - 场景：MCP initialize + tools/list（首次场景指令不再等握手）
#   - 业务问答走 agent/loop，没有"域图"要预编译，所以这里只预热场景
# 可用 PREWARM_ON_STARTUP=0 关闭。
# ============================================================
_prewarm_tasks = []



async def _watch_session_pool() -> None:
    """每分钟打一次会话池大小（T16 的临时观察手段）。

    两处会话存储都没有容量上限；`session_state` 的过期只在**被访问时**才清理，
    过期后没人再访问的会话不会释放。有这行日志就能看出是真增长还是理论风险。
    上了 Redis（T15）之后可以删掉。
    """
    from agent.loop import ws_bridge
    from memory.session_state import session_state

    while True:
        await asyncio.sleep(60)
        try:
            logger.info(
                "[session] states=%d loop_sessions=%d",
                len(session_state.states),
                len(ws_bridge._sessions),
            )
        except Exception as e:  # 统计失败不能影响服务
            logger.warning(f"[session] 会话池统计失败（忽略）: {e}")


def _start_prewarm() -> None:
    if os.getenv("PREWARM_ON_STARTUP", "1").strip().lower() in ("0", "false", "off", "no"):
        logger.info("[startup] PREWARM_ON_STARTUP 已关闭，跳过预热")
        return
    _prewarm_tasks.append(asyncio.create_task(_prewarm_scene_mcp()))


async def _prewarm_scene_mcp() -> None:
    try:
        graph = await get_scene_graph()
        if graph is None or _scene_client is None:
            return
        # 首轮 initialize + tools/list 一次付清；失败只告警，不影响服务
        await asyncio.wait_for(_scene_client.list_tools(), timeout=8.0)
        logger.info("[startup] 场景 MCP 预热完成")
    except Exception as e:
        logger.warning(f"[startup] 场景 MCP 预热失败（不影响启动）: {e}")




def _scene_followup_pending(user_id: str, session_id: str, query: str) -> bool:
    """上一轮场景追问还没结束、本轮像是补答 → 交回场景通道处理。

    loop 链路在场景分支之前，不挡一下会把「第三个」「这个」当成新问题，
    模型手上没有候选清单，只能回一句"没有可对应的内容"。
    """
    prev = session_state.get(user_id, session_id)
    if prev is None or prev.module != "scene_control" or prev.done:
        return False
    text = (query or "").strip()
    if not text:
        return False
    # 「7楼有哪些监控」也是短句，但它是在要清单、不是在补答参数。
    # 放给 loop，否则会被拼回场景通道，回用户一句"选哪台设备"。
    from agent.loop.router import asks_for_list

    if asks_for_list(text):
        return False
    # 短句（「第三个」「7F中通道东向西」）或序号/指代开头，都算补答
    return len(text) <= 12 or bool(re.match(r"^(第|这|那|\d)", text))


# 场景动作常以设备名称指代点位（「打开7F西通道监控」），而 scene_show_camera
# 要的是点位 tag = 设备编号。这里用设备台账（走 device_query 的缓存）把名字换成编号：
#   唯一命中 → 注入 slots.device_tag，场景图直接用，不再追问；
#   多台/没有 → 回一句追问，引导用户补设备类型或编号。
# 设备线索判定已挪到 scene_resolve.has_device_reference（要区分"视角指令"和"点设备"）
# 话术里已经有设备编码的，不用再解析
_DEVICE_CODE_HINT = re.compile(r"[A-Za-z][A-Za-z0-9_\-]{2,}")
_scene_device_toolbox = None


async def _resolve_scene_device_tag(
    query: str,
    candidates: Optional[list] = None,
) -> tuple[Optional[str], Optional[str], list]:
    """把场景话术里的设备名解析成点位 tag（逻辑见 agent/loop/scene_resolve.py）。

    :param candidates: 上一轮追问时列给用户的候选，用来解析「第三个」「这个」
    :return: (tag, 追问话术, 候选列表)，tag 与追问话术互斥。
    """
    global _scene_device_toolbox
    from agent.loop.runtime import McpToolbox
    from agent.loop.scene_resolve import resolve_scene_device_tag

    if _scene_device_toolbox is None:
        _scene_device_toolbox = McpToolbox()
    return await resolve_scene_device_tag(_scene_device_toolbox, query, candidates)

async def run_scene_control_graph(websocket, query: str, user_id: str, session_id,
                                  scene_session_id: Optional[str] = None,
                                  resolve_query: Optional[str] = None) -> None:
    """
    执行一轮 UE 场景控制。

    :param query: 本轮要规划的话术（追问轮由调用方把上一轮原话拼进来，给规划器看）
    :param scene_session_id: 前端声明的场景页面会话，逐请求注入 X-Session-Id
    :param resolve_query: 解析设备名用的话术，默认同 query。
        追问轮里 query 是拼接句（「打开7F西侧的监控 打开7F西通道西向东监控」），
        拿它去抽设备名会抽出整句、永远匹配不到，所以必须传本轮原话。
    """
    # 追问时把候选转成结构化列表（可点），见下面的 clarify 分支
    from agent.loop.scene_resolve import clarify_entities, clarify_headline
    from agent.loop.scene_resolve import ESCAPE_CLARIFY, has_device_reference

    graph = await get_scene_graph()
    if graph is None:
        await websocket.send_text(json.dumps({
            "event": "custom",
            "data": {"type": "error", "content": "场景服务未配置或不可用，请稍后重试。"},
        }, ensure_ascii=False))
        await websocket.send_text(json.dumps({
            "event": "custom", "data": {"type": "done"},
        }, ensure_ascii=False))
        return

    # 追问态：继承上一轮的首问原话（拼接串回写会滚雪球）和追问次数
    prev = session_state.get(user_id, session_id)
    ask_count = 0
    persist_query = query
    last_question = ""
    if prev is not None and prev.module == "scene_control" and not prev.done:
        ask_count = prev.ask_count
        persist_query = prev.original_query or query
        last_question = prev.last_question
        if not scene_session_id:
            scene_session_id = (prev.slots or {}).get("scene_session_id")

    # 名称 → 点位 tag：scene_show_camera 要的是设备编号，用户通常只说名字。
    # 解析不出来（多台/没有）就发一句追问，让用户补设备类型或编号，别让场景图瞎猜。
    # 追问时把候选存进 slots，用户下一轮说「第三个」「这个」时据此解析。
    scene_slots = {"scene_session_id": scene_session_id} if scene_session_id else {}
    prev_candidates = []
    if prev is not None and prev.module == "scene_control":
        prev_candidates = (prev.slots or {}).get("device_candidates") or []

    # 有候选时也要走解析：用户可能只回「第三个」，句子里没有设备类型词
    # 只有本轮真说了设备线索才去查台账。追问态下「切回整栋楼」这类视角指令
    # 之前也会被拉去查全表，回用户一句「匹配到 268 台设备」。
    asks_device = resolve_query or query
    needs_device = bool(asks_device) and has_device_reference(asks_device)
    if needs_device and not _DEVICE_CODE_HINT.search(query):
        try:
            device_tag, clarify, candidates = await _resolve_scene_device_tag(
                resolve_query or query, prev_candidates
            )
        except Exception as e:
            logger.warning(f"[场景] 设备名称解析失败，交给场景图追问: {e}")
            device_tag, clarify, candidates = None, None, prev_candidates
        if clarify:
            # 追问出口：用户既没说序号也没说设备名 → 清状态、放开追问，
            # 否则下一句又会被当成补答，形成死循环。
            if clarify == ESCAPE_CLARIFY:
                session_state.clear(user_id, session_id)
                await websocket.send_text(json.dumps({
                    "event": "custom", "data": {"type": "ask", "question": clarify},
                }, ensure_ascii=False))
                await websocket.send_text(json.dumps({
                    "event": "custom", "data": {"type": "done"},
                }, ensure_ascii=False))
                return
            if candidates:
                scene_slots["device_candidates"] = candidates
                # 候选也给结构化列表：前端渲染成可点设备标签，点一下直接开画面。
                # 同时把追问话术里的候选明细行去掉，避免和标签重复。
                entities = clarify_entities(clarify, candidates)
                if entities:
                    await websocket.send_text(json.dumps({
                        "event": "custom",
                        "data": {"type": "list", **entities.to_dict()},
                    }, ensure_ascii=False))
                    clarify = clarify_headline(clarify) or clarify
            await websocket.send_text(json.dumps({
                "event": "custom", "data": {"type": "ask", "question": clarify},
            }, ensure_ascii=False))
            await websocket.send_text(json.dumps({
                "event": "custom", "data": {"type": "done"},
            }, ensure_ascii=False))
            session_state.set(user_id, session_id, IntentState(
                module="scene_control",
                slots=dict(scene_slots),
                missing_params=[],
                ask_count=ask_count,
                unrelated_count=0,
                last_question=clarify,
                original_query=persist_query,
                done=False,
            ))
            return
        if device_tag:
            scene_slots["device_tag"] = device_tag

    graph_input = {
        "slots": scene_slots,
        "missing_params": [],
        "ask_count": ask_count,
        "unrelated_count": 0,
        "last_question": last_question,
        "original_query": query,
        "answer": None,
        "error": None,
        "done": False,
        "events": [],
        "scene_session_id": scene_session_id,
        "plan": None,
        "tool_result": None,
    }
    print(f"[SCENE GRAPH INPUT] user={user_id}, session={session_id}, scene_session={scene_session_id}")
    print(json.dumps(graph_input, ensure_ascii=False, default=str))

    final_state = await graph.ainvoke(graph_input)

    print(f"[SCENE GRAPH OUTPUT] user={user_id}, session={session_id}")
    print(json.dumps(final_state, ensure_ascii=False, default=str))

    # 发事件；出现 ask 说明在追问，保留状态等用户补充，否则标记 done 留作上下文
    keep_state = False
    for chunk in final_state.get("events", []):
        text = _safe_serialize(chunk)
        await websocket.send_text(json.dumps(text, ensure_ascii=False))
        if chunk.get("event") == "custom" and chunk.get("data", {}).get("type") == "ask":
            keep_state = True

    new_state = IntentState(
        module="scene_control",
        slots={"scene_session_id": scene_session_id} if scene_session_id else {},
        missing_params=[],
        ask_count=final_state.get("ask_count") or 0,
        unrelated_count=0,
        last_question=final_state.get("last_question") or "",
        original_query=persist_query,
        done=not keep_state,
    )
    session_state.set(user_id, session_id, new_state)


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
    
    # legacy 的 InfoDoubleCheckPipeline（rag_pipeline）已移除：
    # 业务问答全部走 agent/loop，落空时由下方兜底话术应答。
    # 知识库直连管线：前端切到"知识库问答"模式（payload 带 mode=kb）时使用，
    # 不走意图识别。创建失败只降级知识库模式，智能体问答不受影响
    kb_pipeline = None
    try:
        # 同步创建、懒握手：sidecar 未就绪不影响创建，只影响知识库模式首次检索
        kb_pipeline = KnowledgeQAPipeline.create(llm=llm)
        logging.info("KnowledgeQAPipeline创建成功")
    except Exception as e:
        logging.error(f"KnowledgeQAPipeline创建失败（仅知识库模式不可用）: {e}")
 # 持续监听客户端消息（WebSocket长连接循环）
    # 上一轮执行期间又收到的消息（用户抢着补一句），按顺序在这一轮结束后处理
    pending_payloads: list = []
    # 收包单独一个 task，主循环只从队列取：本轮还在跑时也能读到 stop，
    # 而且取消的只是 Queue.get()，不会损坏 socket（见 _await_turn_with_stop 注释）
    inbox: asyncio.Queue = asyncio.Queue()

    async def _pump_incoming() -> None:
        try:
            while True:
                await inbox.put(await websocket.receive_text())
        except Exception:
            # 客户端断开 / 连接异常：塞一个 None 让主循环退出
            await inbox.put(None)

    pump_task = asyncio.create_task(_pump_incoming())
    while True:
        try:
            if pending_payloads:
                payload = pending_payloads.pop(0)
                if payload is None:
                    logging.info(f"客户端已断开 -- {user_id}-{session_id}")
                    pump_task.cancel()
                    return
            else:
                # 接收客户端发送的JSON数据（格式：{"query": "用户问题"}）
                raw = await inbox.get()
                if raw is None:
                    logging.info(f"客户端已断开 -- {user_id}-{session_id}")
                    pump_task.cancel()
                    return
                payload = json.loads(raw)
            # 停止指令不是一轮查询：本轮之外收到就直接告诉用户没有在跑的请求
            if str(payload.get("type") or "").lower() == "stop":
                await websocket.send_text(json.dumps({
                    "event": "custom",
                    "data": {"type": "answer", "content": "已收到停止请求，当前没有进行中的查询。"},
                }, ensure_ascii=False))
                await websocket.send_text(json.dumps({"status": "done"}))
                continue
            # 从 payload 中获取用户输入
            query = payload.get("query", "")
            # 每轮开头打一行：谁、哪个会话、问了什么（排查时全靠它对齐时间线）
            logging.info(
                f"[chat] 收到提问 user={user_id} session={session_id} "
                f"mode={payload.get('mode') or 'agent'} query={query!r}"
            )

            # ============================================================
            # 知识库问答模式：前端切到"知识库问答"后 payload 带 mode=kb，
            # 跳过下面的意图识别/会话状态，直接走知识库直连管线
            # ============================================================
            if payload.get("mode") == "kb" and query:
                if kb_pipeline is None:
                    await websocket.send_text(json.dumps({
                        "event": "custom",
                        "data": {"type": "error", "content": "知识库服务未就绪，请稍后重试或切回智能体问答。"}
                    }, ensure_ascii=False))
                    await websocket.send_text(json.dumps({"status": "done"}))
                    continue

                async for chunk in kb_pipeline.astream_run(query, user_id, session_id):
                    text = _safe_serialize(chunk)
                    # 过滤 token 级事件，与主流程的转发口径一致
                    if text.get("event") != "token":
                        await websocket.send_text(json.dumps(text, ensure_ascii=False))
                await websocket.send_text(json.dumps({"status": "done"}))
                logging.info(f"kb answer done -- {user_id}-{session_id}")
                continue

            # ============================================================
            # Loop Agent 灰度（新链路）
            # 命中条件：payload.mode=loop，或灰度比例/白名单命中
            # 任何异常都回落旧链路，存量功能不受影响
            # 配置见 docs/loop-agent-改造文档.md 第 8 节
            # ============================================================
            if query:
                try:
                    from agent.loop.rollout import get_rollout
                    from agent.loop.ws_bridge import handle_chat as loop_handle_chat

                    loop_decision = get_rollout().decide(
                        user_id, str(session_id), payload.get("mode")
                    )
                    # 场景追问态（上轮列了候选等用户选）优先交回场景通道：
                    # 否则 loop 会把「第三个」「这个」当成新问题，模型手上没有清单。
                    if loop_decision.enabled and not _scene_followup_pending(
                        user_id, session_id, query
                    ):
                        # 本轮放任务里跑，主循环趁机继续收消息 →
                        # 用户点"停止"能立刻置 cancel，不用等这一轮跑完
                        turn_task = asyncio.create_task(
                            loop_handle_chat(
                                websocket, query, user_id, str(session_id), llm, loop_decision
                            )
                        )
                        from agent.loop.ws_flow import await_turn_with_stop

                        loop_handled = await await_turn_with_stop(
                            turn_task, user_id, str(session_id), inbox, pending_payloads
                        )
                        if loop_handled:
                            continue
                except Exception as loop_err:
                    logging.error(
                        f"[loop] 灰度链路异常，回落旧链路: {loop_err} query={query!r}",
                        exc_info=True,
                    )

            # ============================================================
            # UE 3D 场景控制（MCP sidecar /mcp/scene）
            # 场景指令和业务查询共用同一个聊天入口，自动识别（先于业务状态机）：
            #   ① payload.mode == "scene"：显式模式，保留给前端/脚本强制走场景；
            #   ② 上一轮场景指令正在等参数（追问态）；
            #   ③ 本轮同时判业务意图与场景意图，命中场景就分流：
            #      · 业务意图为 other → 直接走场景；
            #      · 业务域也命中时，只有出现明确场景操作词、或场景分比业务分
            #        高出 SCENE_ROUTE_MARGIN 才走场景，避免「摄像头在线率」
            #        「各防区告警统计」这类问法被场景通道抢走；
            #   ④ 未配置 scene.url 时自动识别自动关闭，只保留显式 mode=scene。
            # ============================================================
            scene_session_id = _extract_scene_session_id(payload)
            scene_prev = session_state.get(user_id, session_id)
            # 同一场景页面的会话在 300s 内稳定：payload 没带时沿用上轮记录的
            if not scene_session_id and scene_prev and scene_prev.module == "scene_control":
                scene_session_id = (scene_prev.slots or {}).get("scene_session_id")
            scene_waiting = bool(
                scene_prev and scene_prev.module == "scene_control" and not scene_prev.done
            )
            route_to_scene = False
            scene_query = query
            # 自动识别时顺带算出的业务意图，供下面复用，避免重复编码
            precomputed_top_intent = None

            if query and payload.get("mode") == "scene":
                route_to_scene = True
            elif query and scene_waiting:
                # 追问轮：拼上一轮首问原话再判意图，避免单看参数值（如「3 楼」）判不出来
                merged_query = f"{scene_prev.original_query} {query}"
                try:
                    route_to_scene = await is_scene_control_intent(merged_query)
                except Exception as e:
                    logging.warning(f"[场景分流] 意图判定失败，维持追问: {e}")
                    route_to_scene = True  # 判定失败时仍按追问处理，由场景图兜底

                if not route_to_scene and len(query) <= 12:
                    # 短回答（「3楼」「A-01」「第二个」）大概率是在补参数：
                    # 只要没明确落到别的业务域，就继续交给场景图（它内部会再确认）
                    try:
                        other_intent = (await classify_intent(query))["intent"]
                    except Exception:
                        other_intent = "other"
                    route_to_scene = other_intent not in MODULE_HANDLERS

                # 「7楼有哪些监控」这类是在要清单，不是补答场景追问：
                # 不拦的话会拿上一轮场景上下文再问一次"选哪台设备"。
                from agent.loop.router import asks_for_list

                if route_to_scene and asks_for_list(query):
                    route_to_scene = False

                if route_to_scene:
                    scene_query = merged_query
                else:
                    # 用户跑题去问别的业务域了：放弃场景追问，交回主流程
                    session_state.clear(user_id, session_id)
            elif query:
                # 无显式模式/无场景追问：业务查询与场景指令混在一起，自动分流
                business_waiting = bool(
                    scene_prev and not scene_prev.done
                    and scene_prev.module in BUSINESS_MODULES
                )
                intent_info = None
                try:
                    intent_info = await classify_intent_with_scene(query)
                except Exception as e:
                    logging.warning(f"[场景分流] 意图判定失败，按业务问答处理: {e}")

                if intent_info is not None:
                    precomputed_top_intent = intent_info["intent"]
                    scene_available = bool(_scene_mcp_url())
                    if should_route_scene(intent_info, query) and scene_available:
                        # 业务追问中的简短补参（「3楼」「A-01」）不被场景截胡，
                        # 除非本轮是明确的场景操作（切到3楼/重置视角…）
                        if business_waiting and len(query.strip()) <= 6 \
                                and not has_strong_scene_cue(query):
                            route_to_scene = False
                        else:
                            route_to_scene = True
                    logging.info(
                        f"[场景分流] query={query}, business={intent_info['intent']}"
                        f"({intent_info['score']:.4f}), scene={intent_info['scene_score']:.4f},"
                        f" biz_center={intent_info.get('business_query_score', -1.0):.4f},"
                        f" matched={intent_info['scene_matched']}, route={route_to_scene}"
                    )

            if route_to_scene:
                await run_scene_control_graph(
                    websocket, scene_query, user_id, session_id, scene_session_id,
                    # 设备名一律用本轮原话解析：scene_query 可能是拼接句
                    resolve_query=query,
                )
                continue

            # ============================================================
            # legacy 状态机已移除（loop-only 分支）
            #
            # 走到这里说明：KB 分支没命中、loop 没接管（灰度未命中/未处理）、
            # 场景分流也没走。原来这里是 13 个业务域 Handler + LangGraph 图，
            # 现在统一交给 loop，兜底就一句话，不再回落旧链路。
            # ============================================================
            await websocket.send_text(json.dumps({
                "event": "custom",
                "data": {
                    "type": "answer",
                    "content": (
                        "这个说法我暂时没理解，可以换个问法吗？"
                        "比如「现在园区有多少人」「5楼有哪些监控」「园区设备类型统计」。"
                    ),
                },
            }, ensure_ascii=False))
            # answer 之后必须补 done：前端收到 done 才收起加载指示、解锁输入框
            # （排查记录案例 14：只发 answer 会让前端永久转圈）
            await websocket.send_text(json.dumps({
                "event": "custom",
                "data": {"type": "done"},
            }, ensure_ascii=False))
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

    # 会话结束：释放知识库直连客户端的 MCP/HTTP 会话
    if kb_pipeline is not None:
        try:
            await kb_pipeline.aclose()
        except Exception:
            logger.warning("KnowledgeQAPipeline 释放失败(忽略)")



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
