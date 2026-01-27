# Agentic App - 智能代理系统

这是一个基于 LangChain 的智能代理系统，旨在实现多轮、交互式、自我优化的复杂任务处理。系统通过一个反应式工作流，能够理解用户意图，动态补充参数，并调用工具完成查询，最后通过评估循环确保输出质量。

## 项目文件结构

```
agentic-app/
├── agent/                  # 代理相关模块，负责任务流程控制
│   ├── executor.py         # 代理执行器，封装了AgentExecutor，支持同步/异步执行
│   ├── ask_for_param_prompt.py # 生成请求用户补充信息的提示词
│   ├── info_double_check_prompts.py # 主要问答流程的系统提示词
│   ├── intent_get_prompt.py # 识别用户意图和工具的提示词
│   ├── get_intent_and_select_tools_prompt.py # 选择工具和获取参数的提示词
│   └── rag_prompts.py      # RAG（检索增强生成）相关的系统提示词
├── graph/                  # 图形工作流模块，定义了工作流节点和流程
│   ├── reactive_pipeline.py # 反应式工作流的核心文件，实现了包含意图识别、参数请求、主代理执行、评估和组装的完整流程
│   └── langgraph开发模板代码.py # 可能是模板参考文件
├── memory/                 # 记忆管理模块，用于持久化保存对话历史
│   ├── store.py            # 记忆存储服务，提供统一接口
│   ├── memory_persistor.py # 记忆持久化抽象基类
│   ├── memory_persistor_json.py # JSON持久化实现
│   └── memory_persistor_sqlite.py # SQLite持久化实现
├── tools/                  # 工具模块，包含具体的工具函数
│   ├── rag_tools.py        # RAG（检索增强生成）核心工具，实现了知识库检索
│   ├── custom_tool.py      # 自定义工具基类，用于生成动态工具
│   ├── system_tools.py     # 系统级工具的集合
│   └── dify_datasets_controller.py # Dify知识库控制器，封装了与Dify API的交互
├── dynamic_tools/          # 动态工具模块，支持从外部数据源动态加载工具
│   ├── api_dynamic_tool.py # 基于API的动态工具实现类
│   ├── file_dynamic_tool.py # 基于文件的动态工具实现类
│   ├── dynamic_tool_generator.py # 动态工具生成器，抽象基类
│   └── dynamic-tools-data.json # 工具描述数据文件，定义了可用工具的元信息
├── models/                 # 模型配置模块，管理大模型实例
│   └── llm.py              # 大模型工厂类，支持多种模型（OpenAI, Ollama）
├── utils/                  # 工具函数模块
│   ├── utils.py            # 通用工具函数，如配置读取
│   └── static.py           # 静态配置，如文件路径
├── app.py                  # 主应用入口，FastAPI服务器
├── global_config.json      # 全局配置文件，包含各种参数
├── memory_store.json       # 内存存储的JSON文件（仅用于测试）
├── memory_store.db         # 内存存储的SQLite数据库文件
└── workflow.png            # 工作流图（由代码自动生成）
```

## 核心功能

1. **意图识别与参数获取**：通过 `intent_get_prompt.py` 和 `get_intent_and_select_tools_prompt.py` 中的提示词，系统能够精准识别用户请求的核心意图，并判断需要调用的工具和缺失的参数。
2. **动态交互式问答**：当参数缺失时，系统会使用 `ask_for_param_prompt.py` 主动向用户请求补充信息，实现真正的交互式对话。
3. **工具链式调用**：系统能够调用 RAG（`rag_tools.py`）和动态生成的工具（`dynamic_tools/`）来完成复杂查询。
4. **记忆持久化**：通过 `memory_store.py` 和 `memory_persistor_*.py`，系统支持将对话历史持久化到JSON文件或SQLite数据库，确保上下文连贯性。
5. **自我评估与迭代优化**：核心在 `reactive_pipeline.py` 中实现，构建了“回答 - 评估 - 修正”的闭环，通过 `Evaluator` 节点判断回答质量，若不充分则循环执行，直到达到“完全充分”标准。

## 详细功能模块说明

### 1. `agent/` 模块
- **`executor.py`**：核心执行器。`AgentExecutorWrapper` 类封装了 LangChain 的 `AgentExecutor`，提供同步 `run()` 和异步 `stream_run()` 接口。它能根据配置选择使用 `MemoryStore` 读取持久化记录或使用 `ConversationBufferWindowMemory` 保持最近对话。
- **`rag_prompts.py`**：定义了RAG (Retrieval-Augmented Generation) 问答的系统提示词，指导LLM如何进行检索-精读-总结的完整流程。
- **`intent_get_prompt.py`**：专门用于判断用户意图和参数的提示词，系统通过分析对话历史和用户输入，精确提取参数。
- **`get_intent_and_select_tools_prompt.py`**：此提示词用于更复杂的意图获取，它会分析之前的意图，生成完整的调用逻辑说明和参数状态。
- **`ask_for_param_prompt.py`**：当参数不完整时，此提示词生成具体的、自然语言的询问，例如“请补充您想查询的区域信息”。

### 2. `graph/` 模块 (`reactive_pipeline.py`)
- **`InfoDoubleCheckPipeline` 类**：这是整个系统的核心。它使用 LangGraph 构建了一个状态机，定义了工作流的各个节点：`IntentGet` -> `AskForParam` -> `MainAgent` -> `Evaluator` -> `Composer`。
- **`MyState` 类型**：作为状态字典，管理整个流程中的关键状态，如 `query` (用户问题), `missing_params` (缺失参数), `intent_get_result` (意图获取结果), `evaluator_iter` (评估迭代次数) 等。
- **`evaulator_node` 方法**：调用大模型来评估 `MainAgent` 的回答是否充分，决定是否继续迭代。
- **`composer_node` 方法**：根据评估结果和中间输出，生成最终的、自然语言的合集答案，并支持流式输出。
- **`should_ask_for_param` 和 `should_redo_rag_after_evaluation`**：路由逻辑函数，控制工作流的分支流向。

### 3. `tools/` 模块
- **`rag_tools.py`**：核心工具集。包含 `query_knowledge_base` (通过语义搜索获取候选片段), `list_datasets` (获取知识库列表), `get_document_segments` (精读指定文档片段) 和 `list_documents` (列出文档) 等工具。所有工具均通过 `@tool` 装饰器定义，可被大模型直接调用。
- **`dify_datasets_controller.py`**：Dify知识库的适配层。它封装了与 Dify API 的交互逻辑，使得 `rag_tools.py` 可以方便地调用这些接口，无需关心具体实现。
- **`custom_tool.py`**：提供 `CustomTool` 基类，扩展了 LangChain 的 `StructuredTool`，添加了 `displayName` (显示名称) 和 `endPoint` (接口地址) 等字段，便于前端展示工具调用情况。

### 4. `dynamic_tools/` 模块
- **`file_dynamic_tool.py`**：一个具体的实现类。它继承自 `DynamicToolGenerator`，其 `query_tool_info_list` 方法从 `dynamic-tools-data.json` 文件中读取工具列表信息，并通过 `generate_tools` 方法将其转换为 LangChain 可识别的工具对象。这实现了“数据驱动”的工具加载。
- **`dynamic_tool_generator.py`**：抽象基类，定义了生成动态工具的通用接口，如 `query_tool_info_list` (获取工具列表) 和 `tool_request` (发起API请求)，并提供了 `get_tool_name_mapping` 和 `get_tool_json_desc` 等静态方法来获取工具元信息。
- **`dynamic-tools-data.json`**：关键的元数据文件。它以 JSON 格式定义了所有可用工具的详细信息，包括名称、显示名称、描述、API地址、参数等。系统在启动时会加载此文件，并以此为基础创建所有工具实例。

### 5. `memory/` 模块
- **`store.py`**：记忆存储的工厂类。`MemoryStore` 单例类根据配置 (`memory_persistor/type`) 选择使用 `MemoryPersistorJSON` 或 `MemoryPersistorSqlite` 实现。
- **`memory_persistor_json.py`**：一个仅用于开发测试的简易JSON持久化方案，不支持并发，仅作为演示。
- **`memory_persistor_sqlite.py`**：生产环境推荐的持久化方案。它使用SQLite数据库，通过`get_conn()`上下文管理器确保事务安全和连接关闭。该实现具备完整功能，支持多用户、多会话、数据持久化和内存窗口化（即: 只持久化最近的对话）。

### 6. `models/` 模块
- **`llm.py`**：大模型工厂。`CustomLLMFactory` 类初始化了多个大模型的实例（如 `silicon` 和 `local`），并根据配置选择默认模型。这使得系统可以轻松切换不同的大模型，且支持 OpenAI 和 Ollama 两种后端。

### 7. `utils/` 模块
- **`utils.py`**：通用工具函数。`get_config()` 函数用于从 `global_config.json` 文件中读取全局配置。
- **`static.py`**：静态配置。定义了内存存储文件的路径，便于管理和维护。

### 8. `app.py` 模块
- **FastAPI 服务器**：主入口，监听 WebSocket `/agentic_rag_query/{user_id}/{session_id}` 路径。当收到用户消息时，它会初始化 `InfoDoubleCheckPipeline` 实例，并通过 `astream_run` 方法进行流式调用，将结果（包括工具调用状态、最终答案等）实时返回给前端。这实现了真正的对话式交互体验。

## 依赖与使用

### 1. 依赖
- **pytest**: 用于单元测试
- **langchain**: LLM框架
- **langgraph**: 图形工作流框架
- **openai, anthropic**: 大模型API
- **pydantic**: 数据验证

### 2. 快速启动
```bash
# 1. 安装依赖
docker run -d -v /Users/louisliu/dev/AI_projects/agentic-app:/root/agentic-app --name langchain-agent-dev qingyanjiu/langchain:1.0.3 tail -f /dev/null


# 2. 运行FastAPI服务器
python app.py

# 3. 通过WebSocket连接前端进行测试（例如，使用浏览器的WebSocket客户端或编写简单的客户端）

# 访问 http://localhost:8000/ 跳转到 /docs 路由查看API文档
```

## 配置说明

所有配置均在 `global_config.json` 中管理，主要包括：
- `memory_persistor` : 配置记忆持久化类型 (如 `json` 或 `sqlite`)
- `dify` : Dify知识库的API密钥
- `llm` : 默认使用的语言模型名称

## 贡献

欢迎提交 Issues 和 Pull Requests 以改进系统功能。请遵循项目提交规范。