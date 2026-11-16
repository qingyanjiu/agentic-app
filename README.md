# 智慧园区问答智能体（agentic-app）

面向智慧园区的**多域问答智能体后端**：自然语言提问 → 意图识别 → 调 MCP（Java sidecar）拿数据 →
模板或模型组织成人话，通过 WebSocket 流式返回；同时承载 UE 3D 场景控制的下发通道。

> **架构细节、协议契约、环境变量、已知限制：见 [`docs/架构文档.md`](docs/架构文档.md)（唯一入口文档）。**
>
> 当前分支 `feature/loop-only`：已经**彻底移除 LangChain / LangGraph**——
> 13 张业务图、报告生成（gen_doc）、语音链路、旧记忆持久化均已删除；
> 业务问答统一走 `agent/loop`（loop agent），场景控制是顺序调用的 `agent/scene_control.py`。

## 能力概览

- **13 个业务域**：人员态势、安防态势、食堂、车辆、信息发布、能源、会议、消防、周界、孪生巡检、
  设备查询（台账口径）、设备状态、综合概览；69 个子类型。
- **两级通道**：L1 预路由命中 → 不调模型直接答（模板 + 结构化清单）；
  L2 loop agent → 模型自选工具、多轮调用，全程流式。
- **场景控制**：20 个场景动作（切楼层、看监控、改天气、防区布防…），设备名自动解析成点位 tag。
- **结构化清单**：设备列表由工具层产出 `list` 事件，不经模型改写。

## 快速开始

```bash
pip install -r requirements.txt          # 只含直接依赖（无 langchain 家族）
uvicorn app:app --host 0.0.0.0 --port 8001
```

必需的环境变量（其余有默认值，见架构文档第 10/13 节）：

```bash
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-flash
LLM_API_KEY=sk-xxx
MCP_BASE_URL=http://<sidecar-host>:2223/mcp    # Java MCP sidecar
```

调试页：`http://127.0.0.1:8001/static/`（`web/`）。

## 目录速览

```
app.py                 FastAPI 入口：WS /chat、/upload/image、分流编排
agent/loop/            Loop Agent（router / pipeline / agent / domains / runtime / ws_bridge）
agent/scene_control.py UE 场景控制编排（顺序调用，无图引擎）
agent/intent/          意图分类（BGE 向量）+ 槽位抽取（正则/词典）
mcp_client/            MCP 配置与直连客户端（scene / knowledge）
memory/session_state.py 追问态会话（内存 + TTL）
models/llm.py          唯一模型配置（openai 兼容 / ollama）
web/  static/          调试页前端　tests/  pytest　scripts/  打包与冒烟
```

## 测试

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python3 -m pytest tests -q
# 期望：544 passed / 1 skipped / 7 xfailed
python tools/eval_intent.py                  # 意图识别评测门禁（分级退出码）
```

## 打包

```bash
./scripts/pack.sh                # 输出 agentic-app-src-<时间>.zip，不含 .git / BGE 权重 / 缓存
```

## 文档

| 文档 | 用途 |
|---|---|
| [`docs/架构文档.md`](docs/架构文档.md) | 架构、协议、环境变量、已知限制（**先看这个**） |
| [`docs/Java-MCP接口清单与Mock返回格式.md`](docs/Java-MCP接口清单与Mock返回格式.md) | MCP 工具与字段口径 |
| [`docs/UE场景控制接入说明.md`](docs/UE场景控制接入说明.md) | 场景工具清单与 UE 侧对接 |
| [`docs/前端对接-list事件.md`](docs/前端对接-list事件.md) | `list` 事件与前端渲染契约 |
| [`docs/loop-agent-TODO.md`](docs/loop-agent-TODO.md) | 在办清单与优先级 |
| [`docs/loop-agent-skill方案.md`](docs/loop-agent-skill方案.md) | skill 化方案（未实施） |
| [`docs/问题排查记录.md`](docs/问题排查记录.md) | 历史 bug 案例（代码注释按「案例 N」引用） |
| [`docs/语料评测记录.md`](docs/语料评测记录.md) | 意图分类基线与阈值依据 |
| [`docs/测试方案.md`](docs/测试方案.md) | 分层测试与门禁 |
