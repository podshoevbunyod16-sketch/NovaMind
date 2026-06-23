/* ══════════════════════════════════════════
   NovaMind — Основной скрипт чата
══════════════════════════════════════════ */

// ========== СОСТОЯНИЕ ==========
let isRecording    = false;
let recognition    = null;
let isTyping       = false;
let webSearchOn    = false;
let reasoningOn    = false;
let currentMode    = 'chat';
let msgCount       = 0;
let chatHistory    = JSON.parse(localStorage.getItem('nova_history') || '[]');
let selectedModelName = 'Nova Ultra';
let inputMode      = null;

// ========== DOM-ЭЛЕМЕНТЫ ==========
const input         = document.getElementById('chat-input');
const sendBtn       = document.getElementById('sendBtn');
const voiceBtn      = document.getElementById('voiceBtn');
const voiceTooltip  = document.getElementById('voiceTooltip');
const chatContainer = document.getElementById('chatContainer');
const welcomeScreen = document.getElementById('welcomeScreen');
const modelDropdown = document.getElementById('modelDropdown');

// ========== ПРОВЕРКА АВТОРИЗАЦИИ ==========
if (!localStorage.getItem('nova_user_nick')) {
  window.location.href = '/';
}

// ========== АДМИН-ПАНЕЛЬ И НИКНЕЙМ ==========
(function() {
  const isAdmin = localStorage.getItem('nova_is_admin') === 'true';
  const adminLink = document.getElementById('admin-link');
  const displayNick = document.getElementById('display-nick');
  if (displayNick) {
    displayNick.textContent = localStorage.getItem('nova_user_nick') || 'Пользователь';
  }
  if (isAdmin && adminLink) {
    adminLink.style.display = 'flex';
  }
  
  // Аватарка Google
  const googleAvatar = localStorage.getItem('nova_user_avatar');
  const avatarEl = document.querySelector('.user-avatar');
  if (googleAvatar && avatarEl) {
    avatarEl.innerHTML = `<img src="${googleAvatar}" style="width:100%;height:100%;border-radius:50%;object-fit:cover;">`;
    avatarEl.style.background = 'none';
  }
})();

// ========== ВЫХОД ==========
function logout() {
  localStorage.removeItem('nova_user_nick');
  localStorage.removeItem('nova_user_code');
  localStorage.removeItem('nova_is_admin');
  localStorage.removeItem('nova_google_login');
  localStorage.removeItem('nova_user_avatar');
  window.location.href = '/';
}

// ========== ВВОД ТЕКСТА ==========
input.addEventListener('input', () => {
  sendBtn.disabled = !input.value.trim();
});

function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 150) + 'px';
}

function handleKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    if (!sendBtn.disabled) sendMessage();
  }
}

// ========== ТРИ КНОПКИ ==========
function toggleWebSearch() {
  webSearchOn = !webSearchOn;
  const btn = document.getElementById('btn-web-search');
  btn.classList.toggle('active', webSearchOn);
  showNotification(webSearchOn ? 'Поиск в интернете включён' : 'Поиск выключен', 'info');
}

function toggleReasoning() {
  reasoningOn = !reasoningOn;
  const btn = document.getElementById('btn-reasoning');
  btn.classList.toggle('active', reasoningOn);
  showNotification(reasoningOn ? 'Режим рассуждения включён' : 'Рассуждение выключено', 'info');
}

function toggleAttachMenu() {
  document.getElementById('attachDropdown').classList.toggle('open');
}


function attachImage() {
  document.getElementById('attachDropdown').classList.remove('open');
  const el = document.createElement('input');
  el.type = 'file';
  el.accept = 'image/*';
  el.onchange = (e) => {
    const file = e.target.files[0];
    if (!file) return;
    
    // Показываем сообщение в чате
    appendMessage('user', '📷 Анализ изображения: ' + file.name);
    
    // Загружаем на сервер
    const formData = new FormData();
    formData.append('image', file);
    
    showTyping();
    fetch('/upload_image', {
      method: 'POST',
      body: formData
    })
    .then(r => r.json())
    .then(data => {
      removeTyping();
      if (data.error) {
        appendMessage('ai', 'Ошибка: ' + data.error);
      } else if (data.result) {
        appendMessage('ai', data.result);
      } else {
        // Если сервер не анализирует — просто показываем путь
        appendMessage('ai', '✅ Изображение загружено: ' + (data.filepath || data.filename || file.name));
      }
    })
    .catch(() => {
      removeTyping();
      appendMessage('ai', 'Ошибка загрузки изображения');
    });
  };
  el.click();
}

function attachDocument() {
  document.getElementById('attachDropdown').classList.remove('open');
  const el = document.createElement('input');
  el.type = 'file';
  el.accept = '.txt,.pdf,.doc,.docx,.png,.jpg,.jpeg,.json,.csv,.py,.js,.html,.css';
  el.onchange = (e) => {
    const file = e.target.files[0];
    if (!file) return;
    
    appendMessage('user', '📁 Файл: ' + file.name);
    
    const formData = new FormData();
    formData.append('file', file);
    
    showTyping();
    fetch('/upload_file', {
      method: 'POST',
      body: formData
    })
    .then(r => r.json())
    .then(data => {
      removeTyping();
      if (data.error) {
        appendMessage('ai', 'Ошибка: ' + data.error);
      } else {
        appendMessage('ai', '✅ Файл загружен: ' + (data.filepath || data.filename || file.name) + '\n\n' + (data.preview || ''));
      }
    })
    .catch(() => {
      removeTyping();
      appendMessage('ai', 'Ошибка загрузки файла');
    });
  };
  el.click();
}



// ========== РЕЖИМЫ КОМАНД ==========
function activateMode(mode) {
  inputMode = mode;
  input.placeholder = mode.placeholder;
  input.value = '';
  input.focus();
}

// ========== ОТПРАВКА СООБЩЕНИЙ ==========
async function sendMessage(text) {
  const msg = (text || input.value).trim();
  if (!msg || isTyping) return;

  let finalMsg = msg;

  // Если активен режим — формируем команду
  if (inputMode && !msg.startsWith('/')) {
    finalMsg = inputMode.prefix + msg;
    inputMode = null;
    input.placeholder = 'Напишите сообщение или нажмите 🎤 для голосового ввода...';
  }

  hideWelcome();
  appendMessage('user', finalMsg);

  input.value = '';
  input.style.height = 'auto';
  sendBtn.disabled = true;
  addToHistory(finalMsg);
  showTyping();

  const isCommand = finalMsg.startsWith('/');

  // Если команда — отправляем на /command
  if (isCommand) {
    try {
      const resp = await fetch('/command', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ command: finalMsg })
      });
      const data = await resp.json();
      removeTyping();
      if (data.error) {
        appendMessage('ai', 'Ошибка: ' + data.error);
      } else {
        appendMessage('ai', data.result || data.reply || 'Готово');
      }
    } catch (e) {
      removeTyping();
      appendMessage('ai', 'Ошибка соединения');
    }
    return;
  }

  // Если включён поиск — используем DuckDuckGo
  if (webSearchOn) {
    try {
      const resp = await fetch('/command', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ command: '/search ' + finalMsg })
      });
      const data = await resp.json();
      removeTyping();
      if (data.error) {
        appendMessage('ai', 'Ошибка: ' + data.error);
      } else {
        appendMessage('ai', '🔍 **Результаты поиска:**\n\n' + (data.result || 'Ничего не найдено'));
      }
    } catch (e) {
      removeTyping();
      appendMessage('ai', 'Ошибка поиска');
    }
    return;
  }

  // Обычный запрос к ИИ
  // Обычный запрос к ИИ
try {
    const resp = await fetch('/send', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ 
        message: finalMsg, 
        reasoning: reasoningOn  // ← передаём флаг рассуждения
      })
    });
    const data = await resp.json();
    removeTyping();
    if (data.error) {
      appendMessage('ai', 'Ошибка: ' + data.error);
    } else {
      appendMessage('ai', data.reply);
    }
} catch (e) {
    removeTyping();
    appendMessage('ai', 'Ошибка соединения');
}
}

function sendSuggestion(text) { sendMessage(text); }
function hideWelcome() { if (welcomeScreen) welcomeScreen.style.display = 'none'; }

// ========== СООБЩЕНИЯ (С ПОДДЕРЖКОЙ ИЗОБРАЖЕНИЙ) ==========
function appendMessage(role, content) {
  msgCount++;
  const isAI = role === 'ai';
  const wrap = document.createElement('div');
  wrap.className = 'message ' + (role === 'user' ? 'user' : 'ai');

  // ══ COMPOSIO КАРТОЧКИ ══
  if (isAI && content.startsWith('COMPOSIO_CARDS:')) {
    const json = content.replace('COMPOSIO_CARDS:', '');
    try {
      const cards = JSON.parse(json);
      wrap.innerHTML = `
        <div class="msg-avatar">✦</div>
        <div class="msg-body">
          <div class="msg-name">NovaMind</div>
          <div class="msg-bubble">${renderComposioCards(cards)}</div>
        </div>`;
      chatContainer.appendChild(wrap);
      scrollToBottom();
      return;
    } catch(e) {}
  }

  // ══ COMPOSIO AUTH КНОПКА ══
  if (isAI && content.startsWith('COMPOSIO_AUTH:')) {
    const withoutPrefix = content.replace('COMPOSIO_AUTH:', '');
    const colonIdx = withoutPrefix.indexOf(':');
    const toolkit = withoutPrefix.substring(0, colonIdx);
    const url = withoutPrefix.substring(colonIdx + 1);
    wrap.innerHTML = `
      <div class="msg-avatar">✦</div>
      <div class="msg-body">
        <div class="msg-name">NovaMind</div>
        <div class="msg-bubble">${renderComposioAuth(toolkit, url)}</div>
      </div>`;
    chatContainer.appendChild(wrap);
    scrollToBottom();
    return;
  }

  // ══ ОБЫЧНОЕ СООБЩЕНИЕ ══
  let formatted = isAI ? formatContent(content) : escapeHtml(content);
  let imageHtml = '';

  const imageMatch = content.match(/!\[Image\]\((.*?)\)/);
  if (imageMatch) {
    imageHtml = `<img src="${imageMatch[1]}" alt="Generated image" style="max-width:100%;border-radius:12px;margin-top:8px;" onload="scrollToBottom()">`;
    formatted = formatted.replace(/!\[Image\]\(.*?\)/, '');
  }

  wrap.innerHTML = `
    <div class="msg-avatar">${isAI ? '✦' : '👤'}</div>
    <div class="msg-body">
      <div class="msg-name">${isAI ? 'NovaMind' : 'Вы'}</div>
      <div class="msg-bubble">${formatted}${imageHtml}</div>
    </div>`;
  chatContainer.appendChild(wrap);
  scrollToBottom();
}

function formatContent(text) {
  let html = escapeHtml(text);
  
  // Блоки кода
  html = html.replace(/```(\w+)?\n?([\s\S]*?)```/g, (_, lang, code) => `<pre><code>${code.trim()}</code></pre>`);
  
  // Инлайн-код
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
  
  // Жирный
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  
  // Курсив
  html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');
  
  // Таблицы
  html = html.replace(/(\|[^\n]+\|\n\|[-| :]+\|\n(?:\|[^\n]+\|\n?)*)/g, (match) => {
    const rows = match.trim().split('\n');
    let tableHtml = '<table style="width:100%;border-collapse:collapse;margin:10px 0;">';
    
    rows.forEach((row, index) => {
      const cells = row.split('|').filter(c => c.trim() !== '');
      const tag = index === 0 ? 'th' : 'td';
      
      if (index === 1 && cells.every(c => /^[-| :]+$/.test(c))) return; // пропускаем разделитель
      
      tableHtml += '<tr>';
      cells.forEach(cell => {
        tableHtml += `<${tag} style="border:1px solid rgba(255,255,255,0.15);padding:8px 12px;text-align:left;">${cell.trim()}</${tag}>`;
      });
      tableHtml += '</tr>';
    });
    
    tableHtml += '</table>';
return `<div style="overflow-x: auto; max-width: 100%; -webkit-overflow-scrolling: touch;">${tableHtml}</div>`;
  });
  
  // Переносы строк
  html = html.replace(/\n\n/g, '<br><br>');
  html = html.replace(/\n/g, '<br>');
  
  return html;
}

function escapeHtml(text) {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ========== ИНДИКАТОР ПЕЧАТИ ==========
function showTyping() {
  isTyping = true;
  const wrap = document.createElement('div');
  wrap.className = 'message ai typing-indicator';
  wrap.id = 'typingIndicator';
  wrap.innerHTML = '<div class="msg-avatar">✦</div><div class="msg-body"><div class="msg-name">NovaMind</div><div class="msg-bubble"><div class="typing-dots"><span></span><span></span><span></span></div></div></div>';
  chatContainer.appendChild(wrap);
  scrollToBottom();
}

function removeTyping() {
  const el = document.getElementById('typingIndicator');
  if (el) el.remove();
  isTyping = false;
}

// ========== ГОЛОС ==========
function toggleVoice() {
  if (!('webkitSpeechRecognition' in window) && !('SpeechRecognition' in window)) {
    showNotification('Браузер не поддерживает голосовой ввод', 'warn');
    return;
  }
  isRecording ? stopRecording() : startRecording();
}

function startRecording() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  recognition = new SR();
  recognition.lang = 'ru-RU';
  recognition.continuous = false;
  recognition.interimResults = true;
  recognition.onstart = () => {
    isRecording = true;
    voiceBtn.classList.add('recording');
    voiceTooltip.textContent = '● Запись...';
  };
  recognition.onresult = (e) => {
    input.value = Array.from(e.results).map(r => r[0].transcript).join('');
    sendBtn.disabled = !input.value.trim();
  };
  recognition.onend = () => stopRecording();
  recognition.onerror = () => stopRecording();
  recognition.start();
}

function stopRecording() {
  isRecording = false;
  voiceBtn.classList.remove('recording');
  voiceTooltip.textContent = 'Нажмите для записи';
  if (recognition) { recognition.stop(); recognition = null; }
}

// ========== САЙДБАР ==========
function toggleSidebar() {
  document.getElementById('sidebar').classList.toggle('open');
  document.getElementById('overlay').classList.toggle('visible');
}
function closeSidebar() {
  document.getElementById('sidebar').classList.remove('open');
  document.getElementById('overlay').classList.remove('visible');
}
function setActive(el) {
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  el.classList.add('active');
}

// ========== МОДЕЛИ ==========
function toggleModelDropdown() { modelDropdown.classList.toggle('open'); }
function selectModel(name, el) {
  document.querySelectorAll('.model-option').forEach(o => o.classList.remove('selected'));
  el.classList.add('selected');
  document.getElementById('currentModel').textContent = name;
  modelDropdown.classList.remove('open');
}
document.addEventListener('click', (e) => {
  if (!e.target.closest('#modelDropdown') && !e.target.closest('#modelSelectorBtn')) modelDropdown.classList.remove('open');
});

// ========== ЧАТ ==========
function newChat() {
  chatContainer.innerHTML = '';
  chatContainer.appendChild(welcomeScreen);
  welcomeScreen.style.display = 'flex';
  msgCount = 0;
}
function clearChat() { newChat(); }
function shareChat() {
  navigator.clipboard.writeText(window.location.href).then(() => showNotification('Ссылка скопирована', 'success'));
}
function scrollToBottom() {
  setTimeout(() => chatContainer.scrollTo({ top: chatContainer.scrollHeight, behavior: 'smooth' }), 50);
}
function addToHistory(text) {
  if (chatHistory.includes(text)) return;
  chatHistory.unshift(text);
  if (chatHistory.length > 8) chatHistory.pop();
  localStorage.setItem('nova_history', JSON.stringify(chatHistory));
}

// ========== УВЕДОМЛЕНИЯ ==========
function showNotification(msg, type = 'info') {
  const colors = {
    success: { bg: 'rgba(34,197,94,.15)', text: '#4ade80' },
    warn: { bg: 'rgba(234,179,8,.15)', text: '#facc15' },
    info: { bg: 'rgba(124,58,237,.15)', text: '#c4b5fd' },
  };
  const c = colors[type] || colors.info;
  const toast = document.createElement('div');
  toast.style.cssText = `position:fixed;bottom:80px;left:50%;transform:translateX(-50%);background:${c.bg};color:${c.text};padding:8px 18px;border-radius:99px;font-size:12px;font-weight:600;z-index:9999;`;
  toast.textContent = msg;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 2500);
}


// ══════════════════════════════════════════
// COMPOSIO — рендер карточек и авторизации
// ══════════════════════════════════════════
const COMPOSIO_ICONS = {
  github:'🐙', gmail:'📧', notion:'📝', slack:'💬',
  googlecalendar:'📅', googledrive:'☁️', trello:'📋',
  twitter:'🐦', discord:'🎮', jira:'🔵', linear:'⚡',
  youtube:'▶️', shopify:'🛒', hubspot:'🟠', airtable:'🗃️',
  dropbox:'📦', figma:'🎨', stripe:'💳', zoom:'📹', asana:'🎯'
};

function renderComposioCards(cards) {
  let grid = '';
  cards.forEach(card => {
    const icon = COMPOSIO_ICONS[card.slug] || '🔗';
    const border = card.connected ? '#10b981' : '#3730a3';
    const statusColor = card.connected ? '#10b981' : '#6b7280';
    const statusText = card.connected ? '✅ Подключено' : 'Нажми — подключить';
    const dot = card.connected
      ? '<div style="position:absolute;top:5px;right:5px;width:7px;height:7px;background:#10b981;border-radius:50%;"></div>'
      : '';

    grid += `
      <div onclick="composioAuthFromChat('${card.slug}')"
        style="position:relative;background:#0f0f1a;border:1px solid ${border};
        border-radius:10px;padding:12px 8px;text-align:center;cursor:pointer;
        transition:transform .2s;"
        onmouseover="this.style.transform='translateY(-2px)'"
        onmouseout="this.style.transform='translateY(0)'">
        ${dot}
        <div style="font-size:22px;margin-bottom:5px;">${icon}</div>
        <div style="font-size:11px;font-weight:600;color:#e2e8f0;">${card.name}</div>
        <div style="font-size:10px;margin-top:3px;color:${statusColor};">${statusText}</div>
      </div>`;
  });

  return `
    <div style="font-weight:600;color:#a5b4fc;margin-bottom:10px;">
      🧩 Интеграции Composio — нажми для подключения:
    </div>
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:8px;">
      ${grid}
    </div>
    <div style="margin-top:10px;font-size:11px;color:#6b7280;">
      💡 После подключения используй <code>/composio accounts</code> для проверки
    </div>`;
}

function renderComposioAuth(toolkit, url) {
  const icon = COMPOSIO_ICONS[toolkit] || '🔗';
  return `
    <div style="background:#1a1a2e;border:1px solid #3730a3;border-radius:12px;padding:16px;">
      <div style="font-size:28px;margin-bottom:8px;">${icon}</div>
      <div style="font-size:14px;font-weight:700;color:#a5b4fc;margin-bottom:6px;">
        Подключить ${toolkit.toUpperCase()}
      </div>
      <div style="font-size:12px;color:#9ca3af;margin-bottom:14px;">
        Нажми кнопку ниже — откроется страница авторизации.<br>
        После входа вернись и введи <code>/composio accounts</code>
      </div>
      <a href="${url}" target="_blank"
        style="display:inline-block;background:linear-gradient(135deg,#4f46e5,#7c3aed);
        color:#fff;padding:10px 20px;border-radius:9px;font-size:13px;
        font-weight:600;text-decoration:none;">
        🔐 Войти в ${toolkit.toUpperCase()} →
      </a>
      <div style="margin-top:10px;font-size:11px;color:#6b7280;">
        После: <code>/composio tools ${toolkit}</code> или
        <code>/composio do покажи данные из ${toolkit}</code>
      </div>
    </div>`;
}

function composioAuthFromChat(toolkit) {
  hideWelcome();
  appendMessage('user', `/composio auth ${toolkit}`);
  showTyping();
  fetch('/command', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({command: `/composio auth ${toolkit}`})
  })
  .then(r => r.json())
  .then(data => {
    removeTyping();
    appendMessage('ai', data.result || data.error || 'Ошибка');
  })
  .catch(() => {
    removeTyping();
    appendMessage('ai', '❌ Ошибка соединения');
  });
}
// ========== ИНИЦИАЛИЗАЦИЯ ==========
input.focus();
