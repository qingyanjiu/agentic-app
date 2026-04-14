import json
import time
from langgraph.checkpoint.memory import MemorySaver
from typing import TypedDict, Optional, Dict, Any, List
from langgraph.graph import StateGraph, START, END
from langgraph.types import StreamWriter
from langgraph.runtime import Runtime
from langchain_core.runnables.config import RunnableConfig
from langchain_core.language_models import BaseChatModel

import logging
from dynamic_tools.dynamic_tool_generator import DynamicToolGenerator
from tools.custom_tool import CustomTool
from agent.executor import AgentExecutorWrapper
# from doc_tools.word_generator import CustomWordGenerator
from doc_tools.word_gen_json import CustomWordGenerator
# 自定义模块导入
from agent.doc_gen_prompt.normal_prompt import normal_prompt
# 各章节/维度专属Prompt
from agent.doc_gen_prompt.dimension_analysis.get_security_analysis_prompt import get_prompt as gen_security_para_prompt
from agent.doc_gen_prompt.dimension_analysis.get_energy_analysis_prompt import get_prompt as gen_energy_para_prompt
from agent.doc_gen_prompt.dimension_analysis.get_operation_analysis_prompt import get_prompt as gen_operation_para_prompt
from agent.doc_gen_prompt.get_report_overview_prompt import get_prompt as gen_report_overview_prompt  # 新增
from agent.doc_gen_prompt.get_executive_summary_prompt import get_prompt as gen_exec_summary_prompt      # 新增
from agent.doc_gen_prompt.get_operation_metrics_prompt import get_prompt as gen_operation_metrics_prompt  # 新增
from agent.doc_gen_prompt.get_comprehensive_evaluation_prompt import get_prompt as gen_comprehensive_evaluation_prompt  # 新增
from agent.doc_gen_prompt.get_improvement_suggestions_prompt import get_prompt as gen_improvement_suggestions_prompt  # 新增


# ===================== 核心配置（可配置化章节） =====================
# 节点对应章节配置（支持动态增删改）
# 每个配置项说明：
# - name: 章节名称（对应节点逻辑）
# - index: 章节序号（用于Word文档标题）
# - max_words: 章节最大生成字数限制
# - prompt_func: 章节专属Prompt生成函数（None表示特殊处理）
CHAPTER_CONFIG = [
    {"name": "报告概述","index":"一、", "max_words": 500, "prompt_func": gen_report_overview_prompt},
    {"name": "执行摘要","index":"二、", "max_words": 800, "prompt_func": gen_exec_summary_prompt},
    {"name": "园区运营总览指标","index":"三、", "max_words": 1000, "prompt_func": gen_operation_metrics_prompt},
    {"name": "维度深度分析","index":"四、", "max_words": 1500, "prompt_func": None},  # 特殊处理子维度
    {"name": "综合评估","index":"五、", "max_words": 1000, "prompt_func": gen_comprehensive_evaluation_prompt},
    {"name": "改进建议","index":"六、", "max_words": 800, "prompt_func": gen_improvement_suggestions_prompt},
]

# 维度数据映射
DIMENSION_DATA_MAPPING= {
    "安防": "security",
    "能耗": "energy",
    "运营": "operation"
}

# 维度Prompt映射（维度深度分析专用）
DIMENSION_PROMPT_MAPPING = {
    "安防": gen_security_para_prompt,
    "能耗": gen_energy_para_prompt,
    "运营": gen_operation_para_prompt
}
logging.basicConfig(
    filename='app.log',
    # 追加模式 'a'，覆盖模式 'w' 
    filemode='w',
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
)

class MyState(TypedDict, total=False):
    # 基础字段
    query: str
    evaluator_iter: int
    agent_output: Dict[str, Any]
    eval_decision: str
    # 维度内容
    security_paragraph: str
    energy_paragraph: str
    operation_paragraph: str

    # 文档内容
    full_doc_content: str
    current_chapter: str          # 当前生成章节
    chapter_order: List[str]      # 章节顺序
    completed_chapters: List[str] # 已完成章节
    # 业务字段
    company: str
    start_date: str
    end_date: str
    file_path: str                # Word文件路径
    dimension: List[str]                # 当前分析维度
    dimension_data:str
    # Word生成器（复用避免重复初始化）
    word_generator: CustomWordGenerator
    # 样式配置（JSON字符串）
    config_style: str  
    # 章节内容缓存
    chapter_contents: Dict[str, str]
  

class GenDocPipeline:
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
    def __init__(self, llm: BaseChatModel, tools: CustomTool, user_id: str, session_id: str, main_node_system_prompt = '',   max_iters: int = 3, enable_debug=False, chapter_order: List[str] = None):
        self.llm = llm
        self.tools = tools
        # 工具名称和displayName的关联关系，方便前端展示工具调用情况
        self.tool_mapping = DynamicToolGenerator.get_tool_name_mapping(tools)
        self.tool_json_desc = DynamicToolGenerator.get_tool_json_desc(tools)
        self.user_id = user_id
        self.session_id = session_id
        self.max_iters = max_iters
        self.enable_debug = enable_debug
         # 章节顺序（默认使用配置顺序，支持自定义）
        self.chapter_order = chapter_order or [chap["name"] for chap in CHAPTER_CONFIG]
        self.main_node_system_prompt = main_node_system_prompt
        # 初始化 Word 生成器（每个实例一个）
        self.word_generator = CustomWordGenerator()
        self.max_retries_per_chapter = 2  # 每个章节最多重试2次
        # 主逻辑agent智能体
        self.main_agent = AgentExecutorWrapper(
            llm=self.llm,
            tools=self.tools,
            user_id=self.user_id,
            session_id=self.session_id,
            system_prompt=self.main_node_system_prompt,
            persist_memory=False
        )
        # 初始化图
        self.init_graph()
    
    @classmethod
    async def create(cls, llm: BaseChatModel, tools: CustomTool,user_id: str, session_id: str, main_node_system_prompt='', max_iters: int = 3, enable_debug=False):
        main_node_system_prompt = await normal_prompt()
        return cls(llm, tools, user_id, session_id, main_node_system_prompt, max_iters, enable_debug)
        
    def stream_output(self, writer: StreamWriter, content: str, chapter_name: str, content_type: str = "chapter_content"):
        """标准化流式输出（给前端）"""
        output = {
            "type": content_type,
            "chapter": chapter_name,
            "content": content,
            "company": self.company if hasattr(self, 'company') else "园区",
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        writer(output)
    def update_word(self, state: MyState, chapter_name: str, chapter_content: str, writer: StreamWriter = None) -> str:
        """
        每完成一个章节就更新Word文档
        :return: 更新后的Word文件路径
        """
        # 用来记录哪些大章节已经加过标题（报告概述、执行摘要...）
        if not hasattr(self, "_added_chapter_set"):
            self._added_chapter_set = set()

        index=self.get_chapter_title_with_index(chapter_name)
        # 复用Word生成器
        if self.word_generator is None:
            # 获取state中的样式配置（JSON字符串）
            config_style = state.get("config_style", "{}")  # 兜底空JSON
            # 确保是字符串格式（防止传入字典）
            if isinstance(config_style, dict):
                config_style = json.dumps(config_style, ensure_ascii=False)
            
            self.word_generator = CustomWordGenerator(config_json_str=config_style)
            self.word_generator.create_document()  # 创建新文档

            # 添加文档标题（只在第一次初始化时添加）
            company = state.get("company", "园区")
            start_date = state.get("start_date", "未知时间")
            end_date = state.get("end_date", "未知时间")
            dimension = state.get("dimension", "综合")
         
            # 添加主标题
            title = f"{company}智慧运营分析报告"
            self.word_generator.add_heading(title, level=1)
            # 添加一个空行
            self.word_generator.add_paragraph("")

            logging.info("Word文档标题添加完成")
            
            # 如果是第一次初始化，设置一个固定的文件路径
            session_id = getattr(self, 'session_id', 'default') or 'default'
            timestamp = int(time.time())
            # 确保session_id非空
            session_id = session_id.strip() if session_id.strip() else "default"
            # 生成基础路径
            self.word_file_path = f"reports/report_{session_id}_{timestamp}.docx"
            self.word_file_name = f"report_{session_id}_{timestamp}.docx"
            # 绝对确保路径非空
            if not self.word_file_path or self.word_file_path.strip() == "":
                self.word_file_path = f"report_fallback_{timestamp}.docx"
            

        # 只在第一次进入该章节时加标题 =====================
        if chapter_name not in self._added_chapter_set:
            self.word_generator.add_heading(index, level=2)
            self._added_chapter_set.add(chapter_name)  # 标记已加
        # 确保章节内容是字符串
        if isinstance(chapter_content, list):
            chapter_content = "\n".join(chapter_content)
      
            
       # 添加章节内容到Word

        self.word_generator.add_markdown_content(chapter_content)
        self.word_generator.add_paragraph("")
    # 调用save前再次校验路径 
        if not self.word_file_path or self.word_file_path.strip() == "":
            timestamp = int(time.time())
            self.word_file_path = f"report_fallback_{timestamp}.docx"
            logging.info(f"word_file_path为空，使用兜底路径：{self.word_file_path}")
        if self.word_file_path:
            saved_path = self.word_generator.save(self.word_file_path)
            if saved_path:
                state["file_path"] = saved_path
                logging.info(f"章节【{chapter_name}】已添加到Word文档: {saved_path}")
                
                # 流式输出保存状态
                if writer:
                    writer({
                        "type": "word_save",
                        "chapter": chapter_name,
                        "file_path": saved_path,
                        "message": f"章节【{chapter_name}】已保存"
                    })
                return saved_path
            else:
                logging.warning(f"章节【{chapter_name}】保存失败")
                return None
        else:
            logging.error("word_file_path 未设置")
            return None

    def get_chapter_config(self, chapter_name: str) -> Dict:
        """根据章节名获取配置"""
        for config in CHAPTER_CONFIG:
            if config["name"] == chapter_name:
                return config
        raise ValueError(f"未找到章节配置：{chapter_name}")
    def get_chapter_title_with_index(self, chapter_name: str) -> str:
        """获取带序号的章节标题"""
        for config in CHAPTER_CONFIG:
             if config["name"]==chapter_name:
                return f"{config['index']}{chapter_name}"
        return chapter_name

 # ===================== 核心节点实现 =====================
    async def gen_report_overview(self, state: MyState, config: RunnableConfig, runtime: Runtime, writer: StreamWriter) -> MyState:
        """节点1：报告概述"""
        chapter_name = "报告概述"
        logging.info(f"第 {state['evaluator_iter']} 次迭代，生成{chapter_name}章节")
        
        # 1. 获取配置和参数
        chapter_config = self.get_chapter_config(chapter_name)
        company = state.get("company", "园区")
        start_date=state.get("start_date", "未知时间")
        end_date=state.get("end_date", "未知时间")
        dimension = state.get("dimension", "综合")
        dimension_data=state.get("dimension_data", {})
        
        # 2. 构建Prompt（调用专属prompt函数）
        prompt_func = chapter_config["prompt_func"]
        prompt_str = prompt_func(
            company=company,
            start_date=start_date,
            end_date=end_date,
            max_words=chapter_config["max_words"],
            dimension_data=dimension_data,
            dimension=dimension,
        )
        title = f"# {company}智慧运营分析报告\n"
        chapter_title=f"\n## {self.get_chapter_title_with_index(chapter_name)}\n"
        self.stream_output(writer, title, "主标题")  # 流式输出给前端
        self.stream_output(writer, chapter_title, chapter_name)  # 流式输出给前端
        # 3. LLM流式生成
        chapter_content = ""
        async for chunk in self.llm.astream([{"role": "system", "content": prompt_str}], config=config):
            chapter_content += chunk.content
            self.stream_output(writer, chunk.content, chapter_name)  # 流式输出给前端
        
        # 4. 更新Word文档
        self.update_word(state, chapter_name, chapter_content, writer)
     
        # 5. 更新状态
        completed_chapters = state.get("completed_chapters", [])
        completed_chapters.append(chapter_name)
        
        full_doc_content = state.get("full_doc_content", "")
        full_doc_content += f"\n## {chapter_name}\n{chapter_content}"
        
        return {
            "completed_chapters": completed_chapters,
            "chapter_contents": {**state.get("chapter_contents", {}), chapter_name: chapter_content},
            "full_doc_content": full_doc_content
        }

    async def gen_exec_summary(self, state: MyState, config: RunnableConfig, runtime: Runtime, writer: StreamWriter) -> MyState:
        """节点2：执行摘要"""
        chapter_name = "执行摘要"
        logging.info(f"第 {state['evaluator_iter']} 次迭代，生成{chapter_name}章节")
        
        chapter_config = self.get_chapter_config(chapter_name)
        company = state.get("company", "园区")
        start_date=state.get("start_date", "未知时间")
        end_date=state.get("end_date", "未知时间")
        dimension_data=state.get("dimension_data", {})
        dimension=state.get("dimension", "综合")
        
        # 构建Prompt
        prompt_func = chapter_config["prompt_func"]
        prompt_str = prompt_func(
            company=company,
            start_date=start_date,
            end_date=end_date,
            max_words=chapter_config["max_words"],
            full_doc_content=state.get("full_doc_content", ""),
            dimension_data=dimension_data,
            dimension=dimension,
        )
        chapter_title=f"\n## {self.get_chapter_title_with_index(chapter_name)}\n"
        self.stream_output(writer, chapter_title, chapter_name)  # 流式输出给前端
        # 流式生成
        chapter_content = ""
        async for chunk in self.llm.astream([{"role": "system", "content": prompt_str}], config=config):
            chapter_content += chunk.content
            self.stream_output(writer, chunk.content, chapter_name)
        
        # 更新Word+流式输出
        self.update_word(state, chapter_name, chapter_content,writer)
       
        # 更新状态
        completed_chapters = state.get("completed_chapters", [])
        completed_chapters.append(chapter_name)
        
        full_doc_content = state.get("full_doc_content", "")
        full_doc_content += f"\n## {chapter_name}\n{chapter_content}"
        
        return {
            "completed_chapters": completed_chapters,
            "chapter_contents": {**state.get("chapter_contents", {}), chapter_name: chapter_content},
            "full_doc_content": full_doc_content
        }

    async def gen_operation_metrics(self, state: MyState, config: RunnableConfig, runtime: Runtime, writer: StreamWriter) -> MyState:
        """节点3：园区运营总览指标"""
        chapter_name = "园区运营总览指标"
        logging.info(f"第 {state['evaluator_iter']} 次迭代，生成{chapter_name}章节")
        
        chapter_config = self.get_chapter_config(chapter_name)
        company = state.get("company", "园区")
        start_date=state.get("start_date", "未知时间")
        end_date=state.get("end_date", "未知时间")
        dimension=state.get("dimension", "综合")
        # 整合所有维度数据
        dimension_data = state.get("dimension_data", {})
        
        # 构建Prompt
        prompt_func = chapter_config["prompt_func"]
        prompt_str = prompt_func(
            company=company,
            start_date=start_date,
            end_date=end_date,
            dimension_data=dimension_data,
            dimension=dimension,
            max_words=chapter_config["max_words"]
        )
        #流式输出章节标题
        chapter_title=f"\n## {self.get_chapter_title_with_index(chapter_name)}\n"
        self.stream_output(writer, chapter_title, chapter_name)  # 流式输出给前端
        # 流式生成
        chapter_content = ""
        async for chunk in self.llm.astream([{"role": "system", "content": prompt_str}], config=config):
            chapter_content += chunk.content
            self.stream_output(writer, chunk.content, chapter_name)
        
        # 更新Word+流式输出
        self.update_word(state, chapter_name, chapter_content,writer)
        self.stream_output(writer, state["file_path"], chapter_name, "file_path")
        
        # 更新状态
        completed_chapters = state.get("completed_chapters", [])
        completed_chapters.append(chapter_name)
        
        full_doc_content = state.get("full_doc_content", "")
        full_doc_content += f"\n## {chapter_name}\n{chapter_content}"
        
        return {
            "completed_chapters": completed_chapters,
            "chapter_contents": {**state.get("chapter_contents", {}), chapter_name: chapter_content},
            "full_doc_content": full_doc_content
        }

    async def gen_dimension_analysis(self, state: MyState, config: RunnableConfig, runtime: Runtime, writer: StreamWriter) -> MyState:
        """节点4：维度深度分析（包含安防/能耗/运营子维度）"""
        chapter_name = "维度深度分析"
        logging.info(f"第 {state['evaluator_iter']} 次迭代，生成{chapter_name}章节")
        
        #流式输出章节标题
        chapter_title=f"\n## {self.get_chapter_title_with_index(chapter_name)}\n"
        self.stream_output(writer, chapter_title, chapter_name)  # 流式输出给前端

        chapter_config = self.get_chapter_config(chapter_name)
        company = state.get("company", "园区")
        start_date=state.get("start_date", "未知时间")
        end_date=state.get("end_date", "未知时间")
        sub_dimensions = state.get("dimension", "综合")
        
        # 生成各子维度内容
        main_index = 4
        sub_dimension_contents = []

        if "综合" in sub_dimensions or not sub_dimensions:
            sub_dimensions=["安防","能耗","运营"]

        for sub_index, sub_dim in enumerate(sub_dimensions, start=1):
            # 获取子维度数据
            sub_dim_eng =DIMENSION_DATA_MAPPING[sub_dim]
            all_dim_data = state.get("dimension_data", {})
            sub_dim_data = all_dim_data[sub_dim_eng]
            
            # 调用子维度专属Prompt
            prompt_func = DIMENSION_PROMPT_MAPPING[sub_dim]
            prompt_str = prompt_func(
                company=company,
                start_date=start_date,
                end_date=end_date,
                
                dimension_data=json.dumps(sub_dim_data, ensure_ascii=False)[:800],
                max_words=chapter_config["max_words"] // 3  # 均分字数
            )
            
            # 流式生成子维度内容
            sub_content = ""
            full_index = f"\n### {main_index}.{sub_index} {sub_dim}维度分析\n"  # 4.1、4.2、4.3
            self.stream_output(writer,full_index, chapter_name)
           
            async for chunk in self.llm.astream([{"role": "system", "content": prompt_str}], config=config):
                sub_content += chunk.content
                self.stream_output(writer, chunk.content, chapter_name)

            sub_dimension_contents=(f"{full_index}{sub_content}")
            self.update_word(state, chapter_name, sub_dimension_contents,writer)
            # 缓存子维度内容
            state[f"{sub_dim.lower()}_paragraph"] = sub_content
        

        # 更新状态
        completed_chapters = state.get("completed_chapters", [])
        completed_chapters.append(chapter_name)
        chapter_content = "\n".join(sub_dimension_contents)
        full_doc_content = state.get("full_doc_content", "")
        full_doc_content += f"\n## {chapter_name}\n"  # 只记一次章节标题
        
        return {
            "completed_chapters": completed_chapters,
            "chapter_contents": {**state.get("chapter_contents", {}), chapter_name: chapter_content},
            "full_doc_content": full_doc_content,
            "security_paragraph": state.get("security_paragraph", ""),
            "energy_paragraph": state.get("energy_paragraph", ""),
            "operation_paragraph": state.get("operation_paragraph", "")
        }

    async def gen_comprehensive_evaluation(self, state: MyState, config: RunnableConfig, runtime: Runtime, writer: StreamWriter) -> MyState:
        """节点5：综合评估"""
        chapter_name = "综合评估"
        logging.info(f"第 {state['evaluator_iter']} 次迭代，生成{chapter_name}章节")
        
        #流式输出章节标题
        chapter_title=f"\n## {self.get_chapter_title_with_index(chapter_name)}\n"
        self.stream_output(writer, chapter_title, chapter_name)  # 流式输出给前端

        chapter_config = self.get_chapter_config(chapter_name)
        company = state.get("company", "园区")
        start_date=state.get("start_date", "未知时间")
        end_date=state.get("end_date", "未知时间")
        dimension_data=state.get("dimension_data", "综合")
        dimension=state.get("dimension", {})
        
        # 构建Prompt（基于已生成的所有维度内容）
        prompt_str = chapter_config["prompt_func"](
            company=company,
            start_date=start_date,
            end_date=end_date,
            dimension_data=dimension_data,
            dimension=dimension,
            max_words=chapter_config["max_words"]
        )
        
        # 流式生成
        chapter_content = ""
        async for chunk in self.llm.astream([{"role": "system", "content": prompt_str}], config=config):
            chapter_content += chunk.content
            self.stream_output(writer, chunk.content, chapter_name)
        
        # 更新Word+流式输出
        self.update_word(state, chapter_name, chapter_content,writer)
       
        # 更新状态
        completed_chapters = state.get("completed_chapters", [])
        completed_chapters.append(chapter_name)
        
        full_doc_content = state.get("full_doc_content", "")
        full_doc_content += f"\n## {chapter_name}\n{chapter_content}"
        
        return {
            "completed_chapters": completed_chapters,
            "chapter_contents": {**state.get("chapter_contents", {}), chapter_name: chapter_content},
            "full_doc_content": full_doc_content
        }

    async def gen_improvement_suggestions(self, state: MyState, config: RunnableConfig, runtime: Runtime, writer: StreamWriter) -> MyState:
        """节点6：改进建议"""
        chapter_name = "改进建议"
        logging.info(f"第 {state['evaluator_iter']} 次迭代，生成{chapter_name}章节")
        
        #流式输出章节标题
        chapter_title=f"\n## {self.get_chapter_title_with_index(chapter_name)}\n"
        self.stream_output(writer, chapter_title, chapter_name)  # 流式输出给前端

        chapter_config = self.get_chapter_config(chapter_name)
        company = state.get("company", "园区")
        start_date=state.get("start_date", "未知时间")
        end_date=state.get("end_date", "未知时间")
        dimension=state.get("dimension", "综合")
        dimension_data=state.get("dimension_data", {})
    
        # 构建Prompt（基于综合评估结果）
        prompt_str = chapter_config["prompt_func"](
            company=company,
            start_date=start_date,
            end_date=end_date,
            dimension_data=dimension_data,
            dimension=dimension,
            comprehensive_evaluation=state["chapter_contents"].get("综合评估", ""),
            max_words=chapter_config["max_words"]
        )
        
        # 流式生成
        chapter_content = ""
        async for chunk in self.llm.astream([{"role": "system", "content": prompt_str}], config=config):
            chapter_content += chunk.content
            self.stream_output(writer, chunk.content, chapter_name)
        
        # 更新Word+流式输出
        self.update_word(state, chapter_name, chapter_content,writer)
        # self.stream_output(writer, state["file_path"], chapter_name, "file_path")
        
        # 更新状态
        completed_chapters = state.get("completed_chapters", [])
        completed_chapters.append(chapter_name)
        
        full_doc_content = state.get("full_doc_content", "")
        full_doc_content += f"\n## {chapter_name}\n{chapter_content}"
        
        return {
            "completed_chapters": completed_chapters,
            "chapter_contents": {**state.get("chapter_contents", {}), chapter_name: chapter_content},
            "full_doc_content": full_doc_content
        }
 
  # ===================== 路由与流程控制 =====================
    def get_next_chapter(self, state: MyState) -> str:
        """
        路由逻辑：获取下一个待生成的章节
        返回章节名或"COMPLETE"（完成）
        """
        chapter_order = state.get("chapter_order", self.chapter_order)
        completed_chapters = state.get("completed_chapters", [])
        
        # 找到第一个未完成的章节
        for chap in chapter_order:
            if chap not in completed_chapters:
                return chap
        return "COMPLETE"

    def route_chapter(self, state: MyState) -> str:
        """章节路由：根据当前章节跳转到对应生成节点"""
        next_chapter = self.get_next_chapter(state)
        if next_chapter == "COMPLETE":
            return "END"
        
        # 章节名 → 节点名映射（匹配原代码命名风格）
        chapter_to_node = {
            "报告概述": "gen_report_overview",
            "执行摘要": "gen_exec_summary",
            "园区运营总览指标": "gen_operation_metrics",
            "维度深度分析": "gen_dimension_analysis",
            "综合评估": "gen_comprehensive_evaluation",
            "改进建议": "gen_improvement_suggestions"
          
        }
        return chapter_to_node[next_chapter]
    '''
    初始化graph
    '''
     # ===================== 初始化LangGraph图 =====================
    def init_graph(self):
        '''
        初始化graph
        '''
        # @@@@@ 定义图
        self.flow_graph = StateGraph(MyState)
        
        '''
        @@@@@@@ 定义节点（匹配原代码命名风格）
        '''
        # 注册8个核心节点
        self.flow_graph.add_node("gen_report_overview", self.gen_report_overview)
        self.flow_graph.add_node("gen_exec_summary", self.gen_exec_summary)
        self.flow_graph.add_node("gen_operation_metrics", self.gen_operation_metrics)
        self.flow_graph.add_node("gen_dimension_analysis", self.gen_dimension_analysis)
        self.flow_graph.add_node("gen_comprehensive_evaluation", self.gen_comprehensive_evaluation)
        self.flow_graph.add_node("gen_improvement_suggestions", self.gen_improvement_suggestions)
       
        '''
        @@@@@@ 定义边
        '''
        # 起始点 → 动态路由
        self.flow_graph.add_conditional_edges(START, self.route_chapter, {
            "gen_report_overview": "gen_report_overview",
            "gen_exec_summary": "gen_exec_summary",
            "gen_operation_metrics": "gen_operation_metrics",
            "gen_dimension_analysis": "gen_dimension_analysis",
            "gen_comprehensive_evaluation": "gen_comprehensive_evaluation",
            "gen_improvement_suggestions": "gen_improvement_suggestions",
          
            "END": END
        })
        
        # 每个节点完成后 → 回到路由（继续下一章）
        for node_name in [
            "gen_report_overview", "gen_exec_summary", "gen_operation_metrics",
            "gen_dimension_analysis", "gen_comprehensive_evaluation", "gen_improvement_suggestions",
           
        ]:
            self.flow_graph.add_conditional_edges(node_name, self.route_chapter, {
                "gen_report_overview": "gen_report_overview",
                "gen_exec_summary": "gen_exec_summary",
                "gen_operation_metrics": "gen_operation_metrics",
                "gen_dimension_analysis": "gen_dimension_analysis",
                "gen_comprehensive_evaluation": "gen_comprehensive_evaluation",
                "gen_improvement_suggestions": "gen_improvement_suggestions",
                "END": END
            })
        
        '''
        @@@@ 编译图
        '''
        # 使用内存checkpointer
        self.checkpointer = MemorySaver()
        # 用这个 checkpointer 编译 graph
        self.graph = self.flow_graph.compile(checkpointer=self.checkpointer)


    # ===================== 对外调用接口 =====================
    async def astream_run(self, query: str,style: str, user_id: str, session_id: str):
        thread_id = f'{user_id}|{session_id}' 
        '''
        流式调用langgraph，流式返回最终节点数据
        数据格式 {"query": "用户问题", "sessionId": "对话id"}
        '''
        # query是所有数据的json，转成数据放入state
        data_json = json.loads(query) if isinstance(query, str) else query
        data_dict = data_json.get("data", {})
        style_json=json.loads(style) if isinstance(style, str) else style
        dimension = []
        if isinstance(data_dict, dict) and data_dict:  # 确保 data 是有效字典
            dimension = [v.get("type") for k, v in data_dict.items() if v.get("type")]
        # 兜底：如果没提取到任何 type，就用 "综合"
        if not dimension:
            dimension = ["综合"]


        # 重置Word生成器
        self.word_generator = None
        self.word_file_path = ''
        self.word_file_name= ''
        self.company = data_json.get("company", "园区")

        # 初始 state
        init_state: MyState = {
            "query": query, 
            "evaluator_iter": 0, 
            "full_doc_content": "",
            "chapter_order": self.chapter_order,
            "completed_chapters": [] , # 初始化已完成章节
            "chapter_contents": {},
            "company": self.company,  
            "start_date":data_json.get("start_date", "未知时间"),
            "end_date":data_json.get("end_date", "未知时间"),       
            "dimension":dimension,          # 安防/能耗/运营/综合
            # 业务数据（单次复用）
            "dimension_data": data_dict,
            "config_style":style_json,
            "word_generator": self.word_generator  # 注入Word生成器
        }
      
        # 异步执行，流式输出
        async for mode, chunk in self.graph.astream(
            init_state,
            stream_mode=["updates", "messages", "custom"],
            config={
                "configurable": {
                    "thread_id": thread_id
                }
            }
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
         # 最后确认文档已保存
        if hasattr(self, 'word_file_path') and self.word_file_path:
            # 可以再保存一次确保所有内容都已写入
            if self.word_generator:
                self.word_generator.save(self.word_file_path)
            
            # 输出最终文件路径
            yield {
                "event": "custom",
                "data": {
                    "type": "final_file",
                    "content": self.word_file_path,
                    "company":self.company,
                    "file_name": self.word_file_name,
                    "file_path": self.word_file_path,
                    "message": "文档生成完成"
                }
            }
        # 如果持久化对话历史，则持久化
        if (self.main_agent.persist_memory is True):
            self.main_agent.memory_store.persist_memory(self.user_id, self.session_id)
