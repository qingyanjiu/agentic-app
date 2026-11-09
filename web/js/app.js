/* ============================================================
   园区智能助手 · 前端逻辑
   WebSocket 协议（与 app.py /chat/{user_id}/{session_id} 对应）：
   - 发送: {"query": "..."}
           知识库模式: {"query": "...", "mode": "kb"}
           场景控制不需要单独模式：后端会把场景指令和业务查询混在一起自动识别
           （仍保留 {"query": "...", "mode": "scene", "scene_session_id": "..."} 供脚本强制走场景）
   - 接收:
     {"event": "custom", "data": {"type": "answer",   "content": "..."}}  回答（流式分块）
     {"event": "custom", "data": {"type": "ask",      "question": "..."}} 多轮追问
     {"event": "custom", "data": {"type": "intent_desc", ...}}            意图理解进度
     {"event": "custom", "data": {"type": "tool", ...}}                   工具调用进度
     {"event": "custom", "data": {"type": "done"}}                        本轮结束
     {"event": "custom", "data": {"type": "error",   "content": "..."}}   错误
     {"event": "on_tool_start" | "on_tool_end" | ...}                     LangChain 生命周期
     {"status": "done"}                                                   整轮结束标识
     {"error": "..."}                                                     全局错误
   ============================================================ */

// ------------------- 连接配置 -------------------
// 支持用页面 URL 参数指定后端,例如:index.html?ws=ws://localhost:8001
const wsParam = new URLSearchParams(location.search).get('ws');
const isHttp = location.protocol === 'http:' || location.protocol === 'https:';
const WS_BASE = wsParam
    ? wsParam.replace(/\/+$/, '')
    : isHttp
        ? `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}`
        : 'ws://127.0.0.1:8001'; // 直接双击打开 HTML 时的兜底地址(本机服务端口)

function getUserId() {
    let uid = localStorage.getItem('demo_user_id');
    if (!uid) {
        uid = 'user_' + Math.random().toString(36).slice(2, 10);
        localStorage.setItem('demo_user_id', uid);
    }
    return uid;
}

let sessionId = crypto.randomUUID();
let ws = null;
let sendQueue = []; // 连接建立前待发送的消息
let turnActive = false;
let manualClose = false; // 新对话主动关闭时不触发自动重连
let reconnectTimer = null; // 断线重连定时器
let reconnectAttempts = 0; // 连续重连失败次数
let reconnectDelay = 1500;
let sendTimeout = null; // 发送后迟迟连不上后端的兜底提示
// 问答模式：agent=智能体问答（业务查询与场景指令自动识别） | kb=知识库直连
let chatMode = 'agent';

// ------------------- DOM -------------------
const messagesEl = document.getElementById('messages');
const statusBarEl = document.getElementById('statusBar');
const statusTextEl = document.getElementById('statusText');
const chipsEl = document.getElementById('chips');
const inputEl = document.getElementById('input');
const sendBtn = document.getElementById('sendBtn');
const newChatBtn = document.getElementById('newChatBtn');
const connDot = document.getElementById('connDot');
const connText = document.getElementById('connText');
const modeAgentBtn = document.getElementById('modeAgentBtn');
const modeKbBtn = document.getElementById('modeKbBtn');
const sceneChipsEl = document.getElementById('sceneChips');

// ------------------- 消息渲染 -------------------
function escapeHtml(s) {
    return s
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

// 轻量行内格式：**加粗** 与 `代码`（正文用 pre-wrap 保留换行，流式渲染安全）
function renderInline(text) {
    let s = escapeHtml(text);
    s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>');
    s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    return s;
}

// 对话区随页面整体滚动，这里滚动文档到底部
function isNearBottom() {
    const doc = document.documentElement;
    return doc.scrollHeight - window.scrollY - window.innerHeight < 160;
}

function scrollToBottom(force = false) {
    if (force || isNearBottom()) {
        window.scrollTo({
            top: document.documentElement.scrollHeight,
            behavior: force ? 'smooth' : 'auto',
        });
    }
}

function addUserBubble(text) {
    const div = document.createElement('div');
    div.className = 'msg msg-user';
    div.textContent = text;
    messagesEl.appendChild(div);
    scrollToBottom(true);
}

function addErrorBubble(text) {
    const div = document.createElement('div');
    div.className = 'msg msg-error';
    div.textContent = text;
    messagesEl.appendChild(div);
    scrollToBottom(true);
}

function addTypingIndicator() {
    removeTypingIndicator();
    const div = document.createElement('div');
    div.className = 'typing';
    div.id = 'typing';
    div.innerHTML = '<span></span><span></span><span></span>';
    messagesEl.appendChild(div);
    scrollToBottom(true);
}

function removeTypingIndicator() {
    const t = document.getElementById('typing');
    if (t) t.remove();
}

// 当前流式回答的气泡（AI）与原始文本缓冲
let currentAiBubble = null;
let currentAiRaw = '';

function ensureAiBubble() {
    if (!currentAiBubble) {
        removeTypingIndicator();
        currentAiRaw = '';
        const div = document.createElement('div');
        div.className = 'msg msg-ai streaming';
        messagesEl.appendChild(div);
        currentAiBubble = div;
    }
}

function appendAiText(chunk) {
    ensureAiBubble();
    currentAiRaw += chunk;
    currentAiBubble.innerHTML = renderInline(currentAiRaw);
    scrollToBottom();
}

function finishAiBubble() {
    if (currentAiBubble) currentAiBubble.classList.remove('streaming');
    currentAiBubble = null;
    currentAiRaw = '';
}

// ------------------- 状态条 -------------------
function setStatus(text) {
    statusTextEl.textContent = text;
    statusBarEl.classList.add('active');
}

function clearStatus() {
    statusBarEl.classList.remove('active');
}

// ------------------- 连接状态 -------------------
function setConnState(state) {
    connDot.classList.remove('connected', 'error');
    if (state === 'connected') {
        connDot.classList.add('connected');
        connText.textContent = '已连接';
    } else if (state === 'error') {
        connDot.classList.add('error');
        connText.textContent = '连接失败';
    } else {
        connText.textContent = '未连接';
    }
}

// ------------------- WebSocket -------------------
function connect() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
        return;
    }
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
    manualClose = false;
    const url = `${WS_BASE}/chat/${getUserId()}/${sessionId}`;
    setConnState('connecting');
    ws = new WebSocket(url);

    ws.onopen = () => {
        reconnectAttempts = 0;
        reconnectDelay = 1500;
        setConnState('connected');
        // 发送连接建立前排队的消息（队列里存的是完整 payload 对象）
        while (sendQueue.length && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify(sendQueue.shift()));
        }
        clearTimeout(sendTimeout);
    };

    ws.onmessage = (e) => handleWsMessage(e.data);

    ws.onclose = () => {
        ws = null;
        if (manualClose) return;
        // 若本轮还没结束就断线，结束当前轮，避免输入框被锁死
        if (turnActive) finishTurn();
        // 连接过就保持红点提示错误，否则显示未连接
        if (!connDot.classList.contains('error')) {
            setConnState('disconnected');
        }
        scheduleReconnect();
    };

    ws.onerror = () => setConnState('error');
}

// 断线自动重连：指数退避，最多连续 5 次；手动发消息时会清零计数重新尝试
function scheduleReconnect() {
    if (manualClose || reconnectTimer) return;
    if (reconnectAttempts >= 5) return;
    reconnectAttempts += 1;
    reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        reconnectDelay = Math.min(reconnectDelay * 2, 15000);
        connect();
    }, reconnectDelay);
}

function sendMessage(text) {
    text = text.trim();
    if (!text || turnActive) return;

    reconnectAttempts = 0; // 用户主动发送时重置重连计数
    connect();
    addUserBubble(text);
    addTypingIndicator();
    setStatus(
        chatMode === 'kb' ? '正在检索知识库…' : '正在思考…'
    );
    turnActive = true;
    updateComposerState();
    chipsEl.classList.add('hidden');
    sceneChipsEl.classList.add('hidden');
    scrollToBottom(true);

    // 知识库模式旁路：跳过意图直连 RAG；其余查询统一入口，由后端自动识别场景指令
    const payload = { query: text };
    if (chatMode === 'kb') {
        payload.mode = 'kb';
    }
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify(payload));
        clearTimeout(sendTimeout);
    } else {
        sendQueue.push(payload);
        // 10s 仍没连上后端，给出提示并解锁输入
        clearTimeout(sendTimeout);
        sendTimeout = setTimeout(() => {
            if (turnActive && sendQueue.length) {
                sendQueue = [];
                addErrorBubble('连接后端失败，请确认服务已启动后重试');
                finishTurn();
            }
        }, 10000);
    }
}

// ------------------- 服务端消息处理 -------------------
function handleWsMessage(raw) {
    let data;
    try {
        data = JSON.parse(raw);
    } catch {
        return;
    }

    // 整轮结束
    if (data.status === 'done') {
        finishTurn();
        return;
    }
    // 全局错误
    if (data.error) {
        addErrorBubble(data.error);
        finishTurn();
        return;
    }

    if (data.event === 'custom') {
        handleCustomEvent(data.data || {});
        return;
    }

    // LangChain 生命周期事件 → 转成轻量状态提示
    switch (data.event) {
        case 'on_tool_start':
            setStatus(`正在调用工具 · ${data.name || ''}`);
            break;
        case 'on_tool_end':
            setStatus('工具调用完成，正在整理结果…');
            break;
        default:
            break;
    }
}

function handleCustomEvent(d) {
    switch (d.type) {
        case 'answer':
            // 流式分块或完整回答（有内容输出就收起状态条）
            appendAiText(d.content || '');
            clearStatus();
            break;
        case 'ask':
            // 多轮追问：作为独立回答气泡展示
            finishAiBubble();
            removeTypingIndicator();
            if (d.question) appendAiText(d.question);
            finishTurn();
            break;
        case 'intent_desc':
            setStatus('正在理解你的需求…');
            break;
        case 'tool':
            setStatus(`正在调用工具 · ${d.name || d.tool || ''}`);
            break;
        case 'done':
            finishTurn();
            break;
        case 'error':
            addErrorBubble(d.content || '服务处理异常');
            finishTurn();
            break;
        default:
            break;
    }
}

function finishTurn() {
    clearTimeout(sendTimeout);
    finishAiBubble();
    removeTypingIndicator();
    clearStatus();
    turnActive = false;
    updateComposerState();
    // 智能体模式下业务/场景快捷入口都恢复，方便连续测试
    if (chatMode === 'agent') {
        chipsEl.classList.remove('hidden');
        sceneChipsEl.classList.remove('hidden');
    }
}

// ------------------- 输入区 -------------------
function updateComposerState() {
    sendBtn.disabled = turnActive || !inputEl.value.trim();
    inputEl.placeholder = turnActive
        ? '助手正在回复…'
        : chatMode === 'kb' ? '知识库问答：直接向园区知识库提问'
            : '输入问题或场景指令，例如“切到 3 楼”“重置视角”';
}

function autoGrow() {
    inputEl.style.height = 'auto';
    inputEl.style.height = Math.min(inputEl.scrollHeight, 120) + 'px';
}

inputEl.addEventListener('input', () => {
    autoGrow();
    updateComposerState();
});

inputEl.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage(inputEl.value);
        inputEl.value = '';
        autoGrow();
        updateComposerState();
    }
});

sendBtn.addEventListener('click', () => {
    sendMessage(inputEl.value);
    inputEl.value = '';
    autoGrow();
    updateComposerState();
    inputEl.focus();
});

// ------------------- 场景入口（卡片 / chips） -------------------
document.querySelectorAll('.cap-card, .chip').forEach((el) => {
    el.addEventListener('click', () => {
        const q = el.dataset.query;
        if (!q) return;
        // 从能力卡片点击时，先平滑滚动到对话区
        document.getElementById('chat').scrollIntoView({ behavior: 'smooth' });
        sendMessage(q);
    });
});

// ------------------- 问答模式切换（智能体 / 知识库） -------------------
// 场景控制不再单独设选项卡：场景指令和业务查询共用一个输入框，由后端自动识别
function setMode(mode) {
    if (chatMode === mode) return;
    chatMode = mode;
    const isAgent = mode === 'agent';
    const isKb = mode === 'kb';

    modeAgentBtn.classList.toggle('active', isAgent);
    modeKbBtn.classList.toggle('active', isKb);
    modeAgentBtn.setAttribute('aria-selected', String(isAgent));
    modeKbBtn.setAttribute('aria-selected', String(isKb));

    updateComposerState();
    // 智能体模式展示业务 + 场景快捷入口；知识库模式都不显示
    chipsEl.classList.toggle('hidden', !isAgent);
    sceneChipsEl.classList.toggle('hidden', !isAgent);
    inputEl.focus();
}

modeAgentBtn.addEventListener('click', () => setMode('agent'));
modeKbBtn.addEventListener('click', () => setMode('kb'));

// ------------------- 新对话 -------------------
newChatBtn.addEventListener('click', (e) => {
    e.preventDefault();
    if (ws) {
        manualClose = true; // 主动关闭不触发断线兜底与自动重连
        ws.onclose = null;
        ws.close();
        ws = null;
    }
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
    reconnectAttempts = 0;
    sessionId = crypto.randomUUID();
    sendQueue = [];
    finishTurn();
    // 清空消息区，按当前模式恢复对应的快捷入口
    messagesEl.innerHTML = '<div class="greeting">你好，我是园区智能助手。<br>点击下方场景快速体验，或直接输入你的问题。</div>';
    chipsEl.classList.toggle('hidden', chatMode !== 'agent');
    sceneChipsEl.classList.toggle('hidden', chatMode !== 'agent');
    setConnState('disconnected');
    inputEl.focus();
});

// ------------------- 初始化 -------------------
updateComposerState();
connect(); // 页面打开即建立连接（后端连接时会初始化 pipeline，提前预热）
