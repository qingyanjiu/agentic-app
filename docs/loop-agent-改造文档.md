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
| `LOOP_ROLLOUT_ENABLED` | `0` | 总开关。`0` 时只有 `mode=loop` 能命中 |
| `LOOP_ROLLOUT_PERCENT` | `0` | 命中比例 0–100，按 `user_id:session_id` 稳定哈希取模 |
| `LOOP_ROLLOUT_USERS` | 空 | 白名单，逗号分隔的 `user_id` 或 `session_id` |

payload 也可以强制指定：

| payload | 效果 |
|---|---|
| `{"query": "...", "mode": "loop"}` | 强制走新链路（联调/回归用） |
| `{"query": "...", "mode": "legacy"}` | 强制走旧链路（出问题时的应急开关） |
| `mode=kb` / `scene` / `gen_doc` / `asr` | 不接管，保持原逻辑 |

判定优先级：`mode` 显式 > 总开关 > 白名单 > 比例。

常用配置：

```bash
# 联调：只给自己开
LOOP_ROLLOUT_ENABLED=1 LOOP_ROLLOUT_USERS=louisliu uvicorn app:app

# 灰度 10%
LOOP_ROLLOUT_ENABLED=1 LOOP_ROLLOUT_PERCENT=10 uvicorn app:app

# 全量
LOOP_ROLLOUT_ENABLED=1 LOOP_ROLLOUT_PERCENT=100 uvicorn app:app

# 紧急回滚（不重启也行：前端带 mode=legacy）
LOOP_ROLLOUT_ENABLED=0 uvicorn app:app
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

所以筛子是 `screen_schema()`（`agent/loop/discover.py`）：

| 条件 | 处理 |
|---|---|
| required 为空或全为枚举 | 放行 |
| required 含 time / date / start / end / id / code / source / space / area / region | 排除（模型填不出来） |
| required 类型不是 string/number/integer/boolean | 排除 |
| 已被显式域使用的 MCP 工具 | 排除（同一能力两个入口，模型会选错） |
| `_EXCLUDE_TOOLS` 黑名单 | 排除（语义重叠，如 `listDeviceWithSpace` vs `device_query__list`） |

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
`listDeviceWithSpace`（与 `device_query__list` 重复）后，**实际新增 19 个，
registry 从 14 个工具变成 33 个**。

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

启动日志会打 `[startup] MCP 工具发现完成：1 个域 / 19 个工具`；
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
