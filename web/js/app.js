/* ============================================================
   园区智能助手 · 前端逻辑（对话演示页）

   后端只剩 WS /chat/{user_id}/{session_id}：
   - 发送：{"query": "...", "mode": "loop"}
       mode 显式传 loop：本分支 loop 是唯一链路，传了就不依赖服务端 AGENT_MODE 的环境配置；
       场景指令不单独设模式，后端按意图自动分流（{"mode":"scene"} 只留给脚本强制走场景）。
   - 接收：
     {"event":"custom","data":{"type":"answer","content":"..."}}   回答，追加语义（流式分块）
     {"event":"custom","data":{"type":"ask","question":"..."}}     追问（读 question，不是 content）
     {"event":"custom","data":{"type":"tool","name":"..."}}        工具调用 → 上段文字落成过程稿
     {"event":"custom","data":{"type":"list","kind":"device",...}} 结构化清单（设备列表）
     {"event":"custom","data":{"type":"error","content":"..."}}    错误
     {"status":"done","level":"L0/L1/L2","escalated":bool}         整轮结束
     {"error":"..."}                                              全局错误

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
const sceneChipsEl = document.getElementById('sceneChips');
const routeBarEl = document.getElementById('routeBar');


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



    if (turnActive) return;

    reconnectAttempts = 0; // 用户主动发送时重置重连计数
    connect();
    addUserBubble(text);
    currentToolNames = [];
    addTypingIndicator();
    setStatus('正在思考…');
    turnActive = true;
    updateComposerState();
    chipsEl.classList.add('hidden');
    sceneChipsEl.classList.add('hidden');
    scrollToBottom(true);

    // loop 是唯一链路：显式带 mode=loop，不依赖服务端 AGENT_MODE 怎么配
    const payload = { query: text, mode: 'loop' };
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
    // 一轮结束：快捷入口恢复，方便连续测试
    chipsEl.classList.remove('hidden');
    sceneChipsEl.classList.remove('hidden');
}

// ------------------- 输入区 -------------------
function updateComposerState() {

    inputEl.disabled = false;
    sendBtn.disabled = turnActive || !inputEl.value.trim();
    inputEl.placeholder = turnActive
        ? '助手正在回复…'
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

// ------------------- Markdown → HTML（聊天回答用） -------------------
// 表格行：以 | 开头即可（模型经常漏写行尾的 |，不能要求首尾都有）
function isTableLine(line) {
    const t = String(line || '').trim();
    return t.startsWith('|') && t.indexOf('|', 1) !== -1;
}

function isTableDivider(line) {
    const t = String(line || '').trim();
    return t.startsWith('|') && t.includes('-') && /^[|\s:|-]+$/.test(t);
}

function splitTableRow(line) {
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
            html.push(`<pre><code>${escapeHtml(buf.join('\n'))}</code></pre>`);
            continue;
        }

        // 表格
        if (isTableLine(line)) {
            const rows = [];
            while (i < lines.length && isTableLine(lines[i])) {
                if (!isTableDivider(lines[i])) rows.push(splitTableRow(lines[i]));
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
            && !isTableLine(lines[i])
            && !isMdListLine(lines[i])
            && !/^```/.test(lines[i].trim())) {
            para.push(lines[i].trim());
            i += 1;
        }
        html.push(`<p>${para.map(renderMdInline).join('<br>')}</p>`);
    }

    return html.join('\n');
}

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
    // 清空消息区，快捷入口恢复
    messagesEl.innerHTML = '<div class="greeting">你好，我是园区智能助手。<br>点击下方场景快速体验，或直接输入你的问题。</div>';
    chipsEl.classList.remove('hidden');
    sceneChipsEl.classList.remove('hidden');
    setConnState('disconnected');
    inputEl.focus();
});

// ------------------- 初始化 -------------------
updateComposerState();
connect(); // 页面打开即建立连接（后端连接时会初始化 pipeline，提前预热）
