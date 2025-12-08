from langchain_classic.agents import AgentExecutor, create_openai_tools_agent
from langchain.agents import create_agent
from langchain_core.prompts.chat import ChatPromptTemplate, MessagesPlaceholder, SystemMessagePromptTemplate, HumanMessagePromptTemplate
from memory.store import MemoryStore

# 写文件肯定会有并发问题，要改成redis或者数据库持久化历史
memory_store = MemoryStore()

class AgentExecutorWrapper:
    def __init__(self, llm, tools, user_id, session_id, agent_name='custom_agent', system_prompt=None, agent_recursion_limit=10):
        self.llm = llm
        self.tools = tools
        self.user_id = user_id
        self.session_id = session_id
        self.memory_store = memory_store
        self.memory = self.memory_store.get_memory(user_id, session_id)
        self.agent_recursion_limit = agent_recursion_limit
        self.agent_name = agent_name

        # option 1 直接使用agent
        # self.executor = create_agent(model=llm, tools=tools, system_prompt=system_prompt, debug=False, name=self.agent_name)
        
        # option 2 支持history的AgentExecutor
        chat_prompt = ChatPromptTemplate.from_messages([
            SystemMessagePromptTemplate.from_template(template=system_prompt),
            MessagesPlaceholder(variable_name="chat_history", optional=True),
            HumanMessagePromptTemplate.from_template(template="{input}"),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ])
        self.agent = create_openai_tools_agent(llm=llm, tools=tools, prompt=chat_prompt)
        self.executor = AgentExecutor(agent=self.agent, tools=tools, memory=self.memory, verbose=False, name=self.agent_name)

    def run(self, query: str):
        # 同步调用
        output = self.executor.invoke(
            query,
            config={
                "recursion_limit": self.agent_recursion_limit
            }
        )
        return output

    async def stream_run(self, query: str, version="v2"):
        """
        异步流式调用 agent，基于 astream_events
        """
        # inputs = {"messages": [{"role": "user", "content": query}]}
        inputs = {"input": query}
        # 用 astream_events 监听事件流
        async for chunk in self.executor.astream_events(
            inputs, 
            version=version,
            config={
                "recursion_limit": self.agent_recursion_limit
            }):
            # event 是一个 dict，包含 event["event"]，event["data"] 等
            # 可以根据事件类型过滤，只输出 token 或最终答案
            yield chunk