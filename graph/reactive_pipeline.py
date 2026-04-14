import json
import time
from langchain.messages import AnyMessage
from langchain_classic.schema import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langchain_classic.chains.llm import LLMChain
from langchain_core.prompts import PromptTemplate
from typing import Annotated, Callable, TypedDict, Optional, Dict, Any
from langgraph.graph import StateGraph, START, END, add_messages
from langgraph.types import StreamWriter
from langgraph.runtime import Runtime
from langchain_core.runnables.config import RunnableConfig
from langchain_core.prompts import ChatPromptTemplate
from agent.executor import AgentExecutorWrapper
from langchain_core.language_models import BaseChatModel
from agent.tool_implement_main_prompt import gen_prompt
from agent.get_intent_and_select_tools_prompt import SYSTEM_PROMPT as get_intent_and_select_tools_prompt
from agent.ask_for_param_prompt import SYSTEM_PROMPT as ask_for_param_prompt
import logging

from dynamic_tools.dynamic_tool_generator import DynamicToolGenerator
from tools.custom_tool import CustomTool

'''
带交互的API查询流程
'''

logging.basicConfig(
    filename='app.log',
    # 追加模式 'a'，覆盖模式 'w' 
    filemode='w',
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
)

class MyState(TypedDict, total=False):
    query: str
    # 已获取的工具参数
    params_got: list
    # 用户意图概述
    intent_desc: str
    # 用户意图获取结果,用于识别无效问题，直接告知无法回答
    intent_get_result: str
    # 缺失的工具参数
    missing_params: list
    evaluator_iter: int
    agent_output: Dict[str, Any]
    # 消息历史
    messages: Annotated[list[AnyMessage], add_messages]
    last_turn: list[dict]
    eval_decision: str
    final_answer: Dict[str, Any]

class InfoDoubleCheckPipeline:
    '''
    llm: 大模型
    tools: agent要用的工具
    user_id: 用户id，用来持久化对话历史
    session_id: 对话id，用来持久化对话历史
    main_node_system_prompt: 主prompt，占位参数，可以不传，类初始化时会处理
    max_iters: 流程评估不通过时的迭代次数
    use_evaluator: 是否启用评估节点
    enable_debug: 是否开启调试模式，开启调试的话，LLM流式输出会输出额外节点信息
    '''
    '''
    这个函数的功能是：
    用户输入："帮我查一下天气"
    1. 意图识别Agent：判断需要天气工具，但缺少city参数
    2. 状态转移：进入参数补充状态
    3. 参数补充Agent："请问您想查询哪个城市的天气？"
    4. 用户："北京的"
    5. 主Agent执行：调用get_weather("北京")
    6. 返回结果："北京今天晴，15-25℃"'''
    def __init__(self, llm: BaseChatModel, tools: CustomTool, user_id: str, session_id: str, main_node_system_prompt = '', use_evaluator = True,  max_iters: int = 3, enable_debug=False):
        self.llm = llm
        self.tools = tools
        # 工具名称和displayName的关联关系，方便前端展示工具调用情况
        self.tool_mapping = DynamicToolGenerator.get_tool_name_mapping(tools)
        #生成工具的JSON Schema描述，包含：工具名称、功能描述\参数列表（参数名、类型、是否必填、描述）返回值的格式说明
        self.tool_json_desc = DynamicToolGenerator.get_tool_json_desc(tools)
        self.user_id = user_id
        self.session_id = session_id
        self.use_evaluator = use_evaluator#是否启用结果评估器，用于判断主任务执行结果是否满足需求
        self.max_iters = max_iters#最大迭代次数，防止无限循环（如工具调用失败时的重试）
        self.enable_debug = enable_debug#调试模式开关，控制日志详细程度
        self.persist_memory = True
        
        # 判断用户输入问题调用工具的关键词是否完整，
        self.get_intent_and_select_tools_prompt = PromptTemplate(
            template=get_intent_and_select_tools_prompt,
            input_variables=["input","params_got","tool_json_desc","intent_desc"]
        )
        self.get_intent_and_select_tools_agent = LLMChain(llm=llm, prompt=self.get_intent_and_select_tools_prompt)
        
        # 当工具参数不完整时，引导用户补充缺失信息
        self.ask_for_param_prompt = PromptTemplate(
            template=ask_for_param_prompt,
            input_variables=["input","params_got"]
        )
        self.ask_for_param_agent = LLMChain(llm=llm, prompt=self.ask_for_param_prompt)
        
        self.main_node_system_prompt = main_node_system_prompt
        # 主逻辑agent智能体
        self.main_agent = AgentExecutorWrapper(
            llm=self.llm,
            tools=self.tools,
            user_id=self.user_id,
            session_id=self.session_id,
            system_prompt=self.main_node_system_prompt,
            persist_memory=self.persist_memory
        )
        # 初始化图
        self.init_graph()
    
    @classmethod
    async def create(cls, llm: BaseChatModel, tools: CustomTool,user_id: str, session_id: str, main_node_system_prompt='', use_evaluator = True,  max_iters: int = 3, enable_debug=False):
        main_node_system_prompt = await gen_prompt()# 异步生成提示词
        return cls(llm, tools, user_id, session_id, main_node_system_prompt, use_evaluator, max_iters, enable_debug)
        
    # 模拟流式输出,将完整的文本分块、延时发送，模拟大模型生成内容时的流式输出效果。
    def fake_stream(
        self,
        text: str,# 要模拟输出的完整文本
        writer: StreamWriter,# 写入器（回调函数），用于发送每个数据块
        step=2,# 每次发送的字符数（块大小）
        delay: float = 0.05,# 块之间的延迟时间（秒）
    ):
        for i in range(0, len(text), step): # 按步长遍历文本
            output = {"type": "answer", "content": text[i: i+step]} # 构造数据块
            writer(output) # 发送当前块
            time.sleep(delay) # 等待，模拟生成时间
            
    # 流程图可视化工具:绘制langgraph图,将 LangGraph 的状态图（StateGraph）生成为 PNG 格式的流程图图片并保存到本地文件。
    def gen_flow_graph(self, graph):
        graph_png = graph.get_graph().draw_mermaid_png()
        with open("chat-agent-workflow.png", "wb") as f:
            f.write(graph_png)
    # @@@ 节点：用户意图识别，要求用户补全所需参数
    def intent_get_node(self, state: MyState, config: RunnableConfig, runtime: Runtime, writer: StreamWriter) -> MyState:
        logging.info(f"第 {state['evaluator_iter']} 次迭代，获取用户意图")
        query = state["query"]
        params_got = state.get("params_got", [])
        intent_desc = state.get("intent_desc", "")
        inputs = {
            "input": query,                    # 用户问题
            "tool_json_desc": self.tool_json_desc,  # 可用工具描述
            "params_got": params_got,           # 已获取的参数
            "intent_desc": intent_desc          # 历史意图描述
        }
        ## 调用判断用户输入问题调用工具的关键词是否完整的agent，
        result = self.get_intent_and_select_tools_agent.invoke(inputs)
        result = json.loads(result.get('text', '{}'))#result.get('text', {}) - 获取文本内容,json.loads(...) - JSON解析
        # 3. 解析结果
        intent_get_result = result.get('intent_get_result', {})
        if intent_get_result == '无法回答':
            output = {
                "type": "intent_desc",
                "content": f"之前的用户意图是：{intent_desc}，最新的用户输入是：{query}"
            }
            writer(output)
            return {
                "intent_get_result": intent_get_result
            }      
            
        intent_desc = result.get('intent_desc', {})
        if(intent_get_result):
            missing_params = intent_get_result.get("missing_params", [])
            params_got = intent_get_result.get("params_got", [])
            output = {
                "type": "intent_desc",
                "content": intent_desc
            }
            # 写入返回数据
            writer(output)
            return {
                "missing_params": missing_params, 
                "params_got": params_got, 
                "intent_desc": intent_desc
            }
        else:
            logging.error(f"意图识别节点出错：返回数据为空:{result}")
        
    # @@@ 参数询问节点：当参数不足时，向用户询问缺失信息
    def ask_for_param_node(self, state: MyState, config: RunnableConfig, runtime: Runtime, writer: StreamWriter) -> MyState:
        query = state.get("query", "")
        missing_params = state.get("missing_params", [])
        intent_get_result = state.get("intent_get_result", "")
        intent_desc = state.get("intent_desc", "")
        
        # 如果是无法回答，直接就结束了
        if intent_get_result == '无法回答':
            output = {
                "type": "answer",
                "content": "对不起，我目前掌握的能力无法解决你的问题"
            }
            writer(output)
            return {
                "intent_get_result": ""
            }
        # 2. 生成询问问题
        inputs = {
            "input": query, 
            "intent_desc": intent_desc,
            "missing_params": missing_params
        }
        # 调用当工具参数不完整时，引导用户补充缺失信息的agent
        content = self.ask_for_param_agent.invoke(inputs)
        content = content.get('text')
        
        # 3. 模拟流式输出（让用户感觉像真人打字）
        self.fake_stream(content, writer)
        
    # @@@ 核心执行节点，调用工具完成任务：调用工具的Agent,处理主要逻辑
    async def tool_agent_node(self, state: MyState, config: RunnableConfig, runtime: Runtime, writer: StreamWriter) -> MyState:
        logging.info(f"第 {state['evaluator_iter']} 次迭代，处理主要逻辑")
        query = state.get("query", "") # 用户原始问题
        # 分析的解决问题的逻辑
        intent_desc = state.get("intent_desc", "") # 意图描述
         # 已获取的参数
        params_got = state.get("params_got", [])
        # 构建输入：将参数和意图组合成JSON字符串
        input = f'{{"params_got": {params_got}, "intent_desc": {intent_desc}}}'
        input_str = json.dumps(input)

        # 如果没超过最大迭代次数，则迭代，否则，啥都不做，也就是使用上一次的结果(不改变 state 中的 agent_output)
        if(state["evaluator_iter"] < self.max_iters):
            ##############################
            # AgentExecutorWrapper 同步 run
            ##############################
            # agent_out = agent_wrapper.run(query)
            # return {"agent_output": agent_out}

            agent_out = ''
             # 2. 流式执行Agent
            async for chunk in self.main_agent.stream_run(input_str):
                event = chunk['event']
                
                # 如果是工具节点
                if(event.find('tool') != -1):
                    # 开启debug的话，就打印所有工具调用信息
                    if(self.enable_debug is True):
                        writer(chunk)
                    # 如果不是debug模式，就只打印工具调用信息
                    elif(self.enable_debug is False):
                        # 生产模式：输出友好的工具调用提示
                        tool_name = chunk.get("name")
                        tool_display_name = self.tool_mapping[tool_name]
                        tool_action = "处理中..." if event.find('start') != -1 else "处理完成"
                        output = {
                            "type": "tool",
                            "content": f"{tool_display_name} - {tool_action}"
                        }
                        writer(output)

                # 4. 捕获最终输出
                elif(
                    # 思考链结束事件，且整个agent结束，认为是该节点完成的标志，输出chunk并获取最终的output，写入state
                    event == 'on_chain_end'
                    and chunk['name'] == self.main_agent.agent_name
                ):
                    agent_out = chunk['data']['output']# 获取最终输出         
                    # 如果开启debug，则输出chunk,否则不输出
                    if(self.enable_debug is True):
                        writer(chunk)
            return {
                    "agent_output": agent_out['output'],
                    "messages": [
                        HumanMessage(content=query),
                        AIMessage(content=agent_out['output']),
                    ],
                    "last_turn": [
                        {"type": "human", "content": query},
                        {"type": "ai", "content": agent_out['output']},
                    ]
                }
        else:
            return {"agent_output": "达到最大循环次数，未获取到答案"}

    # @@@ 节点：Evaluator，评估检索效果，决定是否迭代
    def evaluator_node(self, state: MyState, config: RunnableConfig, runtime: Runtime) -> MyState:
        logging.info(f"第 {state['evaluator_iter']} 次迭代，评估检索结果")
        agent_out = state["agent_output"]
        agent_out = agent_out['messages'][-1].content
        eval_prompt = f"""
你是评估者 (Evaluator)，请根据你的专业知识，判断以下 MainAgent的 回答是否充分：
用户问题: {state['query']}
MainAgent 回答: {agent_out}
返回 "完全充分"、"基本充分" 或 "不充分"。
注意：
- 返回只能是 "完全充分","基本充分" 或 "不充分" 其中的一个
"""
        # 调用 LLM，这里用同步invoke，因为会直接返回结果，否则要拼装chunk，不稳定
        resp = self.llm.invoke(
            [{"role": "system", "content": eval_prompt}],
            config=config
        )
        
        decision = resp.content.strip()
        logging.info(f"评估结果：{decision}")
        return {
            "eval_decision": decision,
            # 增加迭代计数
            "evaluator_iter": state["evaluator_iter"] + 1
        }

    # 节点：Composer，组织最终答案并返回给用户
    async def composer_node(self, state: MyState, config: RunnableConfig, runtime: Runtime, writer: StreamWriter) -> MyState:
        logging.info(f"第 {state['evaluator_iter']} 次迭代，组织最终答案")
        # writer 是 LangGraph 提供的流写工具，可以流式输出自定义数据
        
        # @@@@@@@@@@@@@ 
        # 如果使用评估节点，则获取评估节点评估结果，否则默认是完全充分
        # @@@@@@@@@@@@@
        decision = state["eval_decision"] if self.use_evaluator else '完全充分'
        agent_out = state.get("agent_output", '')
        composer_prompt = f"""
你是 Answer Composer，请基于聚合结果生成回答：
用户问题：{state['query']}
检索结果：
{agent_out}
充分性评价：{decision}
输出自然语言答案。
注意：
- 输出的答案必须基于检索结果，不能重复。
- 输出的答案必须基于用户问题，不能重复。
- 如果充分性评价是基本充分，请根据你的判断大概说明一下不完全充分的可能原因。
- 如果充分性评价是完全充分，就不要添加任何关于充分性评价的内容。
"""
        final_answer = ""
        # LLM 调用
        # 处理 <think> 标签（让模型可以内部思考后再输出）
        is_think_enabled = False # 是否进入了思考模式
        is_think_end = False# 思考是否结束
        output_lines_after_think = 0# 思考后输出的行数计数
        async for chunk in self.llm.astream( # 流式接收LLM的输出
            [{"role": "system", "content": composer_prompt}],
            config=config
        ):
        # 1. 检测思考开始标签
            if(chunk.content.find('<think>') != -1):
                is_think_enabled = True
                continue
            # 2. 检测思考结束标签 
            if(is_think_enabled and chunk.content.find('</think>') != -1):
                is_think_end = True
                continue
            # 3. 思考结束后才开始输出
            # 如果开启了think，则等think结束后输出文本，如果没开启think，直接输出文本
            if(is_think_end or not is_think_enabled):
                # 忽略可能出现在第一行的空行
                if (output_lines_after_think == 0 and chunk.content.strip() == ''):
                    continue
                else:
                    output_lines_after_think += 1
                    # 流式写最终 result，给个type是answer方便前端判断最终结果的流式响应
                    writer({"type": "answer", "content": chunk.content})
                final_answer += chunk.content
        # 保存最新一轮对话
        if self.persist_memory == True:
            last_turn_msg = state.get('last_turn', [])
            self.main_agent.memory_store.graph_persist_memory(self.user_id, self.session_id, last_turn_msg)
        
        return {"final_answer": final_answer}

    # 边条件，参数不足时找用户提供，参数足够时直接执行主agent逻辑
    def should_ask_for_param(self, state: MyState) -> str:
        missing_params = state.get('missing_params', [])
        intent_get_result = state.get('intent_get_result', '')
        # 情况1：有缺失参数 → 去询问用户
        if missing_params is not None and len(missing_params) > 0:
            return "ask_for_param"
        else:
            # 情况2：无法回答 → 也去询问（实际上是告知无法回答）
            if intent_get_result == '无法回答':
                return "ask_for_param"
            # 情况3：参数齐全 → 直接执行主逻辑
            else:
                return "to_main_agent"

    # 路径分支逻辑
    # 如果充分 Evaluator → Composer 
    # 不充分 Evaluator → MainAgent
    def should_redo_rag_after_evaluation(self, state: MyState) -> str:
        """
        判断是否需要重新执行 RAG 步骤
        """            
        logging.info("路由判断 state:", state)
        if(state["eval_decision"] == "不充分"):
            return "redo_rag"# 回答不充分，需要重试
        elif(state["eval_decision"] in ("完全充分", "基本充分")):
            return "do_compose"# 回答充分，可以合成最终答案

    '''
    初始化graph
    '''
    def init_graph(self):
        # @@@@@ 定义图
        self.flow_graph = StateGraph(MyState)
        '''
        @@@@@@@ 定义节点
        '''
        self.flow_graph.add_node("IntentGet", self.intent_get_node)# 意图识别
        
        self.flow_graph.add_node("AskForParam", self.ask_for_param_node)# 参数询问

        self.flow_graph.add_node("MainAgent", self.tool_agent_node)# 主执行
        
        # @@@@@@@@@@@@@ 
        # 如果使用评估节点，增加评估节点
        # @@@@@@@@@@@@@
        if (self.use_evaluator):
            self.flow_graph.add_node("Evaluator", self.evaluator_node)
            
        self.flow_graph.add_node("Composer", self.composer_node)

        '''
        @@@@@@ 定义边
        '''
        # 添加边：控制流程
        # START → IntentAgent
        self.flow_graph.add_edge(START, "IntentGet")
        
        # 添加条件边 意图识别后可能找用户提供更多参数，也可能直接执行下一步逻辑
        self.flow_graph.add_conditional_edges(
            "IntentGet",
            self.should_ask_for_param,
            {
                "ask_for_param": "AskForParam",# 需要参数 → 询问节点
                "to_main_agent": "MainAgent" # 参数齐全 → 主执行节点
            }
        )
        
        # @@@@@@@@@@@@@ 
        # 如果使用评估节点,agent节点到评估节点连线
        # 如果使用评估节点,agent节点到评估节点连线
        # @@@@@@@@@@@@@
        if (self.use_evaluator):
             # 有评估器：主Agent → 评估器
            # MainAgent → Evaluator
            self.flow_graph.add_edge("MainAgent", "Evaluator")
        # @@@@@@@@@@@@@ 
        # 如果不使用评估节点,agent节点到总结节点连线
        # @@@@@@@@@@@@@
        else:
            # 无评估器：主Agent → 合成器
            self.flow_graph.add_edge("MainAgent", "Composer")
        
        # @@@@@@@@@@@@@ 
        # 如果使用评估节点, 添加评估节点条件边
        # @@@@@@@@@@@@@
        if (self.use_evaluator):
            # 添加条件边
            self.flow_graph.add_conditional_edges(
                "Evaluator",
                self.should_redo_rag_after_evaluation,
                {
                    "redo_rag": "MainAgent",# 不充分 → 重试
                    "do_compose": "Composer"# 充分 → 合成答案
                }
            )
        # Composer → END
        self.flow_graph.add_edge("Composer", END)
        
        '''
        @@@@ 编译图
        '''
        # 使用内存checkpointer
        self.checkpointer = MemorySaver() # 内存检查点，保存状态
        # 用这个 checkpointer 编译 graph
        self.graph = self.flow_graph.compile(checkpointer=self.checkpointer)

        '''
        @@@@@@@ 绘制图
        '''
        # self.gen_flow_graph(self.graph)

    '''
    流式调用langgraph，流式返回最终节点数据
    数据格式 {"query": "用户问题", "sessionId": "对话id"}
    '''
    async def astream_run(self, query: str, user_id: str, session_id: str):
        thread_id = f'{user_id}|{session_id}'        
        # 初始 state
        # evaluator_iter: 评估节点迭代次数，不能超过 max_iters
        init_state: MyState = {
            "query": query, 
            "evaluator_iter": 0
        }
        
        config={
            "configurable": {
                "thread_id": thread_id
            }
        }
        
        if (self.persist_memory is True):
            snapshot = await self.graph.aget_state(config)
            has_checkpoint = bool(
                snapshot
                and snapshot.values
                and snapshot.values.get("messages")
            )
            # 看是否有记忆如果没有，说明是新拉起来的对话，需要查询库中的历史对话加载
            if not has_checkpoint:
                # 读取已经落库的聊天记录
                history_messages = self.main_agent.memory_store.graph_get_history_messages(user_id, session_id)
                # 冷启动时：历史 + 当前 query 一次性放进 messages
                init_state['messages'] = history_messages + [HumanMessage(content=query)]

        
            
        # 异步执行，流式输出
        async for mode, chunk in self.graph.astream(
            init_state,
            stream_mode=["updates", "messages", "custom"],
            config=config
        ):
        # 把不同类型的数据分发给前端的不同处理逻辑
            if mode == "messages":
                 # 通道1：LLM生成的文字 → 显示在对话气泡中
                # chunk 是 AIMessageChunk
                ai_chunk, info = chunk
                yield {
                    "event": "token",
                    "text": ai_chunk.content,
                    "node": info["langgraph_node"]
                }
            # 处理状态更新（调试用）
            elif mode == "updates":
                 # 通道3：调试信息 → 显示在开发者工具中
                # chunk 是 state 变化,其实是不需要的
                # 调试模式打印出来
                if (self.enable_debug is True):
                    yield {
                        "event": "state_update",
                        "data": chunk
                    }
                # 非调试模式，不打印信息
                elif (self.enable_debug is False):
                    continue
            # 处理自定义输出
            elif mode == "custom":
                 # 通道2：系统状态 → 显示在进度条或状态栏
                # chunk 是 writer() 推出的自定义内容
                yield {
                    "event": "custom",
                    "data": chunk
                }
