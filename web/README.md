# 园区智能助手 · 对话演示页

苹果官网风格(`apple.com`)的对话框交互演示页面:大标题 Hero、毛玻璃导航、
圆角卡片、克制的蓝紫渐变点缀,零依赖纯静态实现(HTML + CSS + 原生 JS)。

## 快速开始

```bash
# 项目根目录下启动后端
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

浏览器访问: <http://localhost:8000/web/>

## 页面结构

```
web/
├── index.html      # 页面结构:导航 / Hero / 能力场景 / 对话区
├── css/style.css   # 苹果风格样式(毛玻璃、气泡、动效、响应式)
└── js/app.js       # WebSocket 对话逻辑与轻量 Markdown 渲染
```

## 功能说明

- **对话框交互**:通过 WebSocket 连接 `/chat/{user_id}/{session_id}`,
  支持流式回答、多轮追问、工具调用状态提示
- **九大场景入口**:人员 / 安防 / 食堂 / 车辆 / 信息 / 能源 / 会议 / 设备 / 消防,
  点击卡片或 chips 直接提问
- **新对话**:右上角一键重置,生成新 session_id
- **连接状态**:导航栏圆点实时指示 WebSocket 连接情况

## 消息协议

发送:`{"query": "用户问题"}`

接收(由 `js/app.js` 解析渲染):

| 消息                                                          | 含义         |
| ------------------------------------------------------------- | ------------ |
| `{"event":"custom","data":{"type":"answer","content":"..."}}` | 回答(流式) |
| `{"event":"custom","data":{"type":"ask","question":"..."}}`   | 多轮追问    |
| `{"event":"custom","data":{"type":"tool", ...}}`              | 工具调用中  |
| `{"event":"custom","data":{"type":"done"}}`                   | 本轮结束    |
| `{"status":"done"}`                                           | 整轮结束    |
