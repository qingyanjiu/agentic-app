# Loop Agent 改造文档

> 分支：`feature/real-agent`
> 状态：内核、三级分流、P1 健壮性、app.py 灰度入口均已实现；灰度默认关闭
> 最后更新：2026-11-09

---

## 1. 这份文档解决什么问题

把「13 张 LangGraph 业务图 + 13 个 handler」的重复结构，收敛成：

- **一份工具声明**（每个业务域一个模块）
- **一个 loop 内核**（模型选工具、多轮执行）
- **一个预路由**（分类器直接产出工具调用，兜住延迟）

目标不是"更智能"，而是**业务口径只维护一处**。当前 3 个 P0 追问缺陷和 1 个 xfail（`alarm_detail` 缺参规则不一致）都是"同一规则在图和 handler 各写一遍"的症状。

---

## 2. 目标架构

```
                        ┌──────────────────────────┐
   WebSocket /chat ───► │  ChatPipeline.handle()   │
                        └────────────┬─────────────┘
                                     ▼
                        ┌──────────────────────────┐
                        │         Router           │
                        │  L0 规则 / L1 BGE 分类   │
                        └──────┬────────────┬──────┘
                    命中       │            │ 未命中 / 跨域 / 低置信
                               ▼            ▼
                     ┌──────────────┐  ┌──────────────┐
                     │   Executor   │  │  LoopAgent   │
                     │ 直接调工具   │  │ 模型选工具   │
                     └──────┬───────┘  └──────┬───────┘
                            │ escalate        │
                            └────────►────────┘
                               ▼
                     ┌──────────────────────┐
                     │      Composer        │
                     │ 工具结果 → 自然语言   │
                     └──────────────────────┘
```

三条路径共用 `Executor` 和 `Composer`，区别只在"谁决定调哪个工具"。

---

## 3. 组件清单

| 文件 | 职责 | 关键 API |
|---|---|---|
| `agent/loop/types.py` | 数据结构 | `ToolSpec` / `ToolOutcome` / `LoopResult` / `DomainPlan` |
| `agent/loop/registry.py` | 工具注册表 | `ToolRegistry.add_domain()` / `to_openai_tools()` |
| `agent/loop/router.py` | 预路由 | `Router.route(query, pending)` → `RoutePlan \| None` |
| `agent/loop/executor.py` | 工具执行 | `Executor.execute(tool, args)` |
| `agent/loop/composer.py` | 回答生成 / 追问模板 | `Composer.compose(..., on_delta=)` / `ask_text()` |
| `agent/loop/pipeline.py` | 编排 | `ChatPipeline.handle(query, session, cancel, on_delta)` |
| `agent/loop/agent.py` | Loop 内核 | `LoopAgent.run(query, history, cancel)` |
| `agent/loop/validate.py` | 参数校验 / 上下文压缩 | `validate_args()` / `compress_tool_messages()` |
| `agent/loop/rollout.py` | 灰度判定 | `Rollout.decide(user_id, session_id, mode)` |
| `agent/loop/ws_bridge.py` | WebSocket 桥接 + 会话池 | `handle_chat(...)` → True/False |
| `agent/loop/domains/*.py` | 各业务域 | `build_domain(toolbox)` / `route_plan(query)` |
| `agent/loop/cli.py` | 命令行验证 | `--list-tools` / `--dry-run` / `--route` / `--chat` |

---

## 4. 目录结构

```
agent/loop/
├── __init__.py          # 对外导出 + build_registry()
├── types.py             # 数据结构
├── registry.py          # DomainSpec / ToolRegistry
├── runtime.py           # McpToolbox（官方 mcp SDK，不依赖 langchain）
├── govern.py            # 工具输出截断（2000 行 / 50KB + 落盘）
├── validate.py          # 参数校验 + 上下文压缩
├── router.py            # 三级分流的前两级
├── executor.py          # L1 执行器
├── composer.py          # 回答生成 + 追问模板
├── pipeline.py          # Session + ChatPipeline
├── agent.py             # Loop 内核
├── cli.py               # 命令行验证
└── domains/
    ├── __init__.py            # 13 个域装配 + PREVIEWERS
    ├── _generic.py            # 通用查询域工厂（映射表驱动）
    ├── person_status.py       # 人员态势（6 子类型）
    ├── security_status.py     # 安防态势（11）
    ├── canteen_status.py      # 食堂管理（3）
    ├── vehicle_status.py      # 车辆态势（5）
    ├── information_status.py  # 信息发布（6）
    ├── energy_status.py       # 能源态势（7）
    ├── meeting_status.py      # 会议管理（6）
    ├── emergency_fire.py      # 消防态势（4）
    ├── emergency_perimeter.py # 周界态势（4）
    ├── device_status.py       # 设备态势（8，统计口径）
    ├── device_query.py        # 设备查询（2，台账口径 + 详情定位）
    ├── compositive_overview.py# 综合总览（2）
    └── twins_inspection.py    # 孪生巡检（4）
```

合计 **13 个域 / 69 个子类型 / 14 个工具**（device_query 有 list + detail 两个）。

---

## 5. 关键设计决策

### 5.1 参数抽取下沉到工具内部

工具 schema 只有一个字段：

```json
{ "type": "object", "properties": { "query": { "type": "string" } }, "required": ["query"] }
```

`execute` 内部调用现有的 `extract_*_slots(query)` 做规范化。

**原因**：模型不需要知道 `syncSource`、`startTime`、`spaceId` 这些接口细节；现有 25 项准确率优化（时间解析、别名词典、楼层归一化）原样复用。

### 5.2 子类型判断不交给模型

`person_status__query` 一个工具覆盖 realtime / enter / leave / count / flow / structure，内部按 `query_type` 选 MCP 工具。

**原因**：子类型靠中文关键词区分，正则比模型稳定；而且不增加 LLM 往返。

### 5.3 为什么要有 L1（而不是全走 loop）

| 路径 | 延迟构成 |
|---|---|
| L1 直连 | BGE 分类 ~30ms + MCP + 1 次 LLM（Composer） |
| L2 loop | 1 次 LLM 选工具 + MCP + 1 次 LLM（Composer） |

L1 把"选工具"交给分类器，**省掉一次 LLM 往返**，与现有图持平。这是"预路由兜延迟"的全部含义。

### 5.4 追问不调 LLM

`ask_text()` 用模板映射（`device_keyword` → "请问您想查看哪台设备？…"）。追问轮 0 次模型调用，比现在更快，且输出确定。

### 5.5 pending 存原问句，不存槽位

```python
merged_query = f"{pending.original_query} {reply}".strip()
plan = planner(merged_query)          # 整句重新解析
```

**原因**：复用现有全句解析能力，避免"短回复被默认值污染"（P0 根因）。

### 5.6 工具串行执行

**原因**：现有 MCP 客户端与 Java sidecar 的连接模型不支持安全并发；且多数工具有先后依赖（先列表定位 id，再查详情）。

### 5.7 escalate 与 error 的区分

| 情况 | `is_error` | `escalate` | 处理 |
|---|---|---|---|
| 参数不足 | ✔ | ✘ | 走追问 |
| 参数校验失败 | ✔ | ✘ | 让模型修正 |
| 工具不存在 | ✔ | ✔ | 升级 L2 |
| 后端超时 / 异常 | ✔ | ✔ | 升级 L2 |

**原因**：追问和"换工具"是两种完全不同的恢复策略，混在一起会导致追问被误升级。

### 5.8 L0 场景控制暂不接管

路由到 `level="L0", domain="scene", tool=None`，pipeline 返回 `handoff`，由旧链路处理。

**原因**：场景动作是破坏性操作，灰度期不动它。等工具适配完成后再纳入。

**判定口径必须与旧链路一致**（这里踩过一次线上坑）：旧链路用
`should_route_scene(intent_info, query)`，是「正则 + 向量兜底」两层；
最早的新链路只抄了正则 `has_strong_scene_cue`，于是：

| 说法 | 强场景词正则 | 场景向量分 | 业务分 | 旧链路 | 只抄正则的新链路 |
|---|---|---|---|---|---|
| 关闭监控窗口 | ❌ 不命中 | 0.687 | 0.594 | 场景 | **L2 → 模型答"没有这个能力"** |
| 打开监控视频 | ❌ 不命中 | 0.652 | 0.595 | 场景 | **L2 → 同上** |
| 退出监控页面 | ❌ 不命中 | 0.688 | 0.592 | 场景 | **L2 → 同上** |

`_SCENE_STRONG_CUES` 里「打开/查看/显示/展示/调出」都有，唯独没有
「关闭/关掉/退出」—— 用户打开监控视频后说"关闭监控窗口"正好踩中。

修法：`router.route()` 在分类之后补一道 `should_route_scene`（与旧链路同一函数），
命中即判 L0 handoff。正则快路径保留在前，命中时不必再跑 BGE。
这样灰度链路的场景判定与存量链路**完全同源**，不会出现"同一个问题两条链路结论不同"。

**存量正则的第二处差异**：`_SCENE_STRONG_CUES` 里这两行是**故意**拼成一条的
（Python 相邻字符串字面量自动拼接，中间没有 `|`）：

```python
r"(查看|打开|调出|看|看下|显示|展示).{0,4}(监控|摄像头|画面).{0,4}"   # 注意：行尾无 |
r"[A-Za-z0-9][A-Za-z0-9_\-]{2,}|"                                  # 行尾有 |
```

合起来语义是「**场景动词 + 监控/摄像头/画面 + ≥3 字符的设备编号**才算强场景指令」：

```python
has_strong_scene_cue("查看监控 jk_zl_1f_0000")  # True  ← 有编号
has_strong_scene_cue("查看监控数据")             # False ← 无编号，留在业务
has_strong_scene_cue("查看监控在线率")           # False ← 同上
```

`tests/test_scene_routing.py` 的 `test_monitor_data_query_stays_business` 和
`test_camera_tag_after_monitor_is_scene_cue` 就是锁这个的 —— **这不是 bug，
不要补 `|`**（补了 `查看监控数据` 会被强场景词抢走）。

那么不带编号的「查看监控」怎么走场景？靠 `_SCENE_VIEW_CUES`
（`(查看|看|看下|看一下).{0,4}(设备|摄像头|监控|画面|…)`）兜住，且要求无问句语气。

差异就出在这里：这套两层规则对**带问号**的指令不友好 ——
「能打开监控吗」既没有编号（第一层不中），又因问句被 `_SCENE_VIEW_CUES`
判成业务（第二层返回 `_QUESTION_CUES` → True）：

| 说法 | 存量判定 | 加 ②.5 后 |
|---|---|---|
| 能打开监控吗 | 业务 → L2「没有能力」 | L0 |
| 能不能看一下监控 | 业务 → L1 security_status | L0 |

**为什么不改存量正则**：`has_strong_scene_cue` / `is_business_query` 被 13 张旧图和
旧链路共用。实测补 `|` 后「能打开监控吗」能正确归场景，但会让
`查看监控数据` / `查看监控在线率` 一起被判强场景词（直接违反上面两个测试），
还要额外加数据词否决才补得回来 —— 改动面和回归风险都不值得。所以只在新链路补：

```python
_SCENE_MONITOR_CUE = re.compile(
    r"(查看|看一下|看下|看看|看|打开|调出|显示|展示|切到).{0,4}(监控|摄像头|画面|视频)"
)
_DATA_VETO = re.compile(r"数据|统计|数量|多少|在线率|占比|排名|趋势|报表|明细|清单|列表|记录|日志|台账|情况")
```

命中 `_SCENE_MONITOR_CUE` 且不含 `_DATA_VETO` → L0。数据词否决同时用在 ③.5 上，
否则 `should_route_scene` 里的 `_SCENE_VIEW_CUES` 会把「看摄像头在线率」判成场景。

实测（真实 BGE）：

```
查看监控 / 查看监控回放 / 打开监控 / 能打开监控吗 / 能不能看一下监控 / 帮我看看摄像头  -> L0
看摄像头在线率          -> L2（业务）
查看监控里的情况         -> L1 security_status
监控设备离线了多少台      -> L2（业务）
```

### 5.9 设备查询的楼层/名称过滤

MCP 的 `listDeviceOnly` **没有按楼层筛选的参数**（`spaceId` 需要内部 id，用户给不出来），
所以设备查询采用「全量拉取 + 本地确定性过滤」：

```
syncSource 下发给 Java（缩小返回体）
        ↓
拿到 579 台（全量）／按类型后的子集
        ↓
Python 侧过滤：楼层 + 名称
        ↓
只把命中行喂给模型（最多 60 行）
```

楼层归一化（**以真实数据为准**）：

| 用户说法 | 数据里的写法 | 归一化 |
|---|---|---|
| 1楼 / 一层 | `1F` | `1F` |
| 负一层 / 地下一层 / B1 / -1楼 | `-1F` | `-1F` |
| 11楼 | `11F` | `11F` |

注意：数据里负层是 **`-1F`**，不是 `B1F`。楼层从 `spaceName`（如 `园区/科研办公楼/3F`）
或 `name`/`rawName`（如 `3F-主楼梯MJ`、`-1F-电梯厅MJ`）两处提取，
因为 579 行里有 144 行 `spaceName` 为空。

名称过滤走 `rawName` / `name` / `code` 三者包含匹配；对 `3F-主楼梯MJ` 这类带楼层
前缀、slots 抽不到的名称，用正则做兜底。

**两个已知坑**：

1. `slots.py` 的 `parse_area_slot` 会把「负一层」解析成「1楼」，
   所以楼层优先从**用户原句**归一化，取不到才用 slots 的 `area`；
2. 带楼层前缀的设备名（`3F-主楼梯MJ`）不在 `device_keyword` 里，需要正则兜底。

**为什么不交给 LLM 过滤**：579 行原文约 6k token，全塞给模型既费 token 又容易漏；
楼层匹配本身是确定性的，放在工具里更稳。

---

## 6. 一次请求的完整时序

### L1（分类命中）

```
1. Router.route("3楼的门禁设备都列一下")
   → 分类 intent=device_query, score=0.774
   → 无跨域词、score ≥ 0.75
   → device_query.route_plan → {tool: "device_query__list", args: {query: "…"}}
2. Executor.execute("device_query__list", {query})
   → validate_args 通过
   → before_tool（未配置）
   → device_query 工具内部：extract_device_query_slots → syncSource=0
   → McpToolbox.call("devicequery", "device_query:listDeviceOnly", {syncSource: 0})
   → 格式化 + govern
3. Composer.compose(query, 结果文本, history, on_delta)
   → 1 次 LLM，流式返回
4. Session.add_turn(query, answer)
```

### L2（跨域/低置信）

```
1. Router.route("对比一下昨天和今天的告警，再看看设备在线率")
   → 命中跨域词"对比/再看看" → None
2. LoopAgent.run(query, history, cancel)
   → 模型第一次：tool_call("security_status__query", {...})
   → 执行 → 结果回填
   → 模型第二次：tool_call("device_status__query", {...})
   → 执行 → 结果回填
   → 模型第三次：纯文本回答
3. Session.add_turn(...)
```

---

## 7. 路由规则

| 条件 | 结果 |
|---|---|
| `pending` 存在 | 拼接原问句重新规划（L1） |
| `has_strong_scene_cue(query)` 且 `not is_business_query(query)` | L0 → handoff |
| `intent == "other"` | None → L2 |
| 命中跨域词表 | None → L2 |
| `score < 0.75` | None → L2 |
| 域无可用工具 | None → L2 |

跨域词表（`router.CROSS_DOMAIN_HINTS`）：

```
对比 比较 分别 同时 还有 以及 顺便 然后 再看 另外 和昨天 比昨天
```

阈值 `high_confidence=0.75` 是初始值。**上线前应用 `docs/语料评测记录.md` 跑一遍分数分布，取准确率拐点。**

---

## 8. 接入 app.py（灰度已实现）

### 8.0 灰度配置

灰度开关全部通过**环境变量**配置，改完重启生效（进程级单例）。

| 环境变量 | 默认 | 作用 |
|---|---|---|
| `LOOP_ROLLOUT_PERCENT` | `0` | 命中比例 0–100，按 `user_id:session_id` 稳定哈希取模 |
| `LOOP_ROLLOUT_USERS` | 空 | 白名单，逗号分隔的 `user_id` 或 `session_id` |

payload 也可以强制指定：

| payload | 效果 |
|---|---|
| `{"query": "...", "mode": "loop"}` | 强制走新链路（联调/回归用） |
| `{"query": "...", "mode": "legacy"}` | 强制走旧链路（出问题时的应急开关） |
| `mode=kb` / `scene` / `gen_doc` / `asr` | 不接管，保持原逻辑 |

判定优先级：`mode` 显式 > 白名单 > 比例。没有单独的灰度总开关——配了
`LOOP_ROLLOUT_PERCENT`（>0）或 `LOOP_ROLLOUT_USERS` 就算开，两者都空即关闭。

常用配置：

```bash
# 联调：只给自己开
LOOP_ROLLOUT_USERS=louisliu uvicorn app:app

# 灰度 10%
LOOP_ROLLOUT_PERCENT=10 uvicorn app:app

# 全量（等价 AGENT_MODE=loop）
LOOP_ROLLOUT_PERCENT=100 uvicorn app:app

# 紧急回滚（不重启也行：前端带 mode=legacy）
AGENT_MODE=legacy uvicorn app:app
```

验证命中情况：

```bash
grep "\[loop\] done" app.log | tail -5
# [loop] done level=L1 reason=answered escalated=False rollout=bucket=7<10
```

`rollout=` 字段直接说明这次为什么走新链路（`mode=loop` / `whitelist` / `bucket=7<10`）。

### 8.1 实际接入代码

`app.py` 的 `/chat` 里，在 `mode=kb` 分支之后、场景分支之前：

```python
if query:
    try:
        from agent.loop.rollout import get_rollout
        from agent.loop.ws_bridge import handle_chat as loop_handle_chat

        loop_decision = get_rollout().decide(user_id, str(session_id), payload.get("mode"))
        if loop_decision.enabled:
            loop_handled = await loop_handle_chat(
                websocket, query, user_id, str(session_id), llm, loop_decision
            )
            if loop_handled:
                continue
    except Exception as loop_err:
        logging.error(f"[loop] 灰度链路异常，回落旧链路: {loop_err}", exc_info=True)
```

三个安全设计：

1. **异常回落**：整段包在 `try/except` 里，任何异常都记日志并继续走旧流程；
2. **handoff 回落**：`handle_chat` 返回 `False` 表示"没接管"（场景控制），继续走旧流程；
3. **不改旧代码**：只插入一段 `if`，原有分支一行未动。

### 8.2 Session 的存放

`ws_bridge.get_session()` 按 `user_id:session_id` 缓存 `Session`，并做空闲回收：

```python
_sessions: dict[str, Session] = {}
_touched: dict[str, float] = {}
SESSION_TTL = 300.0      # 5 分钟无活动即回收
```

**TODO**：多副本部署时改 Redis 存储（与现有 P2 备忘一致）。

### 8.3 事件映射

| Pipeline / Loop 事件 | 前端事件 | 说明 |
|---|---|---|
| `on_delta`（Composer） | `{"type":"answer","content":"分块"}` | L1 流式，**追加**语义 |
| `assistant_delta`（LoopAgent） | `{"type":"answer","content":"分块"}` | L2 流式，模型边说边出字 |
| `tool_start`（LoopAgent） | `{"type":"tool","name":"工具名"}` | 前端据此把前面的文字落成「过程稿」气泡 |
| `tool_end` | 不发 | 下一次 `answer` 会清掉状态提示，发了反而闪烁 |
| `entities`（LoopAgent） | `{"type":"list","kind":"device","items":[…]}` | 结构化清单，工具产出即发，不经过模型。前端按 `kind` 渲染可点控件（见第 21 节） |
| `asked` | `{"type":"ask","question":"..."}` | 追问（模板生成，不调模型）。**前端读 `question`，不是 `content`** |
| `error` | `{"type":"error","content":"..."}` | |
| 结束 | `{"status":"done","level":"L1/L2","escalated":bool}` | `level` / `escalated` 用于埋点 |

约束：一个字符都没流出去时（`streamed_chars == 0`），才补发整段 `answer`；
否则前端会看到重复内容——这是"看起来没走流式"的根因之一，已用 `streamed_chars` 计数挡住。

前端侧（`AiChatDialog.vue`）的配套改动：

- `case 'tool'` 里先 `flushAnswer(true)`：把模型调工具前那段话落成独立「过程稿」气泡，
  之后的流式内容进入新气泡，两者不再混成一条；
- 过程稿用 `ai-message--process` 弱化样式（虚线边框、半透明、小字号）区分最终回答；
- `clearConversation()` 同时清 `processMessageIds`，避免 id 复用后样式串台。

### 8.4 灰度和回滚

- 灰度：`mode=loop` 显式开关，或按 `user_id` 哈希比例
- 回滚：删掉分支即可，旧路径完全未改动

---

## 9. 上下文治理

| 机制 | 位置 | 行为 |
|---|---|---|
| 工具输出截断 | `govern.govern()` | 超过 2000 行 / 50KB 截断，完整内容落盘并给出路径 |
| 上下文压缩 | `validate.compress_tool_messages()` | 工具结果总量超 60k 字符时，从最早的长结果开始替换为"保留开头 300 字"的占位 |
| 历史窗口 | `Session.max_history` | 默认 12 轮 |
| 步数上限 | `LoopAgent.max_steps` | 默认 8 |

**已知限制**：压缩是"占位式"，不是语义摘要。如果后续出现"需要跨轮记住细节"的场景，再引入 LLM 摘要。

---

## 10. 超时与中断

| 项 | 默认值 | 位置 |
|---|---|---|
| 单工具超时（L2） | 30s | `LoopAgent(tool_timeout=)` |
| 单工具超时（L1） | 30s | `Executor(timeout=)` |
| 前端中断 | — | `cancel: asyncio.Event`，在每轮开始和每个工具前检查 |

**已知限制**：工具执行过程中不能立即中断（只在执行前检查）。如需强中断，需要把 `cancel` 传进工具实现。

---

## 11. 工具钩子

```python
async def before_tool(name: str, args: dict) -> ToolOutcome | None:
    # 返回非 None 即阻断，用返回值作为给模型的结果
    if name == "scene__control" and not user_has_permission:
        return ToolOutcome("当前账号无权执行场景控制", is_error=True)

async def after_tool(name: str, args: dict, outcome: ToolOutcome) -> ToolOutcome:
    # 可改写结果：脱敏、追加口径提示
    return outcome
```

两者在 `LoopAgent` 和 `Executor` 上语义一致。

---

## 12. 配置项

| 参数 | 默认 | 说明 |
|---|---|---|
| `Router.high_confidence` | 0.75 | L1 直连阈值 |
| `Router.low_confidence` | 0.60 | 预留（低于此值直接 L2） |
| `LoopAgent.max_steps` | 8 | 最大模型轮数 |
| `LoopAgent.tool_timeout` | 30.0 | 单工具超时（秒） |
| `LoopAgent.context_budget` | 60000 | 工具结果字符预算 |
| `Executor.timeout` | 30.0 | L1 单工具超时 |
| `Session.max_history` | 12 | 对话窗口轮数 |

---

## 13. 测试与验收

### 13.1 已有测试

| 文件 | 覆盖 |
|---|---|
| `tests/test_loop_agent.py` | 工具执行、未知工具、截断保护、超时、cancel、参数校验、before_tool 阻断、压缩、govern |
| `tests/test_loop_pipeline.py` | L1 直连、缺参追问、pending 透传、escalate、无 plan、L0 handoff |

### 13.2 验收指标

| 指标 | 门槛 |
|---|---|
| 工具选择准确率 | ≥ 现有语料基线 |
| 首字延迟 P50 | ≤ 现状 + 100ms（L1） |
| 单次 token 成本 | ≤ 现状 × 1.3 |
| 追问轮成功率 | ≥ 现状 |
| L2 占比 | < 30% |
| L1→L2 升级率 | < 5% |
| 无工具作答率（L2） | ≈ 0 |

### 13.3 阈值实测（2026-11-09，真实分类器）

13 条典型问法的 `classify_intent_with_scene` 分数：

| 期望域 | 判定域 | 分数 | @0.75 | @0.60 |
|---|---|---:|---|---|
| person_status | person_status | 0.811 | ✅ | ✅ |
| **security_status** | **emergency_fire** | 0.745 | ❌ | ✅ ← 查错域 |
| canteen_status | canteen_status | 0.756 | ✅ | ✅ |
| vehicle_status | vehicle_status | 0.780 | ✅ | ✅ |
| information_status | information_status | 0.702 | ❌ | ✅ |
| energy_status | energy_status | 0.723 | ❌ | ✅ |
| meeting_status | meeting_status | 0.732 | ❌ | ✅ |
| emergency_fire | emergency_fire | 0.767 | ✅ | ✅ |
| emergency_perimeter | emergency_perimeter | 0.808 | ✅ | ✅ |
| **device_status** | **compositive_overview** | 0.764 | ✅ | ✅ ← 恰好吃到正确数据 |
| device_query | device_query | 0.774 | ✅ | ✅ |
| compositive_overview | compositive_overview | 0.791 | ✅ | ✅ |
| twins_inspection | twins_inspection | 0.831 | ✅ | ✅ |

**结论：保持 `high_confidence=0.75`。**

- 0.75 时 9/13 进 L1，4 条走 L2；冒烟 14/14 全 PASS，L2 兜底有效；
- 0.60 时 security 会被判成 emergency_fire 并**查错域**（用消防工具查安防告警）；
- 降阈值前必须先修两个已知错判（`security↔fire`、`device_status↔overview`），
  这两条在 `docs/语料评测记录.md` 里本就是 xfail。

修好后可以考虑降到 0.70（预计 L1 覆盖 11/13）。

### 13.4 真实环境冒烟（2026-11-09）

`scripts/ws_loop_smoke.py --port 8001`，14 条（13 域 + 1 跨域）：

```
结果：14 passed, 0 failed
L1 命中：8 条（person / canteen / vehicle / fire / perimeter / device_query / overview / inspection）
L2 兜底：6 条（security / information / energy / meeting / device_status / 跨域对比）
```

发现并修复：`twins_inspection` 的 4 个 MCP 工具名是旧名（`getTodayInspection` 等），
真实后端为 `getTodayOverview` / `getTodayTaskList` / `getStatistics` / `getExecutionStatus`。
旧图里也是旧名，说明**旧链路的巡检一直在报错**，只是回答被兜底成"接口报错"没被发现。

---

## 14. 迁移路线

| 阶段 | 内容 | 风险 |
|---|---|---|
| **P1（已完成）** | 工具层 + loop 内核 + 三级分流 + 离线测试 | 零（未接线上） |
| **P2（已完成）** | `app.py` 灰度入口：`mode=loop` + 比例/白名单，异常自动回落 | 低（按开关） |
| **P3（已完成）** | 扩到全部 13 个业务域（每域一个 `domains/*.py`，共 14 个工具） | 中 |
| **P4** | 用评测集校准阈值，逐步扩大 L1 覆盖 | 中 |
| **P5（已完成）** | MCP 接入层换成官方 `mcp` SDK，新链路彻底不依赖 langchain | 低（接口不变） |

每个阶段都可以独立回滚。

---

## 15. 已知限制

1. **L2 的收尾总结仍是非流式**：模型主循环已全程流式（`stream=True`，`assistant_delta` 逐块下发），
   但 `_finalize()`（步数用尽时的兜底总结）是一次性调用，用户会看到一段"无声"等待。
   另外关闭流式（`stream=False`）时前端只能整段出字。
2. **压缩是占位式**：不做语义摘要。
3. **工具串行**：没有并行执行。
4. **场景控制未纳入**：L0 只做 handoff。
5. **Session 内存态**：多副本需要改 Redis。
6. **provider 抽象**：直接用 OpenAI messages 作为内部格式，接 Anthropic 需要在适配层转换。
7. **依赖现状**：新链路只依赖 `openai` + `mcp`（官方 SDK），**不再引用 langchain**；`langchain` / `langgraph` 仍被 13 张旧图和 `mcp_client/mcp_loader.py` 使用，等 P3/P4 把域迁完、旧图下线后统一移除。`mcp_loader.py` 本身可以保留（旧图在用），也可以随旧图一起删。

---

## 16. 验证命令

```bash
# 工具声明
python3 -m agent.loop.cli --list-tools

# 参数解析（不连 MCP / 不调模型）
python3 -m agent.loop.cli --dry-run "3楼的门禁设备都列一下"

# 路由决策（需要 BGE，不调 LLM）
python3 -m agent.loop.cli --route "现在园区有多少人"

# 端到端（需要 LLM key + Java sidecar）
LLM_API_KEY=sk-xxx python3 -m agent.loop.cli --chat "现在园区有多少人"

# 测试
python3 -m pytest -q tests/test_loop_agent.py tests/test_loop_pipeline.py
```

---

## 17. 回滚

所有改动均为新增文件，除以下两项：

| 变更 | 回滚方式 |
|---|---|
| 删除 `mcp_servers/` 6 个 mock | `git checkout -- mcp_servers/` |
| `mcp_client/mcp_server_config.yaml` 删注释块 | `git checkout -- mcp_client/mcp_server_config.yaml` |
| 新增 `agent/__init__.py` | **不要回滚**（否则本机 site-packages 的同名 `agent` 包会覆盖本项目） |

接入 `app.py` 后如需回滚，删除 `mode=loop` 分支即可，旧路径未改动。

---

## 18. 后续 TODO

未实施项（分类埋点、语料补充、阈值校准、多中心分类等）见 [`docs/loop-agent-TODO.md`](loop-agent-TODO.md)。

其中 **T1 分类埋点**（问题 + 分类结果写 SQLite）是下一步的第一步——先用真实数据回答三个问题，再决定改什么：

- 哪些域经常被判错（人工标注 `correct` 后统计）
- 哪些域分数长期偏低（说明语料不够）
- L1 / L2 的实际占比与延迟

**当前明确不做的**：分类器语料补充（等业务给出确定的提示词/问法）、阈值调整（等 T1 数据）。


## 19. MCP 工具动态发现（试点）

### 19.1 问题

loop 的 **14 个工具全部手写**，而 MCP 侧实际有 **87 个**（不含 knowledge / scene 两个专属直连 server）：

```
local 3   security 10   canteen 3   vehicle 6   information 6
energy 7  meeting 6     fire 4      perimeter 4 device 8
devicequery 24          overview 2  inspection 4
```

手写的理由是**参数口径**：Java 接口要 `startTime`（`yyyy-MM-dd HH:mm:ss`）、
`spaceId`（内部 id）、`syncSource`（枚举码），模型编不出来。所以过去把
「用户原话 → 接口参数」的解析下沉到工具内部，模型只传 `{"query": "原话"}`。

### 19.2 判据：不是"返回值要不要处理"，而是"参数能不能自己填"

最初的设想是"返回体需要改写的才手写，其余直通"。实测后这个判据不够——
真正卡住的是参数：返回 JSON 交给模型组织没有问题，但 `startTime` 填错，
返回的就已经是错数据了。

所以筛子是 `screen_schema()`（`agent/loop/discover.py`）。判据**不是参数名长得可不可疑**，
而是**描述里有没有可枚举的取值**：

| 条件 | 处理 | 为什么 |
|---|---|---|
| 参数有 `enum`，或描述里带"编码-中文"对照（`7-电表` / `0门禁` / `jk(监控设备)`） | 放行 | 模型照着选项选，不会错 |
| 必填参数名是 time / date / start / end | 排除 | 要秒级时间戳，模型算不准"今天"，且填错会返回看似合法的错误时段 |
| 必填参数是内部 id（`id` / `xxxId`） | 排除 | 只能从另一个接口的返回里取，用户不会主动报 uuid |
| 已被显式域使用的 MCP 工具 | 排除 | 同一能力两个入口，模型会选错 |
| `_EXCLUDE_TOOLS` 黑名单 | 排除 | 语义重叠，如 `listDeviceWithSpace` vs `device_query__list` |

**这条判据是踩过坑改出来的**。最初按参数名一刀切（`time` / `date` / `id` / `code` /
`source` / `space`），结果把 `getEnergyOnlineRate` 和 `getVendorDeviceDetail` 也排掉了。
读它们的原文才发现 MCP 侧描述写得极细：

- `getEnergyOnlineRate`：必传 `syncSource:7-电表 / 6-水表`，只支持这两个；还写了
  `ONLINE_PERCENTAGE` 是 0~1 的小数（0.92 表示 92%）要换算、分母只含"启用"设备
- `getVendorDeviceDetail`：`code` 必填，形如 `CY-HIK-JK-001-0001`，**不是**
  `getVendorDeviceList` 的 channelCode/EndpointID，也不是 `getDeviceDetail` 的内部 id

枚举、单位换算、易错点都写明了，模型完全能填对。所以判据改成"看描述"，
这两个放行（19 → 21 个）。

注意**只看 required**：可选参数不填就是后端默认口径，不该因为它整条排除。
所以 `countByDeviceType`（`spaceId` 可选）是放行的——不传就是全园区统计。

### 19.3 实测：只有 devicequery 有空缺

```
server          总数  显式已用  空缺  其中可直通
local             3      3      0        0
security         10     10      0        0
canteen           3      3      0        0
vehicle           6      5      1        0
information       6      6      0        0
energy            7      7      0        0
meeting           6      6      0        0
fire              4      4      0        0
perimeter         4      4      0        0
device            8      8      0        0
devicequery      24      2     22       20
overview          2      2      0        0
inspection        4      4      0        0
```

**12 个 server 的工具已被显式域用满**——这也解释了为什么当初手写 14 个工具就够用。
唯一有空缺的是 `devicequery`：24 个工具只用了 2 个（listDeviceOnly / getDeviceDetail），
剩下 22 个里 20 个是统计类接口：

```
countByDeviceType / countByFloor / countByParam / countBySpace / countByStatus
countByTypeAndStatus / countMonitorByStatus / countMonitorBySubType
getAnfangDeviceOnlinePercentage / getCameraOnlinePercentage / getMjOnlinePercentage
getCategoryHealth / getDeviceStatistics / getDistribution / getEnergyDeviceStatus
getEquipClass / getEquipDistribution / getStatisRegion / getVendorDeviceList
```

这批正好补上「设备在线率」「设备分布」「各类设备数量」这类现在答不了的问法
（旧链路只覆盖其中一部分，L2 loop 之前碰到就答"查不了"）。排除
`listDeviceWithSpace`（与 `device_query__list` 重复）后，**实际新增 21 个，
registry 从 14 个工具变成 35 个**。

### 19.4 装配方式：为什么不能启动后动态加

`LoopAgent` 的 system prompt（含工具概览）是**构造时固化**的，晚加的域进不去。
所以走「启动预热 → 进程内缓存 → `build_registry()` 同步读取」：

```
app.py startup
  └─ warm_discovered()            # asyncio.wait_for 12s，失败只告警
       ├─ build_default_domains(box)   # 拿已知的 64 个 (server, tool)
       ├─ discover_domains()           # 拉 list_tools + 筛选
       └─ 写入 discover._discovered
ws_bridge.build_pipeline()        # 首次聊天时
  └─ build_registry()             # 显式 13 域 + get_discovered()
```

MCP 不可达 → `_discovered` 为空 → 只有 14 个显式工具，**行为与改造前完全一致**。

### 19.5 运维

```bash
# 手动看筛出了什么（不写缓存、不影响服务）
python -m agent.loop.cli --discover

# 一键退回：只用 14 个手写工具
LOOP_DISCOVER=0
```

启动日志会打 `[startup] MCP 工具发现完成：1 个域 / 21 个工具`；
被跳过的工具和原因也打 `[discover] devicequery 跳过 N 个：...`。

### 19.6 已知风险

1. **工具变多，模型选择面变大**：14 → 33。同类统计接口名字相近
   （`countByStatus` / `countByTypeAndStatus`），可能选偏。要盯的是埋点里的工具命中率。
2. **返回体大小未知**：`getVendorDeviceList` 带分页，`getDeviceStatistics` 也可能返回长列表。
   它们走 `govern()` 兜底（2000 行 / 50KB），但截断提示词会进模型上下文。
3. **passthrough=False**：这些工具返回 MCP 原始 JSON，必须由模型组织成自然语言，
   不能像 `device_query__list` 那样直出。
4. **只覆盖了 devicequery**：其余 server 没有空缺，扩不了。真正想"少写代码"，
   要等 MCP 侧新增工具时才有素材。

---

## 20. 2026-11-11 增补：设备定位与场景联动

这一轮围绕一个真实问题展开：**用户说「打开7F西通道监控」，系统要么拉全量设备列一遍，
要么直接说"没有控制类工具"**。排查下来是四个独立的问题叠在一起，逐条记录。

### 20.1 模型配置统一（`models/llm.py`）

原来 `model_confs` 里写死两组配置（`deepseek` / `local`），`LLM_BASE_URL`
`LLM_MODEL` 这些环境变量主服务**根本不读**；`LLM_THINKING` / `LLM_REASONING_EFFORT`
读了但被配置里的硬编码值遮蔽，等于无效。

现在改成**单一配置，全部走环境变量 + 默认值**：

| 变量 | 默认 | 说明 |
|---|---|---|
| `LLM_TYPE` | `openai` | `openai` / `ollama`，决定用哪个客户端类 |
| `LLM_BASE_URL` | openai: `https://api.deepseek.com/v1`<br>ollama: `http://localhost:11434` | |
| `LLM_MODEL` | openai: `deepseek-flash`<br>ollama: `qwen3:8b` | |
| `LLM_API_KEY` | 回退 `OPENAI_API_KEY` | |
| `LLM_REASONING_EFFORT` | `low` | `off` 表示不传该字段 |
| `LLM_THINKING` | `disabled` | `off` 表示不传该字段 |

两条规则：**空串按"未设置"处理**（compose 的 `${VAR:-}` 会传空串）；`LLM_TYPE` 拼错直接抛错。
`app.py` 启动时打一行 `[startup] 模型 openai / deepseek-flash @ ...`，方便核对实际生效值。

### 20.2 灰度变量精简（`agent/loop/rollout.py`）

`LOOP_ROLLOUT_ENABLED` 和 `AGENT_MODE` 表达同一件事，已删除。现在：
**`LOOP_ROLLOUT_PERCENT > 0` 或 `LOOP_ROLLOUT_USERS` 非空即视为开启**，两者都空等价于关闭。
`AGENT_MODE` 的三档语义不变。

### 20.3 MCP 地址外置（`mcp_client/mcp_config.py`）

新增 `resolve_base_url()`：**环境变量 `MCP_BASE_URL` 优先于 yaml 的 `mcp_base_url`**。
`mcp_config.load_servers()` 和 `mcp_loader.load_mcp_config()` 两个入口共用，部署时不用再改仓库里的 yaml。

### 20.4 设备查询：名称抽取与清单缓存（`agent/loop/domains/device_query.py`）

**名称抽取**原来只认两种说法，中文最自然的第三种全漏：

| 说法 | 改前 | 改后 |
|---|---|---|
| `3F-主楼梯MJ` | ✅ | ✅ |
| `监控5F客梯厅`（类型词在前） | ✅ | ✅ |
| `打开7F西通道监控`（类型词在句末） | ❌ 空 | ✅ `7F西通道` |

新增 `_NAME_BEFORE_TYPE_RE` 覆盖句末式；`_NAME_STOP` 补了指代词（这个/那个/这台…），
避免把代词当设备名。

**清单缓存**：一次全量 `listDeviceOnly` 约 1.3MB，字段 30 多个，但只有 7 个有用。
现在按 `syncSource` 分桶缓存，只留 `id/code/name/rawName/spaceName/status/syncSource`，
TTL 由 `DEVICE_CACHE_TTL` 控制（默认 300 秒）。给了名称/楼层却没命中时，**强制重拉一次**再过滤，
仍没有就明说"没有找到"，不再把全量倒给用户。

抽出 `find_devices()` 供设备查询和场景定位共用。

### 20.5 路由：分清"问设备"和"开画面"

`_SCENE_MONITOR_CUE` 原来的 `.{0,4}` 太宽，把「查看 监控5F客梯厅」这类**问设备**的话
也判成场景指令，导致 handoff 回旧链路拉全量。现在要求「监控/摄像头」之后只有可选的
窗口词或语气词（即落在句末）。

另外补了两条：

- `_SCENE_OPEN_MONITOR`：打开类动词 + 监控词，**允许中间夹设备名**（`打开7F西通道监控`）
- `_SCENE_OPEN_VERB` + `_device_target()`：带「打开/调出/切到」的一律不否决场景；
  只有「查看/查询」类才可能被当成设备查询

> **一条已撤回的弯路**：曾试图在 loop 里加 `scene__show_camera` 工具（L1 直连）直接下发。
> 实测不可行——`scene` 是 `direct_client: true`，只能由 `SceneMcpClient` 带逐请求
> `X-Session-Id` 调用，且动作类工具**不能重试**（会重复下发）。已删除该工具和对应路由。

### 20.6 场景：设备名称 → 点位 tag（`agent/loop/scene_resolve.py`）

`scene_show_camera` 的 `tag` 是设备编号（`jk_zl_7f_0002`），用户说不出来。
新增解析层，在 **app.py 场景分支**（不是 loop）里做：

```
打开7F西通道监控
  → 设备台账模糊匹配 → tag=jk_zl_7f_0002
  → 注入 slots.device_tag
  → scene_control_langgraph.plan_action 把它拼进规划 query（「… 编号:jk_zl_7f_0002」）
  → _extract_tag 和 LLM 提示都能取到 → scene_show_camera(tag=…)
```

多台命中时**不下发**，而是追问并列出候选，引导用户补设备类型或编号。
解析不出来也不再让服务端按名字匹配（服务端不分设备类型，会误命中非监控点位）。

### 20.7 会话记忆：候选列表与序号解析

排查「第三个」失败时发现，**三套记忆互不相通**：

| 记忆 | 位置 | key | 默认存活 | 持久化 |
|---|---|---|---|---|
| 场景/业务追问态 | `memory/session_state.py` | `user_id:session_id` | 300s → **1800s** | ❌ 进程内存 |
| loop 会话 | `agent/loop/ws_bridge.py` | `user_id:session_id` | 300s → **1800s** | ❌ 进程内存 |
| 旧链路历史 | `memory/memory_persistor_*` | `user_id:session_id` | 长期 | ✅ 只有这套落盘 |

**都不是全局的**，按用户+会话隔离；前两套重启即丢。

改动：

1. 追问时把候选写进 `IntentState.slots["device_candidates"]`
2. 下一轮支持「第三个」「第2个」「3」按序号取候选；「这个」在候选唯一时直接用，
   多台时再问一次（不猜）；序号越界不瞎取
3. `_scene_followup_pending()`：场景追问态下，短句/序号开头的输入**不让 loop 抢走**——
   这是「第三个」被 L2 模型回成"没有可对应的清单内容"的直接原因
4. 两处 TTL 统一读 `SESSION_TTL_SECONDS`，默认 1800 秒

**「之前为什么没记忆」**：loop 的 `Session.history` 只存 user/assistant 纯文本，
工具结果从不入 history；场景追问态虽存在但只存了 `scene_session_id`，没存候选；
再加上 loop 抢在场景分支之前。三件事叠加，与模型配置无关。

### 20.8 尚未解决

1. **「西侧」≠「西通道」**（已部分解决）：用户说方位词，数据里是通道名，子串匹配必然失败。
   现在 `_loosen_keyword` 会去掉「侧/面/边」再匹配一次，多台就列候选（可点），
   不再直接回「没有找到」。但「西侧」仍可能同时命中「西消防电梯厅」「西步梯间」，
   要更准需要按通道语义打分。
2. **纯设备名不含动词时不抽取**：`7F中通道东向西` 这种没有「打开/查看」的话，
   `_name_fallback` 返回 None。目前靠模型兜。
3. **多副本不共享会话**：见 TODO 的 T6 / T15。

### 20.9 会话池监控（`app.py`）

会话池有两处，都没有容量上限（见 TODO T16）。启动时挂一个 60 秒的后台任务打一行：

```
[session] states=3 loop_sessions=2
```

`states` 是 `session_state.states`（场景/业务追问态），`loop_sessions` 是
`ws_bridge._sessions`（loop 会话）。

不挂在 `_start_prewarm()` 里：预热可以被 `PREWARM_ON_STARTUP=0` 关掉，监控要一直开，
所以在 `startup()` 里单独 append 到 `_prewarm_tasks`（只是借用这个列表存引用，
否则任务对象会被 GC；关机时并不 cancel 它）。

**为什么现在需要这个数字**：`SESSION_TTL_SECONDS` 从 300 提到 1800 之后，
同一时刻驻留的会话数大约是原来的 6 倍，而 `session_state` 的过期只在被访问时才清理。
有了这行日志才能区分"真泄漏"和"只是 TTL 变长"。上了 Redis（T15）之后可以删。

### 20.10 上下文压缩与 prompt cache（结论）

**结论：记忆策略和压缩策略动的是同一段前缀，必须一起设计。** 详细方案见 TODO 的 T14 ④。

事实（`agent/loop/validate.py:60`）：

1. `messages[i] = {**messages[i], "content": placeholder}` 是**就地改写**历史消息；
2. 遍历 `tool_indexes` **从最早的一条开始压** —— 改动点越靠前，失效的缓存后缀越长，
   这是最坏的方向；
3. 它在 `while` 循环里**每一步都跑**（`agent.py:236`）：新工具结果累积到超预算就再压一条，
   于是断点不是断一次，而是每步往前挪一次。

现状里本来是好的部分：`messages[0]` 恒为 system（`agent.py:223`）、history 只追加、
工具 schema 与 system prompt 在进程内只构造一次（`agent.py:79`）。这个前缀**本来稳定可缓存**，
压缩是唯一的破坏源。

另一处会平移前缀的是 `pipeline.py:42`：`self.history = self.history[-24:]` 超限就从头部砍一条，
砍完立刻发请求。单条粒度越细，平移越频繁 —— 应按整块对齐砍（比如一次砍 6 条）。

**先加观测**：DeepSeek 报 `usage.prompt_cache_hit_tokens`、OpenAI 报
`usage.prompt_tokens_details.cached_tokens`，两者目前都没读。命中与否只体现在账单和
首字延迟上，"优化有没有用"没有数字就只能靠猜。

方向：压缩**单调化**（压过就保持，别每步重判）；优先压靠后的长结果，或一次性压"最早的一段"，
把断点次数压到每会话个位数。—— 未实施，等 T12/T14 一起做。

---

## 21. 2026-11-12 增补：清单类回答改用结构化实体（`list` 事件）

### 21.1 问题

设备清单原本是纯文本：

```
14 台设备，楼层 5F：
- 5F客梯厅｜编号 jk_zl_5f_0006｜id 1001｜位置 园区/科研办公楼/5F｜状态 启用｜类型 监控
```

前端要把它变成可点按钮，只能靠正则猜「这段文字是不是设备清单」：
表头词、竖线分段、`名称（编号 xxx）`、整行设备名、markdown 表格……每条规则都在
赌模型的输出形态。模型换个说法就漏，漏了用户就点不动。已经写了 500 行仍会漏。

根因是**判定权放错了地方**：数据是后端给的，格式是模型复述的，前端只能靠文本反推。
模型措辞不可控，正则永远追不完。

### 21.2 设计

把「这是什么东西」的判定搬回**产出数据的工具**——它最懂自己的字段：

```
MCP 原始 JSON
   ↓ device_query 域（知道哪列是名称、哪列是编号）
ToolOutcome.text      ← 照旧：给模型看、给 history 用、旧前端兜底
ToolOutcome.entities  ← 新增：结构化列表，不经过模型
   ↓
WS 事件 {"type":"list","kind":"device","items":[…]}
   ↓
前端 kind → 组件（一张映射表，不再做文本推断）
```

三个关键点：

1. **不经过模型**。清单不再让模型复述，实体由工具直接产出，模型写得天花乱坠也不影响。
2. **文本仍然保留**。history 要存（下一轮「第三个」靠它），旧前端也要能降级显示。
3. **`kind` 是扩展点**。前端只认 `kind`，后端加一种业务类型（`person` / `vehicle` / `space`）
   只是多填一个字段，协议不变。

代码在 `agent/loop/entities.py`（`Entity` / `EntityList`）。

### 21.3 协议

`{"event":"custom","data":{…}}`，`data.type == "list"`：

```json
{
  "type": "list",
  "kind": "device",
  "title": "14 台设备，楼层 5F",
  "items": [
    {"kind": "device", "id": "jk_zl_5f_0006", "label": "5F客梯厅",
     "attrs": {"code": "jk_zl_5f_0006", "type": "监控", "space": "园区/科研办公楼/5F", "floor": "5F", "status": "启用"}}
  ],
  "total": 268,
  "truncated": false
}
```

| 字段 | 含义 |
|---|---|
| `kind` | 业务类型，前端据此选组件。目前只实现了 `device` |
| `title` | 一句话概括，和文本首行是同一句话（都出自 `_summary()`） |
| `items[].id` | 点击后回传的标识。设备就是点位编号，等于场景下发的 tag |
| `items[].label` | 按钮文字 |
| `items[].attrs` | `kind` 私有字段，前端不认识可以忽略。`device` 目前有 `code` / `type` / `space` / `floor` / `status`；`floor` 是 `5F` / `-1F` 短写法，可直接做场景聚焦 |
| `total` | 过滤前的总量（园区共 N 台） |
| `truncated` / `note` | 是否截断 + 提示语，超过 `MAX_ROWS=60` 时出现 |

发送时机：

- **L1 直出**（`passthrough`）：先发 `answer`（整段文本，兼容旧前端），再发 `list`；
- **L2 loop**：工具执行完立刻发 `list`（`LoopAgent` 的 `entities` 内部事件 → `ws_bridge` 翻译成 `list`），
  比最终回答先到，前端可以先渲染卡片再等结论。

### 21.4 前端怎么改（已完成）

**状态：`tf_park_ue` 已接入（`b936340`）。** 实际做法不是新建卡片组件，而是把 `items`
拼成 `<device>` 标签，复用现有的 `DeviceNode` → `openDeviceScene` 链路——点击行为和样式
零成本对齐。下面的文字保留作协议说明，改动详见 `docs/前端对接-list事件.md`。

现在那套文本推断（`DEVICE_WORD_RE` / `nameIndex` / `parsePipeEntry` / `convertDeviceTable` …）
保留做**兜底**（旧链路仍只发文本），`list` 事件到达时以结构化数据为准。

```ts
case 'list':
  // 有结构化数据就以它为准，不再走 markdown 文本增强
  applyEntityList(data)
  break
```

`applyEntityList` 内部只认 `kind: 'device'`（把 `items` 拼成 `<device>` 标签），
其他 `kind` 直接 return、交给文本路径；以后加人员/车辆就是在这个函数里再加一个分支。

点击行为：`onClick(id)` → 把 `id` 当设备编号（tag）下发场景，和现在 `<device code>` 完全一致。

**建议保留旧路径做灰度**：`list` 事件没来时（老后端 / 其他入口）继续走原来的文本增强。
前端按「收到 `list` 用结构化渲染，没收到退回正则」切换，两边不用同时上线。

### 21.5 扩展新业务类型

以人员列表为例，后端只需要：

```python
EntityList(
    kind="person",
    title="5F 在岗 3 人",
    items=[Entity(kind="person", id="zhangsan", label="张三",
                  attrs={"dept": "物业", "phone": "138…"})],
)
```

协议、事件、`ws_bridge` 一行都不用改。前端加 `person: PersonCard` 即可。

### 21.6 未做

1. **行内引用**：模型自由回答里提到某台设备时走
   `[5F客梯厅](entity:device/jk_zl_5f_0006)` 这种 markdown 链接（`Entity.to_link()` 已实现），
   前端渲染器认 `entity:` scheme 即可转成同一组件。暂不启用——点不动是小事，
   "一块列表整片漏掉"才是痛点。
2. **其余 12 个域**：只有 `device_query__list` 填了 `entities`，人员/车辆/告警等域要在各自
   工具里补，协议不变（见 TODO T17）。

3. **动态发现的 21 个工具**：返回 MCP 原始 JSON，没有实体语义，不填 `entities`
   （前端继续按文本渲染）。

### 21.7 修复：清单问句被场景追问截走

线上两轮对话暴露的问题：

```
我：打开7F西侧监控
AI：没有找到「7F西侧」对应的设备，请确认一下名称或编号。      ← ① 方位词匹配失败
我：7楼有哪些监控
AI：这个条件匹配到 6 台设备，请补充设备类型…                ← ② 被场景追问态截走，且只有纯文本
```

三个独立缺陷叠加：

| # | 缺陷 | 位置 | 修法 |
|---|---|---|---|
| ① | 「7F西侧」不是任何设备名的子串 | `device_query._filter_rows` | `_loosen_keyword` 去掉「侧/面/边」再匹配一次，多台列候选 |
| ② | 上一轮场景追问没结束，`len(query) <= 12` 就把「7楼有哪些监控」当成补答参数拼回场景通道 | `app.py` 追问态分支、`_scene_followup_pending` | `asks_for_list()` 判定「有哪些/几个/多少」→ 放弃场景追问，交回业务 |
| ③ | 场景追问的候选只有纯文本，前端点不了 | `app.py` clarify 分支 | 追加 `list` 事件（`candidate_entities`），追问话术只留标题句（`clarify_headline`），避免同一份清单显示两遍 |

第 ② 条的连锁反应值得记一笔：①失败后 `session_state` 留下 `module=scene_control, done=False`，
于是下一轮**任何短句**都会被当成补答 —— 用户越问越错，必须靠序号或长句才能脱身。

修完的实测（假台账，10 台 7F 监控）：

```
第 1 轮 打开7F西侧监控  → L0 scene → 4 台西侧候选 → list(可点) + 「匹配到 4 台设备，请补充…」
第 2 轮 7楼有哪些监控   → L1 device_query__list → 「10 台设备，楼层 7F」→ list(10 项，带 floor/type/status)
```

测试：`test_loop_router_scene.py`（`asks_for_list` 真值表 + 不被场景截走 + 「打开7F西侧监控」仍进场景）、
`test_loop_scene_resolve.py`（7F 回放、候选 → 实体、headline 去明细）、
`test_loop_device_filter.py`（方位词放宽到候选）。

### 21.8 修复：追问一次之后，说完整设备名也定位不到

21.7 上线后实测发现：`打开7F西侧的监控` 触发追问是对的，但**接着说什么都定位不到** ——
包括完整的 `打开7F西通道西向东监控`；而单开一轮说同一句却是好的。

根因：追问轮 app.py 把上一轮原话拼进去，本来是给场景图规划器用的：

```python
scene_query = f"{scene_prev.original_query} {query}"   # 给规划器补充语境
```

这个拼接句**同时被喂给了设备名解析**，于是字符串里出现两个「监控」：

```
打开7F西侧的监控 打开7F西通道西向东监控
                  ↑ _DEVICE_TYPED_NAME_RE 从「监控 打开…」把整句吞成设备名
→ 关键词「打开7F西通道西向东监控」→ 台账里没有 → 没有找到
```

四处修：

1. **解析用本轮原话**：`run_scene_control_graph(..., resolve_query=query)`，
   拼接句只留给规划器。
2. **候选清洗** `_clean_candidate`：去掉开头的动词、去掉结尾的类型词
   （`打开7F西通道西向东监控` → `7F西通道西向东`），顺带挡住拼接句。
3. **台账反查** `_name_in_text`：query 里直接出现的设备全名优先，覆盖
   「打开 7F西通道西向东」这种带空格、无类型词、无横线的说法。
4. **候选只在「多台命中」时下发**：`clarify_entities()` 只在话术含「匹配到」时返回实体。
   之前「没有找到 X」也把上一轮候选当可点清单发了，用户看到「请选择 3 台」+「没有找到 X」，
   自相矛盾。

回归覆盖（`test_loop_scene_resolve.py` / `test_loop_device_filter.py`）：

```
打开7F西通道西向东监控      → jk_zl_7f_0002
打开 7F西通道西向东 监控    → jk_zl_7f_0002
打开 7F西通道西向东         → jk_zl_7f_0002
打开监控7F西通道西向东      → jk_zl_7f_0002
第二个                      → jk_zl_7f_0002
拼接句（防御）              → jk_zl_7f_0002
```

### 21.9 修复：楼层级请求与视角指令

```
我：打开7F监控
AI：匹配到 6 台设备，请选择 …（列了 6 台，且第 7~10 台根本点不到）
我：切回整栋楼
AI：这个条件匹配到 … 台设备，请补充设备类型…
```

三个问题：

| # | 问题 | 原因 | 修法 |
|---|---|---|---|
| ① | 「打开7F监控」被当成"选一台设备" | `_name_fallback` 把纯楼层 `7F` 当成设备名 → 去匹配整层 10 台 | `_PURE_FLOOR` 过滤：纯楼层不算设备名，楼层交给 `normalize_floor` |
| ② | 候选只有 6 台，后几台点不到 | `MAX_CANDIDATES = 6` | 改 12（7F 一层 10 台），超出的才写「先列出前 N 台」 |
| ③ | 「切回整栋楼」被拉去查台账 | 追问态只要 `prev_candidates` 非空就一定解析 | `has_device_reference()`：序号/指代/类型词/去掉动作词后仍有实义内容才算设备线索，`整栋楼/全部/楼层` 这类范围词排除 |

第 ③ 条是追问态的通病：一旦进入追问，**任何话**都会被拿去查全表，回一句
「匹配到 268 台设备」——用户完全无法脱身，只能靠序号。

顺带改文案：用户已经说了「监控」就不再让他「补充设备类型」。

```
打开7F监控    → 7F 楼层匹配到 10 台设备。该层点位比较多，请说具体点位名称或序号：
                （list 事件带 10 个可点标签）
打开7楼的     → 7F 楼层匹配到 10 台设备。请补充设备类型（监控/门禁/广播等）…
切回整栋楼    → 不算设备线索，直接交给场景图（视角复位）
```

**仍未解决（产品级）**：「打开7F监控」在直觉上是"看这一层的监控画面"（监控墙/楼层视图），
而场景通道只有 `scene_show_camera(tag)` 单点位能力。现在只能降级成"列出该层点位让你选一个"。
要做真·楼层级画面，得在 scene 侧加能力，不是话术能补的（见 TODO T19）。

### 21.10 追问出口：答不上来就跳出，不再二次追问

21.9 之后仍然存在一个体验坑：**追问态一旦开启，用户说什么都会被当成"补答设备名"**。
修法不再靠猜，直接定规则：

> 追问态下只接受两种回答——**序号**（「第三个」「3」）和**说得出的设备名/编号**。
> 其余一律跳出：清掉会话态，回一句「请给出完整的要求描述」，本轮结束。

实现（`scene_resolve` / `app.py`）：

- `ESCAPE_CLARIFY`：跳出话术常量，调用方靠它识别"这是出口不是追问"；
- `_answers_pick(query, keyword)`：`_ordinal_index` 命中，或台账里落到了 `keyword` → 算答上来；
- `resolve_scene_device_tag` 在 `find_devices` 之后判断：`pool` 非空且 `not _answers_pick(...)`
  → 返回 `(None, ESCAPE_CLARIFY, [])`，候选一并清空；
- `app.py` 看到 `ESCAPE_CLARIFY` → `session_state.clear()` → 发 `ask`（提示）→ 发 `done` → `return`；
- 多台候选时说「这个/那个」算没选（原先是二次追问"你指的是哪一台"，只会绕圈子）。

行为矩阵（10 台 7F 监控的候选池）：

| 用户说 | 结果 |
|---|---|
| `第三个` / `3` | 下发 `jk_zl_7f_0002` |
| `打开7F西通道西向东` / `7F西消防电梯厅` | 下发对应 tag |
| `打开7F监控` | **跳出**：清状态 + 「请给出完整的要求…」 |
| `切回整栋楼` | **跳出** |
| `看下8楼` | **跳出** |
| `这个` | **跳出** |

退出后下一句按全新请求走，不会继承任何候选——这比"再问一遍"好，因为用户根本不知道自己
被困在了追问里。

测试：`test_followup_escapes_when_user_says_nothing_pickable`、
`test_followup_accepts_ordinal_and_full_name`、`test_first_time_ambiguity_still_lists_candidates`、
`test_pronoun_with_multiple_candidates_escapes_followup`。

### 21.11 修复：子类型字段两套写法，导致「走的 MCP 不对」

现场：「列出各会议室使用情况」打到了 `meeting:getMeetingStatistics`（返回预约数/平均时长），
而它应该打 `meeting:getMeetingRoomOverview`（返回各类会议室数量）。

**根因在通用域工厂，不在会议域**：

```python
# agent/loop/domains/_generic.py（修复前）
query_type = slots.get("query_type") or self.default_type
```

`agent/intent/slots.py` 里的抽取器**两套写法并存**：

| 用 `query_type` | 用 `event_type` |
|---|---|
| compositive_overview / device_query / device_status / emergency_fire / emergency_perimeter / energy_status / person_status / twins_inspection / vehicle_status | **canteen_status / information_status / meeting_status / security_status** |

后四个域的 `tool_map` 因此永远是死代码：不管问什么，`query_type` 都取不到 →
一律落到 `default_type` → 永远打同一个 MCP 工具。除会议外，食堂也是全打 `getWeekMenu`。

修：

1. `_generic.QueryDomain.resolve()` 同时认 `query_type` 和 `event_type`；
2. 会议抽取器：`会议室.*使用` 收窄为 `使用中/有人/没人/空着`，把
   「使用情况/使用率/各会议室」判给 `meeting_room_overview`，且**排在状态判定之前**；
3. 会议兜底 `event_type` 从 `count` 改成 `meeting_statistics` —— `count` 不在 `tool_map` 里，
   会让工具解析成 `None`（「会议室现在有人吗」原本就卡在这）。

修复前后（实测，真 MCP）：

```
修复前 列出各会议室使用情况 → meeting:getMeetingStatistics   {"reservations":34,"avgDuration":0}
修复后 列出各会议室使用情况 → meeting:getMeetingRoomOverview  [{"普通会议室":10},{"中型会议室":10}…]

食堂：今天食堂多少人/本周菜谱/什么菜最受欢迎 → getDiningCount / getWeekMenu / getDishPopularity
```

测试：`test_meeting_subtype_maps_to_expected_mcp_tool`、
`test_meeting_types_are_not_all_the_same_tool`、`test_event_type_domains_resolve_distinct_tools`。

**同类隐患（未改，属关键词质量问题而非字段问题）**：

- `安防概览` → 仍是 `security:getSecurityAlarmList`（抽取器没有「概览」关键词）；
- `今天开了多少会` → `room_meet_list_by_day`（规则的 `今天.*会` 抢在「会议数量」之前）；
- `本月电费` → `energy_status` 抽到 `count`，不在 `tool_map` → 工具解析为 `None`。

### 21.12 再修三处子类型判定（并补一张全量矩阵测试）

21.11 之后又发现三处判定不准，同时确认「`count` 兜底」有两种截然不同的用法：

| 问题 | 现象 | 修法 |
|---|---|---|
| 会议数量 | 「今天开了多少会」被判成当日会议列表（`今天.*会` 抢先） | 新增「会议数量」规则并**排在最前**：`多少.*会 / 几场会 / 会议.*数量 / 开了.*会` |
| 安防概览 | 「安防概览」落到告警列表（返回一堆明细，答不上"概览"） | 关键词并入 AI 告警总览：`安防概览/安防总览/安全概览/整体安全` → `getAlarmView` |
| 能源兜底 | 抽取器兜底 `count` 不在 `tool_map` → 工具解析成 `None` → 「本月电费」直接"后端暂未接入" | 不动抽取器，改在 `_generic.resolve` 兜底（见下） |

**关于 `count`**：它在 fire / perimeter / inspection / vehicle / device / meeting / energy
六个抽取器里都是兜底值，语义是"没认出子类型"。旧链路（13 张图）**依赖这个标记触发反问**
（「查下最新的消防」→ 图会问"设备台账还是实时告警"），所以：
- 抽取器保持 `count` 不变，旧链路行为零变化；
- 只在 loop 的 `_generic.resolve` 里把不在 `tool_map` 的子类型**映射到 `default_type`**。

一开始我把抽取器的 `count` 改成具体工具名，结果 `test_graphs` 的两轮反问流程直接挂了——
这正是"低层标记被上层当业务语义用"的典型坑，记在这里。

**新增测试矩阵**（`tests/test_loop_domains.py`）：

- `SUBTYPE_MATRIX`：11 个域 × 35 句真实说法 → 期望子类型 → 期望 MCP 工具，
  MISS 会直接打印"判成什么、会打哪个工具"；
- `test_neutral_query_falls_back_to_a_real_tool`：每个域用「你好/随便问问」探一次，
  断言 `resolve()` 一定拿得到工具——用来防 `count` 这类"兜底不在映射表"的坑。

覆盖的域：meeting / canteen / security / energy / information / vehicle /
twins_inspection / emergency_fire / emergency_perimeter / compositive_overview / device_status。

**仍未解决**：`args_builder` 一个域都没用，MCP 调用的参数恒为 `{}`，
所以「本月电费」虽然落到 `getOverallEnergyConsu`，返回的仍是接口默认口径
（`todayElectricity/yearElectricity`），日期范围没生效。要按时间取数得先确认
各 Java 工具是否接受 `startTime/endTime`。

#### 子类型矩阵（35 条，就是测试用例）

这张表把「用户怎么说 → 判成哪个子类型 → 打哪个 MCP 工具」列全了。
**唯一来源是 `tests/test_loop_domains.py` 的 `SUBTYPE_MATRIX`**，本表只是镜像——
两边不一致时以代码为准。加新说法就往矩阵里补一行，MISS 会直接报
「判成什么、会打哪个工具」。

**meeting_status**

| 说法 | 子类型 | MCP 工具 |
|---|---|---|
| 列出各会议室使用情况 | `meeting_room_overview` | `meeting:getMeetingRoomOverview` |
| 会议室概览 | `meeting_room_overview` | `meeting:getMeetingRoomOverview` |
| 会议室现在有人吗 | `meeting_room_status` | `meeting:getMeetingRoomStatus` |
| 哪些会议室空闲 | `meeting_room_status` | `meeting:getMeetingRoomStatus` |
| 今天开了多少会 | `number_of_meetings` | `meeting:getNumberOfMeetings` |
| 今天有几场会 | `number_of_meetings` | `meeting:getNumberOfMeetings` |
| 今天的会议列表 | `room_meet_list_by_day` | `meeting:getRoomMeetListByDay` |
| 今天有什么会 | `room_meet_list_by_day` | `meeting:getRoomMeetListByDay` |
| 哪个会议室用得最多 | `high_freq_meeting_rooms` | `meeting:getHighFrequencyMeetingRooms` |
| 会议室使用排行 | `high_freq_meeting_rooms` | `meeting:getHighFrequencyMeetingRooms` |
| 平均会议时长 | `meeting_statistics` | `meeting:getMeetingStatistics` |

**canteen_status**

| 说法 | 子类型 | MCP 工具 |
|---|---|---|
| 今天食堂多少人 | `dining_count` | `canteen:getDiningCount` |
| 本周菜谱 | `week_menu` | `canteen:getWeekMenu` |
| 什么菜最受欢迎 | `dish_rank` | `canteen:getDishPopularity` |

**security_status**

| 说法 | 子类型 | MCP 工具 |
|---|---|---|
| 今天的告警 | `alarm_list` | `security:getSecurityAlarmList` |
| 安防概览 | `ai_overview` | `security:getAlarmView` |
| 告警总览 | `ai_overview` | `security:getAlarmView` |
| 安全指数 | `security_index` | `security:getSecurityIndex` |
| 园区安全吗 | `security_index` | `security:getSecurityIndex` |
| 告警趋势 | `ai_trend` | `security:getAlertSituation` |
| 巡查任务 | `patrol` | `security:getPatrolMission` |

**energy_status**

| 说法 | 子类型 | MCP 工具 |
|---|---|---|
| 今天用水多少 | `realtime_water` | `energy:getRealtimeWaterUsage` |
| 本月电费 | `overall_energy` | `energy:getOverallEnergyConsu` |
| 总能耗 | `overall_energy` | `energy:getOverallEnergyConsu` |
| 用电排名 | `electricity_rank` | `energy:getElectricityUsageRanking` |
| 水表在线情况 | `metering_equipment` | `energy:getMeteringEquipment` |

**information_status**

| 说法 | 子类型 | MCP 工具 |
|---|---|---|
| 今天发了几条信息 | `info_view` | `information:getInfoView` |

**vehicle_status**

| 说法 | 子类型 | MCP 工具 |
|---|---|---|
| 还剩多少停车位 | `parking_space` | `vehicle_status:getParkingSpace` |
| 今天的车辆通行记录 | `traffic_flow` | `vehicle_status:getTrafficVolume` |

**twins_inspection**

| 说法 | 子类型 | MCP 工具 |
|---|---|---|
| 今天巡检任务 | `today_tasks` | `inspection:getTodayTaskList` |
| 巡检统计 | `inspection_statistics` | `inspection:getStatistics` |

**emergency_fire**

| 说法 | 子类型 | MCP 工具 |
|---|---|---|
| 消防设备有多少 | `fire_assets` | `fire:getFireAssets` |

**emergency_perimeter**

| 说法 | 子类型 | MCP 工具 |
|---|---|---|
| 周界告警统计 | `perimeter_alarm_stats` | `perimeter:getPerimeterAlarmStats` |

**compositive_overview**

| 说法 | 子类型 | MCP 工具 |
|---|---|---|
| 园区基本情况 | `basic_info` | `overview:getBasicInfo` |

**device_status**

| 说法 | 子类型 | MCP 工具 |
|---|---|---|
| 设备健康度 | `category_health` | `device:getCategoryHealth` |

**怎么用这张表**

1. 用户说法没命中（答非所问 / 后端暂未接入）→ 先查它属于哪个域、期望哪个子类型；
2. 去 `agent/intent/slots.py` 对应抽取器补关键词，或调整规则**顺序**（顺序即优先级，
   21.11/21.12 的坑都在顺序上：`今天.*会` 抢了「多少会」、`会议室.*使用` 抢了「使用情况」）；
3. 在 `SUBTYPE_MATRIX` 里加一行把它锁住，再跑 `pytest tests/test_loop_domains.py`。

### 21.13 修复：清单被回答两遍（L2 里模型复述工具结果）

现场：「有哪些道闸设备」→ 先出 `list`（2 台），又出一段 `2 台设备：` + 同样两行。

**为什么会重复**：这条问句分类置信度不够，走的是 **L2**。顺序是：

```
tool_start → device_query__list 执行 → entities 事件 → list（前端渲染 2 个标签）
           → 模型继续生成回答 → system prompt 第 7 条要求"把清单逐行原样列出来"
           → 同一份清单又变成文本 → 前端 enhanceDeviceLines 再转成 2 个标签
```

`passthrough` 在 L1 里是"直出、不让模型复述"，但 **L2 之前完全没用上这个标记**。

修两层：

1. **后端（根）** `LoopAgent`：工具产出 entities 且 `spec.passthrough` 为真时
   **直接收尾**，不再回到模型 —— 少一次 LLM 调用，也从源头消掉复述。
   出错（`is_error`）时仍回到模型，错误文案是写给模型看的。
2. **前端（兜）** `AiChatDialog`：记下本轮已渲染的 `list.title`；
   - `answer` 先到（L1）：`flushAnswer` 前就丢弃（原有逻辑）
   - `list` 先到（L2）：`flushAnswer` 里判断"文本以该 title 开头"→ 丢弃
   - 已落成消息的也做就地替换，覆盖 `tool` 事件提前 flush 的情况

两层都做是刻意的：后端保证不再浪费一次模型调用；前端保证**不管顺序如何**都不会显示两遍
（老后端、老前端混跑时也不会）。

测试：`test_passthrough_tool_ends_run_without_model_restating`（断言只调一次模型）、
`test_passthrough_does_not_end_run_on_error`（出错仍回模型）。
