/* ══════════════════════════════════════════
   NovaMind — Основной скрипт чата
══════════════════════════════════════════ */

// ========== СОСТОЯНИЕ ==========
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

  // ── Авто-определение URL в сообщении ─────────────────────
  const urlMatch = finalMsg.match(
    /(?:^|\s)(https?:\/\/[^\s]+|www\.[^\s]+\.[a-z]{2,}[^\s]*)/i
  );
  const isUrlOnly = /^(https?:\/\/|www\.)[^\s]+$/i.test(finalMsg.trim());
  const hasFetchCmd = /^\/(?:fetch|browse|url|сайт|открой|прочитай)\s+/i.test(finalMsg);

  // ── URL запрос (сайт + опциональный вопрос) ───────────────
  if (hasFetchCmd || isUrlOnly || (urlMatch && finalMsg.length < 300)) {
    let url = '';
    let userPrompt = '';

    if (hasFetchCmd) {
      // /fetch https://... что тут написано?
      const parts = finalMsg.replace(/^\/\S+\s+/, '').split(' ');
      url = parts[0];
      userPrompt = parts.slice(1).join(' ');
    } else if (urlMatch) {
      url = urlMatch[1];
      userPrompt = finalMsg.replace(url, '').trim();
    } else {
      url = finalMsg.trim();
    }

    // Обновляем индикатор
    showTyping();
    const typEl = document.getElementById('typingIndicator');
    if (typEl) {
      typEl.querySelector('.msg-bubble').innerHTML =
        '<span style="color:#06b6d4;font-size:13px">🌐 Читаю сайт...</span>';
    }

    try {
      const resp = await fetch('/api/fetch_url', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({url, prompt: userPrompt})
      });
      const data = await resp.json();
      removeTyping();
      if (data.error) {
        appendMessage('ai',
          `❌ Не удалось открыть сайт\n\n**${url}**\n\n${data.error}\n\n` +
          `💡 **Попробуй так:**\n` +
          `• Проверь правильность URL\n` +
          `• Напиши \`/search ${userPrompt || url}\` для поиска\n` +
          `• Некоторые сайты блокируют автоматические запросы`
        );
      } else {
        appendMessage('ai', data.reply || data.raw || 'Готово');
        saveServerHistory();
      }
    } catch(e) {
      removeTyping();
      appendMessage('ai', '❌ Ошибка соединения при чтении сайта: ' + e.message);
    }
    return;
  }

  // ── Если команда — отправляем на /command ─────────────────
  if (isCommand) {
    // Разбираем команду /search, /wiki, /news отдельно
    const cmdLow = finalMsg.toLowerCase();

    if (cmdLow.startsWith('/search ') || cmdLow.startsWith('/поиск ')) {
      const query = finalMsg.replace(/^\/\S+\s+/,'').trim();
      showTyping();
      const typEl = document.getElementById('typingIndicator');
      if (typEl) typEl.querySelector('.msg-bubble').innerHTML =
        `<span style="color:#10b981;font-size:13px">🔍 Ищу: "${escapeHtml(query)}"...</span>`;
      try {
        const resp = await fetch('/api/web_search_groq', {
          method:'POST',
          headers:{'Content-Type':'application/json'},
          body: JSON.stringify({query})
        });
        const data = await resp.json();
        removeTyping();
        appendMessage('ai', data.reply || data.error || 'Нет результатов');
        if (!data.error) saveServerHistory();
      } catch(e) {
        removeTyping();
        appendMessage('ai','❌ Ошибка поиска: '+e.message);
      }
      return;
    }

    // Все остальные команды → /command
    try {
      const resp = await fetch('/command', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ command: finalMsg })
      });
      const data = await resp.json();
      removeTyping();
      if (data.error) {
        appendMessage('ai', '❌ ' + data.error);
      } else {
        appendMessage('ai', data.result || data.reply || 'Готово');
        saveServerHistory();
      }
    } catch (e) {
      removeTyping();
      appendMessage('ai', '❌ Ошибка соединения');
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

  // ── Потоковый запрос к ИИ (SSE) ──
  await sendStream(finalMsg, reasoningOn);
}

async function sendStream(message, reasoning) {
  // Создаём пустой AI пузырь сразу
  removeTyping();
  const msgEl = createStreamBubble();
  let fullText = '';
  let hasError = false;

  try {
    const resp = await fetch('/stream', {
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify({message, reasoning: !!reasoning})
    });

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      updateStreamBubble(msgEl, '❌ Ошибка: ' + (err.error || resp.statusText), true);
      return;
    }

    const reader  = resp.body.getReader();
    const decoder = new TextDecoder();
    let   buffer  = '';

    while (true) {
      const {value, done} = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, {stream: true});
      const lines = buffer.split('\n');
      buffer = lines.pop(); // последняя неполная строка остаётся

      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed) continue;

        // SSE формат: "event: token\ndata: {...}"
        if (trimmed.startsWith('event: ')) continue; // пропускаем event строку
        if (!trimmed.startsWith('data: ')) continue;

        const jsonStr = trimmed.slice(6);
        try {
          const obj = JSON.parse(jsonStr);

          if (obj.t !== undefined) {
            // Новый токен
            fullText += obj.t;
            updateStreamBubble(msgEl, fullText, false);
          } else if (obj.msg) {
            // Ошибка
            updateStreamBubble(msgEl, '❌ ' + obj.msg, true);
            hasError = true;
          } else if (obj.full !== undefined) {
            // done событие — финальный текст
            if (obj.full) {
              updateStreamBubble(msgEl, obj.full, false);
              fullText = obj.full;
            }
          }
        } catch(e) { /* JSON parse error — игнорируем */ }
      }
    }

  } catch(e) {
    if (fullText) {
      // Уже что-то получили — показываем что есть
      updateStreamBubble(msgEl, fullText + '\n\n⚠️ Соединение прервано', false);
    } else {
      updateStreamBubble(msgEl, '❌ Ошибка соединения: ' + e.message, true);
    }
  }

  // Финальный рендер с полным Markdown форматированием
  if (!hasError && fullText) {
    finalizeStreamBubble(msgEl, fullText);
  }

  saveServerHistory();
}

function createStreamBubble() {
  const wrap = document.createElement('div');
  wrap.className = 'message ai streaming';
  wrap.innerHTML = `
    <div class="msg-avatar">✦</div>
    <div class="msg-body">
      <div class="msg-name">NovaMind</div>
      <div class="msg-bubble stream-bubble">
        <span class="stream-cursor">▋</span>
      </div>
    </div>`;
  chatContainer.appendChild(wrap);
  scrollToBottom();
  return wrap;
}

function updateStreamBubble(wrap, text, isError) {
  const bubble = wrap.querySelector('.msg-bubble');
  if (!bubble) return;

  if (isError) {
    bubble.innerHTML = `<span style="color:#f87171">${escapeHtml(text)}</span>`;
    wrap.classList.remove('streaming');
    return;
  }

  // Быстрый рендер во время стриминга: только переносы строк + курсор
  // Полный Markdown применяется в finalizeStreamBubble()
  const escaped = escapeHtml(text).replace(/\n/g, '<br>');
  bubble.innerHTML = escaped + '<span class="stream-cursor">▋</span>';
  scrollToBottom();
}

function finalizeStreamBubble(wrap, fullText) {
  const bubble = wrap.querySelector('.msg-bubble');
  if (!bubble) return;
  wrap.classList.remove('streaming');
  // Применяем полное Markdown форматирование
  bubble.innerHTML = formatContent(fullText);
  // Подсветка кода если есть
  if (window.Prism) Prism.highlightAllUnder(bubble);
  scrollToBottom();
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
// ═══════════════════════════════════════════════════════════
//  ГОЛОСОВОЙ ВВОД + WAKE WORD "Khirad" / "Хирад"
// ═══════════════════════════════════════════════════════════

const WAKE_WORDS = ['khirad','хирад','кхирад','кирад','hirad'];

// Состояние голосовой системы
let recognition      = null;   // основной STT для ввода
let wakeRecognition  = null;   // фоновый слушатель wake word
let isRecording      = false;  // идёт ли основная запись
let wakeListening    = false;  // слушаем ли wake word
let interimText      = '';     // промежуточный текст (interim)
let finalText        = '';     // финальный накопленный текст

// ── Поддержка SpeechRecognition ──────────────────────────────
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
const hasSR = !!SR;

// ── UI элементы ───────────────────────────────────────────────
// ── Анимация кнопки ──────────────────────────────────────────
function setVoiceState(state) {
  // state: 'idle' | 'wake' | 'recording' | 'processing'
  if (!voiceBtn) return;
  voiceBtn.classList.remove('recording','wake-active','processing');
  if (state === 'recording') {
    voiceBtn.classList.add('recording');
    voiceTooltip.textContent = '● Говорите... (остановится автоматически)';
  } else if (state === 'wake') {
    voiceBtn.classList.add('wake-active');
    voiceTooltip.textContent = '👂 Слушаю "Khirad"...';
  } else if (state === 'processing') {
    voiceBtn.classList.add('processing');
    voiceTooltip.textContent = '⏳ Обрабатываю...';
  } else {
    voiceTooltip.textContent = wakeListening
      ? '👂 Слушаю "Khirad"...'
      : 'Нажмите для записи';
  }
}

// ══════════════════════════════════════════════════════════════
//  ОСНОВНАЯ ЗАПИСЬ (кнопка или после wake word)
// ══════════════════════════════════════════════════════════════
function toggleVoice() {
  if (!hasSR) {
    showNotification('❌ Браузер не поддерживает голосовой ввод. Используй Chrome.', 'error');
    return;
  }
  if (isRecording) {
    stopRecording(true); // остановить и отправить
  } else {
    startRecording();
  }
}

function startRecording(fromWake = false) {
  if (isRecording) return;
  if (!hasSR) return;

  // Останавливаем wake listener пока пишем основное
  if (wakeRecognition) { try { wakeRecognition.stop(); } catch(e){} }

  finalText    = '';
  interimText  = '';
  isRecording  = true;
  setVoiceState('recording');

  if (fromWake) {
    showNotification('🎤 Слушаю вас, ' + (window.KHIRAD_NAME || 'Khirad') + ' готов!', 'success');
  }

  recognition             = new SR();
  recognition.lang        = detectLang();
  recognition.continuous  = true;      // продолжает пока не скажем стоп
  recognition.interimResults = true;   // показываем текст в реальном времени

  recognition.onstart = () => {
    setVoiceState('recording');
  };

  recognition.onresult = (e) => {
    let interim = '';
    let final   = '';
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const t = e.results[i][0].transcript;
      if (e.results[i].isFinal) {
        final += t + ' ';
      } else {
        interim += t;
      }
    }
    if (final) finalText += final;
    interimText = interim;

    // Пишем в поле ввода в реальном времени (final + interim)
    const combined = (finalText + interimText).trim();
    if (input) {
      input.value = combined;
      input.dispatchEvent(new Event('input'));
      // Авто-ресайз
      input.style.height = 'auto';
      input.style.height = Math.min(input.scrollHeight, 120) + 'px';
    }

    // Проверяем слово-стоп: если пользователь сказал "отправить" / "готово"
    const low = combined.toLowerCase();
    if (/\b(отправить|готово|send|submit|ок хирад|ok khirad)\b/.test(low)) {
      // Убираем стоп-слово из текста
      const clean = combined
        .replace(/\b(отправить|готово|send|submit|ок хирад|ok khirad)\b/gi,'')
        .trim();
      if (input) input.value = clean;
      stopRecording(true);
      return;
    }

    // Авто-стоп через 3 сек тишины (если есть финальный текст)
    clearTimeout(window._voiceSilenceTimer);
    if (finalText.trim()) {
      window._voiceSilenceTimer = setTimeout(() => {
        if (isRecording) stopRecording(false); // просто стоп, не отправляем
      }, 3000);
    }
  };

  recognition.onerror = (e) => {
    if (e.error === 'no-speech') {
      // Тишина — просто останавливаемся
      stopRecording(false);
    } else if (e.error !== 'aborted') {
      showNotification('Ошибка микрофона: ' + e.error, 'error');
      stopRecording(false);
    }
  };

  recognition.onend = () => {
    // SpeechRecognition остановился сам (браузерный лимит ~1 мин)
    // Если есть текст — оставляем в поле, пользователь сам нажмёт отправить
    isRecording = false;
    setVoiceState('idle');
    // Возобновляем wake listener
    if (wakeListening) setTimeout(startWakeListener, 500);
  };

  recognition.start();
}

function stopRecording(sendNow = false) {
  clearTimeout(window._voiceSilenceTimer);
  isRecording = false;
  interimText = '';

  if (recognition) {
    try { recognition.stop(); } catch(e) {}
    recognition = null;
  }

  setVoiceState('processing');

  const text = (input ? input.value : finalText).trim();
  finalText = '';

  if (sendNow && text) {
    setTimeout(() => {
      setVoiceState('idle');
      sendMessage(); // отправляем
    }, 300);
  } else {
    setTimeout(() => {
      setVoiceState('idle');
      if (wakeListening) startWakeListener(); // возобновляем wake
    }, 300);
  }
}

// ══════════════════════════════════════════════════════════════
//  WAKE WORD LISTENER — фоновый, всегда слушает "Khirad"
// ══════════════════════════════════════════════════════════════
function detectLang() {
  // Определяем язык из настроек браузера
  const lang = navigator.language || 'ru-RU';
  if (lang.startsWith('ru')) return 'ru-RU';
  if (lang.startsWith('en')) return 'en-US';
  if (lang.startsWith('tg')) return 'tg-TJ';
  return 'ru-RU';
}

function startWakeListener() {
  if (!hasSR || isRecording || wakeRecognition) return;

  wakeRecognition               = new SR();
  wakeRecognition.lang          = detectLang();
  wakeRecognition.continuous    = true;
  wakeRecognition.interimResults= true;
  wakeListening                 = true;
  setVoiceState('wake');

  wakeRecognition.onresult = (e) => {
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const transcript = e.results[i][0].transcript.toLowerCase().trim();
      // Проверяем wake word
      const detected = WAKE_WORDS.some(w => transcript.includes(w));
      if (detected) {
        // Останавливаем wake listener
        try { wakeRecognition.stop(); } catch(ex) {}
        wakeRecognition = null;
        wakeListening   = false;
        // Показываем анимацию активации
        showWakeActivation();
        // Через 600мс начинаем основную запись
        setTimeout(() => startRecording(true), 600);
        return;
      }
    }
  };

  wakeRecognition.onerror = (e) => {
    if (e.error === 'aborted') return;
    wakeRecognition = null;
    // Перезапускаем через 2 сек
    if (wakeListening && !isRecording) {
      setTimeout(startWakeListener, 2000);
    }
  };

  wakeRecognition.onend = () => {
    wakeRecognition = null;
    // Перезапускаем автоматически (браузер ограничивает сессию)
    if (wakeListening && !isRecording) {
      setTimeout(startWakeListener, 500);
    }
  };

  try {
    wakeRecognition.start();
  } catch(e) {
    wakeRecognition = null;
    setTimeout(startWakeListener, 2000);
  }
}

function stopWakeListener() {
  wakeListening = false;
  if (wakeRecognition) {
    try { wakeRecognition.stop(); } catch(e) {}
    wakeRecognition = null;
  }
  setVoiceState('idle');
}

function toggleWakeWord() {
  if (wakeListening) {
    stopWakeListener();
    showNotification('🔇 Распознавание "Khirad" отключено', 'info');
    if (wakeToggleBtn) {
      wakeToggleBtn.classList.remove('active');
      wakeToggleBtn.title = 'Включить "Khirad" wake word';
    }
    localStorage.setItem('khirad_wake', '0');
  } else {
    startWakeListener();
    showNotification('👂 Скажи "Khirad" чтобы активировать!', 'success');
    if (wakeToggleBtn) {
      wakeToggleBtn.classList.add('active');
      wakeToggleBtn.title = 'Отключить "Khirad" wake word';
    }
    localStorage.setItem('khirad_wake', '1');
  }
}

// ── Анимация активации wake word ────────────────────────────
function showWakeActivation() {
  // Пульсирующий оверлей на 600мс
  const el = document.createElement('div');
  el.id = 'wakeFlash';
  el.style.cssText = `
    position:fixed; inset:0; z-index:9999; pointer-events:none;
    background:radial-gradient(circle at 50% 50%,
      rgba(124,58,237,0.25) 0%, transparent 70%);
    animation: wakeFlash 0.6s ease-out forwards;
  `;
  document.body.appendChild(el);
  // Вибрация на телефоне
  if (navigator.vibrate) navigator.vibrate([50, 30, 80]);
  // Звуковой сигнал
  playWakeSound();
  setTimeout(() => el.remove(), 650);
}

function playWakeSound() {
  try {
    const ctx  = new (window.AudioContext || window.webkitAudioContext)();
    const osc  = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.type            = 'sine';
    osc.frequency.setValueAtTime(880, ctx.currentTime);
    osc.frequency.exponentialRampToValueAtTime(440, ctx.currentTime + 0.15);
    gain.gain.setValueAtTime(0.3, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.3);
    osc.start(ctx.currentTime);
    osc.stop(ctx.currentTime + 0.3);
  } catch(e) {}
}

// ── Инициализация при загрузке страницы ─────────────────────
const wakeToggleBtn = document.getElementById('wakeToggleBtn');

(function initVoice() {
  if (!hasSR) {
    if (voiceBtn) {
      voiceBtn.style.opacity = '0.4';
      voiceBtn.title = 'Голосовой ввод не поддерживается. Используй Chrome.';
    }
    if (wakeToggleBtn) wakeToggleBtn.style.display = 'none';
    return;
  }
  // Восстанавливаем состояние wake word из localStorage
  const savedWake = localStorage.getItem('khirad_wake');
  if (savedWake === '1') {
    setTimeout(() => {
      startWakeListener();
      if (wakeToggleBtn) wakeToggleBtn.classList.add('active');
    }, 1000);
  }
})();


// ── Обновляем placeholder с подсказкой про URL ──
(function updatePlaceholder(){
  const inp = document.getElementById('messageInput') ||
              document.querySelector('.message-input') ||
              document.querySelector('textarea');
  if (inp && !inp.dataset.placeholderSet) {
    inp.placeholder =
      'Сообщение, URL сайта, или /fetch https://... | /search запрос';
    inp.dataset.placeholderSet = '1';
  }
})();


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
    llama_local: {color:'#10b981', label:'💻 Llama.cpp (локальный)'},
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
    border-radius:8px;padding:7px;color:#f59e0b;font-size:12px;
    cursor:pointer;font-weight:600;">🦙 Загрузить Ollama модели</button>
    <button onclick="showLlamaCppPanel()" style="
      width:100%;background:rgba(16,185,129,.12);border:1px solid #10b981;
      border-radius:8px;padding:7px;color:#10b981;font-size:12px;
      cursor:pointer;font-weight:600;margin-top:6px;">⚙️ Настроить llama.cpp</button>
  `;
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

// ══════════════════════════════════════════
//  llama.cpp — функции
// ══════════════════════════════════════════

async function loadLlamaCppModels() {
  try {
    const r = await fetch('/api/llama/models');
    const d = await r.json();
    if (d.models && d.models.length) {
      const count = d.models.length;
      showNotification(`✅ llama.cpp: найдено ${count} моделей`, 'success');
      loadModelsFromServer();
    } else {
      showNotification('⚠️ llama.cpp: ' + (d.error || 'нет моделей'), 'warning');
    }
  } catch(e) {
    showNotification('❌ llama.cpp недоступна — запусти сервер!', 'error');
  }
}

async function checkLlamaCppStatus() {
  try {
    const r = await fetch('/api/llama/status');
    const d = await r.json();
    return d.success;
  } catch(e) { return false; }
}

function showLlamaCppPanel() {
  // Закрываем dropdown
  document.getElementById('modelDropdown').classList.remove('open');

  // Убираем старую панель если есть
  document.getElementById('llamaPanel')?.remove();

  const panel = document.createElement('div');
  panel.id = 'llamaPanel';
  panel.style.cssText = `
    position:fixed; inset:0; background:rgba(0,0,0,.7);
    display:flex; align-items:center; justify-content:center;
    z-index:500; backdrop-filter:blur(4px);
  `;

  panel.innerHTML = `
    <div style="
      background:#1a1a2e; border:1px solid #10b981;
      border-radius:16px; padding:24px; width:90%; max-width:420px;
      box-shadow:0 0 40px rgba(16,185,129,.25);
    ">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:18px;">
        <div style="font-size:16px;font-weight:700;color:#e0e0f0;">
          ⚙️ Настройки llama.cpp
        </div>
        <button onclick="document.getElementById('llamaPanel').remove()" style="
          background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.15);
          border-radius:8px;padding:4px 10px;color:#888;cursor:pointer;font-size:14px;">✕</button>
      </div>

      <label style="font-size:11px;font-weight:700;text-transform:uppercase;
        letter-spacing:1px;color:#6b7280;display:block;margin-bottom:6px;">
        URL сервера llama.cpp
      </label>
      <input id="llamaUrlInput" value="http://127.0.0.1:8080" placeholder="http://127.0.0.1:8080"
        style="width:100%;background:#0d0d1a;border:1px solid #2a2a4a;border-radius:8px;
          padding:10px 13px;color:#e0e0f0;font-size:14px;outline:none;font-family:monospace;
          box-sizing:border-box;margin-bottom:10px;">

      <label style="font-size:11px;font-weight:700;text-transform:uppercase;
        letter-spacing:1px;color:#6b7280;display:block;margin-bottom:6px;">
        Имя модели (необязательно — если пусто, берётся из /v1/models)
      </label>
      <input id="llamaModelInput" placeholder="например: qwen2.5-7b-instruct"
        style="width:100%;background:#0d0d1a;border:1px solid #2a2a4a;border-radius:8px;
          padding:10px 13px;color:#e0e0f0;font-size:14px;outline:none;font-family:monospace;
          box-sizing:border-box;margin-bottom:14px;">

      <div id="llamaStatusBox" style="
        background:rgba(16,185,129,.08);border:1px solid rgba(16,185,129,.2);
        border-radius:8px;padding:10px 13px;font-size:12px;color:#6b7280;
        margin-bottom:14px;display:flex;align-items:center;gap:8px;">
        <span id="llamaStatusDot" style="width:8px;height:8px;border-radius:50%;
          background:#6b7280;flex-shrink:0;display:inline-block;"></span>
        <span id="llamaStatusText">Нажми «Проверить» для проверки соединения</span>
      </div>

      <div style="display:flex;gap:8px;flex-direction:column;">
        <button onclick="testLlamaCpp()" style="
          width:100%;background:rgba(16,185,129,.15);border:1px solid #10b981;
          border-radius:8px;padding:10px;color:#10b981;font-size:13px;
          cursor:pointer;font-weight:600;">🔍 Проверить соединение</button>
        <button onclick="applyLlamaCpp()" style="
          width:100%;background:linear-gradient(135deg,#10b981,#059669);
          border:none;border-radius:8px;padding:11px;color:#fff;font-size:13px;
          cursor:pointer;font-weight:700;">✅ Применить и загрузить модели</button>
      </div>

      <div style="margin-top:14px;padding:10px;background:rgba(0,0,0,.3);
        border-radius:8px;font-size:11px;color:#6b7280;line-height:1.6;">
        <strong style="color:#10b981;">💡 Как запустить llama.cpp:</strong><br>
        <code style="color:#a78bfa;">./llama-server -m model.gguf --port 8080 --host 0.0.0.0</code><br>
        Или через Python: <code style="color:#a78bfa;">pip install llama-cpp-python[server]</code>
      </div>
    </div>
  `;

  document.body.appendChild(panel);
  panel.addEventListener('click', e => { if(e.target===panel) panel.remove(); });
}

async function testLlamaCpp() {
  const url = document.getElementById('llamaUrlInput').value.trim().replace(/\/+$/, '');
  const dot  = document.getElementById('llamaStatusDot');
  const text = document.getElementById('llamaStatusText');
  dot.style.background  = '#f59e0b';
  text.textContent = '⏳ Проверяю соединение...';

  try {
    // Сначала /health, потом /v1/models
    let ok = false;
    try {
      const r = await fetch('/api/llama/status');
      const d = await r.json();
      ok = d.success;
    } catch(e) {}

    // Через прокси — сохраняем URL сначала
    await fetch('/api/llama/set_url', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({url})
    });

    const r2 = await fetch('/api/llama/status');
    const d2 = await r2.json();

    if (d2.success) {
      dot.style.background  = '#10b981';
      dot.style.boxShadow   = '0 0 6px #10b981';
      text.textContent = '✅ Соединение установлено! llama.cpp работает.';
      text.style.color = '#10b981';
    } else {
      throw new Error(d2.error || 'Нет ответа');
    }
  } catch(e) {
    dot.style.background = '#ef4444';
    text.textContent     = '❌ Ошибка: ' + e.message;
    text.style.color     = '#ef4444';
  }
}

async function applyLlamaCpp() {
  const url       = document.getElementById('llamaUrlInput').value.trim().replace(/\/+$/, '');
  const modelName = document.getElementById('llamaModelInput').value.trim();

  // Сохраняем URL
  await fetch('/api/llama/set_url', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({url})
  });

  // Загружаем модели
  const r = await fetch('/api/llama/models');
  const d = await r.json();

  let modelId = 'local-model';
  let modelLabel = 'Llama.cpp';

  if (modelName) {
    modelId    = modelName;
    modelLabel = modelName;
  } else if (d.models && d.models.length) {
    modelId    = d.models[0].id;
    modelLabel = d.models[0].name;
  }

  // Переключаем провайдера
  const sw = await fetch('/switch_model?model_id='+encodeURIComponent(modelId)+'&provider_id=llama_local');
  const sd = await sw.json();

  if (sd.success) {
    document.getElementById('currentModel').textContent = modelLabel;
    selectedModelName = modelLabel;
    document.getElementById('llamaPanel').remove();
    showNotification('✅ llama.cpp подключён: ' + modelLabel, 'success');
    loadModelsFromServer();
  } else {
    showNotification('❌ Ошибка переключения: ' + (sd.error||''), 'error');
  }
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
  // Обновляем сайдбар после каждого сообщения
  await loadChatList();
}

async function loadChatList() {
  try {
    const r = await fetch('/api/chats');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const data = await r.json();
    const chats = data.chats || [];
    // Кэшируем в localStorage
    try {
      localStorage.setItem('nova_chats_cache', JSON.stringify({
        chats, active: data.active_chat, ts: Date.now()
      }));
    } catch(e2) {}
    renderChatList(chats, data.active_chat);
  } catch(err) {
    console.warn('[History] Сервер недоступен, читаем из кэша:', err);
    try {
      const cached = localStorage.getItem('nova_chats_cache');
      if (cached) {
        const d = JSON.parse(cached);
        renderChatList(d.chats || [], d.active);
      } else {
        renderChatList([], null);
      }
    } catch(e3) {
      renderChatList([], null);
    }
  }
}

function renderChatList(chats, activeChatId) {
  const container = document.getElementById('historyContainer');
  if (!container) return;
  container.innerHTML = '';

  if (!chats || chats.length === 0) {
    container.innerHTML = `
      <div class="history-empty">
        <span style="font-size:24px;display:block;margin-bottom:6px;">💬</span>
        Начни диалог — он появится здесь
      </div>`;
    return;
  }

  // Группируем по дате
  const today     = new Date().toLocaleDateString('ru');
  const yesterday = new Date(Date.now()-86400000).toLocaleDateString('ru');
  const groups    = {};

  for (const chat of chats) {
    const d = chat.updated_at
      ? new Date(chat.updated_at).toLocaleDateString('ru')
      : 'Ранее';
    const label = d === today ? 'Сегодня' : d === yesterday ? 'Вчера' : d;
    (groups[label] = groups[label] || []).push(chat);
  }

  for (const [label, groupChats] of Object.entries(groups)) {
    // Заголовок группы
    const hdr = document.createElement('div');
    hdr.className = 'history-group-label';
    hdr.textContent = label;
    container.appendChild(hdr);

    for (const chat of groupChats) {
      const item = document.createElement('div');
      item.className = 'history-item' + (chat.id === activeChatId ? ' active' : '');
      item.dataset.chatId = chat.id;
      item.innerHTML = `
        <div class="history-item-icon">💬</div>
        <div class="history-item-body">
          <div class="history-item-title">${escapeHtml(chat.title)}</div>
          <div class="history-item-meta">${chat.count} сообщ.</div>
        </div>
        <div class="history-item-actions">
          <button class="hist-btn load-btn" title="Загрузить" onclick="loadChat('${chat.id}',event)">↩</button>
          <button class="hist-btn del-btn"  title="Удалить"   onclick="deleteChat('${chat.id}',event)">×</button>
        </div>`;
      item.addEventListener('click', () => loadChat(chat.id));
      container.appendChild(item);
    }
  }
}

async function loadChat(chatId, e) {
  if (e) e.stopPropagation();
  try {
    const r    = await fetch(`/api/chats/${chatId}/load`, {method:'POST'});
    const data = await r.json();
    if (!data.success) { showNotification('❌ Чат не найден', 'error'); return; }
    // Отображаем сообщения
    chatContainer.innerHTML = '';
    for (const msg of data.messages) {
      appendMessage(msg.role === 'user' ? 'user' : 'ai', msg.content);
    }
    closeSidebar();
    // Помечаем активный
    document.querySelectorAll('.history-item').forEach(el => {
      el.classList.toggle('active', el.dataset.chatId === chatId);
    });
    showNotification('✅ Чат загружен', 'success');
  } catch(e) {
    showNotification('❌ Ошибка: ' + e.message, 'error');
  }
}

async function deleteChat(chatId, e) {
  if (e) e.stopPropagation();
  if (!confirm('Удалить этот чат?')) return;
  try {
    await fetch(`/api/chats/${chatId}`, {method:'DELETE'});
    await loadChatList();
    showNotification('🗑 Чат удалён', 'info');
  } catch(err) {
    showNotification('❌ Ошибка удаления', 'error');
  }
}

async function startNewChat() {
  try {
    await fetch('/api/chats/new', {method:'POST'});
  } catch(e) {}
  chatContainer.innerHTML = '';
  appendMessage('ai', '👋 Новый диалог начат! Чем могу помочь?');
  setTimeout(loadChatList, 300);
  closeSidebar();
}

// Устаревшие функции — оставляем для совместимости
function updateSidebarHistory(history) { loadChatList(); }

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



// ════════════════════════════════════════════════════════════
//  MCP СЕРВЕРЫ — панель подключения и использование в чате
// ════════════════════════════════════════════════════════════

let mcpAllTools  = [];   // Все инструменты из каталога
let mcpPendingId = null; // Ожидающий подключения toolkit_id

// ── Открытие/закрытие панели ─────────────────────────────────
function openMcpPanel() {
  const drawer = document.getElementById('mcpDrawer');
  drawer.style.display = 'flex';
  loadMcpCatalog();
  // Слушаем сообщения от OAuth popup
  window.addEventListener('message', onMcpOAuthMessage);
}

function closeMcpPanel() {
  document.getElementById('mcpDrawer').style.display = 'none';
  window.removeEventListener('message', onMcpOAuthMessage);
}

// Закрытие по клику на фон
document.getElementById('mcpDrawer')?.addEventListener('click', e => {
  if (e.target.id === 'mcpDrawer') closeMcpPanel();
});

// ── Загрузка каталога с сервера ──────────────────────────────
async function loadMcpCatalog() {
  try {
    const r    = await fetch('/api/mcp/catalog');
    const data = await r.json();
    mcpAllTools = data.tools || [];
    renderMcpTools(mcpAllTools);
    updateMcpBadge();
  } catch(e) {
    document.getElementById('mcpToolList').innerHTML =
      '<div style="grid-column:1/-1;text-align:center;padding:30px;color:#f87171;">❌ Ошибка загрузки</div>';
  }
}

// ── Рендер карточек ──────────────────────────────────────────
function renderMcpTools(tools) {
  const list = document.getElementById('mcpToolList');
  if (!tools.length) {
    list.innerHTML = '<div style="grid-column:1/-1;text-align:center;padding:30px;color:#6b7280;">Ничего не найдено</div>';
    return;
  }

  list.innerHTML = tools.map(t => `
    <div class="mcp-card ${t.connected ? 'connected' : ''}"
         onclick="${t.connected ? `mcpDisconnect('${t.id}','${t.name}')` : `mcpConnect('${t.id}')`}">
      <div style="position:absolute;top:8px;right:8px;">
        <span class="mcp-card-badge ${t.connected ? 'badge-on' : 'badge-off'}">
          ${t.connected ? '✓ Вкл' : 'Подключить'}
        </span>
      </div>
      <div class="mcp-card-icon">${t.icon}</div>
      <div class="mcp-card-name">${t.name}</div>
      <div class="mcp-card-desc">${t.desc}</div>
      ${t.connected && t.user ? `<div class="mcp-card-user">@${t.user}</div>` : ''}
    </div>
  `).join('');

  // Обновляем счётчик подключённых
  const connCount = tools.filter(t => t.connected).length;
  const countEl = document.getElementById('mcpConnCount');
  if (countEl) {
    countEl.textContent = connCount > 0
      ? `✅ ${connCount} подключено — используй в чате!`
      : 'Подключи инструменты — используй в чате';
  }
}

// ── Поиск по инструментам ────────────────────────────────────
function filterMcpTools(query) {
  const q = query.toLowerCase();
  const filtered = q
    ? mcpAllTools.filter(t =>
        t.name.toLowerCase().includes(q) ||
        t.desc.toLowerCase().includes(q))
    : mcpAllTools;
  renderMcpTools(filtered);
}

// ── Подключение инструмента ──────────────────────────────────
async function mcpConnect(toolkitId) {
  mcpPendingId = toolkitId;
  const tool = mcpAllTools.find(t => t.id === toolkitId);
  if (!tool) return;

  // Показываем индикатор загрузки на карточке
  showNotification(`⏳ Подключаю ${tool.name}...`, 'info');

  try {
    const r = await fetch(`/api/mcp/connect/${toolkitId}`, {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({})
    });
    const data = await r.json();

    if (data.oauth_url) {
      // Открываем OAuth popup
      const popup = window.open(
        data.oauth_url,
        `mcp_oauth_${toolkitId}`,
        'width=520,height=680,scrollbars=yes,resizable=yes'
      );
      if (!popup) {
        // Popup заблокирован — перенаправляем напрямую
        showNotification('💡 Popup заблокирован. Открываю в новой вкладке...', 'info');
        window.open(data.oauth_url, '_blank');
      }
    } else if (data.needs_apikey) {
      // Показываем диалог ввода API ключа
      showMcpApiKeyDialog(toolkitId, tool.name);
    } else if (data.needs_composio) {
      showNotification('💡 ' + data.message, 'info');
      setTimeout(() => {
        if (confirm('Перейти на composio.dev для получения бесплатного ключа?')) {
          window.open('https://composio.dev', '_blank');
        }
      }, 500);
    } else if (data.success) {
      showNotification(`✅ ${tool.name} подключён!`, 'success');
      loadMcpCatalog();
    } else if (data.error) {
      showNotification(`❌ ${data.error}`, 'error');
    }
  } catch(e) {
    showNotification(`❌ Ошибка: ${e.message}`, 'error');
  }
}

// ── OAuth success callback (из popup) ────────────────────────
function onMcpOAuthMessage(e) {
  if (e.data?.type === 'mcp_connected') {
    showNotification(`✅ ${e.data.tool_name} подключён!`, 'success');
    if (navigator.vibrate) navigator.vibrate([50, 30, 80]);
    loadMcpCatalog(); // Обновляем список
  }
}

// ── API Key диалог ───────────────────────────────────────────
function showMcpApiKeyDialog(toolkitId, toolName) {
  mcpPendingId = toolkitId;
  document.getElementById('mcpApiKeyTitle').textContent = `🔑 API ключ для ${toolName}`;
  document.getElementById('mcpApiKeyInput').value = '';
  document.getElementById('mcpApiKeyDialog').style.display = 'flex';
}

async function submitMcpApiKey() {
  const key = document.getElementById('mcpApiKeyInput').value.trim();
  if (!key || !mcpPendingId) return;

  const btn = document.getElementById('mcpApiKeySubmit');
  btn.textContent = '⏳ Подключаю...';
  btn.disabled = true;

  try {
    const r = await fetch(`/api/mcp/connect/${mcpPendingId}`, {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({api_key: key})
    });
    const data = await r.json();
    document.getElementById('mcpApiKeyDialog').style.display = 'none';
    btn.textContent = 'Подключить';
    btn.disabled = false;

    if (data.success) {
      showNotification(`✅ ${data.tool} подключён!`, 'success');
      loadMcpCatalog();
    } else {
      showNotification(`❌ ${data.error || 'Ошибка'}`, 'error');
    }
  } catch(e) {
    btn.textContent = 'Подключить';
    btn.disabled = false;
    showNotification(`❌ ${e.message}`, 'error');
  }
}

// ── Отключение ───────────────────────────────────────────────
async function mcpDisconnect(toolkitId, toolName) {
  if (!confirm(`Отключить ${toolName}?`)) return;
  await fetch(`/api/mcp/disconnect/${toolkitId}`, {method:'POST'});
  showNotification(`🔌 ${toolName} отключён`, 'info');
  loadMcpCatalog();
}

// ── Бейдж в сайдбаре ─────────────────────────────────────────
async function updateMcpBadge() {
  try {
    const r    = await fetch('/api/mcp/status');
    const data = await r.json();
    const badge = document.getElementById('mcpBadge');
    if (badge) {
      if (data.count > 0) {
        badge.textContent  = data.count;
        badge.style.display = 'inline';
      } else {
        badge.style.display = 'none';
      }
    }
  } catch(e) {}
}

// ── Использование MCP в главном чате ─────────────────────────
// Перехватываем sendMessage и проверяем — нужен ли MCP инструмент
const _origSendStream = window.sendStream;

async function mcpCheckAndExecute(message) {
  // Определяем упоминание инструмента
  const mcpKeywords = {
    github:    /github|гитхаб|репозитор|issue|pull request|коммит/i,
    notion:    /notion|нотион|страниц|баз данных/i,
    gmail:     /gmail|почт|письм|email/i,
    google_drive: /google drive|диск|файл|документ/i,
    slack:     /slack|слак|канал|сообщени/i,
    linear:    /linear|задач|sprint|спринт/i,
    jira:      /jira|issue|задач|баг трек/i,
    trello:    /trello|доск|карточк/i,
    spotify:   /spotify|музык|треk|плейлист/i,
    youtube:   /youtube|видео|канал/i,
  };

  for (const [tid, rx] of Object.entries(mcpKeywords)) {
    if (rx.test(message)) {
      // Проверяем подключён ли
      const r    = await fetch('/api/mcp/status');
      const data = await r.json();
      if (data.connected[tid]) {
        // Выполняем через MCP
        showTyping();
        const typEl = document.getElementById('typingIndicator');
        const tool = data.connected[tid];
        if (typEl) typEl.querySelector('.msg-bubble').innerHTML =
          `<span style="color:#a78bfa;font-size:13px">🔌 ${tid} работает...</span>`;
        try {
          const execR = await fetch('/api/mcp/execute', {
            method: 'POST',
            headers: {'Content-Type':'application/json'},
            body: JSON.stringify({toolkit: tid, user_message: message})
          });
          const execData = await execR.json();
          removeTyping();
          if (execData.result) {
            appendMessage('ai', execData.result);
            saveServerHistory();
            return true; // Обработано MCP
          }
        } catch(e) { removeTyping(); }
      } else {
        // Инструмент упомянут но не подключён
        appendMessage('ai',
          `🔌 **${tid.charAt(0).toUpperCase()+tid.slice(1)} не подключён**\n\n` +
          `Нажми **боковое меню → MCP Серверы** и подключи ${tid}.\n` +
          `После подключения я смогу выполнять действия напрямую!`
        );
        return true; // Обработано (с подсказкой)
      }
    }
  }
  return false; // Не MCP запрос
}

// ── Инициализация ─────────────────────────────────────────────
(async function initMcp() {
  await updateMcpBadge();
  // Обновляем бейдж каждые 30 сек
  setInterval(updateMcpBadge, 30000);
})();



// ── Загружаем историю чатов при старте ───────────────────────
// Загружаем историю при старте — надёжный вызов
(function _initHistory() {
  function run() {
    loadChatList().catch(function(e) {
      console.error('[History] init failed:', e);
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', run);
  } else {
    setTimeout(run, 150);
  }
})();
