from typing import TypedDict, Optional, Dict, Any
from langgraph.graph import StateGraph, START, END
from langgraph.types import StreamWriter
from langgraph.runtime import Runtime
from langchain_core.runnables.config import RunnableConfig
from agent.executor import AgentExecutorWrapper
from langchain_core.language_models import BaseChatModel
from agent.info_double_check_prompts import SYSTEM_PROMPT
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

# 绘制langgraph图
def gen_flow_graph(graph):
    graph_png = graph.get_graph().draw_mermaid_png()
    with open("workflow.png", "wb") as f:
        f.write(graph_png)

class MyState(TypedDict, total=False):
    query: str
    evaluator_iter: int
    agent_output: Dict[str, Any]
    eval_decision: str
    final_answer: Dict[str, Any]

class InfoDoubleCheckPipeline:
    '''
    llm: 大模型
    tools: agent要用的工具
    user_id: 用户id，用来持久化对话历史
    session_id: 对话id，用来持久化对话历史
    max_iters: 流程评估不通过时的迭代次数
    use_evaluator: 是否启用评估节点
    enable_debug: 是否开启调试模式，开启调试的话，LLM流式输出会输出额外节点信息
    '''
    def __init__(self, llm: BaseChatModel, tools: CustomTool, user_id: str, session_id: str, use_evaluator = True,  max_iters: int = 3, enable_debug=False):
        self.llm = llm
        self.tools = tools
        # 工具名称和displayName的关联关系，方便前端展示工具调用情况
        self.tool_mapping = DynamicToolGenerator.get_tool_name_mapping(tools)
        self.system_prompt = SYSTEM_PROMPT
        self.user_id = user_id
        self.session_id = session_id
        self.use_evaluator = use_evaluator
        self.max_iters = max_iters
        self.enable_debug = enable_debug
        self.agent_wrapper = AgentExecutorWrapper(
            llm=self.llm,
            tools=self.tools,
            user_id=self.user_id,
            session_id=self.session_id,
            system_prompt=self.system_prompt,
            persist_memory=False
        )

        # 定义图
        flow_graph = StateGraph(MyState)

        # 节点：Agent,处理主要逻辑
        async def agent_node(state: MyState, config: RunnableConfig, runtime: Runtime, writer: StreamWriter) -> MyState:
            logging.info(f"第 {state['evaluator_iter']} 次迭代，处理主要逻辑")
            
            query = state["query"]

            # 如果没超过最大迭代次数，则迭代，否则，啥都不做，也就是使用上一次的结果(不改变 state 中的 agent_output)
            if(state["evaluator_iter"] < self.max_iters):
                ##############################
                # AgentExecutorWrapper 同步 run
                ##############################
                # agent_out = agent_wrapper.run(query)
                # return {"agent_output": agent_out}

                agent_out = ''
                async for chunk in self.agent_wrapper.stream_run(query):
                    event = chunk['event']
                    
                    # 如果是工具节点
                    if(event.find('tool') != -1):
                        # 开启debug的话，就打印所有工具调用信息
                        if(self.enable_debug is True):
                            writer(chunk)
                        # 如果不是debug模式，就只打印工具调用信息
                        elif(self.enable_debug is False):
                            tool_name = chunk.get("name")
                            tool_display_name = self.tool_mapping[tool_name]
                            tool_action = "正在" if event.find('start') != -1 else "已"
                            output = {
                                "type": "tool",
                                "content": f"{tool_action}{tool_display_name}"
                            }
                            writer(output)
                    elif(
                        # 思考链结束事件，且整个agent结束，认为是该节点完成的标志，输出chunk并获取最终的output，写入state
                        event == 'on_chain_end'
                        and chunk['name'] == self.agent_wrapper.agent_name
                    ):
                        agent_out = chunk['data']['output']
                        # 如果开启debug，则输出chunk,否则不输出
                        if(self.enable_debug is True):
                            writer(chunk)
                        
                return {"agent_output": agent_out}
            else:
                return {"agent_output": "达到最大循环次数，未获取到答案"}

        # 节点：Evaluator，评估检索效果，决定是否迭代
        def evaluator_node(state: MyState, config: RunnableConfig, runtime: Runtime) -> MyState:
            logging.info(f"第 {state['evaluator_iter']} 次迭代，评估检索结果")
            agent_out = state["agent_output"]
            agent_out = agent_out['messages'][-1].content
            eval_prompt = f"""
你是评估者 (Evaluator)，请根据你的专业知识，判断以下 Agent 回答是否充分：
用户问题: {state['query']}
Agent 回答: {agent_out}
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
        async def composer_node(state: MyState, config: RunnableConfig, runtime: Runtime, writer: StreamWriter) -> MyState:
            logging.info(f"第 {state['evaluator_iter']} 次迭代，组织最终答案")
            # writer 是 LangGraph 提供的流写工具，可以流式输出自定义数据
            
            # @@@@@@@@@@@@@ 
            # 如果使用评估节点，则获取评估节点评估结果，否则默认是完全充分
            # @@@@@@@@@@@@@
            decision = state["eval_decision"] if self.use_evaluator else '完全充分'
            agent_out = state["agent_output"]
            agent_out = agent_out['messages'][-1].content
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
            # 标签，</think>出现后再输出文本
            is_think_enabled = False
            is_think_end = False
            output_lines_after_think = 0
            async for chunk in self.llm.astream(
                [{"role": "system", "content": composer_prompt}],
                config=config
            ):
                if(chunk.content.find('<think>') != -1):
                    is_think_enabled = True
                    continue
                if(is_think_enabled and chunk.content.find('</think>') != -1):
                    is_think_end = True
                    continue
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
            return {"final_answer": final_answer}


        flow_graph.add_node("Agent", agent_node)
        
        # @@@@@@@@@@@@@ 
        # 如果使用评估节点，增加评估节点
        # @@@@@@@@@@@@@
        if (self.use_evaluator):
            flow_graph.add_node("Evaluator", evaluator_node)
            
        flow_graph.add_node("Composer", composer_node)

        # 添加边：控制流程
        # START → Agent
        flow_graph.add_edge(START, "Agent")
        
        # @@@@@@@@@@@@@ 
        # 如果使用评估节点,agent节点到评估节点连线
        # 如果使用评估节点,agent节点到评估节点连线
        # @@@@@@@@@@@@@
        if (self.use_evaluator):
            # Agent → Evaluator
            flow_graph.add_edge("Agent", "Evaluator")
        # @@@@@@@@@@@@@ 
        # 如果不使用评估节点,agent节点到总结节点连线
        # @@@@@@@@@@@@@
        else:
            flow_graph.add_edge("Agent", "Composer")
        
        # @@@@@@@@@@@@@ 
        # 如果使用评估节点, 添加评估节点条件边
        # @@@@@@@@@@@@@
        if (self.use_evaluator):
            # 路径分支逻辑
            # 如果充分 Evaluator → Composer 
            # 不充分 Evaluator → Agent
            def should_redo_rag_after_evaluation(state: MyState) -> str:
                """
                判断是否需要重新执行 RAG 步骤
                """            
                logging.info("路由判断 state:", state)
                if(state["eval_decision"] == "不充分"):
                    return "redo_rag"
                elif(state["eval_decision"] in ("完全充分", "基本充分")):
                    return "do_compose"
            # 添加条件边
            flow_graph.add_conditional_edges(
                "Evaluator",
                should_redo_rag_after_evaluation,
                {
                    "redo_rag": "Agent",
                    "do_compose": "Composer"
                }
            )
        
        # Composer → END
        flow_graph.add_edge("Composer", END)
        # 编译图
        self.graph = flow_graph.compile()
        # gen_flow_graph(self.graph)

    '''
    流式调用langgraph，流式返回最终节点数据
    数据格式 {"query": "用户问题", "sessionId": "对话id"}
    '''
    async def astream_run(self, query: str):
        # 初始 state
        # evaluator_iter: 评估节点迭代次数，不能超过 max_iters
        init_state: MyState = {"query": query, "evaluator_iter": 0}
        # 异步执行，流式输出
        async for mode, chunk in self.graph.astream(
            init_state,
            stream_mode=["updates", "messages", "custom"]
        ):
            if mode == "messages":
                # chunk 是 AIMessageChunk
                ai_chunk, info = chunk
                yield {
                    "event": "token",
                    "text": ai_chunk.content,
                    "node": info["langgraph_node"]
                }
            elif mode == "updates":
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
            elif mode == "custom":
                # chunk 是 writer() 推出的自定义内容
                yield {
                    "event": "custom",
                    "data": chunk
                }
        
        # 如果持久化对话历史，则持久化
        if (self.agent_wrapper.persist_memory is True):
            self.agent_wrapper.memory_store.persist_memory(self.user_id, self.session_id)