# 测试说明

全部测试统一放在 `tests/` 目录下，运行环境为 **docker 容器**（依赖齐全、意图模型经 `INTENT_MODEL_PATH` 挂载）。MCP 工具用 FakeTool 模拟、LLM 传 None，不需要 Java sidecar 在线。

## 文件结构

| 文件 | 内容 | 依赖 |
|---|---|---|
| `conftest.py` | 公共设施：FakeTool / 8 域图工厂 / 事件断言 helper / mock 返回数据 | 全部 |
| `test_slots.py` | 8 个域槽位抽取 + 公共时间解析（含案例2 正则链路） | jionlp |
| `test_handlers.py` | 8 域缺参规则 / 追问话术一致性 / 消防追问轮行为（含已知不一致的 xfail 记录）/ 确认等待态回复分流（person/vehicle，整句白名单护栏） | 无模型 |
| `test_intent_classification.py` | 顶层意图 + 各域子类型分类；案例3 skip 占位 | embedding 模型 |
| `test_graphs.py` | 8 域图端到端：正常流 / 追问 / 放弃 / 日期分支 / 守卫；案例1&2 回归 | 无模型 |
| `test_context_scenarios.py` | C1/C4/C5/C6 多轮场景矩阵（缺参闭环 / 接管 / 子类型切换 / 省略继承；takeover 与继承判定为规格快照，源码指针在用例注释） | 无模型（jionlp+FakeTool） |
| `test_mcp_loader.py` | 案例1 回归：单 server 失败不传染其他域 | 无模型 |
| `test_performance.py` | 性能基准：分类/槽位/图执行/并发/loader 隔离 | 模型（部分） |

## 运行方式（容器内）

```bash
cd /root/agentic-app

# 全量（功能 + 分类模型 + 性能）
pytest

# 日常回归：跳过性能基准
pytest -m "not performance"

# 只跑性能基准（阈值可用 PERF_* 环境变量覆盖）
pytest -m performance -v -s

# 只跑排查记录案例的回归
pytest tests/test_mcp_loader.py tests/test_graphs.py -v
```

## 性能阈值环境变量（默认值宽松，按容器实际性能调整）

| 变量 | 默认 | 含义 |
|---|---|---|
| `PERF_SLOTS_AVG_MS` / `PERF_SLOTS_P95_MS` | 10 / 50 | 正则槽位抽取 |
| `PERF_CLASSIFY_AVG_MS` / `PERF_CLASSIFY_P95_MS` | 1500 / 3000 | 顶层意图分类 |
| `PERF_SUBTYPE_AVG_MS` | 1500 | 子类型分类 |
| `PERF_GRAPH_AVG_MS` / `PERF_GRAPH_P95_MS` | 100 / 300 | 图端到端（FakeTool、无 LLM） |
| `PERF_CONCURRENCY_N` / `PERF_CONCURRENCY_MAX_S` | 30 / 60 | 并发路数 / 总耗时上限 |

示例：`PERF_CLASSIFY_AVG_MS=800 pytest -m performance -v`

## 与 docs/问题排查记录.md 的对应

- **案例1**（单 MCP 后端 404 拖垮全部域）：`test_mcp_loader.py::TestCase1ServerIsolation`、`test_graphs.py::test_unloaded_tool_returns_error_event`、`test_performance.py::test_perf_loader_isolation`
- **案例2**（消防笼统问法必然报「暂不支持」）：`test_handlers.py::TestMissingParams::test_fire_count_treated_as_missing`、`test_graphs.py::ASK_CASES`（fire 条目）、`TestCase2FullFlow`、`test_slots.py::test_aska_option_words_all_hit_regex`
- **案例3**（「实时告警」顶层意图竞争，未修复）：`test_intent_classification.py::TestCase3TopIntentCompetition`（skip 占位，修复后删除 skip 即可）
- **新发现**：安防域 graph 与 handler 缺参规则不一致（security_index / alarm_detail），以 xfail 记录于 `test_handlers.py::TestKnownDivergenceSecurity`
- **批3 挂账**（person 裸人名 / 安防消防竞争 / basic_info 吸铁石 / 周界防区结构性重叠，出处 docs/语料评测记录.md）：以 xfail(strict=True) 记录于 `test_intent_classification.py`，语料扩写批3 修复后会以 XPASS 失败提醒删标记
