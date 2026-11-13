# loop agent 的 skill 化改造方案

> 状态：**讨论稿，未实施**
> 关联：`docs/loop-agent-TODO.md` T20
> 更新日期：2026-11-13

---

## 0. 结论先行

**不是用 skill 取代 router，而是让 skill 接管「域内具体口径」，router 只保留「能不能确定性处理」这档判断。**

砍掉 router 会让所有长尾问法掉进 L2（模型面对 30+ 工具自由选），
延迟、成本、稳定性都是负收益。真正和 skill 重叠的只有 router 里
「选具体子类型工具」那一小段，其余三件事 skill 替代不了：

| router 现在的职责 | skill 能否接管 | 结论 |
|---|---|---|
| 判域 + 选子类型工具 | ✅ 完全重叠 | **交给 skill** |
| 判 L1/L2（置信度 0.75 阈值） | ❌ 这是"确定性能不能处理"的判定 | 保留 |
| 追问态合并（`_merge_pending` 拼回原问句） | ❌ 单句触发匹配看不到上一轮 | 保留 |
| 场景分流 L0 / 跨域信号转 L2 | ❌ 不属于业务口径 | 保留 |

一句话：**skill 管"怎么查"，router 管"能不能查、要不要问人"**。

---

## 1. 要解决的真实问题（都有实测证据）

| 现象 | 证据 | 归因 |
|---|---|---|
| 同一问法几次结果不同 | 「每个楼层有多少设备呢」置信度 0.735 → L2；「各楼层设备数量」0.757 → L1 `device_query__list` | 卡在 0.75 阈值边缘，0.003 之差换链路 |
| 楼层统计数据错 | `countByFloor` 每层 1~5 台（合计 14），台账同楼层 122/60/47… | 后端口径问题；同时 loop 侧没有可信替代 |
| L2 选错工具 | `devicequery` 21 个工具 + 13 个域工具全暴露给模型 | 工具面太宽，模型只能靠描述猜 |
| 图表 JSON 被贴给用户 | `getEquipClass` 的 `[{itemStyle,name,value}]` 原样出现在回答里 | 工具输出没有格式化层，模型有时照抄 |

共同点：**这些都是「有明确业务口径、但缺一个确定性入口」的缺口**，
正是 skill 该补的位置。

---

## 2. 三种 skill 形态

| 形态 | 定义 | 判定者 | 适用 |
|---|---|---|---|
| **A. 受约束的 L2** | 提示词片段 + 工具白名单 + 步数上限 | 模型（可选范围被钉死） | 跨表对比、多步推理这类开放式问题 |
| **B. 确定性 skill** | `slots → 工具 → 模板` 固定流程，输出直出 | 代码 | 强口径业务，如「每层多少台」 |
| **C. skill 即工具** | 整个流程包成一个 `ToolSpec.execute()` | 代码 | 需要本地聚合/计算，如楼层分布 |

**先做 B/C，再考虑 A。** 理由：B/C 只是"加一个确定性入口"，零风险、立刻见效；
A 会改变 L2 的上下文，需要先验证"工具白名单真能提升选对率"。

---

## 3. 声明格式

目录：

```
agent/loop/skills.py            ← 加载 / 注册 / 匹配
skills/<id>/skill.yaml          ← 定义
skills/<id>/prompt.md           ← A 型才需要
skills/<id>/templates/*.jinja   ← 输出模板（可选）
```

`skill.yaml`：

```yaml
id: floor_distribution
name: 楼层设备分布
description: 按楼层统计设备数量（口径：设备台账，非后端 countByFloor）

# 归属域：router 判到这个域后，先在这里找更具体的口径
owner_domain: device_status
mode: deterministic          # deterministic | constrained
passthrough: true            # 输出是成品文本，不交 composer 润色

# 触发：只做精确匹配，不做模糊打分（避免和 router 打架）
triggers:
  regex:
    - "每个?楼层.*(多少|几台)"
    - "各楼层.*设备"
    - "楼层.*(分布|统计)"
  deny:                      # 命中这些就不算（防止误吃"5楼有多少监控"）
    - "\\d+\\s*[Ff楼层]"

# 确定性流程：execute 由 skills.py 里的 registry 按 id 找函数
plan:
  tool: floor_distribution_local
  args: {}

# constrained 模式才用：把模型可选工具收紧到这些
tools: []
max_steps: 3
```

约束：

- `id` 全局唯一，日志和灰度都按它统计
- `owner_domain` 必须是现有 13 域之一，否则只能在 router 之前命中（相当于新增一条 L1）
- `triggers` **只允许正则/短语**，不接受"描述性触发"——描述性触发会退化成第二个分类器

---

## 4. 接入点（三处，都不改现有行为）

### 4.1 `skill` 优先于域内子类型

```
app.py /pipeline.run
  └─ Router.route(query)
       ├─ 命中 skill（match_skill）──────────────────┐
       │    mode=deterministic → 直接 DomainPlan      │ 新增
       │    mode=constrained   → 记下 skill，继续     │
       ├─ 分类器判域 + 置信度                          │
       │    ≥0.75 → planner(query)                    │
       │        └─ planner 内部先问 skills（同域）      │ 新增
       │    <0.75 → None（L2）                         │
       └─ 追问态合并 / 跨域信号 / 场景分流（不动）
```

改动位置：

| 文件 | 改动 |
|---|---|
| `agent/loop/skills.py` | 新增：`SkillRegistry`、`load_skills()`、`match(query, domain=None)` |
| `agent/loop/router.py` | `route()` 开头加一次 `match`；`DOMAIN_PLANNERS` 返回值前问一次同域 skill |
| `agent/loop/pipeline.py` | L2 分支按 skill 过滤 registry + 追加 prompt + 覆盖 `max_steps` |
| `agent/loop/registry.py` | `ToolRegistry.filter(tool_names)` 新增（L2 收紧工具面用） |

### 4.2 优先级规则（必须写死，否则会互相抢）

```
1. skill 精确命中（含 owner_domain 匹配）  → 确定性执行
2. 分类器 ≥ 阈值 → L1 planner
3. 分类器 < 阈值 → L2（若命中 constrained skill，收紧工具面）
4. 追问态 / 场景 / 跨域 → 走现有分支，skill 不参与
```

冲突时 **skill 让 router**：追问态优先级最高，因为缺参数时执行 skill 必然失败。

---

## 5. 输出契约（沿用现有约定，不能另立一套）

skill 直出文本时，必须遵守这些已有硬规则，否则前端和用户都会踩坑：

| 规则 | 出处 | 原因 |
|---|---|---|
| `text` 首行 = `_summary() + "："`，`list.title` = `_summary()` | `device_query._format_rows` | 前端靠 `startsWith(title)` 去重，改一边就重复显示 |
| 空结果走 `empty_hint`（"该时段没有…"），不说"服务异常" | `_generic._empty_note` | 线上踩过：空结果被说成故障 |
| 大结果不列明细（`BROAD_RESULT_LIMIT=80`） | `device_query.list_devices` | 268 台逐条列既没用又撑爆单帧 |
| `list` 事件最多 30 项（`MAX_LIST_ITEMS`） | `entities.py` | 前端渲染上限 |
| 输出过 `govern()`（2000 行 / 50KB） | `govern.py` | 超限落盘，避免撑爆上下文 |
| 结构化清单走 `ToolOutcome.entities`，不让模型复述 | `entities.py` + `pipeline._is_passthrough` | 清单是成品，二次润色只会加废话 |

---

## 6. 分阶段实施

### P0 骨架（半天，不改线上行为）

- `skills.py` + `SkillRegistry` + 空 skills 目录
- `router.route()` 加 `match_skill`，**默认开关关闭**（`LOOP_SKILLS_ENABLED=false`）
- 单测：命中/未命中/deny 规则/空目录不影响现有路由（跑 `test_loop_router_scene.py` 全绿）

### P1 第一个 skill：楼层分布（B+C 混合，1 天）

选它的理由：**已知数据不可信 + 现有入口全错**（见 §1），修好立刻可验证。

```
楼层分布（台账口径，共 579 台）：
- 1F 122 台
- 2F 60 台
- 3F 47 台
...
- 未标楼层 158 台
```

- 数据源：`listDeviceOnly` 全量（已在 `device_query` 缓存，`DEVICE_CACHE_TTL=300s`）
- 聚合：`spaceName` 优先、`name` 兜底，复用 `normalize_floor` 的楼层短写法（`5F` / `-1F`）
- 输出直出（`passthrough: true`），不再经过模型
- 验收：「每个楼层有多少设备呢」「各楼层设备数量」「查询楼层设备类型」三句**必须走到同一个工具、同一个输出**

### P2 动态工具收紧（1 天）

- `ToolRegistry.filter()`，constrained skill 生效时把 21 个 `devicequery__*` 收到白名单
- 观测：对比开启前后 L2 的工具选择命中率（用 T1 的分类埋点数据）

### P3 迁移表单化（2~3 天，可选）

- 把 `_generic.QueryDomain` 的声明式字段（`tool_map` / `required` / `empty_hint` / `formatter`）统一成 skill 声明，减少两套配置
- 风险最高，收益是"配置只有一处"，可以最后做

---

## 7. 验收标准

| 项 | 标准 |
|---|---|
| 行为不变 | P0 阶段 `LOOP_SKILLS_ENABLED=false` 时，全量测试与基线完全一致（10 failed / 811 passed） |
| 确定性 | P1 之后，§6 里那三句问法固定走 L1，日志都能看到同一条 `[skill] floor_distribution` |
| 无回归 | `test_loop_router_scene.py`、`test_loop_pipeline.py`、`test_scene_routing.py` 全绿（场景分词那批 case 是雷区） |
| 可观测 | 每次 skill 命中打一行：`[skill] 命中 <id> domain=<d> mode=<m> query='…'` |
| 可回滚 | 单个 skill 通过 `skill.yaml.enabled: false` 关闭，不需要改代码 |

---

## 8. 明确不做

| 不做 | 原因 |
|---|---|
| 用 skill 取代 router 的域判定 | 长尾问法会全掉 L2，工具面回到 30+，飘移更严重 |
| skill 做向量/模糊触发 | 会退化成第二个分类器，两套判定互相抢；要泛化就复用 `classifier.py` 的语料 |
| skill 里写自然语言步骤让模型执行 | 与"判定权分层"冲突，参数下发权必须留在代码 |
| 一次把所有域都 skill 化 | 13 域 × 69 子类型的语料是分类器的核心资产，重写风险远大于收益 |
| skill 自己维护一套输出格式 | 必须复用 `_summary` / `entities` / `govern` 的既有契约 |

---

## 9. 与现有文档的关系

| 文档 | 关系 |
|---|---|
| `docs/loop-agent-改造文档.md` | 主文档，记录已实施改动（21.x 系列）；本方案实施后在此追加章节 |
| `docs/loop-agent-TODO.md` T20 | 本方案的进度跟踪入口 |
| `docs/loop-graph-解耦方案.md` | 同级别的方案文档，格式参考；skill 化不依赖解耦，可并行 |
| `docs/loop-agent-TODO.md` T2 | 分类器语料补充；若将来 skill 复用向量触发，依赖 T2 的语料资产 |

---

## 10. 待定问题（需要拍板）

1. **skill 目录放仓库还是可配置路径？** 仓库内便于 review，部署时想热改就得挂卷。
2. **`owner_domain` 之外是否允许"跨域 skill"**（比如「设备台账 + 人员流动」对比类）？允许的话会在 router 之前命中，等于新增一类 L1，需要额外灰度。
3. **P2 收紧工具面后，模型选不到工具怎么办**？需要兜底：白名单里没命中时是回退全量，还是直接转"这个口径要确认"？
4. **P3 要不要做**？两套配置并存的维护成本 vs 一次性重构风险。
