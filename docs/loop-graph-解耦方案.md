# loop / graph 解耦方案（为将来删 graph 铺路）

> 状态：**P0 已落地（2026-11-12，只加了一个守卫测试，代码零改动）**；P1–P5 仍为计划。
> 目标不是现在删 graph，而是把它挪到一个"可整体拔掉"的插座上。

## 0. 目标与验收

一句话：**删 legacy = 删一个目录 + 改一行注册**，`app.py` 和 `agent/loop` 一行都不用动。

| # | 验收标准 | 检查方式 |
|---|---|---|
| 1 | `app.py` 的 `/chat` 端点 ≤ 120 行，且不含任何域分支/意图判断（**实测现状 938 行**；只做 P1–P4 约 170 行，把 pipeline 创建也挪进 channel 才能到 120） | `wc -l` + 人工 review |
| 2 | `app.py` 不出现 `graph`、`MODULE_HANDLERS`、`classify_intent`、`run_*_graph` | `grep -nE 'graph\|MODULE_HANDLERS\|classify_intent\|run_[a-z_]+_graph' app.py` |
| 3 | `agent/loop/**` 不 import `graph` / `langchain` / `langgraph` | 边界测试 |
| 4 | `graph/**` 不 import `agent.loop` | 边界测试 |
| 5 | 只有 `agent/channels/**` 允许同时 import 两侧 | 边界测试 |
| 6 | 删除演练可跑通：拔掉 legacy 后服务能起，13 个业务域走降级话术，场景/KB/生成文档不受影响 | 分支上演练一次 |

## 1. 现状耦合清单

| # | 耦合点 | 位置 | 现状 | 目标 |
|---|---|---|---|---|
| 1 | WS 端点混装 5 件事 | `app.py:2109-3011`（903 行） | KB 分支(2131)、loop 分支(2150)、场景分流(2178-2274)、业务状态机(2279-2881)、多模态兜底(2882-2904) 全在一个 `while True` 里 | 端点只做「解析 payload → 交给分发器 → 收尾」 |
| 2 | 13 张图的执行包装 | `app.py:241-1416`（1175 行） | `run_xxx_status_graph(websocket, state, ...)` 直接操作 websocket，无法单测 | 搬到 `channels/legacy/flows.py`，改用 `ctx.send(event)` |
| 3 | 域注册表 | `app.py:1651-1716` | `MODULE_HANDLERS` / `MODULE_RUNNERS` / `BUSINESS_MODULES` / `start_module_flow` | 搬到 `channels/legacy/registry.py` |
| 4 | 意图模块一个 `__init__` 导出两类东西 | `agent/intent/__init__.py` | classifier（**两侧共用**：loop 路由 + 旧链路）+ 13 个 `*Handler`（**只有旧链路用**） | 拆成 `shared/intent.py`（classifier+抽槽）与 `channels/legacy/handlers.py` |
| 5 | 会话状态是两套 | `memory/session_state.py` + `agent/loop/ws_bridge._sessions` | legacy 用 `IntentState` 槽位，loop 用 `Session.pending`；场景通道也用 `session_state` | 迁移期**不合并**；legacy 删掉后 `session_state` 只剩场景用途 |
| 6 | 场景通道反向依赖 loop | `app.py:1933` 及场景分支 import `agent.loop.scene_resolve` | 场景功能依赖了 loop 的目录 | `scene_resolve.py` 迁到 `agent/shared/` |
| 7 | 事件发送直接绑 websocket | 所有 `run_*_graph` 签名 | 无法脱离 FastAPI 测试 | `TurnContext.send` 抽象 |
| 8 | 退场开关 | `agent/loop/rollout.py`、`app.py` 的 `LOOP_FALLBACK_MODE` | 已有（`legacy` / `degrade`） | 作为"legacy 是否在册"的开关 |

## 2. 目标目录结构

```
app.py                          # 只剩 HTTP/WS 接入 + 分发（目标 ≤120 行的 agent_ws）
agent/channels/
  __init__.py                   # CHANNELS 注册表（唯一同时认识两侧的地方）
  base.py                       # TurnContext / ChannelResult / Channel 协议
  kb.py                         # 知识库直连（graph/knowledge_qa_langgraph）
  scene.py                      # 场景控制（graph/scene_control_langgraph）
  loop_channel.py               # loop 适配（agent.loop.ws_bridge）
  legacy/                       # ←←← 将来整目录删除
    __init__.py                 # 导出 LegacyChannel
    state_machine.py            # 「情况 1 / 1.1-1.5 / 情况 2」从 app.py 搬出
    flows.py                    # 13 个 run_xxx_graph（websocket → ctx.send）
    handlers.py                 # 从 agent/intent/handlers 迁入
    registry.py                 # MODULE_HANDLERS / MODULE_RUNNERS / BUSINESS_MODULES
agent/shared/
  intent.py                     # classifier + slot extractor（删 graph 后仍保留）
  scene_resolve.py              # 从 agent/loop 迁出（解开 #6 反向依赖）
```

## 3. 接缝契约（关键 60 行）

`agent/channels/base.py`：

```python
@dataclass
class TurnContext:
    user_id: str
    session_id: str
    query: str                    # 本轮原话
    payload: dict                 # 原始 payload（mode / image_url 等）
    send: Callable[[dict], Awaitable[None]]   # 唯一的事件出口，屏蔽 websocket
    original: str | None = None   # 追问轮的首问原话
    extra: dict = field(default_factory=dict)

class Outcome(str, Enum):
    handled = "handled"      # 已完整发事件（含 done），分发器直接结束本轮
    handoff = "handoff"      # 我不接，交给 target 通道（如 loop → scene）
    degraded = "degraded"    # 我接了但失败（原因见 reason）
    declined = "declined"    # 我不认识这个请求，正常继续

@dataclass
class ChannelResult:
    outcome: Outcome
    target: str | None = None
    reason: str | None = None

class Channel(Protocol):
    name: str
    priority: int                      # 顺序：kb(10) → scene(20) → loop(30) → legacy(40)
    def accepts(self, ctx: TurnContext) -> bool: ...
    async def try_handle(self, ctx: TurnContext) -> ChannelResult: ...
```

分发器（`agent/channels/__init__.py`，约 30 行）：

```python
async def dispatch(ctx: TurnContext) -> ChannelResult:
    for ch in channels():                     # 按 priority 排序
        if not ch.accepts(ctx):
            continue
        res = await ch.try_handle(ctx)
        if res.outcome is Outcome.handled:
            return res
        if res.outcome is Outcome.handoff and res.target:
            ctx = replace(ctx, **_handoff_state(res.target))
            continue
        if res.outcome is Outcome.degraded:
            return await degrade(ctx, res.reason)   # 统一兜底话术 + done
    return await degrade(ctx, "no_channel")
```

分发器负责两件最容易漏的事：**补 `done`**（排查记录案例 14）和**降级话术**。

legacy 是否在册：

```python
def channels() -> list[Channel]:
    items = [KbChannel(), SceneChannel(), LoopChannel()]
    if fallback_mode() == "legacy":        # 复用现有 _loop_fallback_mode()
        items.append(LegacyChannel())      # ← 删 graph 时删的就是这一行 + 这个目录
    return sorted(items, key=lambda c: c.priority)
```

## 4. 分步实施（每步可独立合入，线上行为不变）

| 步骤 | 内容 | 风险 | 回滚 |
|---|---|---|---|
| **P0** ✅ | 边界守卫测试 `tests/test_module_boundaries.py`（**已完成**，6 个用例，0.5 秒，见第 5 节） | 零 | 删文件 |
| **P1** | 引入 `base.py` + `dispatch()`；`app.py` 只把 loop 与 kb 包成 channel，legacy 暂时用一个内联 shim channel 包住现有代码 | 低（搬运） | 整体 revert |
| **P2** | 场景分支抽成 `channels/scene.py`；`scene_resolve` 迁 `agent/shared/`；`_scene_followup_pending` 变成 scene channel 的 `accepts()` | 中（场景判定口径易错，需跑 `test_scene_routing.py`） | 保持旧分支开关一个版本 |
| **P3** | 业务状态机 + 13 个 `run_xxx_graph` 抽成 `channels/legacy/*`（纯搬运，`websocket` → `ctx.send`） | 中（1175 行搬运，靠测试兜） | 同上 |
| **P4** | 注册表接 `LOOP_FALLBACK_MODE`；删 `app.py` 里的旧分支残渣；给每个 channel 跑同一套契约测试 | 低 | flag 切回 legacy |
| **P5** | 更新改造文档 + 在临时分支做一次"删除演练"（`git rm` 后起服务、跑测试，再 revert） | 零 | 分支丢弃 |

P2/P3 之间建议插一次线上冒烟（13 域各 1 条 + 场景 4 条），因为这两步最容易改变行为。

## 5. 边界守卫测试（P0，最重要的一条）

已落地：`tests/test_module_boundaries.py`，用 `ast` 扫描而不是字符串匹配。

四条规则（前三条现状 0 违规，新增即红；第四条是存量基线）：

```python
# 1/2/3 硬规则：扫描目录 → 禁止依赖
(("agent/loop", ("graph", "langchain", "langchain_core", "langgraph")),
 ("agent/loop", ("agent.intent.handlers",)),
 ("graph",    ("agent.loop",)))

# 4 app.py 的祖父基线（19 条，只许变少）：新增失败 + 清理后不同步也失败
APP_GRAPH_IMPORTS = {...}
```

三个实现要点（都是踩过才知道的）：

1. **`langchain_core` 必须单独列**：它是独立顶层包，`module.startswith("langchain.")` 匹配不到，
   漏了会让 `app.py` 的 `BaseMessage` 依赖逃过检查；
2. **函数内的延迟 import 也要抓**：`app.py` 的 `from agent.loop.rollout import ...` 就写在 handler 里，
   只扫模块顶层等于留后门；
3. **守卫自身要有自测**：`test_detector_catches_new_violation` 造一个假文件，
   断言能识别违规并报对行号（已实测：`agent/loop/bad.py:3 — import agent.intent.handlers`）。

失败信息带 `文件:行`，例如 `agent/loop/bad.py:1 — import graph.person_status_langgraph`，
—— **解耦靠测试守，不靠自觉**。

另加一个契约测试（P4 落地）：对注册表里的每个 channel 跑同一组断言
（`handled` 必须发过 `done`、`degraded` 必须有话术、`handoff` 必须有 target）。
legacy 删除后这个文件只少跑一个用例，不用改测试逻辑。

## 6. 实测工作量（2026-11-12 量的）

### 最终要删的东西

| 内容 | 位置 | 行数 |
|---|---|---|
| 业务状态机（情况 1 / 1.1–1.5 / 情况 2） | `app.py:2279-2878` | 600 |
| 13 个 `run_xxx_graph` | `app.py:241-1416` | 1,176 |
| `MODULE_HANDLERS` / `MODULE_RUNNERS` / `start_module_flow` | `app.py:1651-1719` | 69 |
| 多模态 + `rag_pipeline` 兜底 | `app.py:2879-2900` | 22 |
| 13 张业务图 | `graph/*_langgraph.py` | **7,350** |
| `reactive_pipeline.py`（InfoDoubleCheck 兜底） | `graph/` | 581 |
| `person_status_pipeline.py` | `graph/` | 331 |
| 13 个 Handler | `agent/intent/handlers/` | **3,297** |
| **合计** | ~35 个文件 | **≈ 13,400 行** |

其中约 13,000 行是**整文件删除**（机械操作）；真正要逐行处理的是 `app.py` 里那 1,867 行。

### 删之前要搬的东西（真正的工作量）

| 搬运 | 目标 | 行数 | 是否改逻辑 |
|---|---|---|---|
| 13 个 `run_xxx_graph` | `channels/legacy/flows.py` | 1,176 | `websocket` → `ctx.send` |
| 业务状态机 | `channels/legacy/state_machine.py` | 600 | 改成 `accepts()` + `try_handle()` |
| 域注册表 | `channels/legacy/registry.py` | 69 | 否 |
| 13 个 Handler | `channels/legacy/handlers.py` | 3,297 | **`git mv` 零改动** |
| 场景分流 + `run_scene_control_graph` | `channels/scene.py` | 258 | 是（判定口径别动） |
| `scene_resolve` | `agent/shared/` | ~200 | 只改 import |
| `agent/intent/__init__` 拆分 | — | 6 | 改导出 |
| 新增契约 + 分发器 | `agent/channels/base.py` | ~150 | 新写 |

**≈ 5,300 行搬运，其中 3,500 行是零改动移动**。

### 删完之后

- `app.py`：3,094 → **≈ 970 行**；`/chat` 端点 938 → **≈ 170 行**
- 保留的通道：`scene`(932) + `knowledge_qa`(229) + `gen_doc`(759) + `asr`(121)
  - `scene` **不是"只叫 graph"**：`scene_control_langgraph.py:7` 真的 `from langgraph.graph import StateGraph, END`，
    6 个节点 + 1 个条件边 + `compile()`；
  - 且 `:766` 用 `llm.ainvoke(prompt)`，要求传入 langchain Runnable（`app.py:117` 给的是 `CustomLLMFactory().llm`），
    所以删 legacy **不能**顺手把 `langgraph` / `langchain` 从 requirements 里去掉；
  - 想卸掉的话，需要单独把场景端口化成纯 async（约 100–150 行，见 §8 第 7 条）。
- `agent/intent`：`classifier`(4,341) + `slots`(1,550) 保留，`handlers`(3,297) 删掉

### 两种路线对比

| 路线 | 改动量 | 风险 |
|---|---|---|
| **先解耦再删**（本文 P0–P5） | 搬 5.3k 行 + 删 13.4k 行 | 低：每步行为不变，删除当天只改 2 行注册 |
| **直接删，不解耦** | 手改 938 行 endpoint | 高：中途是"端点半残"状态，只能一次做完，出错只能整体回滚 |

### 粗估工期（1 人）

| 步骤 | 预估 |
|---|---|
| P0 边界测试 | ✅ 已完成（半天） |
| P1 契约 + 分发器 | 1 天 |
| P2 场景通道（最易出行为差异） | 1 天 |
| P3 搬运状态机 + 13 个流程 | 2 天 |
| P4 接开关 + 清残渣 | 半天 |
| P5 删除演练 + 冒烟 | 半天 |

合计 **≈ 6 人日**，其中约 40% 是回归（13 域 × 场景 4 条 × 异常 1 条）。

## 7. 将来的"删除演练"清单（一步到位）

```bash
git rm -r agent/channels/legacy
git rm graph/{person,security,canteen,vehicle,information,energy,meeting,emergency_fire,emergency_perimeter,device_status,compositive_overview,device_query,twins_inspection}_langgraph.py
git rm graph/reactive_pipeline.py graph/person_status_pipeline.py graph/stream_utils.py
# agent/channels/__init__.py 删掉 `items.append(LegacyChannel())` 两行
# agent/intent/__init__.py 删掉 handlers 导出
LOOP_FALLBACK_MODE=degrade
# requirements.txt 去 langchain / langgraph
```

预期：只有 legacy 相关测试变红，且它们都在 `channels/legacy` 里一起删掉；`app.py` 零改动。

## 8. 注意事项（踩过的坑）

1. **通道顺序就是语义**：现在是 loop → scene → 业务状态机。抽成 channel 后必须保持 `loop.priority < scene.priority < legacy.priority`，否则场景指令会被 legacy 抢走。
2. **`done` 只能由分发器或 channel 中一方发**：建议约定"channel 发事件、分发器发收尾"，避免双 `done` 让前端提前解锁。
3. **两套会话状态不要合并**：legacy 的 `IntentState`（槽位/追问）和 loop 的 `Session.pending` 语义不同，迁移期强行合并会重现"业务追问被 loop 截胡"的老问题。
4. **场景判定口径是共享资产**：`should_route_scene` / `has_strong_scene_cue` 的相邻字符串拼接写法是**故意的**（`test_monitor_data_query_stays_business`），抽 shared 时原样搬，不要"顺手修"。
5. **`MODULE_RUNNERS` 不是死代码**，`start_module_flow` 在用（`app.py:1716`），一起搬走。
6. **`agent/intent` 拆分的底线**：`classifier` 与 slot extractor 必须留在 loop 能 import 的中立位置，否则删 graph 会把 loop 的抽槽能力一起删掉。
7. **场景图的两个隐藏依赖**（实测，容易误判成"只叫 graph"）：
   - `scene_control_langgraph.py:7` 是**真的** langgraph：`StateGraph` + 6 个节点
     （init / plan_action / ask_param / give_up / call_tool / finalize）+ 1 个条件边 + `compile()`；
   - `:766` 的 `llm.ainvoke(prompt)` 要求 langchain Runnable，`app.py:117` 传的是
     `CustomLLMFactory().llm`。所以**删 legacy 后 `langgraph` / `langchain` 仍要留在 requirements**，
     除非把场景端口化成纯 async（6 个节点里只有 `plan_action` 调模型，其余是规则和 MCP 调用，
     约 100–150 行）；
   - `:9` import 了 `agent.intent.slots._parse_chinese_number` 这个**私有函数**：
     拆 `intent` 时先把它公开成 `parse_chinese_number`，否则场景通道会断在一个下划线函数上。

## 9. 可选的先行改造（本次未落地）

下面 4 处小改造**不是删除 legacy 的前置条件**，只是能让 P1 的 `dispatch()` 少设计一遍。
每项都能单独合入、单独回滚，代码量都不大（合计约 150 行 + 测试）。

| 改造 | 位置 | 改动量 | 解决什么 |
|---|---|---|---|
| `degraded` / `handoff_target` 字段 | `agent/loop/pipeline.py`（`PipelineResult`） | ~25 行 | handoff 不再默认"交回旧链路"；降级有明确原因可埋点 |
| `ChannelOutcome` 三态返回 | `agent/loop/ws_bridge.py` | ~60 行 | 调用方能区分 handled / handoff_scene / degraded，P1 直接复用 |
| `LOOP_FALLBACK_MODE` 开关 | `app.py` | ~30 行 | `legacy`（可回落）/ `degrade`（无退路），P4 注册表就用它 |
| 模型调用重试一次 | `agent/loop/agent.py` | ~35 行 | 网络抖动自愈，避免没退路时直接给用户降级话术 |

注意：做了第 2、3 项之后，`handle_chat` 的返回值会从 `bool` 变成枚举，
`tests/test_loop_ws_bridge.py`、`tests/test_loop_entities.py` 里各有一处
`assert handled is True` 需要同步改成 `== "handled"`。

先做 P0（边界测试）再做这些也行；两者互不依赖。
