/* ============================================================
   园区智能助手 · 前端逻辑
   WebSocket 协议（与 app.py /chat/{user_id}/{session_id} 对应）：
   - 发送: {"query": "..."}
           {"query": "...", "mode": "kb"}    知识库直连
           {"query": "...", "mode": "loop"}  强制走 loop 新链路（灰度测试用）
           场景指令不单独设模式：后端自动识别，命中 L0 会交回旧链路
           （{"query": "...", "mode": "scene", ...} 仅保留给脚本强制走场景）
   - 接收:
     {"event":"custom","data":{"type":"answer","content":"..."}}   回答，追加语义（流式分块）
     {"event":"custom","data":{"type":"ask","question":"..."}}     追问（读 question，不是 content）
     {"event":"custom","data":{"type":"tool","name":"..."}}        工具调用 → 上段文字落成过程稿
     {"event":"custom","data":{"type":"intent_desc"}}              意图理解进度
     {"event":"custom","data":{"type":"error","content":"..."}}    错误
     {"status":"done","level":"L0/L1/L2","escalated":bool}         整轮结束（旧链路只带 status）
     {"event":"on_tool_start"|"on_tool_end"}                       LangChain 生命周期（旧链路）
     {"error":"..."}                                               全局错误

   注意：后端不发 {"type":"done"}，结束统一认 {"status":"done"}。
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
// 问答模式：agent=智能体问答（自动识别业务/场景） | kb=知识库直连 | loop=强制走 loop 新链路 | doc=报告生成
let chatMode = 'agent';

// 报告生成（/gen_doc 独立 WebSocket）状态
let docWs = null;
let docGenerating = false;
let docPendingPayload = null;
let docConnectTimer = null;

// 一次生成 = 对话里的一个 doc-run 区块；卡片点击后用 run.markdown 渲染右侧预览
let docRunSeq = 0;
let docRuns = new Map();     // runId -> { id, el, markdown, fileName, ... }
let activeDocRun = null;
let activePreviewRunId = null;

// 「报告内容」可选项（与 index.html 里的复选框、后端 TOPIC_ORDER 保持一致）
const DOC_TOPICS = ['安防', '能耗', '通行', '网络', '设备', '食堂', '会议', '巡检'];

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
const modeLoopBtn = document.getElementById('modeLoopBtn');
const modeDocBtn = document.getElementById('modeDocBtn');
const sceneChipsEl = document.getElementById('sceneChips');
const routeBarEl = document.getElementById('routeBar');

// 报告配置模态框
const docModal = document.getElementById('docModal');
const docModalBackdrop = document.getElementById('docModalBackdrop');
const docModalClose = document.getElementById('docModalClose');
const docSubmitBtn = document.getElementById('docSubmitBtn');
const docModalError = document.getElementById('docModalError');
const docPeriodSeg = document.getElementById('docPeriodSeg');
const docTopicsEl = document.getElementById('docTopics');

// 文档预览抽屉（仅点击文档卡片时打开）
const docPanel = document.getElementById('docPanel');
const docCloseBtn = document.getElementById('docCloseBtn');
const docStream = document.getElementById('docStream');
const docPreviewTitle = document.getElementById('docPreviewTitle');
const docPreviewMeta = document.getElementById('docPreviewMeta');
const docDownloadBtn = document.getElementById('docDownloadBtn');

// ------------------- 消息渲染 -------------------
function escapeHtml(s) {
    return s
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

// 行内格式：**加粗**、`代码`、[链接](https://...)
function renderMdInline(text) {
    let s = escapeHtml(String(text ?? ''));
    s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>');
    s = s.replace(
        /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
        '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>'
    );
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
        div.className = 'msg msg-ai md streaming';
        messagesEl.appendChild(div);
        currentAiBubble = div;
    }
}

// Markdown 整块重渲染，按帧合并，避免每个 token 都重排（长回答/表格尤其明显）
let aiRenderPending = false;

function renderAiBubbleNow() {
    if (!currentAiBubble) return;
    // 回答按 Markdown 渲染（标题/列表/表格/代码块），聊天与报告模式共用同一套渲染器
    currentAiBubble.innerHTML = renderMarkdown(currentAiRaw, { headingOffset: 0 });
    scrollToBottom();
}

function scheduleAiRender() {
    if (aiRenderPending) return;
    aiRenderPending = true;
    requestAnimationFrame(() => {
        aiRenderPending = false;
        renderAiBubbleNow();
    });
}

function appendAiText(chunk) {
    ensureAiBubble();
    currentAiRaw += chunk;
    scheduleAiRender();
}

function finishAiBubble() {
    if (currentAiBubble) {
        renderAiBubbleNow(); // 收尾补一次，保证最后一帧不漏渲染
        currentAiBubble.classList.remove('streaming');
    }
    currentAiBubble = null;
    currentAiRaw = '';
}

// 结构化清单卡片。后端用 list 事件明确告诉我们这是一块什么列表（kind），
// 前端只做 kind → 样式 的映射，不再从文本里猜"这行是不是设备"。
// 文本 answer 里也带着逐行明细（兼容还没升级的前端）：如果当前气泡正好是
// 那段清单，直接丢掉气泡只留卡片，避免同一份内容显示两遍。
function renderEntityList(d) {
    if (currentAiBubble && currentAiRaw.trim()) {
        if (currentAiRaw.trim().startsWith(d.title || '')) {
            currentAiBubble.remove();
        } else {
            currentAiBubble.classList.remove('streaming');
        }
        currentAiBubble = null;
        currentAiRaw = '';
    }
    removeTypingIndicator();

    const card = document.createElement('div');
    card.className = `msg msg-list msg-list--${d.kind || 'unknown'}`;

    const title = document.createElement('div');
    title.className = 'msg-list__title';
    title.textContent = d.title || '';
    card.appendChild(title);

    const items = document.createElement('div');
    items.className = 'msg-list__items';
    (d.items || []).forEach((item) => {
        const chip = document.createElement('button');
        chip.type = 'button';
        chip.className = 'msg-list__item';
        chip.textContent = item.label || item.id;
        const attrs = item.attrs || {};
        chip.title = [attrs.type, attrs.space, attrs.status].filter(Boolean).join(' · ');
        // 点击就是把这个点位填进输入框，用户接着输入动作（"打开xxx监控"）。
        // 真正的场景下发仍然走场景通道，测试页不直接触发动作。
        chip.addEventListener('click', () => {
            inputEl.value = item.label || item.id;
            inputEl.focus();
            updateComposerState();
        });
        items.appendChild(chip);
    });
    card.appendChild(items);

    if (d.note) {
        const note = document.createElement('div');
        note.className = 'msg-list__note';
        note.textContent = d.note;
        card.appendChild(note);
    }

    messagesEl.appendChild(card);
    scrollToBottom(true);
}

// 过程稿：模型调工具前说的那段话。落成独立气泡并弱化，避免和最终回答混成一坨
function markProcessBubble() {
    if (!currentAiBubble) return;
    if (currentAiRaw.trim()) {
        currentAiBubble.classList.remove('streaming');
        currentAiBubble.classList.add('msg-process');
    } else {
        currentAiBubble.remove(); // 只有光标没有内容，直接扔掉
    }
    currentAiBubble = null;
    currentAiRaw = '';
}

// 本轮用到的 loop 工具名（用于下面的链路信息）
let currentToolNames = [];

// 链路信息条：只有 loop 链路会在 done 里带 level，旧链路不显示
function showRouteInfo(info) {
    const level = info && info.level;
    if (!level) {
        routeBarEl.hidden = true;
        routeBarEl.textContent = '';
        return;
    }
    const parts = [`链路 ${level}`];
    if (info.escalated) parts.push('已升级');
    if (currentToolNames.length) parts.push(`工具 ${currentToolNames.length} 次`);
    routeBarEl.textContent = parts.join(' · ');
    routeBarEl.hidden = false;
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
    if (!text) return;

    // 报告生成模式：对话框里的一句话就是生成指令，结果卡片直接出现在对话里
    if (chatMode === 'doc') {
        if (docGenerating) return;
        startDocGenerationFromChat(text);
        return;
    }

    if (turnActive) return;

    reconnectAttempts = 0; // 用户主动发送时重置重连计数
    connect();
    addUserBubble(text);
    currentToolNames = [];
    addTypingIndicator();
    setStatus(
        chatMode === 'kb' ? '正在检索知识库…' : '正在思考…'
    );
    turnActive = true;
    updateComposerState();
    chipsEl.classList.add('hidden');
    sceneChipsEl.classList.add('hidden');
    scrollToBottom(true);

    // 知识库模式旁路；Loop 新链路用 mode=loop 强制命中灰度
    const payload = { query: text };
    if (chatMode === 'kb') {
        payload.mode = 'kb';
    } else if (chatMode === 'loop') {
        payload.mode = 'loop';
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

    // 整轮结束（loop 链路会额外带 level / escalated）
    if (data.status === 'done') {
        showRouteInfo(data);
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
            // 工具调用说明上一段文字只是"过程说明"：先把它落成过程稿气泡，
            // 之后的流式内容进新气泡，过程稿与最终回答就不会混在一起。
            markProcessBubble();
            if (d.name || d.tool) currentToolNames.push(d.name || d.tool);
            setStatus(`正在调用工具 · ${d.name || d.tool || ''}`);
            break;
        case 'list':
            // 结构化清单：后端明确说了「这是一块 device 列表」，不用再从文本里猜
            renderEntityList(d);
            clearStatus();
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
    // 业务快捷入口只在智能体模式；场景入口在 agent / loop 都恢复，方便连续测试
    // （loop 下点场景指令正好验证 L0 handoff）
    if (chatMode === 'agent' || chatMode === 'loop') {
        chipsEl.classList.toggle('hidden', chatMode !== 'agent');
        sceneChipsEl.classList.remove('hidden');
    }
}

// ------------------- 输入区 -------------------
function updateComposerState() {
    // 报告生成模式：输入框就是指令入口，生成过程中禁用
    if (chatMode === 'doc') {
        inputEl.disabled = docGenerating;
        inputEl.placeholder = docGenerating
            ? '报告生成中…'
            : '描述要生成的报告，例如“生成2025年10月园区安防和能耗报告”';
        sendBtn.disabled = docGenerating || !inputEl.value.trim();
        return;
    }

    inputEl.disabled = false;
    sendBtn.disabled = turnActive || !inputEl.value.trim();
    inputEl.placeholder = turnActive
        ? '助手正在回复…'
        : chatMode === 'kb' ? '知识库问答：直接向园区知识库提问'
            : chatMode === 'loop' ? 'Loop 新链路：可以问设备统计、跨域问题'
                : '输入问题或场景指令，例如"切到 3 楼""重置视角"';
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

// ------------------- 问答模式切换（智能体 / 知识库 / Loop 新链路 / 报告生成） -------------------
// 场景控制不再单独设选项卡：场景指令和业务查询共用一个输入框，由后端自动识别
function setMode(mode) {
    if (chatMode === mode) return;
    // 生成中禁止切走：面板关闭后进度状态会丢失
    if (docGenerating && mode !== 'doc') return;

    chatMode = mode;
    const isAgent = mode === 'agent';
    const isKb = mode === 'kb';
    const isLoop = mode === 'loop';
    const isDoc = mode === 'doc';

    modeAgentBtn.classList.toggle('active', isAgent);
    modeKbBtn.classList.toggle('active', isKb);
    modeLoopBtn.classList.toggle('active', isLoop);
    modeDocBtn.classList.toggle('active', isDoc);
    modeAgentBtn.setAttribute('aria-selected', String(isAgent));
    modeKbBtn.setAttribute('aria-selected', String(isKb));
    modeLoopBtn.setAttribute('aria-selected', String(isLoop));
    modeDocBtn.setAttribute('aria-selected', String(isDoc));

    // 生成报告模式不再自动开右侧面板；右侧只在点击文档卡片时打开预览
    if (!isDoc) closeDocPanel();

    updateComposerState();
    // 业务快捷入口只在智能体模式展示；场景入口在 agent / loop 都保留
    // （loop 下点场景指令正好验证 L0 handoff 有没有交回旧链路）
    chipsEl.classList.toggle('hidden', !isAgent);
    sceneChipsEl.classList.toggle('hidden', isKb || isDoc);
    if (!isDoc) inputEl.focus();
}

modeAgentBtn.addEventListener('click', () => setMode('agent'));
modeKbBtn.addEventListener('click', () => setMode('kb'));
modeLoopBtn.addEventListener('click', () => setMode('loop'));
modeDocBtn.addEventListener('click', () => {
    if (chatMode !== 'doc') setMode('doc');
    openDocModal();
});

// ------------------- 报告生成（/gen_doc 独立 WebSocket） -------------------
// ---- 报告配置模态框 ----
function openDocModal() {
    docModalError.hidden = true;
    docModalError.textContent = '';
    docModal.hidden = false;
    document.body.classList.add('doc-modal-open');
}

function closeDocModal() {
    docModal.hidden = true;
    document.body.classList.remove('doc-modal-open');
}

function isDocModalOpen() {
    return !docModal.hidden;
}

function setDocModalError(message) {
    docModalError.textContent = message || '';
    docModalError.hidden = !message;
}

function readDocConfig() {
    const periodBtn = docPeriodSeg.querySelector('.doc-seg-btn.active');
    const topics = Array.from(docTopicsEl.querySelectorAll('input[type="checkbox"]:checked'))
        .map((el) => el.value)
        .filter((v) => DOC_TOPICS.includes(v));
    return {
        period: periodBtn ? periodBtn.dataset.value : '日报',
        topics,
    };
}

// ---- 右侧文档预览抽屉（仅点击文档卡片时打开） ----
function openDocPanel() {
    docPanel.classList.add('open');
    docPanel.setAttribute('aria-hidden', 'false');
    // 非模态抽屉：把对话区整体左移，保证输入框和右侧面板同时可用
    document.body.classList.add('doc-open');
}

function closeDocPanel() {
    docPanel.classList.remove('open');
    docPanel.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('doc-open');
}

// 新对话时清空报告相关状态（不碰后端连接，由 closeDocSocket 负责）
function resetDocState() {
    closeDocPanel();
    closeDocModal();
    docGenerating = false;
    activeDocRun = null;
    activePreviewRunId = null;
    docRuns = new Map();
    docStream.innerHTML = '';
    docPreviewTitle.textContent = '文档预览';
    docPreviewMeta.textContent = '点击对话中的文档卡片查看';
    docDownloadBtn.hidden = true;
    docDownloadBtn.href = '#';
    docDownloadBtn.removeAttribute('download');
    modeAgentBtn.disabled = false;
    modeKbBtn.disabled = false;
    updateComposerState();
}

// ------------------- 报告内容渲染（Markdown → HTML） -------------------
function escapeDocHtml(text) {
    return String(text)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

// 表格行：以 | 开头即可（模型经常漏写行尾的 |，不能要求首尾都有）
function isDocTableLine(line) {
    const t = String(line || '').trim();
    return t.startsWith('|') && t.indexOf('|', 1) !== -1;
}

function isDocTableDivider(line) {
    const t = String(line || '').trim();
    return t.startsWith('|') && t.includes('-') && /^[|\s:|-]+$/.test(t);
}

function splitDocTableRow(line) {
    return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim());
}

// 列表标记：无序 - * +；有序 1. / 1、/ (1) / （1）、（兼容中文报告写法）
// 无序标记后必须跟空白（Markdown 规范）：否则「**设备统计**：」会被当成列表行，
// 吞掉一个星号后渲染成「*设备统计**」。
const MD_LIST_RE = /^\s*(?:[-*+]\s+|\d{1,3}\.\s+|\(\d{1,3}\)、?\s*|（\d{1,3}）、?\s*|\d{1,3}、)\s*\S/;
const MD_ORDERED_RE = /^\s*(?:\d{1,3}\.\s+|\(\d{1,3}\)、?\s*|（\d{1,3}）、?\s*|\d{1,3}、)\s*\S/;
const MD_LIST_MARKER_RE = /^\s*(?:[-*+]\s+|\d{1,3}\.\s+|\(\d{1,3}\)、?\s*|（\d{1,3}）、?\s*|\d{1,3}、)\s*/;

function isMdListLine(line) {
    return MD_LIST_RE.test(line);
}

function isMdOrderedLine(line) {
    return MD_ORDERED_RE.test(line);
}

function stripMdListMarker(line) {
    return line.replace(MD_LIST_MARKER_RE, '');
}

// 通用 Markdown 渲染器：聊天回答与报告预览共用
// options.headingOffset：标题整体降级级数（报告预览降 1 级，避免和面板标题打架）
function renderMarkdown(md, options = {}) {
    const headingOffset = options.headingOffset ?? 1;
    const lines = String(md || '').replace(/\r\n/g, '\n').split('\n');
    const html = [];
    let i = 0;

    while (i < lines.length) {
        const line = lines[i];

        // 代码块
        if (/^```/.test(line.trim())) {
            const buf = [];
            i += 1;
            while (i < lines.length && !/^```/.test(lines[i].trim())) {
                buf.push(lines[i]);
                i += 1;
            }
            if (i < lines.length) i += 1;
            html.push(`<pre><code>${escapeDocHtml(buf.join('\n'))}</code></pre>`);
            continue;
        }

        // 表格
        if (isDocTableLine(line)) {
            const rows = [];
            while (i < lines.length && isDocTableLine(lines[i])) {
                if (!isDocTableDivider(lines[i])) rows.push(splitDocTableRow(lines[i]));
                i += 1;
            }
            if (rows.length) {
                const head = rows[0].map((c) => `<th>${renderMdInline(c)}</th>`).join('');
                const body = rows.slice(1)
                    .map((r) => `<tr>${r.map((c) => `<td>${renderMdInline(c)}</td>`).join('')}</tr>`)
                    .join('');
                html.push(`<div class="md-table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`);
            }
            continue;
        }

        // 标题（按 headingOffset 整体降级，面板里默认降一级）
        const heading = line.match(/^(#{1,6})\s+(.*)$/);
        if (heading) {
            const level = Math.min(heading[1].length + headingOffset, 6);
            html.push(`<h${level}>${renderMdInline(heading[2])}</h${level}>`);
            i += 1;
            continue;
        }

        // 分割线
        if (/^\s*(---|\*\*\*)\s*$/.test(line)) {
            html.push('<hr>');
            i += 1;
            continue;
        }

        // 有序 / 无序列表
        if (isMdListLine(line)) {
            const ordered = isMdOrderedLine(line);
            const items = [];
            while (i < lines.length && isMdListLine(lines[i])) {
                // 同一列表块内不混排有序/无序
                if (isMdOrderedLine(lines[i]) !== ordered) break;
                items.push(stripMdListMarker(lines[i]));
                i += 1;
            }
            const tag = ordered ? 'ol' : 'ul';
            html.push(`<${tag}>${items.map((t) => `<li>${renderMdInline(t)}</li>`).join('')}</${tag}>`);
            continue;
        }

        // 空行
        if (!line.trim()) {
            i += 1;
            continue;
        }

        // 段落
        const para = [];
        while (i < lines.length && lines[i].trim()
            && !/^(#{1,6})\s+/.test(lines[i])
            && !isDocTableLine(lines[i])
            && !isMdListLine(lines[i])
            && !/^```/.test(lines[i].trim())) {
            para.push(lines[i].trim());
            i += 1;
        }
        html.push(`<p>${para.map(renderMdInline).join('<br>')}</p>`);
    }

    return html.join('\n');
}

// 报告预览：标题降一级
function renderDocMarkdown(md) {
    return renderMarkdown(md, { headingOffset: 1 });
}

// ------------------- 一次生成 = 对话里的一个文档区块 -------------------
function docDisplayName(fileName) {
    const name = String(fileName || '运营分析报告.docx');
    return name.replace(/_\d{8,}\.docx$/i, '.docx');
}

function docTimeLabel(ts) {
    const d = new Date(ts);
    const pad = (n) => String(n).padStart(2, '0');
    return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function docElapsedLabel(ms) {
    const s = Math.max(1, Math.round(ms / 1000));
    return s >= 60 ? `${Math.floor(s / 60)} 分 ${s % 60} 秒` : `${s} 秒`;
}

function docWordCount(text) {
    return (String(text || '').match(/[一-龥]/g) || []).length;
}

function createDocRunBlock() {
    const id = `doc-run-${++docRunSeq}`;
    const wrap = document.createElement('div');
    wrap.className = 'doc-run';
    wrap.dataset.runId = id;
    wrap.innerHTML = `
        <div class="doc-run-head">
            <span class="doc-run-spinner"></span>
            <span class="doc-run-status">正在理解生成指令…</span>
            <span class="doc-run-meta"></span>
        </div>
        <ul class="doc-run-steps"></ul>
        <div class="doc-run-result" hidden></div>`;
    messagesEl.appendChild(wrap);
    scrollToBottom(true);

    const run = {
        id,
        el: wrap,
        statusEl: wrap.querySelector('.doc-run-status'),
        metaEl: wrap.querySelector('.doc-run-meta'),
        stepsEl: wrap.querySelector('.doc-run-steps'),
        resultEl: wrap.querySelector('.doc-run-result'),
        steps: new Map(),
        markdown: '',
        fileName: '',
        downloadUrl: '',
        startedAt: Date.now(),
        finished: false,
    };
    docRuns.set(id, run);
    return run;
}

function updateDocStep(run, d) {
    if (!run || !d || !d.step) return;
    let li = run.steps.get(d.step);
    if (!li) {
        li = document.createElement('li');
        li.className = 'doc-step';
        const dot = document.createElement('span');
        dot.className = 'doc-step-dot';
        const text = document.createElement('span');
        text.className = 'doc-step-text';
        li.appendChild(dot);
        li.appendChild(text);
        run.stepsEl.appendChild(li);
        run.steps.set(d.step, li);
    }
    li.classList.remove('running', 'done', 'failed');
    if (d.status) li.classList.add(d.status);
    const label = li.querySelector('.doc-step-text');
    if (d.message) label.textContent = d.message;
    if (d.status === 'done' || d.status === 'failed') {
        run.statusEl.textContent = d.message || run.statusEl.textContent;
    }
    scrollToBottom();
}

function showDocParams(run, d) {
    if (!run || !d) return;
    const bits = [d.company || '园区'];
    if (d.period) bits.push(d.period);
    if (Array.isArray(d.topics) && d.topics.length) bits.push(d.topics.join(' / '));
    else if (d.start_date) bits.push(`${d.start_date} 至 ${d.end_date}`);
    run.metaEl.textContent = bits.join(' · ');
}

function appendDocContent(run, chapter, content) {
    if (!run || !content) return;
    run.markdown += content;
    const words = docWordCount(run.markdown);
    const where = chapter && chapter !== '主标题' ? `正在写「${chapter}」` : '正在生成正文';
    run.statusEl.textContent = `${where} · 已生成 ${words} 字`;
    scrollToBottom();
}

function showDocResult(run, d) {
    if (!run) return;
    const fileName = d.file_name
        || (d.file_path ? String(d.file_path).split(/[\\/]/).pop() : '运营分析报告.docx');
    const httpBase = WS_BASE.replace(/^ws/, 'http');
    let url = d.download_url || `/reports/${encodeURIComponent(fileName)}`;
    if (url.startsWith('/')) url = httpBase + url;

    run.fileName = fileName;
    run.downloadUrl = url;

    const display = docDisplayName(fileName);
    const words = docWordCount(run.markdown);
    const time = docTimeLabel(Date.now());

    run.resultEl.innerHTML = `
        <button class="doc-card" type="button">
            <span class="doc-card-icon">W</span>
            <span class="doc-card-meta">
                <strong></strong>
                <span></span>
            </span>
            <span class="doc-card-open">预览 ›</span>
        </button>
        <p class="doc-card-note"></p>
        <a class="doc-card-download" href="#" download>下载 DOCX</a>`;

    run.resultEl.querySelector('.doc-card-meta strong').textContent = display;
    run.resultEl.querySelector('.doc-card-meta span').textContent = `创建时间：${time}`;
    run.resultEl.querySelector('.doc-card-note').textContent =
        `文档已经生成：《${display}》，正文 ${words} 字。点击卡片可在右侧查看 HTML 版全文。`;

    const link = run.resultEl.querySelector('.doc-card-download');
    link.href = url;
    link.setAttribute('download', display);

    run.resultEl.querySelector('.doc-card').addEventListener('click', () => openDocPreview(run.id));
    run.resultEl.hidden = false;
    scrollToBottom(true);
}

function finishDocRun(run) {
    if (!run || run.finished) return;
    run.finished = true;
    run.el.classList.add('done');
    run.el.querySelector('.doc-run-spinner')?.classList.add('hidden');
    run.stepsEl.hidden = true;
    const elapsed = docElapsedLabel(Date.now() - run.startedAt);
    const words = docWordCount(run.markdown);
    run.statusEl.textContent = `共用时 ${elapsed}`;
    run.metaEl.textContent = words ? `正文 ${words} 字` : run.metaEl.textContent;
    scrollToBottom();
}

function failDocRun(run, message) {
    const text = message || '报告生成失败';
    if (!run) {
        addErrorBubble(text);
        return;
    }
    run.el.classList.add('failed');
    run.el.querySelector('.doc-run-spinner')?.classList.add('hidden');
    run.statusEl.textContent = '生成失败';
    run.resultEl.hidden = false;
    run.resultEl.innerHTML = '';
    const p = document.createElement('p');
    p.className = 'doc-run-error';
    p.textContent = text;
    run.resultEl.appendChild(p);
    scrollToBottom(true);
}

// 点击文档卡片：用该次生成的 Markdown 渲染右侧 HTML 预览
function openDocPreview(runId) {
    const run = docRuns.get(runId);
    if (!run) return;
    activePreviewRunId = runId;

    docStream.innerHTML = renderDocMarkdown(run.markdown || '文档内容为空');
    docStream.scrollTop = 0;
    docPreviewTitle.textContent = docDisplayName(run.fileName);
    docPreviewMeta.textContent = [
        run.finished ? '生成完成' : '生成中',
        `正文 ${docWordCount(run.markdown)} 字`,
        docTimeLabel(run.startedAt),
    ].join(' · ');

    if (run.downloadUrl) {
        docDownloadBtn.href = run.downloadUrl;
        docDownloadBtn.setAttribute('download', docDisplayName(run.fileName));
        docDownloadBtn.hidden = false;
    } else {
        docDownloadBtn.hidden = true;
    }
    openDocPanel();
}

function closeDocSocket() {
    clearTimeout(docConnectTimer);
    docConnectTimer = null;
    docPendingPayload = null;
    if (docWs) {
        docWs.onopen = null;
        docWs.onmessage = null;
        docWs.onerror = null;
        docWs.onclose = null;
        docWs.close();
        docWs = null;
    }
}

function ensureDocSocket() {
    if (docWs && (docWs.readyState === WebSocket.OPEN || docWs.readyState === WebSocket.CONNECTING)) {
        return docWs;
    }

    const url = `${WS_BASE}/gen_doc/${getUserId()}/${sessionId}`;
    docWs = new WebSocket(url);

    docWs.onopen = () => {
        clearTimeout(docConnectTimer);
        docConnectTimer = null;
        if (docPendingPayload) {
            docWs.send(JSON.stringify(docPendingPayload));
            docPendingPayload = null;
        }
    };
    docWs.onmessage = (e) => handleDocMessage(e.data);
    docWs.onerror = () => {
        if (docGenerating) failDocGeneration('报告服务连接失败，请确认后端已启动');
    };
    docWs.onclose = () => {
        docWs = null;
        if (docGenerating) failDocGeneration('报告连接已断开，生成未完成');
    };

    return docWs;
}

function finishDocGeneration() {
    docGenerating = false;
    docPendingPayload = null;
    clearTimeout(docConnectTimer);
    docConnectTimer = null;
    activeDocRun = null;
    modeAgentBtn.disabled = false;
    modeKbBtn.disabled = false;
    updateComposerState();
}

function failDocGeneration(message) {
    failDocRun(activeDocRun, message);
    finishDocGeneration();
}

function handleDocMessage(raw) {
    let data;
    try {
        data = JSON.parse(raw);
    } catch {
        return;
    }

    if (data.status === 'done') {
        finishDocRun(activeDocRun);
        finishDocGeneration();
        return;
    }
    if (data.error) {
        failDocGeneration(data.error);
        return;
    }
    if (data.event !== 'custom') return;

    const d = data.data || {};
    const run = activeDocRun;
    switch (d.type) {
        case 'doc_step':
            updateDocStep(run, d);
            break;
        case 'doc_params':
            showDocParams(run, d);
            break;
        case 'doc_data':
            // 数据采集结果：步骤状态已由 doc_step 更新，这里不额外渲染
            break;
        case 'chapter_content':
            appendDocContent(run, d.chapter || '', d.content || '');
            break;
        case 'word_save':
            break;
        case 'final_file':
            showDocResult(run, d);
            break;
        case 'error':
            failDocRun(run, d.content || '报告生成失败');
            finishDocGeneration();
            break;
        default:
            break;
    }
}

// 统一入口：payload = {instruction, company?, period?, topics?}（旧协议 {query} 后端仍兼容）
function beginDocRun(payload, label) {
    if (docGenerating) return;

    addUserBubble(label || payload.instruction || '生成运营分析报告');
    activeDocRun = createDocRunBlock();

    docGenerating = true;
    modeAgentBtn.disabled = true;
    modeKbBtn.disabled = true;
    updateComposerState();

    const socket = ensureDocSocket();
    if (socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify(payload));
    } else {
        docPendingPayload = payload;
        clearTimeout(docConnectTimer);
        docConnectTimer = setTimeout(() => {
            if (docGenerating) failDocGeneration('连接报告服务超时，请稍后重试');
        }, 10000);
    }
}

// 模态框：获取报告
function startDocGeneration() {
    if (docGenerating) return;
    const cfg = readDocConfig();
    if (!cfg.topics.length) {
        setDocModalError('请至少选择一项报告内容');
        return;
    }
    const company = '园区';
    closeDocModal();
    beginDocRun({
        instruction: `生成${company}${cfg.period}运营分析报告，内容包含${cfg.topics.join('、')}`,
        company,
        period: cfg.period,
        topics: cfg.topics,
    });
}

// 对话框：一句话就是生成指令
function startDocGenerationFromChat(instruction) {
    beginDocRun({ instruction }, instruction);
}

docCloseBtn.addEventListener('click', closeDocPanel);
docSubmitBtn.addEventListener('click', startDocGeneration);
docModalClose.addEventListener('click', closeDocModal);
docModalBackdrop.addEventListener('click', closeDocModal);
docPeriodSeg.addEventListener('click', (e) => {
    const btn = e.target.closest('.doc-seg-btn');
    if (!btn) return;
    docPeriodSeg.querySelectorAll('.doc-seg-btn')
        .forEach((b) => b.classList.toggle('active', b === btn));
});

document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (isDocModalOpen()) {
        closeDocModal();
        return;
    }
    if (docPanel.classList.contains('open')) {
        closeDocPanel();
        return;
    }
    if (chatMode === 'doc') setMode('agent');
});

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
    closeDocSocket();
    resetDocState();
    finishTurn();
    // 清空消息区，按当前模式恢复对应的快捷入口
    messagesEl.innerHTML = '<div class="greeting">你好，我是园区智能助手。<br>点击下方场景快速体验，或直接输入你的问题。</div>';
    chipsEl.classList.toggle('hidden', chatMode !== 'agent');
    sceneChipsEl.classList.toggle('hidden', chatMode === 'kb' || chatMode === 'doc');
    setConnState('disconnected');
    inputEl.focus();
});

// ------------------- 初始化 -------------------
updateComposerState();
connect(); // 页面打开即建立连接（后端连接时会初始化 pipeline，提前预热）
