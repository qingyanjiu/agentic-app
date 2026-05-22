"""
MCP Skill Agent — 单 Agent 智能调用 MCP 工具

核心能力：
  1. 自动根据用户自然语言描述判断调用哪个 MCP 工具
  2. 自动从用户描述中提取查询参数
  3. 连续多轮对话（纯内存，不持久化）
  4. 流式输出工具调用过程和结果

用法：
    agent = McpSkillAgent(llm, mcp_yaml_path="mcp_client/mcp_server_config.yaml")
    await agent.initialize()
    async for event in agent.stream_chat("查一下服务器负载"):
        print(event)
"""

from langchain.agents import AgentExecutor, create_openai_tools_agent
from langchain_core.language_models import BaseLanguageModel
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_classic.memory import ConversationBufferWindowMemory
from mcp_client.mcp_loader import get_mcp_tools
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

TODAY = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

SYSTEM_PROMPT = f"""你是一个智能查询助手，可以根据用户的描述自动完成以下步骤：

1.  理解用户想查询什么信息
2.  从可用工具中选择最合适的工具
3.  从用户描述中提取查询所需的参数
4.  调用工具获取结果
5.  用自然语言把结果整理给用户

相对日期处理规则（重要）：
- 当前日期时间：{TODAY}
- 当用户提到"今天"、"昨天"、"上个月"、"最近N天/月/年"、"过去N年"等相对时间时，必须基于当前日期计算出具体的起止日期。
- 如果目标工具的参数包含 startDate/endDate（或类似的开始/结束时间参数），务必按以下方式处理：
  1. 先计算出准确的起止日期，格式为 YYYY-MM-DD
  2. 再将计算后的具体日期填入工具参数
  3. 示例：用户说"过去两年"，今天是 2026-05-22，则 startDate="2024-05-22"，endDate="2026-05-22"
- 可用工具列表中可能包含日历/时间工具，必要时可先调用它确认当前日期。

要求：
- 调用工具之前，确保参数完整。缺少必要参数时主动询问用户补充。
- 如果用户描述中包含了参数信息，直接提取使用，不要反问用户。
- 如果工具调用失败，告知用户失败原因。
- 用中文回答用户。"""


class McpSkillAgent:
    """MCP 技能 Agent — 单 Agent、纯内存、自动判断工具调用。"""

    def __init__(
        self,
        llm: BaseLanguageModel,
        mcp_yaml_path: str = "mcp_client/mcp_server_config.yaml",
        *,
        system_prompt: str = None,
        memory_window: int = 10,
        recursion_limit: int = 15,
    ):
        self.llm = llm
        self.mcp_yaml_path = mcp_yaml_path
        self.system_prompt = system_prompt or SYSTEM_PROMPT
        self.recursion_limit = recursion_limit

        self.tools = []
        self.executor: AgentExecutor | None = None
        self._initialized = False

        # 纯内存记忆，不持久化
        self.memory = ConversationBufferWindowMemory(
            k=memory_window,
            memory_key="chat_history",
            return_messages=True,
        )

    # ── 初始化 ──

    async def initialize(self):
        """连接 MCP 服务器并创建 Agent 执行器。"""
        try:
            mcp_tools = await get_mcp_tools(self.mcp_yaml_path)
            self.tools.extend(mcp_tools)
            logger.info("MCP 技能 Agent 已加载 %d 个工具", len(mcp_tools))
        except Exception as e:
            logger.error("MCP 工具加载失败: %s", e)
            raise

        prompt = ChatPromptTemplate.from_messages([
            ("system", self.system_prompt),
            MessagesPlaceholder(variable_name="chat_history", optional=True),
            ("human", "{input}"),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ])

        agent = create_openai_tools_agent(self.llm, self.tools, prompt)
        self.executor = AgentExecutor(
            agent=agent,
            tools=self.tools,
            memory=self.memory,
            verbose=False,
        )
        self._initialized = True

    def _check_initialized(self):
        if not self._initialized:
            raise RuntimeError("Agent 未初始化，请先调用 await agent.initialize()")

    # ── 对话接口 ──

    async def achat(self, query: str) -> str:
        """异步对话，返回文本答案。"""
        self._check_initialized()
        result = await self.executor.ainvoke(
            {"input": query},
            config={"recursion_limit": self.recursion_limit},
        )
        return result["output"]

    async def stream_chat(self, query: str):
        """
        异步流式对话，产出结构化事件字典，适合 WebSocket 转发。

        产出的事件格式：
          {"type": "token",      "content": "..."}          # LLM token 流
          {"type": "tool_start", "tool": "...", "args": {}}  # 开始调用工具
          {"type": "tool_end",   "tool": "...", "result": "..."}  # 工具返回
          {"type": "answer",     "content": "..."}           # 最终答案
          {"type": "error",      "message": "..."}           # 错误
        """
        self._check_initialized()
        try:
            async for chunk in self.executor.astream_events(
                {"input": query},
                version="v2",
                config={"recursion_limit": self.recursion_limit},
            ):
                event = chunk["event"]
                name = chunk.get("name", "")

                # ── LLM token 流 ──
                if event == "on_chat_model_stream":
                    content = chunk["data"]["chunk"].content
                    if content:
                        yield {"type": "token", "content": content}

                # ── 工具调用开始 ──
                elif event == "on_tool_start":
                    yield {
                        "type": "tool_start",
                        "tool": name,
                        "args": chunk["data"].get("input", {}),
                    }

                # ── 工具调用结束 ──
                elif event == "on_tool_end":
                    yield {
                        "type": "tool_end",
                        "tool": name,
                        "result": chunk["data"].get("output", ""),
                    }

                # ── Agent 最终完成 ──
                elif event == "on_chain_end" and name == self.executor.name:
                    output = chunk["data"].get("output", "")
                    yield {"type": "answer", "content": output}

        except Exception as e:
            logger.exception("Agent 执行出错")
            yield {"type": "error", "message": str(e)}

    # ── 工具方法 ──

    def clear_memory(self):
        """清空当前对话历史。"""
        self.memory.clear()

    @property
    def chat_history(self):
        """当前对话历史消息列表。"""
        return self.memory.chat_memory.messages

    @property
    def available_tools(self) -> list[str]:
        """当前可用工具名列表。"""
        return [t.name for t in self.tools]
