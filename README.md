# 🚀 Agentic App - Python智能代理系统

这是一个基于 LangChain 的智能代理系统，实现多轮、交互式、自我优化的复杂任务处理。系统通过反应式工作流能够理解用户意图，动态补充参数，调用工具完成查询，并通过评估循环确保输出质量。

## ✨ 核心特性
- 🎯 **意图识别与参数补全**: 自动分析用户意图，动态请求缺失参数
- 🔄 **反应式工作流**: 基于LangGraph的状态机，支持评估迭代优化
- 🛠️ **动态工具系统**: 配置驱动的工具生成，支持RAG知识库检索
- 💾 **记忆持久化**: SQLite/JSON双重存储，多用户对话管理
- 🌐 **实时交互**: WebSocket流式API，支持工具调用状态可视化

## 📋 核心能力矩阵

### 交互式对话能力
- **意图识别**: 自动分析用户查询目的和参数需求
- **参数补全**: 智能识别缺失参数并主动询问用户
- **多轮对话**: 保持上下文连贯性，支持复杂任务分解

### 工作流引擎
- **状态机驱动**: 基于LangGraph构建的反应式工作流
- **评估迭代**: 自动评估回答质量，支持多次迭代优化
- **条件路由**: 根据参数完整性和评估结果智能路由

### 工具系统
- **RAG检索**: 集成Dify知识库，支持语义搜索和精读
- **动态工具**: 基于JSON配置动态生成工具，支持热更新
- **工具链**: 支持多工具链式调用，处理复杂查询

### 记忆管理
- **双重持久化**: SQLite（生产）和JSON（开发）两种存储方案
- **多用户支持**: 基于user_id和session_id的会话隔离
- **记忆窗口**: 可配置的对话历史保留长度

## 🏗️ 系统架构

### 架构概览
系统采用分层架构设计，核心工作流基于LangGraph状态机：

```
┌─────────────┐   ┌─────────────┐   ┌─────────────┐
│  意图识别   │ → │  参数补全   │ → │  工具执行   │
└─────────────┘   └─────────────┘   └─────────────┘
       ↑                   ↓                 ↓
       │              ┌─────────────┐   ┌─────────────┐
       └──────────────│ 评估迭代    │ ← │  结果评估   │
                      └─────────────┘   └─────────────┘
                               ↓
                       ┌─────────────┐
                       │  最终答案   │
                       └─────────────┘
```

### 数据流转
1. **输入解析**: 用户查询 → 意图识别 → 参数分析
2. **交互补全**: 参数不足 → 用户询问 → 参数获取
3. **工具执行**: 调用RAG/动态工具 → 结果处理
4. **质量评估**: 回答评估 → 判断是否充分
5. **结果输出**: 最终答案生成 → 流式返回

详细工作流图: [workflow.png](workflow.png)

## 🛠️ 技术栈

### 核心框架
- **LangChain + LangGraph**: 代理框架和工作流引擎
- **FastAPI**: Web服务器和WebSocket支持
- **Pydantic**: 数据验证和模型定义

### 模型服务
- **SiliconFlow API**: Qwen/Qwen3-30B-A3B-Instruct-2507（默认）
- **OpenAI兼容接口**: 支持本地部署的OpenAI兼容模型
- **Ollama**: 可选本地模型部署（已注释支持）

### 数据存储
- **SQLite**: 生产环境推荐，支持事务和多用户
- **JSON文件**: 开发测试环境，简单易用

### 依赖库
- `langchain-core`, `langchain-classic`: LangChain核心库
- `langchain-openai`, `langchain-ollama`: 模型集成
- `fastapi`, `uvicorn`: Web服务器
- `requests`: HTTP客户端
- `sqlite3`: 数据库操作

## ⚡ 快速开始

### 环境准备
```bash
# 设置API密钥（使用SiliconFlow API）
export OPENAI_API_KEY="您的SiliconFlow API密钥"
# 或使用Dify知识库密钥
export DIFY_API_KEY="您的Dify API密钥"
```

### Docker开发环境（推荐）
```bash
# 使用预配置的开发容器
docker run -d -v $(pwd):/root/agentic-app --name langchain-agent-dev qingyanjiu/langchain:1.0.3 tail -f /dev/null

# 进入容器
docker exec -it langchain-agent-dev /bin/bash

# 在容器内启动服务
cd /root/agentic-app
python app.py
```

### 本地启动
```bash
# 安装依赖（在虚拟环境中）
pip install langchain langchain-openai fastapi uvicorn requests pydantic

# 启动FastAPI服务器
python app.py
# 或使用uvicorn热重载
uvicorn app:app --host 0.0.0.0 --port 8000 --reload

# 访问API文档
# http://localhost:8000/docs
```

### 验证服务
使用浏览器或工具测试WebSocket连接：
- WebSocket端点: `ws://localhost:8000/agentic_rag_query/{user_id}/{session_id}`
- HTTP API: `http://localhost:8000/docs` (OpenAPI文档)

## 📖 API使用指南

### WebSocket连接
系统通过WebSocket提供流式交互接口：

```javascript
// JavaScript客户端示例
const ws = new WebSocket('ws://localhost:8000/agentic_rag_query/user_1/session_1');

ws.onopen = () => {
  console.log('连接已建立');
  ws.send(JSON.stringify({query: "查询北京天气"}));
};

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  switch(data.event) {
    case 'token':
      console.log('AI回答:', data.data);
      break;
    case 'custom':
      if (data.data.type === 'tool') {
        console.log('工具状态:', data.data.content);
      } else if (data.data.type === 'intent_desc') {
        console.log('意图分析:', data.data.content);
      }
      break;
  }
};

ws.onerror = (error) => {
  console.error('WebSocket错误:', error);
};
```

```python
# Python客户端示例
import websocket
import json
import threading

def test_agent():
    ws = websocket.WebSocket()
    ws.connect("ws://localhost:8000/agentic_rag_query/test/test")
    
    # 发送查询
    ws.send(json.dumps({"query": "今天天气怎么样"}))
    
    # 接收流式响应
    while True:
        try:
            response = ws.recv()
            data = json.loads(response)
            print(f"响应: {data}")
        except websocket.WebSocketConnectionClosedException:
            print("连接已关闭")
            break

# 在新线程中运行
thread = threading.Thread(target=test_agent)
thread.start()
```

### 响应格式
```json
{
  "event": "token",
  "data": "AI生成的文本片段"
}
```
```json
{
  "event": "custom",
  "data": {
    "type": "tool",
    "content": "正在调用[查询知识库]工具..."
  }
}
```

## 🔧 配置说明

### global_config.json详解
系统所有配置都通过 `global_config.json` 管理：

```json
{
  "dify": {
    "datasets_api_key": "dataset-xxxxxxxxxxxxxx"
  },
  "memory_persistor": {
    "type": "sqlite",        // 可选: "sqlite" 或 "json"
    "memory_buffer_window": 3 // 记忆窗口大小，保留最近N轮对话
  }
}
```

### 环境变量配置
```bash
# SiliconFlow API配置
export SILICON_API_KEY="sk-xxxxxxxxxxxx"
export OPENAI_API_KEY=${SILICON_API_KEY}  # 兼容OpenAI格式

# 模型服务URL（如需使用其他模型）
export MODEL_API_URL="https://api.siliconflow.cn/v1"
```

### 模型配置
模型配置在 `models/llm.py` 中定义：

```python
model_confs = [
    { 
        "name": "silicon",  # 模型标识
        "type": "openai",   # API类型
        "model_url": "https://api.siliconflow.cn/v1",
        "model_name": "Qwen/Qwen3-30B-A3B-Instruct-2507",
        "api_key": os.getenv("OPENAI_API_KEY")
    },
    # 可添加其他模型配置...
]
```

### 工具配置
动态工具在 `dynamic_tools/dynamic-tools-data.json` 中配置：

```json
{
  "name": "query_knowledge_base",
  "displayName": "查询知识库",
  "description": "根据问题在知识库中进行语义检索",
  "endpoint": "http://.../retrieve",
  "method": "post",
  "parameters": {
    "properties": {
      "query": {"type": "string", "description": "查询关键词"}
    }
  }
}
```

## 🔨 工具扩展指南

### 添加自定义工具
```python
from langchain_core.tools import tool
from pydantic import BaseModel, Field

# 定义参数模型
class CustomToolParams(BaseModel):
    param1: str = Field(description="参数1描述")
    param2: int = Field(description="参数2描述", default=10)

# 创建工具
@tool(args_schema=CustomToolParams, description="自定义工具描述")
def custom_tool(param1: str, param2: int = 10) -> str:
    """工具实现逻辑"""
    return f"处理结果: {param1}, {param2}"

# 添加到系统
TOOLS = [custom_tool] + existing_tools
```

### 扩展动态工具
1. 在 `dynamic-tools-data.json` 中添加工具定义
2. 系统启动时会自动加载新工具
3. 支持HTTP GET/POST方法，参数自动验证

### 集成外部API
```python
# 在dynamic_tools/中创建新的工具生成器
class APIDynamicTool(DynamicToolGenerator):
    def query_tool_info_list(self):
        # 从外部API或数据库获取工具列表
        return external_tool_list
    
    def tool_request(self, tool_name, parameters):
        # 调用外部API
        response = requests.post(api_url, json=parameters)
        return response.json()
```

## 🧪 测试与部署

### 测试示例
```bash
# 通过curl测试WebSocket（使用websocat工具）
websocat ws://localhost:8000/agentic_rag_query/test/test

# 发送测试消息（交互式）
{"query": "查询知识库中有哪些文档？"}
```

### 对话示例
```
用户: 查询北京天气
系统: 请提供查询日期
用户: 今天
系统: 正在查询天气... 北京今天晴天，25°C
```

### Docker生产部署
```dockerfile
# Dockerfile示例
FROM python:3.10-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
```

### 监控与日志
- 日志文件: `app.log` (详细debug信息)
- 可通过logging配置调整日志级别
- 建议生产环境使用集中式日志收集

## 📁 项目结构

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

## 🔍 详细功能模块说明

### 1. `agent/` 模块
- **`executor.py`**：核心执行器。`AgentExecutorWrapper` 类封装了 LangChain 的 `AgentExecutor`，提供同步 `run()` 和异步 `stream_run()` 接口。它能根据配置选择使用 `MemoryStore` 读取持久化记录或使用 `ConversationBufferWindowMemory` 保持最近对话。
- **`rag_prompts.py`**：定义了RAG (Retrieval-Augmented Generation) 问答的系统提示词，指导LLM如何进行检索-精读-总结的完整流程。
- **`intent_get_prompt.py`**：专门用于判断用户意图和参数的提示词，系统通过分析对话历史和用户输入，精确提取参数。
- **`get_intent_and_select_tools_prompt.py`**：此提示词用于更复杂的意图获取，它会分析之前的意图，生成完整的调用逻辑说明和参数状态。
- **`ask_for_param_prompt.py`**：当参数不完整时，此提示词生成具体的、自然语言的询问，例如"请补充您想查询的区域信息"。

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
- **`file_dynamic_tool.py`**：一个具体的实现类。它继承自 `DynamicToolGenerator`，其 `query_tool_info_list` 方法从 `dynamic-tools-data.json` 文件中读取工具列表信息，并通过 `generate_tools` 方法将其转换为 LangChain 可识别的工具对象。这实现了"数据驱动"的工具加载。
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
