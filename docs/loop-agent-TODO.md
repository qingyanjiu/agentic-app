# Loop Agent 后续 TODO

> 状态：规划中，**均未实施**
> 前置条件：`docs/loop-agent-改造文档.md` 中的 P1–P5 已完成（内核、三级分流、13 域、灰度、MCP SDK）
> 更新日期：2026-11-09

---

## 1. 总览

| 编号 | 事项 | 优先级 | 预估 | 前置条件 | 风险 |
|---|---|---|---|---|---|
| T1 | **分类埋点**（问题 + 分类结果 → SQLite） | **P0** | 半天 | 无 | 低 |
| T2 | 分类器语料补充 | P1 | 1 小时 | **业务确定提示词/问法** | 低 |
| T3 | 路由侧数据词否决 | P1 | 半小时 | 无 | 低 |
| T4 | 阈值校准（0.75 → 0.70） | P1 | 半天 | T2 完成 | 低 |
| T5 | 单中心改多中心分类 | P2 | 1–2 天 | T1 数据积累 | 中 |
| T6 | 会话存储改 Redis | P2 | 1 天 | 多副本部署 | 中 |
| T7 | L2 loop 流式输出 | P3 | 1 天 | 前端支持 | 低 |
| T8 | 压缩从占位改语义摘要 | 并入 T14 | — | 见 6.4 方案 E | 中 |
| T9 | `vehicle_status:getVehiclePassRecord` 未接入 | P3 | 半小时 | 业务确认口径 | 低 |
| T10 | gen_doc / knowledge / scene 纳入 loop | 暂不做 | — | — | 高 |
| T11 | **MCP 参数白名单校验**（参数漂移检测） | P1 | 半小时 | 无 | 低 |
| T12 | **上下文预算可配**（`LOOP_CONTEXT_BUDGET`） | P1 | 半天 | 无 | 中 |
| T13 | **旧链路历史无上限**（persistor 语义不一致） | P1 | 1 小时 | 无 | 高 |
| T14 | **上下文压缩优化**（含 3 个现存缺陷） | P2 | 2~3 天 | T12 完成 + 全量切 loop | 中 |

**顺序建议**：T1 → 等业务语料（T2）→ T3 → T4 → 用 T1 数据决定是否做 T5。

---

## 2. T1 分类埋点（P0，本次新增）

### 2.1 目标

把每一次问答的「问题原文 + 分类结果 + 路由决策 + 执行结果」落到 SQLite，
用于后期回答三个问题：

1. 哪些域经常被判错？（人工标注 `correct` 后统计）
2. 哪些域分数长期偏低？（说明语料需要补）
3. L1 / L2 的占比与延迟分别是多少？

**不做**：不做实时看板、不做自动纠错、不改任何现有逻辑。

### 2.2 表结构

```sql
CREATE TABLE IF NOT EXISTS loop_trace (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,          -- ISO8601 本地时间，如 2026-11-09T14:32:10
    user_id     TEXT,
    session_id  TEXT,
    query       TEXT    NOT NULL,          -- 用户原话
    intent      TEXT,                      -- 分类器判定的域（可能是错的）
    score       REAL,                      -- 余弦相似度
    route_level TEXT,                      -- L0 / L1 / L2
    domain      TEXT,                      -- 实际执行的域
    tool        TEXT,                      -- 实际调用的 loop 工具
    missing     TEXT,                      -- JSON 数组，追问时非空
    rollout     TEXT,                      -- 灰度原因：mode=loop / whitelist / bucket=7<10
    escalated   INTEGER DEFAULT 0,         -- L1 升级到 L2
    stop_reason TEXT,                      -- answered / asked / handoff / aborted / error / max_steps
    latency_ms  INTEGER,                   -- 从收到 query 到发出 done
    answer_len  INTEGER,                   -- 回答字符数，用于发现空回答
    correct     INTEGER DEFAULT NULL       -- 人工标注：1 对 / 0 错 / NULL 未标注
);

CREATE INDEX IF NOT EXISTS idx_loop_trace_ts     ON loop_trace(ts);
CREATE INDEX IF NOT EXISTS idx_loop_trace_intent ON loop_trace(intent, score);
CREATE INDEX IF NOT EXISTS idx_loop_trace_level  ON loop_trace(route_level);
```

**为什么把 `intent` 和 `domain` 分开**：`intent` 是分类器说的，`domain` 是实际执行的
（L2 时两者可能不同）。只有分开存，才能算出"分类器错判率"。

### 2.3 写入点

放在 `agent/loop/ws_bridge.py` 的 `handle_chat()` 末尾——那里信息最全：
`RolloutDecision`、`PipelineResult`、user/session 都有。

```python
# 伪代码
started = time.perf_counter()
result = await pipeline.handle(...)
await trace.record(
    user_id=user_id, session_id=session_id, query=query,
    intent=plan.intent if plan else None,     # 需要 PipelineResult 透出
    score=plan.confidence if plan else None,
    route_level=result.level, domain=..., tool=...,
    missing=..., rollout=decision.reason,
    escalated=result.escalated, stop_reason=result.stop_reason,
    latency_ms=int((time.perf_counter() - started) * 1000),
    answer_len=len(result.text),
)
```

**需要小改一处**：`PipelineResult` 目前不带 `intent` / `score`，要在 `ChatPipeline.handle`
里把 `plan.intent`（或 `plan.domain` + `plan.confidence`）塞进结果对象。改动约 5 行。

### 2.4 写入方式（关键：不能拖慢主流程）

三个约束：

1. **异步、不 await**：主流程发完 `done` 再落库，或直接丢后台队列；
2. **失败只记 warning**：埋点写失败绝不能影响回答；
3. **批量提交**：每 20 条或每 1 秒 `commit()` 一次，避免每条一次 fsync。

推荐实现（约 60 行）：

```python
# agent/loop/trace.py
import queue, sqlite3, threading, time

class TraceStore:
    def __init__(self, path="data/loop_trace.db"):
        self._q: queue.Queue = queue.Queue(maxsize=1000)
        self._thread = threading.Thread(target=self._worker, args=(path,), daemon=True)
        self._thread.start()

    def record(self, **row) -> None:
        try:
            self._q.put_nowait(row)          # 满了直接丢，不阻塞
        except queue.Full:
            pass

    def _worker(self, path):
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")   # 并发读不阻塞写
        while True:
            rows = [self._q.get()]
            # 攒一批
            while len(rows) < 20:
                try: rows.append(self._q.get_nowait())
                except queue.Empty: break
            try:
                conn.executemany("INSERT INTO loop_trace (...) VALUES (...)", rows)
                conn.commit()
            except Exception:
                logging.warning("[trace] 写入失败", exc_info=True)
```

**不要**用同步 `conn.execute()` 直接写在请求协程里——会让每条请求多 1–5ms，
高 QPS 下更明显。

### 2.5 目录与配置

```
data/loop_trace.db          # 默认路径（需加入 .gitignore）
LOOP_TRACE_ENABLED=1        # 开关，默认关闭
LOOP_TRACE_PATH=...         # 可选覆盖
```

### 2.6 常用查询

```sql
-- ① 各域 L1/L2 占比（决定要不要调阈值）
SELECT domain, route_level, COUNT(*) AS n
FROM loop_trace GROUP BY 1,2 ORDER BY n DESC;

-- ② 分数长期偏低的域（说明语料不够）
SELECT intent, ROUND(AVG(score),3) AS avg_score, COUNT(*) AS n
FROM loop_trace
WHERE ts > datetime('now','-7 days')
GROUP BY 1 HAVING AVG(score) < 0.75 ORDER BY avg_score;

-- ③ 人工标注后的错判率
SELECT intent,
       SUM(correct = 0) * 1.0 / COUNT(*) AS error_rate,
       COUNT(*) AS labeled
FROM loop_trace WHERE correct IS NOT NULL GROUP BY 1 ORDER BY error_rate DESC;

-- ④ 升级与失败
SELECT stop_reason, escalated, COUNT(*) FROM loop_trace GROUP BY 1,2;

-- ⑤ 延迟分位（需要按天）
SELECT date(ts) AS d,
       ROUND(AVG(latency_ms)) AS avg_ms,
       MAX(latency_ms) AS max_ms
FROM loop_trace GROUP BY 1 ORDER BY 1 DESC;

-- ⑥ 抽样人工标注
SELECT id, query, intent, score, route_level, stop_reason
FROM loop_trace WHERE correct IS NULL ORDER BY RANDOM() LIMIT 50;
```

### 2.7 容量与清理

- 单条约 500 字节，10 万条 ≈ 50MB
- 保留 30 天：`DELETE FROM loop_trace WHERE ts < datetime('now','-30 days')`
- 建议每周清理一次（启动时或定时任务）

### 2.8 验收

| 项 | 标准 |
|---|---|
| 对主流程延迟影响 | P50 增加 < 5ms |
| 写入失败不影响回答 | 拔掉 db 文件权限后，问答仍正常 |
| 数据完整 | 10 条问答后表里有 10 行，字段无空 |
| 能回答"哪些域判错" | 手工标注 20 条后，SQL ③ 能出结果 |

---

## 3. T2 分类器语料补充（等业务提示词）

### 3.1 现状

实测 13 条典型问法：

- 11 条判对，2 条判错：`security→fire`(0.745)、`device_status→overview`(0.764)
- 4 条分数偏低但判对：information 0.702、energy 0.723、meeting 0.732、security 0.745

项目文档里 `security↔fire`、`overview 吸铁石` 已经是 xfail，说明问题早被识别。

### 3.2 需要业务提供什么

**不要**让业务同学写"示例问法清单"，那容易写成书面语。更有效的方式：

1. 从真实日志/客服记录里抽 20–30 条**原始问法**（口语、省略、带错别字都行）
2. 每条标一个"应该由哪个域回答"
3. 直接追加到 `agent/intent/classifier.py` 对应的 `*_EXAMPLES`

### 3.3 补充优先级

| 域 | 建议补充条数 | 重点锚定词 |
|---|---|---|
| security_status | 5 | 安防、巡查、安防告警、安防指数 |
| emergency_fire | 5 | 消防、火警、灭火、消防资产 |
| device_status | 5 | 健康度、在线率、维保、维修 |
| compositive_overview | 3 | 园区概况、楼栋数、园区面积 |
| information_status | 3 | 信息发布、广播、节目 |
| energy_status | 3 | 能耗、用电、用水 |
| meeting_status | 3 | 会议、会议室、预约 |

### 3.4 验证

补完跑一遍固定测试集（13 条 + 业务新增的 20–30 条），看：

- 判对率（目标 ≥ 95%）
- 最低分（目标 ≥ 0.72）
- 阈值降到 0.70 后 L1 覆盖率（目标 11/13+）

---

## 4. T3 路由侧数据词否决

语料没覆盖到时的兜底，放在 `agent/loop/router.py` 的 `route()` 里，分类之后：

```python
# 命中"数据词"但分类到易混域 → 不信任，转 L2
VETO = (
    (("安防", "巡查"), "emergency_fire"),
    (("健康度", "在线率", "维保"), "compositive_overview"),
)
for words, bad_domain in VETO:
    if domain == bad_domain and any(w in query for w in words):
        return None
```

**注意**：这里选择"转 L2"而不是"强行改成正确域"——硬改的误伤代价比多一次 LLM 大。

---

## 5. T5 单中心改多中心（可选）

当前每个域一个均值中心，`security_status` 的 11 个子类型平均后会被稀释，
导致和 `fire` 更近。

改进：保留每个**子类型**一个中心（约 69 个），分类时取 top-1 子类型，再映射回域。

- 成本：embedding 一次，余弦从 13 次变 69 次（可忽略）
- 收益：子类型边界清晰，且顺带解决 security 的 `classify_security_sub_type`
- 风险：分数分布会整体上移，阈值需要重新校准

**前置条件**：先用 T1 的埋点数据确认"错判确实集中在多子类型的域"。

---

## 6. 其他待办

| 编号 | 内容 | 说明 |
|---|---|---|
| T6 | 会话改 Redis | 现在 `ws_bridge._sessions` 是进程内存，多副本/重启会丢 pending |
| T7 | ~~L2 流式~~ | ✅ 已完成（`a52aab3`）：LoopAgent 全程 `stream=True`，过程稿单独气泡 |
| T8 | 压缩升级 | **已并入 T14**（见 6.4 方案 E）：现在是"占位式"（保留开头 300 字），不是语义摘要 |
| T9 | vehicle 通行记录 | 后端有 `getVehiclePassRecord`，Python 侧没映射 |
| T11 | MCP 参数白名单 | `McpToolbox.call` 原样透传，参数名漂移不会早失败（见 6.1） |
| T12 | 上下文预算可配 | `context_budget` 写死 60000 **字符**，且 `ws_bridge` 没传，线上无法调；接本地模型会超窗（见 6.2） |
| T13 | 旧链路历史无上限 | JSON persistor 覆盖写（只记 1 轮），MySQL/SQLite 追加写且 load **无 LIMIT**（无限增长），见 6.3 |
| T14 | 上下文压缩优化 | spill 死提示 + 不清理 + Composer 无预算三个 bug，外加 token 计量 / 结构感知截断等 6 个方案；**等全量切 loop 再做**（见 6.4） |


### 6.1 T11 细节：MCP 参数白名单校验

**问题**：`McpToolbox.call()` 把参数原样 `session.call_tool(tool_name, payload)` 下发，
不做任何校验。本地写的参数名一旦和 Java 侧 `@McpToolParam` 漂移，**启动期不报错、
调用时也可能不报错**：Java 会忽略不认识的参数、退回默认口径，返回一个看起来合理
但范围错了的结果。用户和日志都发现不了。

两个来源的参数都有这个风险：

- 13 个显式域：`args_builder` 里写的键名（如 `syncSource`、`spaceId`）
- 21 个 discover 工具：模型自己填的参数名（MCP `inputSchema` 原样暴露，模型可能写错）

**旧链路有这层保护**：`graph/device_query_langgraph.py:199` 的 `_build_tool_args`
会读 langchain 从 `inputSchema` 转出的 `tool.args`，把 Java 不认识的参数丢掉并打
INFO 日志。新链路没继承——这是重构时漏掉的一层。

**做法**（约 30 行，改 `agent/loop/runtime.py`）：

1. 首次调用某 `(server, tool)` 时 `list_tools()` 拉一次 `inputSchema.properties`，缓存
2. `call()` 里按缓存过滤参数，dropped 的记 WARNING
3. 缓存失败（MCP 不可达）时放行，不要因为这个把正常调用拦掉

**分两步上线，别一步到位**：

1. **先只打日志、不拦截**，观察一周。要确认没有误杀——Java 侧如果用了
   `@JsonProperty` 别名，`properties` 里的名字可能和实际接收的参数名不同，
   直接拦截会打掉本该传的字段
2. 确认日志长期为空（即从没漂移过）之后，再改成真拦截，并顺手把旧链路的
   `_build_tool_args` 简化掉，两条链路共用这一层

**验收**：正常运行时日志里**不应出现** `[mcp] xxx 不接受参数 [...]`。
一旦出现就是真的漂移了，去对 Java 侧的参数名。

**为什么排 P1 而不是 P0**：它不阻塞功能，但会掩盖问题——参数错了照样返回 200 和一堆数字。
等 T1 埋点上线、能看到"用户说答得不对"的反馈后，这条的价值会更明显。
### 6.2 T12 细节：上下文预算可配 + 按 token 计

**现状**——`agent/loop/` 已经有三层防护，不要重复造：

1. **单条工具结果封顶**：`govern.py` 2000 行 / 50KB，超出截断并落盘（`_spill`），
   提示里带路径。所有域工具都过这一层（`_generic.py:108`、`device_query.py:311/350/365`）
2. **跨步总量压缩**：`agent.py:236` 每轮调模型前跑 `compress_tool_messages`，
   tool 消息总字符超预算时，从最早的长结果（>2000 字符）开始换成"前 300 字符 + 占位"
3. **跨轮历史裁剪**：`pipeline.py` 的 `Session.max_history=12`（24 条消息），
   且 `add_turn` 只存 user/assistant 纯文本——**工具结果不进 history**，
   所以上一轮查到的清单不会带到下一轮

**问题**：`LoopAgent(context_budget=60_000)` 是**字符数**，而且
`ws_bridge.build_pipeline` 构造时**没传这个参数**，线上没有任何入口能调。
60k 中文字符约 3~4 万 token，本地 8k / 32k 窗口的模型在压缩阈值触发**之前**
就已经超窗，表现为"回答开始丢前文"或直接报 context length 超限。

**和 T8 的区别**：T8 管"压缩方式"（占位 → 语义摘要），本项管"预算多大"和
"用什么单位计量"。两件事独立，可以先做 T12。

**做法**：

1. `LoopAgent.__init__` 的 `context_budget` 改读 `LOOP_CONTEXT_BUDGET` 环境变量，
   缺省仍 60_000——默认行为不变，不影响线上
2. `ws_bridge.build_pipeline` 显式把该值传进 `LoopAgent`
3. （第二步再做）按 token 而不是字符计量。字符数在中文/代码场景偏差太大，
   同一份内容不同 tokenizer 能差一倍

**接本地模型时必须一起确认**：Ollama 默认 `num_ctx=2048`，代码侧压得再准也没用，
模型侧会直接截断。要在 Modelfile 里设 `PARAMETER num_ctx 8192`，或请求时显式传。

### 6.3 T13 细节：旧链路历史无限累加（两套 persistor 语义相反）

**问题**：`memory/store.py:101` 的 `graph_get_history_messages` 把持久化里的**所有**
消息无条件转成 `HumanMessage` / `AIMessage`，没有任何切片；`reactive_pipeline.py:541`
又把它整体拼进 `init_state['messages']`。会话越长越容易超窗，而且是**单调增长**——
同一个 session 聊 50 轮，第 51 轮就要把前 50 轮全部塞进上下文。

**两套 persistor 语义完全相反**：

| 实现 | 写入方式 | 读取方式 | 实际能记住 |
|---|---|---|---|
| `memory_persistor_json.py:24` | `all_data[user_id][session_id] = {...}` **覆盖** | 只有上次写的那份 | **仅上一轮** |
| `memory_persistor_mysql.py:115` / sqlite | `INSERT INTO aiagent_chat_content` **追加** | `SQL_QUERY_CHAT_SESSION_CONTENT`，**无 LIMIT** | **无限增长** |

同一份代码换个存储后端，记忆长度完全相反，这个差异目前没有任何文档记录。

**对照 loop**：新链路 `Session.max_history=12` 有明确上限，且工具结果不进 history。

**做法**（排 P1 而不是 P0：旧链路迟早下线，但它现在仍承载流量）：

1. `graph_get_history_messages` 加 `limit` 参数，默认只取最近 N 条（对齐 loop 的 12 轮）
2. 两套 persistor 统一语义：都按"追加 + 读取时截断"，JSON 那套改成 list 追加
3. `SQL_QUERY_CHAT_SESSION_CONTENT` 直接加 `ORDER BY ... DESC LIMIT`，
   别把整张表读进内存再切片

### 6.4 T14 细节：上下文压缩优化

> **前置条件**：等全部流量切到 loop（`AGENT_MODE=loop`）之后再动。
> 现在两条链路并行，压缩策略一改，对比和回滚都会变复杂。

#### 先修三个现存缺陷（是 bug，不是优化）

**① "已落盘"是死信息**

`govern.py:79` 会告诉模型"完整内容已落盘：/tmp/xxx.txt"，但 loop 的 35 个工具
全是 MCP 业务接口，**没有任何读文件的能力**（全仓 grep 无 `read_file`）。
这句话模型用不上，白占 token，还可能诱发它编造"我看过完整文件"。
→ 改成不承诺可读，或直接去掉路径。

**② spill 文件从不清理**

`govern._spill()` 写完就没人管。设备台账那条 1.3MB 的查询每跑一次落一份，
长期运行 `/tmp/agentic-loop-spill` 只涨不降。
→ 启动时清一次 + 按数量或 TTL 轮转（保留最近 N 个）。

**③ Composer 完全没有预算**

`composer.py:68` 把 `tool_text` 原样拼进 prompt，没有任何长度校验。
L1 直连链路里 `tool_text` 只过 `govern`（≤50KB），再加最多 24 条 history，
整个 prompt 没有上限。`LoopAgent` 至少还有 `context_budget`，Composer 是裸的。
→ 给 Composer 也接预算，超了先截 `tool_text`。

#### 再按性价比做优化

| # | 方案 | 成本 | 收益 | 备注 |
|---|---|---|---|---|
| A | 计量从**字符**改 **token** | 半天 | 接本地模型后从"必然超窗"变可控 | 预算 = 模型窗口 × 0.6；tiktoken 近似或 Ollama `/api/tokenize` |
| B | 截断从**固定 300 字符**改**结构感知** | 0.5~1 天 | 同等预算下信息保留度明显提升 | 收益最大：保留 total + 骨架，而不是前 300 字 |
| C | 淘汰策略：同名工具覆盖 + 最近 N 条不压 | 2 小时 | 边际优化 | 现在单条 ≤2000 字符会被 `continue` 跳过，需要兜底 |
| D | 工具返回层瘦身：默认聚合、细节二次调 | 每域半天 | 治本 | `device_query` 已部分这么做，其余 12 个域仍原样转发 MCP 响应 |
| E | 语义摘要（即 T8） | 半天 | 比 B 更省 | 多一次 LLM 调用；本地模型上建议只对 >20KB 的结果用 |
| F | 跨轮摘要代替直接丢弃 | 半天 | 长会话不丢早期上下文 | 现在 `max_history=12` 超了直接丢，第 1 轮说的"我在 5 楼"永久丢失 |

**B 的具体想法**——按返回形态分支，而不是一刀切取前 300 字符：

```python
# JSON 数组   → "[共 268 条，以下为前 8 条与后 2 条]" + 首尾样本
# 表格文本    → device_query._format_rows 的表头 + 前 5 行 + "...省略 N 行..." + 后 2 行
# 统计类      → {count, name} 这种本来就不大，原样保留，不压
```

**推荐顺序**：先修 ①②③（2 小时，纯 bug）→ A+B（1~2 天，核心收益）→ C → D → E/F。

**和 T8 的关系**：T8 就是这里的 E 方案。重新排序后它的定位变了——语义摘要不是
第一选择，结构感知截断（B）性价比更高，只有结果特别长时才轮到摘要。

---

## 7. 明确不做的

| 事项 | 原因 |
|---|---|
| 把 gen_doc 纳入 loop | 强顺序、固定模板、非交互，pipeline 更合适 |
| 把 scene 纳入 loop | 破坏性动作，需要显式确认，走专用通道 |
| 把 knowledge（RAG）纳入 loop | 自带检索-生成流程，直接暴露 tool 反而更难控 |
| 换更大的 embedding 模型 | 当前瓶颈是语料覆盖，不是模型容量 |
| 全量删除旧图 | 等 T1 数据证明 L1/L2 稳定后再评估 |

---

## 8. 与主文档的关系

- 已完成内容见 `docs/loop-agent-改造文档.md`
- 本文档只记录**未实施**的后续项
- 每完成一项，移入主文档并在此标记 ✅
- 与 loop 无关的项目级事项（如**打包/交付**）记在 `docs/打包部署-TODO.md`，不放这里
