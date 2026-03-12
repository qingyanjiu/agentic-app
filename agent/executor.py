from langchain_classic.agents import AgentExecutor, create_openai_tools_agent
from langchain.agents import create_agent
from langchain_core.prompts.chat import ChatPromptTemplate, MessagesPlaceholder, SystemMessagePromptTemplate, HumanMessagePromptTemplate
from memory.store import MemoryStore
from utils.utils import get_config
from langchain_classic.memory import ConversationBufferMemory, ConversationBufferWindowMemory

# 写文件肯定会有并发问题，要改成redis或者数据库持久化历史
memory_store = MemoryStore()# 创建全局唯一的记忆存储实例
config = get_config()# 从配置文件加载配置
# 在持久化存储中保留最近10轮对话历史
# 例如配置文件中：
# memory_persistor:持久化器（persist + or）
#   memory_buffer_window: 10  # 保留最近10轮对话
memory_buffer_window = config['memory_persistor']['memory_buffer_window']

# persist_memory 是否持久化保存和恢复记忆
class AgentExecutorWrapper:
    def __init__(self, llm, tools, user_id, session_id, agent_name='custom_agent', system_prompt=None, agent_recursion_limit=10, persist_memory=True):
        self.llm = llm
        self.tools = tools
        self.user_id = user_id
        self.session_id = session_id
        self.memory_store = memory_store
        self.persist_memory = persist_memory
        # 如果持久化对话历史，则读取
        if (self.persist_memory is True):
            self.memory = self.memory_store.get_memory(user_id, session_id)
        else:
        # 否则初始化一个空memory对象
            self.memory = ConversationBufferWindowMemory(
                k=memory_buffer_window,
                memory_key="chat_history", 
                return_messages=True
            )
        #递归限制，防止无限循环
        self.agent_recursion_limit = agent_recursion_limit
        self.agent_name = agent_name

        # option 1 直接使用agent
        # self.executor = create_agent(model=llm, tools=tools, system_prompt=system_prompt, debug=False, name=self.agent_name)
        
        # option 2 支持history的AgentExecutor
        #构建 Prompt 模板
        chat_prompt = ChatPromptTemplate.from_messages([
             # 1. 系统提示词（角色设定）
            SystemMessagePromptTemplate.from_template(template=system_prompt),
            # 2. 对话历史占位符（可选
            MessagesPlaceholder(variable_name="chat_history", optional=True),
             # 3. 用户当前输入
            HumanMessagePromptTemplate.from_template(template="{input}"),
             # 4. Agent的思考记录（ReAct模式的中间步骤）
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ])
        # 创建 OpenAI 工具型 Agent,这种 Agent 专门针对 OpenAI 的函数调用功能优化
        self.agent = create_openai_tools_agent(llm=llm, tools=tools, prompt=chat_prompt)
        # 创建执行器
        self.executor = AgentExecutor(agent=self.agent, tools=tools, memory=self.memory, verbose=False, name=self.agent_name)
    
    #同步执行Agent任务的方法，接收用户查询，调用执行器处理，并返回结果。用run能统一接口
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