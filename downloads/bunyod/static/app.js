const chatbox = document.getElementById('chatbox');
const userInput = document.getElementById('userInput');
const plusBtn = document.getElementById('plusBtn');
const plusMenu = document.getElementById('plusMenu');
const inputWrapper = document.getElementById('inputWrapper');
const dropdownPanel = document.getElementById('dropdownPanel');
const functionsToggle = document.getElementById('functionsToggle');
const sidebar = document.getElementById('sidebar');
const sidebarOverlay = document.getElementById('sidebarOverlay');
const sidebarFunctions = document.getElementById('sidebarFunctions');
const sidebarModelSelect = document.getElementById('sidebarModelSelect');
const sidebarModelBadge = document.getElementById('sidebarModelBadge');

let functionsLoaded = false;
let inputMode = null;

const ICONS = {
    copy: '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>',
    check: '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>',
    volume: '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4.702a.705.705 0 0 0-1.203-.498L6.413 7.587A1.4 1.4 0 0 1 5.416 8H3a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2.416a1.4 1.4 0 0 1 .997.413l3.383 3.384A.705.705 0 0 0 11 19.298z"/><path d="M16 9a5 5 0 0 1 0 6"/><path d="M19.364 18.364a9 9 0 0 0 0-12.728"/></svg>',
    error: '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" x2="12" y1="8" y2="12"/><line x1="12" x2="12.01" y1="16" y2="16"/></svg>',
    image: '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="18" height="18" x="3" y="3" rx="2" ry="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.1-3.1a2 2 0 0 0-2.814.015L9 19.72"/></svg>',
    file: '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/></svg>',
    download: '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" x2="12" y1="15" y2="3"/></svg>',
    send: '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.536 21.686a.5.5 0 0 0 .937-.024l6.5-19a.496.496 0 0 0-.635-.635l-19 6.5a.5.5 0 0 0-.024.937l7.93 3.18a2 2 0 0 1 1.112 1.11z"/><path d="m21.854 1.145-9.594 9.594"/></svg>',
    code: '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>'
};

// Sidebar
function toggleSidebar() {
    sidebar.classList.toggle('open');
    sidebarOverlay.classList.toggle('visible');
}
function closeSidebar() {
    sidebar.classList.remove('open');
    sidebarOverlay.classList.remove('visible');
}
function switchModelFromSidebar(id) {
    switchModel(id);
    sidebarModelBadge.textContent = window.currentProviderName || 'groq';
}

// Plus button
userInput.addEventListener('focus', () => { plusBtn.style.display = 'none'; plusMenu.style.display = 'none'; plusBtn.classList.remove('active'); });
userInput.addEventListener('blur', () => { if (userInput.value.trim() === '' && !inputMode) plusBtn.style.display = 'flex'; });
plusBtn.addEventListener('click', (e) => { e.stopPropagation(); const isOpen = plusMenu.style.display === 'flex'; plusMenu.style.display = isOpen ? 'none' : 'flex'; plusBtn.classList.toggle('active', !isOpen); });
document.addEventListener('click', (e) => { if (!inputWrapper.contains(e.target)) { plusMenu.style.display = 'none'; plusBtn.classList.remove('active'); } });

function triggerFile(type) { plusMenu.style.display = 'none'; plusBtn.classList.remove('active'); document.getElementById(type === 'image' ? 'imageInput' : 'fileInput').click(); }

// ========== НОВАЯ handleFile с анализом изображений ==========
function handleFile(inputElement, type) {
    if (inputElement.files.length === 0) return;
    const file = inputElement.files[0];
    const fileName = file.name;

    if (type === 'image') {
        // Загружаем и анализируем изображение
        appendMessage('user', '📷 Анализ изображения: ' + fileName);

        const formData = new FormData();
        formData.append('image', file);

        const typingId = showTyping();

        fetch('/upload_image', {
            method: 'POST',
            body: formData
        })
        .then(r => r.json())
        .then(uploadData => {
            if (uploadData.error) {
                hideTyping(typingId);
                appendMessage('error', uploadData.error);
                return;
            }

            // Анализируем через плагин
            return fetch('/command', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({command: '/analyze ' + uploadData.filepath})
            })
            .then(r => r.json())
            .then(analyzeData => {
                hideTyping(typingId);
                if (analyzeData.error) appendMessage('error', analyzeData.error);
                else appendMessage('bot', analyzeData.result);
            });
        })
        .catch(e => {
            hideTyping(typingId);
            appendMessage('error', 'Ошибка при анализе изображения');
        });

        return;
    }

    // Для файлов — старая логика
    const icon = ICONS.file;
    appendMessage('user', '<span class="file-chip">' + icon + fileName + '</span>', true);
    const fakePrompt = 'Файл: ' + fileName;
    fetch('/send', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({message: fakePrompt}) })
    .then(r => r.json()).then(d => { if (d.error) appendMessage('error', d.error); else { appendMessage('bot', d.reply); if (d.voice_enabled) speakText(d.reply); } })
    .catch(() => appendMessage('error', 'Ошибка сети'));
}

function activateMode(mode) {
    inputMode = mode;
    userInput.placeholder = mode.placeholder;
    userInput.value = '';
    plusBtn.style.display = 'none';
    userInput.focus();
    functionsToggle.classList.add('active');
}
function clearMode() {
    inputMode = null;
    userInput.placeholder = 'Спроси что-нибудь...';
    functionsToggle.classList.remove('active');
    if (userInput.value.trim() === '') plusBtn.style.display = 'flex';
}

async function loadModels() {
    try {
        const r = await fetch('/models_list'); const d = await r.json();
        const renderSelect = (select) => {
            select.innerHTML = '';
            d.models.forEach(p => {
                const og = document.createElement('optgroup'); og.label = p.provider;
                p.list.forEach(m => { const o = document.createElement('option'); o.value = m.id; o.textContent = m.name; if (m.id === d.current) { o.selected = true; } og.appendChild(o); });
                select.appendChild(og);
            });
        };
        renderSelect(sidebarModelSelect);
        window.currentProviderName = d.models[0]?.provider || 'groq';
        sidebarModelBadge.textContent = window.currentProviderName;
    } catch(e) {}
}
async function switchModel(id) {
    try {
        const r = await fetch('/switch_model?model_id=' + encodeURIComponent(id));
        const d = await r.json();
        if (!d.error) {
            window.currentProviderName = d.provider;
            sidebarModelBadge.textContent = d.provider;
        }
    } catch(e) {}
}

async function loadFunctions() { if (functionsLoaded) return; try { const r = await fetch('/commands_list'); const d = await r.json(); renderFunctionButtons(d.commands); functionsLoaded = true; } catch(e) {} }

function renderFunctionButtons(commands) {
    dropdownPanel.innerHTML = '';
    sidebarFunctions.innerHTML = '';
    commands.forEach(cmd => {
        const sidebarBtn = document.createElement('button');
        sidebarBtn.innerHTML = cmd.label;
        sidebarBtn.onclick = () => {
            closeSidebar();
            handleCommandAction(cmd.command);
        };
        sidebarFunctions.appendChild(sidebarBtn);

        const dropdownBtn = document.createElement('button');
        dropdownBtn.innerHTML = cmd.label;
        dropdownBtn.onclick = () => {
            toggleFunctions();
            handleCommandAction(cmd.command);
        };
        dropdownPanel.appendChild(dropdownBtn);
    });
}

function handleCommandAction(command) {
    const simpleCmds = ['/clear','/save','/load','/history','/help','/voice on','/voice off','/alias','/snippet list','/monitor battery','/monitor memory','/analyze'];
    if (simpleCmds.includes(command)) {
        sendCommand(command);
        return;
    }
    if (command.startsWith('/services weather')) {
        activateMode({ prefix: '/services weather ', placeholder: 'Введите город для погоды...' });
    } else if (command.startsWith('/services currency')) {
        activateMode({ prefix: '/services currency ', placeholder: 'Введите валюты (например USD RUB)...' });
    } else if (command.startsWith('/services wiki')) {
        activateMode({ prefix: '/services wiki ', placeholder: 'Введите запрос для Википедии...' });
    } else if (command.startsWith('/search')) {
        activateMode({ prefix: '/search ', placeholder: 'Введите поисковый запрос...' });
    } else if (command.startsWith('/snippet create')) {
        activateMode({ prefix: '/snippet create ', placeholder: 'Введите имя и код сниппета...' });
    } else if (command.startsWith('/git')) {
        activateMode({ prefix: '/git ', placeholder: 'Введите путь к папке...' });
    } else if (command.startsWith('/code')) {
        activateMode({ prefix: '/code ', placeholder: 'Опишите, какой код создать...' });
    } else if (command.startsWith('/image') || command.toLowerCase().includes('image')) {
        activateMode({ prefix: '/image ', placeholder: 'Опишите изображение для генерации...' });
    } else {
        sendCommand(command);
    }
}

function toggleFunctions() {
    if (dropdownPanel.classList.contains('hidden')) {
        loadFunctions();
        dropdownPanel.classList.remove('hidden');
        dropdownPanel.classList.add('visible');
        functionsToggle.classList.add('open');
    } else {
        dropdownPanel.classList.remove('visible');
        dropdownPanel.classList.add('hidden');
        functionsToggle.classList.remove('open');
    }
}

function parseMarkdown(text) {
    let html = text
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/```(\w+)?\n?([\s\S]*?)```/g, (m, lang, code) => '<pre><code>' + code.trim() + '</code></pre>')
        .replace(/`([^`]+)`/g, '<code>$1</code>')
        .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
        .replace(/\*([^*]+)\*/g, '<em>$1</em>')
        .replace(/^###\s+(.+)$/gm, '<h3>$1</h3>')
        .replace(/^##\s+(.+)$/gm, '<h3>$1</h3>')
        .replace(/^#\s+(.+)$/gm, '<h2>$1</h2>')
        .replace(/^\-\s+(.+)$/gm, '<li>$1</li>')
        .replace(/^\d+\.\s+(.+)$/gm, '<li>$1</li>')
        .replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
    html = html.replace(/(<li>.*<\/li>\n?)+/g, '<ul>$&</ul>');
    html = html.replace(/\n{2,}/g, '</p><p>').replace(/\n/g, '<br>');
    html = '<p>' + html + '</p>';
    html = html.replace(/<p><\/p>/g,'').replace(/<p>(<pre>.*?<\/pre>)<\/p>/gs, '$1').replace(/<p>(<h[23]>.*?<\/h[23]>)<\/p>/g, '$1').replace(/<p>(<ul>.*?<\/ul>)<\/p>/gs, '$1');
    return html;
}

function appendMessage(role, text, isHtml = false, meta = null) {
    const div = document.createElement('div');
    div.className = `message ${role}-msg`;
    
    const now = new Date();
    const time = now.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });

    if (role === 'system') {
        div.innerHTML = text;
        chatbox.appendChild(div);
        chatbox.scrollTop = chatbox.scrollHeight;
        return;
    }

    if (role === 'error') {
        div.innerHTML = ICONS.error + ' ' + text;
        chatbox.appendChild(div);
        chatbox.scrollTop = chatbox.scrollHeight;
        return;
    }

    if (role === 'user') {
        const content = isHtml ? text : parseMarkdown(text);
        div.innerHTML = `<div class="msg-content">${content}</div><div class="msg-footer"><span class="msg-time">${time}</span></div>`;
        chatbox.appendChild(div);
        chatbox.scrollTop = chatbox.scrollHeight;
        return;
    }

    if (role === 'bot') {
        let extra = '';
        if (meta && meta.type === 'code' && meta.filename && meta.download_url) {
            extra = `
                <div class="code-actions">
                    <a class="code-btn" href="${meta.download_url}" download>
                        ${ICONS.download} Скачать
                    </a>
                    <button class="code-btn" onclick="shareCodeToChat('${meta.filename}', this)">
                        ${ICONS.send} Отправить в чат
                    </button>
                    <button class="code-btn" onclick="runCode('${meta.filename}', 'python')">
                        ▶ Запустить
                    </button>
                </div>
            `;
        }
        div.innerHTML = `
            <div class="msg-meta">
                <div class="bot-avatar">
                    <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a1 1 0 0 1 1 1v2a1 1 0 0 1-1 1 5 5 0 0 0-5 5v1.5a2.5 2.5 0 0 1-2.5 2.5 1 1 0 0 0 0 2 2.5 2.5 0 0 1 2.5 2.5V19a5 5 0 0 0 5 5 1 1 0 0 1 1 1v2a1 1 0 0 1-1 1 8 8 0 0 1-8-8v-1.5a2.5 2.5 0 0 1-2.5-2.5 1 1 0 0 0 0-2 2.5 2.5 0 0 1 2.5-2.5V8a8 8 0 0 1 8-8z"/><path d="M16 9a1 1 0 0 1 1 1v2a1 1 0 0 1-1 1 3 3 0 0 0-3 3v1.5a1.5 1.5 0 0 1-1.5 1.5 1 1 0 0 0 0 2 1.5 1.5 0 0 1 1.5 1.5V19a3 3 0 0 0 3 3 1 1 0 0 1 1 1v2a1 1 0 0 1-1 1 5 5 0 0 1-5-5v-1.5a1.5 1.5 0 0 1-1.5-1.5 1 1 0 0 0 0-2 1.5 1.5 0 0 1 1.5-1.5V14a5 5 0 0 1 5-5z"/></svg>
                </div>
                <span class="bot-label">Assistant</span>
            </div>
            <div class="msg-content">${isHtml ? text : parseMarkdown(text)}</div>
            ${extra}
            <div class="msg-footer">
                <span class="msg-time">${time}</span>
                <div class="msg-actions">
                    <button class="msg-btn" title="Копировать" onclick="copyMessage(this)">${ICONS.copy}</button>
                    <button class="msg-btn" title="Озвучить" onclick="speakMsg(this)">${ICONS.volume}</button>
                </div>
            </div>`;
        chatbox.appendChild(div);
        chatbox.scrollTop = chatbox.scrollHeight;
        return;
    }

    div.textContent = text;
    chatbox.appendChild(div);
    chatbox.scrollTop = chatbox.scrollHeight;
}

async function runCode(filename, language) {
    let code;
    try {
        const resp = await fetch('/share_to_chat', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({filename})
        });
        const data = await resp.json();
        if (data.error) {
            appendMessage('error', data.error);
            return;
        }
        code = data.code;
    } catch(e) {
        appendMessage('error', 'Не удалось получить код файла');
        return;
    }
    
    const typingId = showTyping();
    try {
        const runResp = await fetch('/run_code', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({code: code, language: language || 'python'})
        });
        const runData = await runResp.json();
        hideTyping(typingId);
        if (runData.error) appendMessage('error', runData.error);
        else appendMessage('bot', '**Результат выполнения:**\n```\n' + (runData.result || 'Нет вывода') + '\n```');
    } catch(e) {
        hideTyping(typingId);
        appendMessage('error', 'Ошибка выполнения: ' + e.message);
    }
}

async function shareCodeToChat(filename, btn) {
    try {
        const resp = await fetch('/share_to_chat', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({filename})
        });
        const data = await resp.json();
        if (data.error) { appendMessage('error', data.error); return; }
        appendMessage('user', '📁 ' + filename + ' отправлен в чат');
        const typingId = showTyping();
        const explainResp = await fetch('/send', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({message: `Проанализируй этот код:\n\n\`\`\`\n${data.code}\n\`\`\``})
        });
        const explainData = await explainResp.json();
        hideTyping(typingId);
        if (explainData.error) appendMessage('error', explainData.error);
        else appendMessage('bot', explainData.reply);
        btn.innerHTML = ICONS.check + ' Отправлено';
        setTimeout(() => btn.innerHTML = ICONS.send + ' Отправить в чат', 2000);
    } catch(e) { appendMessage('error', 'Ошибка отправки'); }
}

function showTyping() {
    const id = 'typing-' + Date.now();
    const div = document.createElement('div'); div.id = id; div.className = 'typing-indicator';
    div.innerHTML = '<div class="typing-dot"></div><div class="typing-dot"></div><div class="typing-dot"></div><span class="typing-label">печатает</span>';
    chatbox.appendChild(div);
    chatbox.scrollTo({ top: chatbox.scrollHeight, behavior: 'smooth' });
    return id;
}
function hideTyping(id) { const el = document.getElementById(id); if (el) el.remove(); }

function copyMessage(btn) {
    const msgDiv = btn.closest('.message');
    const content = msgDiv.querySelector('.msg-content').textContent;
    navigator.clipboard.writeText(content.trim()).then(() => { btn.innerHTML = ICONS.check; setTimeout(() => btn.innerHTML = ICONS.copy, 1500); });
}
function speakMsg(btn) {
    const msgDiv = btn.closest('.message');
    const content = msgDiv.querySelector('.msg-content').textContent;
    speakText(content.trim());
}

async function sendMessage() {
    const text = userInput.value.trim();
    if (!text) return;
    let msgText = text;
    const wasInputMode = inputMode;
    const isCommand = text.startsWith('/');
    if (isCommand) appendMessage('user', text);
    else if (wasInputMode) { msgText = wasInputMode.prefix + text; appendMessage('user', msgText); }
    else appendMessage('user', text);
    userInput.value = ''; clearMode(); plusBtn.style.display = 'none';
    const typingId = showTyping();
    try {
        if (isCommand || wasInputMode) {
            const resp = await fetch('/command', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({command: msgText}) });
            const data = await resp.json();
            hideTyping(typingId);
            if (data.error) appendMessage('error', data.error);
            else {
                if (msgText.startsWith('/code') && data.filename && data.download_url) {
                    appendMessage('bot', data.result || data.reply, false, { type: 'code', filename: data.filename, download_url: data.download_url });
                } else {
                    appendMessage('system', data.result || data.reply);
                }
            }
        } else {
            const resp = await fetch('/send', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({message: text}) });
            const data = await resp.json();
            hideTyping(typingId);
            if (data.error) appendMessage('error', data.error);
            else { appendMessage('bot', data.reply); if (data.voice_enabled) speakText(data.reply); }
        }
    } catch(e) { hideTyping(typingId); appendMessage('error', 'Ошибка сети'); }
}

async function fetchAndShow(cmd) {
    const typingId = showTyping();
    try {
        const resp = await fetch('/command', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({command: cmd}) });
        const data = await resp.json();
        hideTyping(typingId);
        if (data.error) appendMessage('error', data.error);
        else appendMessage('system', data.result);
    } catch(e) { hideTyping(typingId); appendMessage('error', 'Ошибка'); }
}
async function sendCommand(cmd) { appendMessage('user', cmd); await fetchAndShow(cmd); }
function speakText(text) { if ('speechSynthesis' in window) { speechSynthesis.cancel(); const u = new SpeechSynthesisUtterance(text); u.lang = 'ru-RU'; u.rate = 1.05; speechSynthesis.speak(u); } }
userInput.addEventListener('keypress', e => { if (e.key === 'Enter') sendMessage(); });
document.addEventListener('click', e => { const wrapper = document.querySelector('.functions-wrapper'); if (!wrapper.contains(e.target) && dropdownPanel.classList.contains('visible')) toggleFunctions(); });
if (userInput.value.trim() === '' && !inputMode) plusBtn.style.display = 'flex'; else plusBtn.style.display = 'none';
loadModels();
