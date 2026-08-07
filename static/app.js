/* ══════════════════════════════════════════
   NovaMind — Основной скрипт чата
══════════════════════════════════════════ */

// ========== СОСТОЯНИЕ ==========
let isRecording    = false;
let recognition    = null;
let isTyping       = false;
let webSearchOn    = false;
let reasoningOn    = false;
let autoSearchOn   = false;  // ← АВТО ПОИСК
let currentMode    = 'chat';
let msgCount       = 0;
let chatHistory    = JSON.parse(localStorage.getItem('nova_history') || '[]');
let serverHistory  = []; // Полная история сообщений с сервера (для авторизованных пользователей)
let selectedModelName = 'Nova Ultra';
let inputMode      = null;
// ══════════════════════════════════════════
// ЗАЩИТА ОТ КОПИРОВАНИЯ ИНТЕРФЕЙСА
// ══════════════════════════════════════════

(function initCopyProtection() {
  // Правый клик — только в сообщениях
  document.addEventListener('contextmenu', function(e) {
    const isInsideMessage = e.target.closest('.msg-bubble') !== null;
    if (!isInsideMessage) {
      e.preventDefault();
      return false;
    }
  });

  // Ctrl+C / Cmd+C — только в сообщениях
  document.addEventListener('keydown', function(e) {
    const selection = window.getSelection();
    const isInsideMessage = selection?.anchorNode?.parentElement?.closest('.msg-bubble') !== null ||
                            document.activeElement?.closest('.msg-bubble') !== null;
    
    if ((e.ctrlKey || e.metaKey) && e.key === 'c') {
      if (!isInsideMessage) {
        e.preventDefault();
        showNotification('Копирование интерфейса запрещено. Выделите текст в сообщении.', 'warn');
        return false;
      }
    }

    // Ctrl+A — выделить всё (только в сообщениях)
    if ((e.ctrlKey || e.metaKey) && e.key === 'a') {
      if (!isInsideMessage) {
        e.preventDefault();
        return false;
      }
    }
  });

  // Drag & drop — только из сообщений
  document.addEventListener('dragstart', function(e) {
    if (!e.target.closest('.msg-bubble')) {
      e.preventDefault();
      return false;
    }
  });
})();

// ========== ЗАГРУЗКА ИСТОРИИ С СЕРВЕРА ==========
async function loadServerHistory() {
  try {
    const resp = await fetch('/api/history');
    const data = await resp.json();
    if (data.history && Array.isArray(data.history)) {
      serverHistory = data.history;
      // Отрисовываем историю в чате
      const chatArea = document.getElementById('chatArea');
      chatArea.innerHTML = '';
      for (const msg of serverHistory) {
        if (msg.role === 'user') {
          appendMessage('user', msg.content);
        } else if (msg.role === 'assistant') {
          appendMessage('ai', msg.content);
        }
      }
      scrollToBottom();
      // Обновляем историю в сайдбаре
      updateSidebarHistory(serverHistory);
    }
  } catch (e) {
    console.log('Server history load error:', e);
  }
}


// ========== ОБНОВЛЕНИЕ ИСТОРИИ В САЙДБАРЕ ==========
function updateSidebarHistory(history) {
  console.log('[Sidebar] Updating with', history ? history.length : 0, 'messages');
  const container = document.getElementById('historyContainer');
  console.log('[Sidebar] Container found:', !!container);

  if (!container) {
    console.error('[Sidebar] historyContainer not found!');
    return;
  }

  container.innerHTML = '';

  // Добавляем заголовок
  const header = document.createElement('div');
  header.className = 'history-section-header';
  header.textContent = 'История чатов';
  container.appendChild(header);

  if (!history || history.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'history-empty';
    empty.textContent = 'Нет истории';
    container.appendChild(empty);
    return;
  }

  // Группируем сообщения по диалогам
  let dialogs = [];
  let currentDialog = null;

  for (let i = 0; i < history.length; i++) {
    const msg = history[i];
    if (msg.role === 'user') {
      if (currentDialog) dialogs.push(currentDialog);
      currentDialog = {
        title: msg.content.substring(0, 30) + (msg.content.length > 30 ? '...' : ''),
        messages: [msg]
      };
    } else if (currentDialog) {
      currentDialog.messages.push(msg);
    }
  }
  if (currentDialog) dialogs.push(currentDialog);

  // Последние 10 диалогов
  const recentDialogs = dialogs.slice(-10).reverse();

  for (const dialog of recentDialogs) {
    const item = document.createElement('div');
    item.className = 'history-item';

    const textDiv = document.createElement('div');
    textDiv.className = 'history-item-text';
    textDiv.textContent = dialog.title;

    const actionsDiv = document.createElement('div');
    actionsDiv.className = 'history-item-actions';

    const loadBtn = document.createElement('button');
    loadBtn.textContent = '↩';
    loadBtn.title = 'Загрузить';
    loadBtn.onclick = (e) => {
      e.stopPropagation();
      loadDialogFromHistory(dialog.messages);
    };

    const delBtn = document.createElement('button');
    delBtn.textContent = '×';
    delBtn.title = 'Удалить';
    delBtn.onclick = (e) => {
      e.stopPropagation();
      deleteDialog(dialog.title);
    };

    actionsDiv.appendChild(loadBtn);
    actionsDiv.appendChild(delBtn);

    item.appendChild(textDiv);
    item.appendChild(actionsDiv);

    item.onclick = () => loadDialogFromHistory(dialog.messages);
    container.appendChild(item);
  }

  console.log('[Sidebar] Added', recentDialogs.length, 'dialogs');
}

function loadDialogFromHistory(messages) {
  const chatArea = document.getElementById('chatArea');
  chatArea.innerHTML = '';
  for (const msg of messages) {
    appendMessage(msg.role === 'user' ? 'user' : 'ai', msg.content);
  }
  scrollToBottom();
  closeSidebar();
}

function deleteDialog(title) {
  // Удаляем диалог из истории (по заголовку)
  if (!serverHistory || serverHistory.length === 0) return;

  // Находим индекс первого сообщения с этим заголовком
  let startIdx = -1;
  for (let i = 0; i < serverHistory.length; i++) {
    if (serverHistory[i].role === 'user' && 
        serverHistory[i].content.substring(0, 35) === title.substring(0, 35)) {
      startIdx = i;
      break;
    }
  }

  if (startIdx === -1) return;

  // Находим конец диалога (следующее сообщение user или конец массива)
  let endIdx = serverHistory.length;
  for (let i = startIdx + 1; i < serverHistory.length; i++) {
    if (serverHistory[i].role === 'user') {
      endIdx = i;
      break;
    }
  }

  // Удаляем диалог
  serverHistory.splice(startIdx, endIdx - startIdx);
  setServerHistory(serverHistory);
  updateSidebarHistory(serverHistory);
}

function setServerHistory(history) {
  serverHistory = history;
  // Сохраняем на сервере
  fetch('/api/history/save', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({history: history})
  }).catch(() => {});
}

// Загружаем историю при старте
setTimeout(() => {
  loadServerHistory();
}, 500);

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
  serverHistory = [];
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

// ========== ЧЕТЫРЕ КНОПКИ (добавлен авто поиск) ==========
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

// ========== АВТО ПОИСК ==========
function toggleAutoSearch() {
  autoSearchOn = !autoSearchOn;
  const btn = document.getElementById('btn-auto-search');
  btn.classList.toggle('active', autoSearchOn);

  if (autoSearchOn) {
    showNotification('🔍 Авто поиск включён. AI будет искать актуальную информацию при необходимости.', 'info');
  } else {
    showNotification('🔍 Авто поиск выключен', 'info');
  }
}

// ========== ПРИКРЕПИТЬ ==========
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

    // Показываем диалог с описанием
    const userDesc = prompt(
      '📷 Что сделать с этим изображением?\n\nОставь пустым — AI сам опишет что видит',
      ''
    );

    // Если нажал Отмена
    if (userDesc === null) return;

    const desc = userDesc.trim() || 'Подробно опиши что изображено на картинке. Опиши объекты, цвета, текст если есть, настроение и детали.';

    appendMessage('user', '📷 ' + file.name + (userDesc ? '\n💬 ' + userDesc : ''));

    const formData = new FormData();
    formData.append('image', file);
    formData.append('description', desc);

    showTyping();
    fetch('/upload_image', {
      method: 'POST',
      body: formData
    })
    .then(r => r.json())
    .then(data => {
      removeTyping();
      if (data.error) {
        appendMessage('ai', '❌ Ошибка: ' + data.error);
      } else if (data.result) {
        appendMessage('ai', data.result);
      } else {
        appendMessage('ai', '⚠️ Изображение сохранено, но анализ не вернул результат.');
      }
      saveServerHistory(); // Сохраняем историю на сервере
    })
    .catch(() => {
      removeTyping();
      appendMessage('ai', '❌ Ошибка загрузки изображения');
    });
  };
  el.click();
}

function attachDocument() {
  document.getElementById('attachDropdown').classList.remove('open');
  const el = document.createElement('input');
  el.type = 'file';
  el.accept = '.txt,.json,.csv,.py,.js,.html,.css,.md,.xml,.yaml,.yml,.log,.ini,.cfg';
  el.onchange = (e) => {
    const file = e.target.files[0];
    if (!file) return;

    // Показываем диалог с описанием
    const userDesc = prompt(
      '📁 Что сделать с файлом "' + file.name + '"?\n\nПример: найди ошибки, объясни код, сделай резюме\nОставь пустым — AI сам решит что делать',
      ''
    );

    // Если нажал Отмена
    if (userDesc === null) return;

    const desc = userDesc.trim() || '';

    appendMessage('user', '📁 ' + file.name + (userDesc ? '\n💬 ' + userDesc : ''));

    const formData = new FormData();
    formData.append('file', file);
    formData.append('description', desc);

    showTyping();
    fetch('/upload_file', {
      method: 'POST',
      body: formData
    })
    .then(r => r.json())
    .then(data => {
      removeTyping();
      if (data.error) {
        appendMessage('ai', '❌ Ошибка: ' + data.error);
      } else if (data.result) {
        appendMessage('ai', data.result);
      } else {
        appendMessage('ai', '⚠️ Файл сохранён, но анализ не вернул результат.');
      }
      saveServerHistory(); // Сохраняем историю на сервере
    })
    .catch(() => {
      removeTyping();
      appendMessage('ai', '❌ Ошибка загрузки файла');
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
        saveServerHistory(); // Сохраняем историю на сервере
      }
    } catch (e) {
      removeTyping();
      appendMessage('ai', 'Ошибка соединения');
    }
    return;
  }

  // Если включён АВТО ПОИСК — сначала проверяем нужен ли поиск
  if (autoSearchOn) {
    try {
      const resp = await fetch('/api/auto_search', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: finalMsg })
      });
      const data = await resp.json();

      if (data.error) {
        removeTyping();
        appendMessage('ai', '❌ Ошибка: ' + data.error);
        return;
      }

      // Если поиск нужен — показываем индикатор поиска и результат
      if (data.needs_search) {
        // Обновляем индикатор
        const typingEl = document.getElementById('typingIndicator');
        if (typingEl) {
          typingEl.querySelector('.msg-bubble').innerHTML = 
            `<div style="display:flex;align-items:center;gap:8px;font-size:13px;color:#10b981;">
              <span style="animation:spin 1s linear infinite;display:inline-block;">🔍</span>
              Ищу в интернете: "${escapeHtml(data.search_query || finalMsg)}"...
             </div>`;
        }

        // Ждём немного для эффекта
        await new Promise(r => setTimeout(r, 800));
        removeTyping();
        appendMessage('ai', data.reply);
        saveServerHistory(); // Сохраняем историю на сервере
        return;
      }
      // Если поиск НЕ нужен — продолжаем обычную отправку (ниже)
    } catch (e) {
      console.error('Auto search error:', e);
      // При ошибке авто поиска — продолжаем обычную отправку
    }
  }

  // Если включён ручной поиск — используем web_search_groq
  if (webSearchOn) {
    try {
      const resp = await fetch('/api/web_search_groq', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query: finalMsg })
      });
      const data = await resp.json();
      removeTyping();
      if (data.error) {
        appendMessage('ai', '❌ Ошибка: ' + data.error);
      } else {
        appendMessage('ai', data.reply);
      }
    } catch (e) {
      removeTyping();
      appendMessage('ai', '❌ Ошибка соединения при поиске');
    }
    return;
  }

  // Обычный запрос к ИИ
  try {
    const resp = await fetch('/send', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ 
        message: finalMsg, 
        reasoning: reasoningOn
      })
    });
    const data = await resp.json();
    removeTyping();
    if (data.error) {
      appendMessage('ai', 'Ошибка: ' + data.error);
    } else {
      appendMessage('ai', data.reply);
      saveServerHistory(); // Сохраняем историю на сервере
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

      if (index === 1 && cells.every(c => /^[-| :]+$/.test(c))) return;

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
function toggleModelDropdown() {
  modelDropdown.classList.toggle('open');
  if (modelDropdown.classList.contains('open')) loadModelsFromServer();
}

async function loadModelsFromServer() {
  try {
    const resp = await fetch('/models_list');
    const data = await resp.json();
    renderModelDropdown(data.models, data.current);
  } catch(e) { console.error('loadModels:', e); }
}

function renderModelDropdown(providersList, currentModelId) {
  const dropdown = document.getElementById('modelDropdown');
  dropdown.innerHTML = '';
  const PC = {
    groq:        {color:'#f97316', label:'⚡ Groq'},
    cerebras:    {color:'#8b5cf6', label:'🧠 Cerebras'},
    openrouter:  {color:'#06b6d4', label:'🌐 OpenRouter'},
    ollama:      {color:'#f59e0b', label:'💻 Ollama (локальный)'},
    llama_local: {color:'#10b981', label:'💻 Llama.cpp'},
  };
  providersList.forEach(({provider: key, list: models=[]}) => {
    if (!models.length) return;
    const {color, label} = PC[key] || {color:'#6c63ff', label: key};
    const hdr = document.createElement('div');
    hdr.style.cssText = `padding:6px 14px 3px;font-size:10px;font-weight:700;
      text-transform:uppercase;letter-spacing:1px;color:${color};margin-top:6px;`;
    hdr.textContent = label;
    dropdown.appendChild(hdr);
    models.forEach(m => {
      const opt = document.createElement('div');
      opt.className = 'model-option'+(m.id===currentModelId?' selected':'');
      opt.innerHTML = `
        <div class="model-option-dot" style="background:${color};box-shadow:0 0 5px ${color}80"></div>
        <div><div class="model-option-name">${m.name}</div>
             <div class="model-option-desc">${key}</div></div>
        ${m.id===currentModelId?`<span style="color:${color}">✓</span>`:''}`;
      opt.onclick = () => selectModel(m.id, m.name, key, opt);
      dropdown.appendChild(opt);
    });
  });
  const w = document.createElement('div');
  w.style = 'padding:8px 14px;';
  w.innerHTML = `<button onclick="loadOllamaModels()" style="
    width:100%;background:rgba(245,158,11,.12);border:1px solid #f59e0b;
    border-radius:8px;padding:8px;color:#f59e0b;font-size:12px;
    cursor:pointer;font-weight:600;">🔄 Загрузить Ollama модели</button>`;
  dropdown.appendChild(w);
}

async function loadOllamaModels() {
  try {
    const r = await fetch('/api/ollama/models');
    const d = await r.json();
    if (d.success && d.models.length) {
      showNotification('✅ Загружено '+d.models.length+' Ollama моделей','success');
      loadModelsFromServer();
    } else {
      showNotification('⚠️ Ollama: '+(d.error||'нет моделей'),'warning');
    }
  } catch(e) { showNotification('❌ Ollama недоступна','error'); }
}

async function selectModel(modelId, modelName, providerKey, el) {
  try {
    const r = await fetch('/switch_model?model_id='+encodeURIComponent(modelId)+'&provider_id='+encodeURIComponent(providerKey));
    const d = await r.json();
    if (d.success) {
      document.querySelectorAll('.model-option').forEach(o=>o.classList.remove('selected'));
      if (el) el.classList.add('selected');
      document.getElementById('currentModel').textContent = modelName;
      selectedModelName = modelName;
      modelDropdown.classList.remove('open');
      showNotification('✅ Модель: '+modelName,'success');
    } else { showNotification('❌ '+(d.error||'Ошибка'),'error'); }
  } catch(e) { showNotification('❌ Ошибка сети','error'); }
}

document.addEventListener('click', e => {
  if (!e.target.closest('#modelDropdown') && !e.target.closest('#modelSelectorBtn'))
    modelDropdown.classList.remove('open');
});

(async function initBadge(){
  try {
    const r = await fetch('/models_list');
    const d = await r.json();
    if (d.current) {
      let name = d.current;
      for (const p of d.models||[])
        for (const m of p.list||[])
          if (m.id===d.current) { name=m.name; break; }
      document.getElementById('currentModel').textContent = name;
      selectedModelName = name;
    }
  } catch(e){}
})();


// ========== ЧАТ ==========
function newChat() {
  chatContainer.innerHTML = '';
  chatContainer.appendChild(welcomeScreen);
  welcomeScreen.style.display = 'flex';
  msgCount = 0;
}
function clearChat() { 
  newChat(); 
  // Очищаем историю на сервере
  fetch('/api/history/clear', {method: 'DELETE'}).catch(() => {});
}
function shareChat() {
  navigator.clipboard.writeText(window.location.href).then(() => showNotification('Ссылка скопирована', 'success'));
}
function scrollToBottom() {
  setTimeout(() => chatContainer.scrollTo({ top: chatContainer.scrollHeight, behavior: 'smooth' }), 50);
}
async function saveServerHistory() {
  try {
    const messages = [];
    const bubbles = document.querySelectorAll('.msg-bubble');
    let currentRole = null;
    let currentContent = '';
    for (const bubble of bubbles) {
      const isUser = bubble.closest('.message')?.classList.contains('user');
      const role = isUser ? 'user' : 'assistant';
      const text = bubble.textContent || '';
      if (text.trim()) {
        messages.push({role: role, content: text.trim()});
      }
    }
    await fetch('/api/history/save', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({history: messages})
    });
  } catch (e) {
    console.log('Save history error:', e);
  }
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
    saveServerHistory(); // Сохраняем историю на сервере
  })
  .catch(() => {
    removeTyping();
    appendMessage('ai', '❌ Ошибка соединения');
  });
}

// ========== ИНИЦИАЛИЗАЦИЯ ==========
input.focus();
