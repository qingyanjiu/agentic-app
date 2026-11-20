# uvicorn app:app --host 0.0.0.0 --port 8000 --reload
import json
import os
import re

# 项目根目录：读 mcp_server_config.yaml / .env 这类"相对仓库根"的路径都靠它。
# 注意：这段定义在去 langchain 时被误删，但 _scene_mcp_url() 还在用 —— 于是
# 读取 scene 配置抛 NameError 被 except 吞掉，场景地址恒为空，
# scene_available 因此为 False，所有场景指令都不再分流，直接落到
# 「这个说法我暂时没理解」的兜底。日志特征：
#   WARNING - 读取 scene MCP 配置失败(仅场景控制不可用): name 'BASE_DIR' is not defined
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---- .env 支持（可选）----
# 仓库里只放 .env.example；真实 .env 被 .gitignore 忽略。
# override=False：已经在环境里的变量（compose / systemd / shell）优先，
# .env 只补缺。没装 python-dotenv 也能跑，纯靠系统环境变量。
try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

if load_dotenv is not None:
    _ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    load_dotenv(_ENV_FILE, override=False)
from typing import Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from models.llm import CustomLLMFactory
# 旧链路的 LangGraphPipeline / McpSkillAgent 已随 graph/、skills/ 一起删除
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
from agent.scene_control import build_scene_control_graph
from memory.session_state import session_state, IntentState

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
# 同时打到控制台：debug / docker logs 时能直接看到关键链路日志；
# 文件里仍保留完整记录，控制台只放 INFO 及以上，避免第三方 DEBUG 淹没输出。
_console_handler = logging.StreamHandler()
_console_handler.setLevel(logging.INFO)
_console_handler.setFormatter(
    logging.Formatter('%(asctime)s - %(levelname)s - %(name)s - %(message)s')
)
logging.getLogger().addHandler(_console_handler)
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
# ============================================================
# 会话历史只读接口（默认不挂载）
#
# 它没有任何鉴权（user_id 直接来自 URL），所以只有显式 CHAT_READ_API=1 才注册路由，
# 仅供本地联调 / 内网回填历史；将来接上 sidecar 的 token 校验再正式对外。
# 实现见 chat_api.py。
# ============================================================
if (os.getenv("CHAT_READ_API") or "").strip().lower() not in ("", "0", "false", "off", "no"):
    from chat_api import build_chat_router

    app.include_router(build_chat_router())
    logger.info(
        "[startup] 会话历史只读接口已挂载: "
        "GET /chat/sessions/{user_id}/{session_id}/messages"
    )

# 默认对外只有 WS /chat；上面那个只读历史接口要 CHAT_READ_API=1 才挂。
# 报告下载（/reports）、图片上传（/upload/image + /uploads）、语音调试页（/static）都已移除 ——
# 它们属于 gen_doc / 多模态 / 语音这类"非 agent 能力"，以后按插件接入，不放在 agent 服务里。
# 调试页是纯静态文件：直接打开 web/index.html（不经过本服务）。

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
    # 对话历史落盘：第一次取用会建表 + 清过期数据（CHAT_DB_PATH / CHAT_RETENTION_DAYS）
    try:
        from memory.chat_store import get_store

        get_store()
    except Exception as exc:  # 存储不可用不该拦住启动
        logger.warning(f"[startup] 对话历史存储初始化失败（不落盘，仅内存）: {exc}")
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
    """递归把消息对象转成可 JSON 序列化的结构。

    原来是 isinstance(obj, BaseMessage) → model_dump()，为此 app.py 顶部
    要 import langchain_core。legacy 业务图删完后已经没有任何一条路径
    产出 BaseMessage，所以改成鸭子类型（有 model_dump 就当消息处理），
    顺带去掉了 app.py 对 langchain_core 的依赖。
    """
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        return dump()
    if isinstance(obj, list):
        return [_safe_serialize(i) for i in obj]
    if isinstance(obj, dict):
        return {k: _safe_serialize(v) for k, v in obj.items()}
    return obj


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
            # load_servers = 纯标准库版的 load_mcp_config（旧的 mcp_loader 已随 langchain 一起删掉）
            from mcp_client.mcp_config import load_servers
            conf = load_servers(os.path.join(BASE_DIR, "mcp_client", "mcp_server_config.yaml"))
            url = ((conf.get("scene") or {}).get("url")) or ""
        except Exception as e:
            logger.warning(f"读取 scene MCP 配置失败(仅场景控制不可用): {e}")
            url = ""
    _scene_url_cache = url
    return url




# ============================================================
# 知识库问答（payload.mode=kb）
#
# 检索：平台 HTTP 直连接口（mcp_client/knowledge_http_client.py，配了
#       KB_BASE_URL + KB_API_KEY 才启用）→ 失败回退 sidecar /mcp/knowledge。
# 作答：ChatClient.astream 流式回 answer 事件，结束时补 done。
# 与 loop 链路共用同一条 WS 通道，靠 payload.mode 区分。
# ============================================================
KB_SYSTEM_PROMPT = """你是园区知识库问答助手，只回答规章制度、管理办法、操作规范、说明文档这类知识库问题。

规则：
- 只依据【检索结果】回答，禁止编造结果里没有的内容。
- 引用哪条就在句末标注来源，格式：（来源：文档名）。
- 检索结果为空，或内容里明确说"未检索到相关内容"时，直接回答"未在知识库中检索到相关内容，建议换个问法再试试。"
- 用户问在园人数、设备状态这类实时数据时，说明实时数据请切回「智能体问答」模式。
- 用中文回答，条理清晰，可以引用原文。"""

_kb_mcp_url_cache = None


def _knowledge_mcp_url() -> str:
    """sidecar 知识库检索端点（兜底通道）；没配置返回空串。结果缓存一次。"""
    global _kb_mcp_url_cache
    if _kb_mcp_url_cache is not None:
        return _kb_mcp_url_cache
    url = ""
    try:
        from mcp_client.mcp_config import load_servers

        conf = load_servers(os.path.join(BASE_DIR, "mcp_client", "mcp_server_config.yaml"))
        url = ((conf.get("knowledge") or {}).get("url")) or ""
    except Exception as e:
        logger.warning(f"读取 knowledge MCP 配置失败(知识库兜底通道不可用): {e}")
    _kb_mcp_url_cache = url
    return url


async def kb_retrieve(query: str) -> str:
    """知识库检索：平台 HTTP 直连优先，sidecar 兜底，都失败给可读文案。"""
    # 1) 平台接口直连（零 langchain，走 httpx）
    direct = None
    try:
        from mcp_client.knowledge_http_client import build_from_env as _build_kb_http

        direct = _build_kb_http()
    except Exception as e:
        logger.warning(f"[kb] 初始化平台直连失败: {type(e).__name__} {e}")
    if direct is not None:
        try:
            return await direct.retrieve(query)
        except Exception as e:
            logger.warning(
                f"[kb] 平台直连检索失败，改用 sidecar 兜底: {type(e).__name__} {e}"
            )

    # 2) sidecar /mcp/knowledge（datasetIds 传空串 = 用 sidecar 配置的默认范围）
    url = _knowledge_mcp_url()
    if url:
        try:
            from mcp_client.knowledge_mcp_client import KnowledgeMcpClient

            client = KnowledgeMcpClient(url)
            try:
                return await client.call_tool(
                    "ragflow:searchKnowledge", {"question": query, "datasetIds": ""}
                )
            finally:
                await client.aclose()
        except Exception as e:
            logger.error(f"[kb] sidecar 检索失败: {type(e).__name__} {e}")

    return "知识库服务暂时不可用，请稍后重试。"


async def _handle_kb_turn(websocket, query: str, llm) -> None:
    """一轮知识库问答：检索 → 拼提示词 → 流式作答 → done。"""
    retrieved = await kb_retrieve(query)
    prompt = f"{KB_SYSTEM_PROMPT}\n\n用户问题：{query}\n\n【检索结果】\n{retrieved}"

    sent = 0
    try:
        async for piece in llm.astream(prompt):
            if not piece:
                continue
            sent += len(piece)
            await websocket.send_text(
                json.dumps(
                    {"event": "custom", "data": {"type": "answer", "content": piece}},
                    ensure_ascii=False,
                )
            )
    except Exception as e:
        logger.exception("[kb] 作答失败")
        await websocket.send_text(
            json.dumps(
                {"event": "custom", "data": {"type": "error", "content": f"知识库作答失败：{e}"}},
                ensure_ascii=False,
            )
        )

    # 模型一个字都没吐（异常或空回复）：至少把检索结果原样给前端，别白屏
    if sent == 0:
        await websocket.send_text(
            json.dumps(
                {"event": "custom", "data": {"type": "answer", "content": retrieved}},
                ensure_ascii=False,
            )
        )

    await websocket.send_text(json.dumps({"status": "done"}))


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
    # 知识库那条也只剩检索客户端（KnowledgeMcpClient），问答编排已删除，
    # 见下方 mode=kb 分支的说明。
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
            # 知识库问答模式（payload 带 mode=kb）
            #
            # 原来这里走 KnowledgeQAPipeline（langgraph 串「改问→检索→作答」），
            # 已按"只留检索接口"的要求移除：检索客户端 KnowledgeMcpClient
            # （mcp_client/knowledge_mcp_client.py，原生 httpx、零 langchain）
            # 保留，将来接知识库直接调它。这里先给一句明确提示，别让用户
            # 以为在查、实际落到 loop 兜底。
            # ============================================================
            if payload.get("mode") == "kb" and query:
                await _handle_kb_turn(websocket, query, llm)
                continue


            # ============================================================
            # Loop Agent 灰度（新链路）
            # 命中条件与优先级见 agent/loop/rollout.py 顶部注释
            # （AGENT_MODE / LOOP_ROLLOUT_PERCENT / LOOP_ROLLOUT_USERS，见 docs/架构文档.md 第 13 节）
            # 任何异常都回落（loop-only 分支上没有旧链路，落到下面的一句话兜底）
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
