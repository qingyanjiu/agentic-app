# 前端对接：`list` 事件（结构化清单）

> 状态：**前端已接入**（`tf_park_ue` `b936340`：`AiChatDialog.vue` 的 `case 'list'` +
> `applyEntityList`，复用现有 `DeviceNode`）。本文档保留作为协议参考与复查清单；
> 后端实现见 `agentic-app` 的 `9bdf1cb` / `7c8610b`。

## 1. 背景（一句话）

AI 回答里的设备清单原来是纯文本，前端靠 `src/utils/agent/deviceList.ts` 里那套正则
反推「这段是不是设备列表」，格式一变就漏。现在后端会额外发一个**结构化清单事件**，
前端直接用，不用再猜。

## 2. 后端发什么

WebSocket 现有消息里新增一种 `data.type == "list"`：

```json
{
  "event": "custom",
  "data": {
    "type": "list",
    "kind": "device",
    "title": "14 台设备，楼层 5F",
    "items": [
      {
        "kind": "device",
        "id": "jk_zl_5f_0006",
        "label": "5F客梯厅",
        "attrs": {
          "code": "jk_zl_5f_0006",
          "type": "监控",
          "space": "园区/科研办公楼/5F",
          "floor": "5F",
          "status": "启用"
        }
      }
    ],
    "total": 268,
    "truncated": false,
    "note": "其余 208 台未展示，可加楼层或名称缩小范围"
  }
}
```

字段说明：

| 字段 | 说明 |
|---|---|
| `kind` | 业务类型。目前只有 `device`；以后会有 `person` / `vehicle`。**不认识就整个忽略**，文本照旧 |
| `title` | 一句话概括。**和文本 `answer` 的首行是同一句话**（去重要用它） |
| `items[].id` | 点击后回传的标识。设备就是点位编号，**等价于现有 `<device code>` 的值** |
| `items[].label` | 按钮文字 |
| `items[].attrs` | 可选。`device` 目前有 `code` / `type` / `space` / `floor` / `status` |
| `items[].attrs.floor` | `5F` / `-1F` 这种短写法，**可直接当 `<device floor>`**（不要用 `space`，那是全路径） |
| `total` | 过滤前总量 |
| `truncated` / `note` | 是否截断 + 提示语；没截断时这两个字段不出现 |

## 3. 什么时候到

- **L1 直出**：先收到整段 `answer`（里面已经有逐行清单文本），**紧接着**一个 `list`；
- **L2 loop**：`list` 会在工具执行完就发，**早于**最终 `answer` 文本。

两种情况都保证：`list` 和它对应的文本清单是**同一批数据**（`title` 相同）。

## 4. 前端怎么接（推荐做法：复用现有 DeviceNode）

现有链路已经是：`AiChatDialog.vue` 的 `renderContent()` → `MarkdownRender` +
`custom-html-tags: ['thinking','device']` → `DeviceNode.vue` → 点击 `openDeviceScene()`。
所以**不需要新组件**，把 `items` 拼成 `<device>` 标签就全部复用，包括点击行为。

### 4.1 `src/components/AI/AiChatDialog.vue`

在 `handleCustomEvent()`（约 151 行 `switch (data.type)`）里加一个分支：

```ts
    case 'list':
      applyEntityList(data)
      break
```

在 `flushAnswer()` 附近加函数（`escapeAttr` 现在没导出，本地写一个即可）：

```ts
function escapeAttr(value: string): string {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
}

/**
 * 结构化清单（后端 list 事件）。
 *
 * 后端已经明确了「这是一块 device 列表」，不需要再从文本里猜。
 * 复用 DeviceNode：把 items 拼成 <device> 标签即可，点击仍然走 openDeviceScene。
 */
function applyEntityList(data: any) {
  // 不认识的业务类型先不处理：文本路径照旧，不影响现有行为
  if (data?.kind !== 'device') return
  const items = Array.isArray(data.items) ? data.items : []
  if (!items.length) return

  const tags = items.map((item: any) => {
    const attrs = item.attrs || {}
    const parts = [
      `label="${escapeAttr(item.label || item.id || '')}"`,
      `code="${escapeAttr(item.id || attrs.code || '')}"`,
    ]
    if (attrs.type) parts.push(`type="${escapeAttr(attrs.type)}"`)
    // floor 用 attrs.floor（5F / -1F），不要用 attrs.space（全路径，场景解析不了）
    if (attrs.floor) parts.push(`floor="${escapeAttr(attrs.floor)}"`)
    return `<device ${parts.join(' ')}></device>`
  })

  const title = String(data.title || '')
  const block = [title, ...tags].filter(Boolean).join('\n')

  // 文本 answer 里带着同一份清单（那是给还没升级的前端兜底的）。
  // 当前累积内容正好以 title 开头 → 用结构化版本整体替换，避免显示两遍。
  if (title && currentContent.value.trim().startsWith(title)) {
    currentContent.value = block
  } else {
    flushAnswer()          // 先把已有内容落成一条，再单独起一条放清单
    currentContent.value = block
  }
  toScrollBottom()
}
```

> 注意 `flushAnswer()` 在内容为空时会直接 return，所以 L2 里「`list` 先到、文本后到」
> 的情况也安全：清单先落一条，后面的文本继续累积成下一条。

### 4.2 不要做的事

1. **别动 `src/utils/agent/deviceList.ts` 的正则**。先留着做兜底（后端没升级、或 `kind`
   不认识时还要用），等 `list` 在线上稳定几周再逐步退役。
2. **不要把 `list` 当 `answer` 累加**（`currentContent += items…`）——会和文本清单重复两遍。
3. **不要在 `list` 里发新的 WS 请求**。它只是渲染数据。
4. **不要用 `attrs.space` 当 `floor`**，也不要把 `space` 塞进 `code`。
5. **`kind` 不认识时什么都别做**（直接 return），不要 fallback 去猜。

## 5. 验收清单

| # | 场景 | 期望 |
|---|---|---|
| 1 | 问「5楼有哪些监控」 | 清单渲染成可点标签，点击能打开对应场景/视频（和现在点文本设备一致） |
| 2 | 同上 | 清单**只出现一次**（不是文本一份 + 标签一份） |
| 3 | 后端没升级 / 没收到 `list` | 界面和现在完全一致，无报错（走原 `enhanceDeviceLines`） |
| 4 | 超过 60 台的查询 | 看到 `note`：「其余 N 台未展示，可加楼层或名称缩小范围」 |
| 5 | 追问「第三个」 | 不受影响（记忆在后端） |

## 6. 怎么验证 `list` 真的到了

打开浏览器 DevTools → Network → WS → 看帧里有没有 `"type":"list"`。

前提：**后端要重启**（`agentic-app` 的 Python 服务），旧进程不会发这个事件。
另外后端有灰度开关（`AGENT_MODE` / `LOOP_ROLLOUT_*`），只有走 loop 链路才发 `list`；
旧链路（langchain 图）不发——这部分本来就要弃用，不用为它适配。

## 7. 后续（现在不用做）

- 人员/车辆列表：后端会发 `kind: "person"` / `"vehicle"`，前端届时加一条
  `kind → 组件` 或 `kind → 点击动作` 的映射即可，协议不变。
- 行内引用：模型回答里提到单台设备时可能用 `[名称](entity:device/编号)` 的 markdown 链接，
  届时 `MarkdownRender` 认这个 scheme 即可（后端暂未启用）。
