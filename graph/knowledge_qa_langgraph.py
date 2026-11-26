import logging
from typing import Any, AsyncIterator, Dict, List, Optional, TypedDict

from langchain_classic.schema import AIMessage, HumanMessage
from langchain_core.language_models import BaseChatModel
from langchain_core.prompts import PromptTemplate
from langgraph.graph import END, START, StateGraph
from langgraph.types import StreamWriter

from mcp_client.knowledge_http_client import EMPTY_HINT, KnowledgeHttpClient
from models.llm import ensure_user_turn
from mcp_client.knowledge_http_client import build_from_env as build_kb_http_client
from mcp_client.knowledge_mcp_client import KnowledgeMcpClient
from mcp_client.mcp_loader import load_mcp_config
from memory.store import MemoryStore

logger = logging.getLogger(__name__)

'''
知识库问答专用管线（前端"知识库问答"模式）。

与 reactive_pipeline 的区别：本管线不走意图识别、没有参数追问/评估迭代——
前端切到知识库模式后 payload 带 mode=kb，app.py 直接把问题交给本管线：
    改写追问(可选) → 直调 ragflow:searchKnowledge → 流式作答

检索直连 Java MCP Sidecar 的 /mcp/knowledge 端点（KnowledgeMcpClient，
协议口径与 Postman 联调脚本一致：initialize 拿 Mcp-Session-Id → tools/call），
检索范围/RAGFlow 地址/密钥都在 sidecar 侧配置，Python 端只管调工具。

会话记忆：与智能体问答共用 MemoryStore，但 session_id 加 kb- 前缀隔离，
避免两种模式的对话历史互相污染（同一条 WS 连接上切模式时 session_id 不变）。
'''

# sidecar 知识库端点暴露的检索工具名（与 RagflowKnowledgeMcp 的 @McpTool 命名一致）
SEARCH_TOOL_NAME = "ragflow:searchKnowledge"

# 知识库模式的记忆命名前缀：graph_get_history_messages / graph_persist_memory 的 session_id 会拼成 kb-<sessionId>
KB_SESSION_PREFIX = "kb-"

KB_SYSTEM_PROMPT = """你是园区知识库问答助手，专门回答规章制度、管理办法、操作规范、说明文档等知识库类问题。

回答规则：
- 只依据下面给出的【检索结果】回答问题，禁止编造检索结果里没有的内容。
- 引用了哪条内容，就在句末标注来源，格式：（来源：文档名）。
- 如果检索结果为空，或内容里明确说"未检索到相关内容"，直接回答"未在知识库中检索到相关内容，建议换个问法再试试。"，不要再调用任何工具。
- 如果用户问的是在园人数、设备状态这类实时动态数据，说明实时数据请切回"智能体问答"模式查询，知识库只收录文档资料。
- 用中文回答，条理清晰，可直接引用检索结果原文。"""

# 多轮追问改写：知识库检索吃"完整问句"，省略式追问必须先补全才能去检索
CONDENSE_PROMPT = """根据对话历史，把用户最新问题改写成一个不依赖上下文、可直接用于知识库检索的完整问题。

对话历史：
{chat_history}

用户最新问题：{query}

要求：
- 只输出改写后的问题本身，不要回答问题，不要任何解释和前缀。
- 最新问题本身已经完整独立时，原样输出。
- 改写时保留用户的原意，补全省略掉的主语和指代（如"它/那里/第二种情况"具体指什么）。"""


class KbState(TypedDict, total=False):
    query: str                       # 用户原始问题
    history: List[Any]               # 知识库模式的历史对话（kb- 前缀会话）
    search_query: str                # 实际用于检索的问题（可能是改写后的）
    retrieved: str                   # ragflow:searchKnowledge 的返回文本
    final_answer: str                # 最终流式拼出的答案
    kb_user_id: str                  # 透传给记忆存储的真实会话标识
    kb_session_id: str


class KnowledgeQAPipeline:
    '''
    llm: 大模型（app.py 统一创建后传入）
    yaml_path: MCP 配置文件，从中读 knowledge 条目的 url
    检索工具直连 MCP，不走 LLM 选工具：searchKnowledge 的入参就是用户完整问句，
    不需要模型填参，直调比套 ReAct 循环少两次 LLM 往返。
    '''

    def __init__(
        self,
        llm: BaseChatModel,
        kb_client: Optional[KnowledgeMcpClient] = None,
        http_client: Optional[KnowledgeHttpClient] = None,
    ):
        self.llm = llm
        # 两条检索通道，至少有一个（create() 保证）；两个都配时优先 http_client
        self.kb_client = kb_client
        self.http_client = http_client
        # 知识库模式自己的记忆存储（与其他管线共用 persistor，靠 kb- 前缀隔离）
        self.memory_store = MemoryStore()
        self.condense_prompt = PromptTemplate(
            template=CONDENSE_PROMPT,
            input_variables=["chat_history", "query"],
        )
        self.init_graph()

    @classmethod
    def create(cls, llm: BaseChatModel,
               yaml_path: str = "mcp_client/mcp_server_config.yaml") -> "KnowledgeQAPipeline":
        """
        选检索通道，优先级：

        1. **平台检索接口直连**（`mcp_client/knowledge_http_client.py`）—— 配了
           `KB_BASE_URL` + `KB_API_KEY` 就用它：不经 sidecar，排查链路短；
        2. **sidecar `/mcp/knowledge`** —— 没配 HTTP 配置时退回，等价于改造前行为。

        握手/建连都延迟到首次检索（懒连接）：sidecar 未就绪或平台接口暂不可用时，
        管线照常可建，app.py 的连接建立不会被拖慢/搞挂。
        """
        http_client = build_kb_http_client()

        kb_client = None
        kb_conf = (load_mcp_config(yaml_path) or {}).get("knowledge")
        if kb_conf and kb_conf.get("url"):
            kb_client = KnowledgeMcpClient(kb_conf["url"])
            logger.info(f"知识库 MCP 客户端就绪: {kb_conf['url']}")
        elif http_client is None:
            raise ValueError(
                f"知识库两条通道都不可用：KB_BASE_URL/KB_API_KEY 未配置，"
                f"且 yaml 里缺 knowledge 条目或 url（{yaml_path}）"
            )

        if http_client is not None:
            logger.info("知识库检索优先走平台 HTTP 接口（sidecar 作为兜底）")
        return cls(llm, kb_client, http_client)

    # ======================== 节点 ========================

    # @@@ 节点：追问改写。首轮没有历史时直接透传原问题，省一次 LLM 调用
    async def rewrite_node(self, state: KbState, config, runtime, writer: StreamWriter) -> Dict[str, Any]:
        query = state["query"]
        history = state.get("history") or []
        if not history:
            return {"search_query": query}

        chat_history = "\n".join(
            f"{'用户' if isinstance(m, HumanMessage) else '助手'}：{m.content}"
            for m in history[-6:]  # 只带最近3轮，避免撑大上下文
        )
        try:
            # 同步 invoke：改写是轻量调用且后续节点依赖结果，无需流式
            resp = self.llm.invoke(
                ensure_user_turn(
                    [{"role": "system",
                      "content": self.condense_prompt.format(chat_history=chat_history, query=query)}],
                    "请按要求改写问题。",
                ),
                config=config,
            )
            rewritten = (resp.content or "").strip()
            # 改写失败/返回空串时回退原始问题，宁可检索质量差一点也不能不检索
            search_query = rewritten if rewritten else query
        except Exception as e:
            logger.error(f"追问改写失败，使用原始问题检索: {e}")
            search_query = query
        return {"search_query": search_query}

    # @@@ 节点：知识库检索。不经过大模型，直接问知识库
    #
    # 两条通道（见 mcp_client/knowledge_http_client.py）：
    #   ① 平台检索接口直连（配了 KB_BASE_URL + KB_API_KEY 时优先，失败也能看出是谁的问题）
    #   ② sidecar /mcp/knowledge（① 没配或 ① 报错时兜底，等价于改造前的行为）
    async def retrieve_node(self, state: KbState, config, runtime, writer: StreamWriter) -> Dict[str, Any]:
        question = state["search_query"]

        writer({"type": "tool", "content": "知识库检索 - 处理中..."})

        retrieved = None
        channel = ""
        logger.info(
            "[kb] 开始检索: q=%r | 直连=%s | sidecar=%s",
            question[:40],
            "启用" if self.http_client is not None else "未配置",
            "启用" if self.kb_client is not None else "未配置",
        )
        if self.http_client is not None:
            try:
                retrieved = await self.http_client.retrieve(question)
                channel = "http-direct"
                logger.info(
                    "[kb] 直连检索返回: chars=%s%s",
                    len(retrieved or ""),
                    "（空结果）" if retrieved == EMPTY_HINT else "",
                )
            except Exception as e:
                logger.warning(
                    "[kb] 直连检索失败，改用 sidecar 兜底: %s: %s", type(e).__name__, e
                )
                retrieved = None

        if retrieved is None and self.kb_client is not None:
            try:
                # datasetIds 传空串 = 检索 sidecar yml 配置的默认范围（未配置则全库），
                # 语义见 RagflowKnowledgeMcp.searchKnowledge 的注释
                retrieved = await self.kb_client.call_tool(
                    SEARCH_TOOL_NAME, {"question": question, "datasetIds": ""})
                channel = "sidecar"
                logger.info("[kb] sidecar 检索返回: chars=%s", len(retrieved or ""))
            except Exception as e:
                logger.error("[kb] sidecar 检索失败: %s: %s", type(e).__name__, e)
                retrieved = None

        if retrieved is None:
            retrieved = "知识库服务暂时不可用，请稍后重试。"
            channel = "none"
            logger.error("[kb] 两条检索通道都不可用，返回兜底文案")
        else:
            logger.info(
                "[kb] 检索结束: 通道=%s | 空结果=%s | chars=%s",
                channel,
                retrieved == EMPTY_HINT,
                len(retrieved),
            )
        writer({"type": "tool", "content": "知识库检索 - 处理完成"})
        return {"retrieved": retrieved}

    # @@@ 节点：组织答案。流式输出，处理逻辑与 reactive_pipeline.composer_node 一致（含 <think> 过滤）
    async def answer_node(self, state: KbState, config, runtime, writer: StreamWriter) -> Dict[str, Any]:
        query = state["query"]
        retrieved = state.get("retrieved", "")
        history = state.get("history") or []

        messages = (
            [{"role": "system", "content": KB_SYSTEM_PROMPT}]
            + list(history)
            + [{"role": "user",
                "content": f"用户问题：{query}\n\n【检索结果】\n{retrieved}"}]
        )

        final_answer = ""
        # 处理 <think> 标签（让模型可以内部思考后再输出）
        is_think_enabled = False
        is_think_end = False
        async for chunk in self.llm.astream(messages, config=config):
            if chunk.content.find('<think>') != -1:
                is_think_enabled = True
                continue
            if is_think_enabled and chunk.content.find('</think>') != -1:
                is_think_end = True
                continue
            if is_think_end or not is_think_enabled:
                writer({"type": "answer", "content": chunk.content})
                final_answer += chunk.content

        # 保存本轮对话到知识库模式的会话（kb- 前缀，与智能体问答隔离）
        self.memory_store.graph_persist_memory(
            state.get("kb_user_id", ""), KB_SESSION_PREFIX + state.get("kb_session_id", ""),
            [
                {"type": "human", "content": query},
                {"type": "ai", "content": final_answer},
            ],
        )
        return {"final_answer": final_answer}

    # ======================== 图 ========================

    def init_graph(self):
        self.flow_graph = StateGraph(KbState)
        self.flow_graph.add_node("Rewrite", self.rewrite_node)
        self.flow_graph.add_node("Retrieve", self.retrieve_node)
        self.flow_graph.add_node("Answer", self.answer_node)

        self.flow_graph.add_edge(START, "Rewrite")
        self.flow_graph.add_edge("Rewrite", "Retrieve")
        self.flow_graph.add_edge("Retrieve", "Answer")
        self.flow_graph.add_edge("Answer", END)

        # 无中途打断，不需要 checkpointer，历史走 MemoryStore 每轮装载
        self.graph = self.flow_graph.compile()

    '''
    流式调用入口，入参/返回与 InfoDoubleCheckPipeline.astream_run 保持一致，
    app.py 里两套管线用同一段转发代码。
    历史读写统一用 kb- 前缀的会话键，靠 user_id/session_id 之外再隔一层。
    '''
    async def astream_run(self, query: str, user_id: str, session_id: str) -> AsyncIterator[dict]:
        history = self.memory_store.graph_get_history_messages(
            user_id, KB_SESSION_PREFIX + session_id)

        init_state: KbState = {
            "query": query,
            "history": history,
            "kb_user_id": user_id,
            "kb_session_id": session_id,
        }
        async for chunk in self.graph.astream(
            init_state,
            stream_mode="custom",
            config={"configurable": {"thread_id": f"{user_id}|kb-{session_id}"}},
        ):
            # 统一包成 reactive_pipeline 同款事件结构：custom 通道内容给前端
            yield {"event": "custom", "data": chunk}

    async def aclose(self):
        """释放 MCP 直连客户端，WS 会话结束时由 app.py 调用"""
        await self.kb_client.aclose()
