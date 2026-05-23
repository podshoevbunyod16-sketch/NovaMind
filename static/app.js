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
})();

// ========== ВЫХОД ==========
function logout() {
  localStorage.removeItem('nova_user_nick');
  localStorage.removeItem('nova_user_code');
  localStorage.removeItem('nova_is_admin');
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
    if (e.target.files[0]) showNotification('Изображение прикреплено', 'success');
  };
  el.click();
}

function attachDocument() {
  document.getElementById('attachDropdown').classList.remove('open');
  const el = document.createElement('input');
  el.type = 'file';
  el.onchange = (e) => {
    if (e.target.files[0]) showNotification('Файл прикреплён', 'success');
  };
  el.click();
}

document.addEventListener('click', (e) => {
  const dd = document.getElementById('attachDropdown');
  const btn = document.getElementById('btn-attach');
  if (dd && !dd.contains(e.target) && e.target !== btn && !btn.contains(e.target)) {
    dd.classList.remove('open');
  }
});

// ========== ОТПРАВКА СООБЩЕНИЙ ==========
async function sendMessage(text) {
  const msg = (text || input.value).trim();
  if (!msg || isTyping) return;

  hideWelcome();
  appendMessage('user', msg);

  input.value = '';
  input.style.height = 'auto';
  sendBtn.disabled = true;
  addToHistory(msg);
  showTyping();

  if (webSearchOn) {
    try {
      const resp = await fetch('/command', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ command: '/search ' + msg })
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

  try {
    const resp = await fetch('/send', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: msg, reasoning: reasoningOn })
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

// ========== СООБЩЕНИЯ ==========
function appendMessage(role, content) {
  msgCount++;
  const isAI = role === 'ai';
  const wrap = document.createElement('div');
  wrap.className = 'message ' + (role === 'user' ? 'user' : 'ai');
  const formatted = isAI ? formatContent(content) : escapeHtml(content);
  wrap.innerHTML = `
    <div class="msg-avatar">${isAI ? '✦' : '👤'}</div>
    <div class="msg-body">
      <div class="msg-name">${isAI ? 'NovaMind' : 'Вы'}</div>
      <div class="msg-bubble">${formatted}</div>
    </div>`;
  chatContainer.appendChild(wrap);
  scrollToBottom();
}

function formatContent(text) {
  let html = escapeHtml(text);
  html = html.replace(/```(\w+)?\n?([\s\S]*?)```/g, (_, lang, code) => `<pre><code>${code.trim()}</code></pre>`);
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/\n\n/g, '<br><br>');
  html = html.replace(/\n/g, '<br>');
  return html;
}

function escapeHtml(text) {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
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
// ========== РЕЖИМЫ КОМАНД ==========
let inputMode = null;

function activateMode(mode) {
  inputMode = mode;
  input.placeholder = mode.placeholder;
  input.value = '';
  plusBtn.style.display = 'none';
  if (typeof plusBtn !== 'undefined') plusBtn.style.display = 'none';
  input.focus();
  
  // Подсветка кнопки функций (если есть)
  if (typeof functionsToggle !== 'undefined') {
    functionsToggle.classList.add('active');
  }
}

function clearMode() {
  inputMode = null;
  input.placeholder = 'Напишите сообщение или нажмите 🎤 для голосового ввода...';
  if (typeof functionsToggle !== 'undefined') {
    functionsToggle.classList.remove('active');
  }
}

// Обновлённая sendMessage с поддержкой inputMode
async function sendMessage(text) {
  const msg = (text || input.value).trim();
  if (!msg || isTyping) return;

  // Если активен режим — формируем команду
  let finalMsg = msg;
  let isCommand = msg.startsWith('/');
  
  if (inputMode && !isCommand) {
    finalMsg = inputMode.prefix + msg;
    isCommand = true;
  }

  hideWelcome();
  appendMessage('user', finalMsg);

  input.value = '';
  input.style.height = 'auto';
  sendBtn.disabled = true;
  clearMode();
  addToHistory(finalMsg);
  showTyping();

  // Если это команда — отправляем на /command
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
  try {
    const resp = await fetch('/send', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: finalMsg, reasoning: reasoningOn })
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
// ========== ИНИЦИАЛИЗАЦИЯ ==========
input.focus();
