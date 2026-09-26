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
let selectedModelName = 'Nova Ultra';
let inputMode      = null;
let historyReplay  = false;
let sidebarDrag    = null;
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
// ========== DOM-ЭЛЕМЕНТЫ ==========
const input         = document.getElementById('chat-input');
const sendBtn       = document.getElementById('sendBtn');
const voiceBtn      = document.getElementById('voiceBtn');
const voiceTooltip  = document.getElementById('voiceTooltip');
const chatContainer = document.getElementById('chatContainer');
const welcomeScreen = document.getElementById('welcomeScreen');

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


async function analyzeAttachment(file, kind) {
  document.getElementById('attachDropdown').classList.remove('open');

  const defaultPrompt = kind === 'image'
    ? 'Подробно опиши изображение, распознай текст на нём и объясни важные детали.'
    : 'Проанализируй этот файл и объясни главное. Если это код — найди ошибки и предложи исправления.';

  const userDesc = prompt(
    kind === 'image'
      ? '📷 Что сделать с изображением?\n\nОставь пустым — AI сам распознает изображение и текст.'
      : '📁 Что сделать с файлом?\n\nМожно написать: найди ошибки, сделай резюме, объясни код и т.д.',
    ''
  );
  if (userDesc === null) return;

  appendMessage('user', \${kind === 'image' ? '📷' : '📁'} + ' ' + file.name + (userDesc ? '\n💬 ' + userDesc : ''));
  showTyping();

  const formData = new FormData();
  formData.append('file', file);
  formData.append('description', userDesc.trim() || defaultPrompt);

  try {
    const response = await fetch('/api/attachments/analyze', {
      method: 'POST',
      body: formData
    });
    const raw = await response.text();
    let data;
    try { data = JSON.parse(raw); }
    catch (_) { throw new Error('Сервер вернул не JSON (HTTP ' + response.status + ')'); }

    if (!response.ok || data.error) {
      throw new Error(data.error || ('Ошибка обработки файла: HTTP ' + response.status));
    }

    removeTyping();
    appendMessage('ai', data.result || 'Анализ завершён, но ответ пустой.');
  } catch (error) {
    removeTyping();
    appendMessage('ai', '❌ ' + (error.message || 'Ошибка анализа вложения'));
  }
}

function attachImage() {
  const el = document.createElement('input');
  el.type = 'file';
  el.accept = 'image/*';
  el.multiple = false;
  el.onchange = () => { if (el.files[0]) analyzeAttachment(el.files[0], 'image'); };
  el.click();
}

function attachDocument() {
  const el = document.createElement('input');
  el.type = 'file';
  el.accept = [
    '.txt','.json','.csv','.tsv','.py','.js','.ts','.jsx','.tsx','.html','.htm',
    '.css','.md','.xml','.yaml','.yml','.log','.ini','.cfg','.conf','.sql',
    '.sh','.bash','.java','.c','.cpp','.h','.hpp','.go','.rs','.php',
    '.pdf','.docx','.xlsx','.xlsm','.pptx'
  ].join(',');
  el.multiple = false;
  el.onchange = () => { if (el.files[0]) analyzeAttachment(el.files[0], 'document'); };
  el.click();
}

function pickMediaFile(accept, callback) {
  document.getElementById('attachDropdown').classList.remove('open');
  const el = document.createElement('input');
  el.type = 'file';
  el.accept = accept;
  el.onchange = () => { if (el.files[0]) callback(el.files[0]); };
  el.click();
}

function attachAudio() {
  pickMediaFile('audio/*', (file) => {
    appendMessage('user', `🎧 ${file.name}`);
    const data = new FormData();
    data.append('file', file);
    showTyping();
    fetch('/api/media/transcribe', {method: 'POST', body: data})
      .then((response) => response.json())
      .then((result) => {
        removeTyping();
        if (result.error) appendMessage('ai', '❌ ' + result.error);
        else {
          input.value = result.text || '';
          autoResize(input);
          sendBtn.disabled = !input.value.trim();
          appendMessage('ai', `🎧 **Расшифровка ${file.name}:**\n\n${result.text || 'Текст не распознан.'}`);
        }
      })
      .catch(() => { removeTyping(); appendMessage('ai', '❌ Ошибка загрузки аудио'); });
  });
}

function appendMediaMessage(kind, url, title) {
  const wrap = document.createElement('div');
  wrap.className = 'message ai';
  const media = kind === 'audio'
    ? `<audio controls src="${url}"></audio>`
    : kind === 'video'
      ? `<video controls playsinline preload="metadata" src="${url}"></video>`
      : `<img src="${url}" alt="${escapeHtml(title)}">`;
  wrap.innerHTML = `<div class="msg-avatar">✦</div><div class="msg-body"><div class="msg-name">NovaMind</div><div class="msg-bubble media-bubble"><div>${escapeHtml(title)}</div>${media}<a href="${url}" target="_blank" rel="noopener">Открыть файл</a></div></div>`;
  chatContainer.appendChild(wrap);
  scrollToBottom();
}

function attachVideo() {
  pickMediaFile('video/*', (file) => {
    appendMessage('user', `🎬 ${file.name}`);
    const data = new FormData();
    data.append('file', file);
    showTyping();
    fetch('/api/media/upload', {method: 'POST', body: data})
      .then((response) => response.json())
      .then((result) => {
        removeTyping();
        if (result.error) appendMessage('ai', '❌ ' + result.error);
        else appendMediaMessage('video', result.url, `Видео: ${file.name}`);
      })
      .catch(() => { removeTyping(); appendMessage('ai', '❌ Ошибка загрузки видео'); });
  });
}

let mediaKind = 'image';
function openMediaStudio() {
  const modal = document.getElementById('mediaModal');
  modal.classList.add('open');
  modal.setAttribute('aria-hidden', 'false');
  document.getElementById('mediaPrompt').focus();
}
function closeMediaStudio() {
  const modal = document.getElementById('mediaModal');
  modal.classList.remove('open');
  modal.setAttribute('aria-hidden', 'true');
}
function setMediaKind(kind) {
  mediaKind = kind;
  document.querySelectorAll('.media-tab').forEach((tab) => tab.classList.toggle('active', tab.dataset.mediaKind === kind));
  document.getElementById('mediaGenerateBtn').textContent = kind === 'image' ? 'Создать изображение' : kind === 'audio' ? 'Создать аудио' : 'Создать видео';
  document.getElementById('mediaOptions').classList.toggle('video-options', kind === 'video');
  document.getElementById('mediaVoice').style.display = kind === 'audio' ? '' : 'none';
  document.getElementById('mediaDuration').style.display = kind === 'video' ? '' : 'none';
}
async function generateMedia() {
  const prompt = document.getElementById('mediaPrompt').value.trim();
  if (!prompt) { showNotification('Введите описание для генерации', 'warn'); return; }
  const button = document.getElementById('mediaGenerateBtn');
  const resultBox = document.getElementById('mediaResult');
  button.disabled = true;
  resultBox.textContent = 'Генерация…';
  const endpoint = mediaKind === 'image' ? '/api/media/image' : mediaKind === 'audio' ? '/api/media/tts' : '/api/media/video';
  const body = mediaKind === 'audio' ? {text: prompt, voice: document.getElementById('mediaVoice').value} : mediaKind === 'video' ? {prompt, duration: Number(document.getElementById('mediaDuration').value)} : {prompt};
  try {
    const response = await fetch(endpoint, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || 'Провайдер не вернул результат');
    if (data.url) {
      appendMediaMessage(mediaKind, data.url, mediaKind === 'image' ? 'Сгенерированное изображение' : mediaKind === 'audio' ? 'Сгенерированное аудио' : 'Сгенерированное видео');
      resultBox.textContent = 'Готово';
    } else {
      resultBox.textContent = data.job_id ? `Задача запущена: ${data.job_id}` : 'Провайдер принял запрос, но ещё не вернул файл.';
    }
  } catch (error) {
    resultBox.textContent = error.message;
    showNotification(error.message, 'warn');
  } finally { button.disabled = false; }
}
setMediaKind('image');


// ========== РЕЖИМЫ КОМАНД ==========
function activateMode(mode) {
  inputMode = mode;
  input.placeholder = mode.placeholder;
  input.value = '';
  input.focus();
}

// ========== ОТПРАВКА СООБЩЕНИЙ ==========
async function streamMessage(message) {
  const response = await fetch('/send_stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, reasoning: reasoningOn })
  });

  if (!response.ok) {
    const raw = await response.text();
    let details = `HTTP ${response.status}`;
    try {
      const data = JSON.parse(raw);
      details = data.error || data.message || details;
    } catch (_) {
      // Flask/proxy can return an HTML error page. Never let JSON.parse/html
      // errors hide the real HTTP failure.
      const plain = raw.replace(/<[^>]*>/g, ' ').replace(/\\s+/g, ' ').trim();
      if (plain) details = `${details}: ${plain.slice(0, 500)}`;
    }
    throw new Error(details);
  }
  if (!response.body) throw new Error('Браузер не поддерживает потоковый ответ');

  removeTyping();
  isTyping = true;
  const wrap = document.createElement('div');
  wrap.className = 'message ai';
  wrap.innerHTML = '<div class="msg-avatar">✦</div><div class="msg-body"><div class="msg-name">NovaMind</div><div class="msg-bubble"></div></div>';
  chatContainer.appendChild(wrap);
  const bubble = wrap.querySelector('.msg-bubble');
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let fullReply = '';
  let streamDone = false;

  const consumeLine = (line) => {
    if (!line.trim()) return false;
    const data = JSON.parse(line);
    if (data.error) throw new Error(data.error);
    if (data.token) {
      fullReply += data.token;
      bubble.innerHTML = formatContent(fullReply);
      scrollToBottom();
    }
    if (data.done) streamDone = true;
    return streamDone;
  };

  try {
    while (true) {
      const {value, done} = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), {stream: !done});
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';
      for (const line of lines) {
        consumeLine(line);
      }
      if (done || streamDone) break;
    }
    if (buffer.trim()) consumeLine(buffer);
    if (!fullReply) throw new Error('AI не вернул текст ответа');
    historyAddMessage('ai', fullReply);
    return fullReply;
  } finally {
    isTyping = false;
    sendBtn.disabled = !input.value.trim();
  }
}

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

  // Обычный запрос к ИИ с потоковым выводом
  try {
    await streamMessage(finalMsg);
  } catch (e) {
    // Надёжный fallback: если потоковый endpoint временно недоступен,
    // используем обычный /send и не теряем сообщение пользователя.
    console.warn('Streaming chat failed, using /send fallback:', e);
    try {
      const resp = await fetch('/send', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: finalMsg, reasoning: reasoningOn })
      });
      const data = await resp.json();
      removeTyping();
      if (!resp.ok || data.error) {
        appendMessage('ai', '❌ Ошибка AI: ' + (data.error || ('HTTP ' + resp.status)));
      } else {
        appendMessage('ai', data.reply || 'AI не вернул ответ');
      }
    } catch (fallbackError) {
      removeTyping();
      appendMessage('ai', '❌ Не удалось подключиться к AI: ' + (fallbackError.message || 'неизвестная ошибка'));
    }
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

  if (!historyReplay && (role === 'user' || role === 'ai') && content) {
    historyAddMessage(role, content);
  }
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
  recognition.continuous = true;
  recognition.interimResults = true;
  let finalTranscript = '';
  recognition.onstart = () => {
    isRecording = true;
    voiceBtn.classList.add('recording');
    voiceTooltip.textContent = '● Слушаю... Нажмите для остановки';
  };
  recognition.onresult = (e) => {
    let interimTranscript = '';
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const transcript = e.results[i][0].transcript;
      if (e.results[i].isFinal) finalTranscript += transcript + ' ';
      else interimTranscript += transcript;
    }
    input.value = (finalTranscript + interimTranscript).trim();
    autoResize(input);
    sendBtn.disabled = !input.value.trim();
  };
  recognition.onend = () => {
    // Браузер может завершить сессию сам после паузы — сохраняем уже распознанный текст.
    if (isRecording) {
      isRecording = false;
      voiceBtn.classList.remove('recording');
      voiceTooltip.textContent = 'Готово — проверьте текст';
      recognition = null;
    }
  };
  recognition.onerror = (event) => {
    if (event.error !== 'no-speech' && event.error !== 'aborted') {
      showNotification('Ошибка голосового ввода: ' + event.error, 'warn');
    }
    stopRecording();
  };
  try {
    recognition.start();
  } catch (error) {
    stopRecording();
    showNotification('Не удалось запустить голосовой ввод', 'warn');
  }
}

function stopRecording() {
  const activeRecognition = recognition;
  isRecording = false;
  voiceBtn.classList.remove('recording');
  voiceTooltip.textContent = input.value.trim() ? 'Готово — проверьте текст' : 'Нажмите для записи';
  recognition = null;
  if (activeRecognition) {
    try { activeRecognition.stop(); } catch (_) {}
  }
}

// ========== САЙДБАР ==========
function openSettings() {
  window.location.href = '/settings';
}

function setSidebarOffset(offset, animate = false) {
  const app = document.querySelector('.app');
  const sidebar = document.getElementById('sidebar');
  const overlay = document.getElementById('overlay');
  if (!app || !sidebar) return;

  const width = Math.min(300, Math.max(240, window.innerWidth * 0.82));
  const x = Math.max(0, Math.min(width, Number(offset) || 0));

  app.style.setProperty('--drawer-x', x + 'px');
  app.style.setProperty('--drawer-width', width + 'px');

  if (animate) {
    app.classList.add('drawer-animate');
  } else {
    app.classList.remove('drawer-animate');
  }

  const isOpen = x > width * 0.5;
  sidebar.classList.toggle('open', isOpen);

  if (overlay) {
    overlay.classList.toggle('visible', isOpen);
    overlay.style.opacity = String(Math.min(0.75, (x / width) * 0.75));
    overlay.style.pointerEvents = isOpen ? 'auto' : 'none';
  }
}

function getSidebarOffset() {
  const app = document.querySelector('.app');
  if (!app) return 0;
  const value = parseFloat(getComputedStyle(app).getPropertyValue('--drawer-x'));
  return Number.isFinite(value) ? value : 0;
}

function toggleSidebar() {
  const sidebar = document.getElementById('sidebar');
  if (!sidebar) return;

  // На ПК sidebar постоянно виден.
  if (!window.matchMedia('(max-width: 768px)').matches) return;

  const width = Math.min(300, Math.max(240, window.innerWidth * 0.82));
  const current = getSidebarOffset();
  setSidebarOffset(current > width * 0.5 ? 0 : width, true);
}

function closeSidebar() {
  setSidebarOffset(0, true);
}

function initSidebarSwipe() {
  const app = document.querySelector('.app');
  const sidebar = document.getElementById('sidebar');
  const overlay = document.getElementById('overlay');
  if (!app || !sidebar) return;

  let swipeEnabled = false;

  const enableForViewport = () => {
    swipeEnabled = window.matchMedia('(max-width: 768px)').matches;
    if (!swipeEnabled) {
      app.classList.remove('drawer-animate');
      sidebar.classList.remove('open');
      if (overlay) {
        overlay.classList.remove('visible');
        overlay.style.pointerEvents = 'none';
        overlay.style.opacity = '0';
      }
      app.style.removeProperty('--drawer-x');
      app.style.removeProperty('--drawer-width');
    } else {
      const width = Math.min(300, Math.max(240, window.innerWidth * 0.82));
      app.style.setProperty('--drawer-width', width + 'px');
      if (!app.style.getPropertyValue('--drawer-x')) {
        app.style.setProperty('--drawer-x', '0px');
      }
    }
  };

  enableForViewport();

  let drag = null;

  const begin = (e) => {
    if (!swipeEnabled) return;
    if (e.pointerType === 'mouse' && e.button !== 0) return;

    const open = getSidebarOffset() > 1;
    const x = e.clientX;
    const startedOnSidebar = sidebar.contains(e.target);

    // Closed: start only from the left screen edge.
    // Open: allow closing from anywhere inside the sidebar.
    if (!open && x > 32) return;
    if (open && !startedOnSidebar) return;

    drag = {
      id: e.pointerId,
      startX: x,
      startY: e.clientY,
      startOffset: getSidebarOffset(),
      lastX: x,
      lastTime: performance.now(),
      velocityX: 0,
      horizontal: false
    };

    app.classList.remove('drawer-animate');

    try {
      app.setPointerCapture(e.pointerId);
    } catch (_) {}
  };

  const move = (e) => {
    if (!drag || e.pointerId !== drag.id) return;

    const dx = e.clientX - drag.startX;
    const dy = e.clientY - drag.startY;
    const now = performance.now();
    const dt = Math.max(1, now - drag.lastTime);

    if (!drag.horizontal) {
      if (Math.abs(dx) < 8) return;

      // If the gesture is mainly vertical, leave it to the browser for scrolling.
      if (Math.abs(dy) > Math.abs(dx) * 1.15) {
        drag = null;
        return;
      }

      drag.horizontal = true;
    }

    e.preventDefault();

    const next = Math.max(0, Math.min(
      Math.min(300, Math.max(240, window.innerWidth * 0.82)),
      drag.startOffset + dx
    ));

    drag.velocityX = (e.clientX - drag.lastX) / dt;
    drag.lastX = e.clientX;
    drag.lastTime = now;

    setSidebarOffset(next, false);
  };

  const end = (e) => {
    if (!drag || e.pointerId !== drag.id) return;

    const currentDrag = drag;
    drag = null;

    try {
      app.releasePointerCapture(e.pointerId);
    } catch (_) {}

    if (!currentDrag.horizontal) return;

    const width = Math.min(300, Math.max(240, window.innerWidth * 0.82));
    const dx = e.clientX - currentDrag.startX;
    const current = getSidebarOffset();

    const shouldOpen =
      current > width * 0.5 ||
      dx > 70 ||
      currentDrag.velocityX > 0.45;

    setSidebarOffset(shouldOpen ? width : 0, true);
  };

  app.addEventListener('pointerdown', begin, {passive: false});
  app.addEventListener('pointermove', move, {passive: false});
  app.addEventListener('pointerup', end, {passive: false});
  app.addEventListener('pointercancel', end, {passive: false});

  window.addEventListener('resize', enableForViewport);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initSidebarSwipe);
} else {
  initSidebarSwipe();
}
function setActive(el) {
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  el.classList.add('active');
}

// ========== ЧАТ ==========
function newChat() {
  const store = historyLoad();
  store.activeId = null;
  historySave(store);
  historyReplay = true;
  try {
    chatContainer.innerHTML = '';
    chatContainer.appendChild(welcomeScreen);
    welcomeScreen.style.display = 'flex';
    msgCount = 0;
  } finally {
    historyReplay = false;
  }
  renderChatList();
}
function clearChat() {
  newChat();
  fetch('/api/history/clear', {method: 'DELETE'}).catch(() => {});
}
function shareChat() {
  navigator.clipboard.writeText(window.location.href).then(() => showNotification('Ссылка скопирована', 'success'));
}
function scrollToBottom() {
  setTimeout(() => chatContainer.scrollTo({ top: chatContainer.scrollHeight, behavior: 'smooth' }), 50);
}

// ========== ИСТОРИЯ ЧАТОВ ==========
// Локальная история: сохраняет полноценные диалоги на этом устройстве.
const HISTORY_KEY = 'nova_history_v2';
const MAX_CHATS = 50;
const MAX_MSGS = 60;

function historyLoad() {
  try {
    const parsed = JSON.parse(localStorage.getItem(HISTORY_KEY) || '{"chats":[],"activeId":null}');
    const chats = Array.isArray(parsed.chats) ? parsed.chats
      .filter(chat => chat && chat.id)
      .map(chat => ({
        ...chat,
        title: String(chat.title || 'Новый диалог'),
        messages: Array.isArray(chat.messages) ? chat.messages : [],
        createdAt: Number(chat.createdAt) || Number(chat.updatedAt) || Date.now(),
        updatedAt: Number(chat.updatedAt) || Number(chat.createdAt) || Date.now()
      })) : [];
    const activeId = chats.some(chat => chat.id === parsed.activeId) ? parsed.activeId : null;
    return {chats, activeId};
  } catch (e) {
    return {chats: [], activeId: null};
  }
}

function historySave(store) {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(store));
  } catch (e) {
    console.warn('[History] localStorage unavailable/full');
  }
}

function historyAddMessage(role, content) {
  if (historyReplay || !content || content.length < 1) return;

  const store = historyLoad();
  let chat = store.chats.find(c => c.id === store.activeId);

  if (!chat) {
    const title = content.slice(0, 55) + (content.length > 55 ? '…' : '');
    chat = {
      id: Date.now().toString(36) + Math.random().toString(36).slice(2, 7),
      title,
      messages: [],
      createdAt: Date.now(),
      updatedAt: Date.now()
    };
    store.chats.unshift(chat);
    store.activeId = chat.id;
  }

  chat.messages.push({role, content});
  chat.updatedAt = Date.now();

  if (role === 'user' && chat.messages.filter(m => m.role === 'user').length === 1) {
    chat.title = content.slice(0, 55) + (content.length > 55 ? '…' : '');
  }

  if (chat.messages.length > MAX_MSGS) {
    chat.messages = chat.messages.slice(-MAX_MSGS);
  }
  if (store.chats.length > MAX_CHATS) {
    store.chats = store.chats.slice(0, MAX_CHATS);
  }

  historySave(store);
  renderChatList();
}

function renderChatList() {
  const container = document.getElementById('historyContainer');
  if (!container) return;

  const store = historyLoad();
  const chats = [...(store.chats || [])].sort((a, b) => {
    const updatedDiff = (Number(b.updatedAt) || 0) - (Number(a.updatedAt) || 0);
    return updatedDiff || String(b.id).localeCompare(String(a.id));
  });
  const active = store.activeId;
  container.innerHTML = '';

  if (!chats.length) {
    container.innerHTML = '<div class="history-empty">💬<br>Начни диалог —<br>он появится здесь</div>';
    return;
  }

  const now = new Date();
  const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const yesterdayStart = todayStart - 86400000;
  const weekStart = todayStart - 6 * 86400000;
  const groups = {'Сегодня': [], 'Вчера': [], 'Последние 7 дней': [], 'Ранее': []};

  for (const chat of chats) {
    const updatedAt = Number(chat.updatedAt) || Date.now();
    if (updatedAt >= todayStart) groups['Сегодня'].push(chat);
    else if (updatedAt >= yesterdayStart) groups['Вчера'].push(chat);
    else if (updatedAt >= weekStart) groups['Последние 7 дней'].push(chat);
    else groups['Ранее'].push(chat);
  }

  for (const [label, group] of Object.entries(groups)) {
    if (!group.length) continue;

    const heading = document.createElement('div');
    heading.className = 'history-group-label';
    heading.textContent = label;
    container.appendChild(heading);

    for (const chat of group) {
      const item = document.createElement('div');
      item.className = 'history-item' + (chat.id === active ? ' active' : '');
      item.title = chat.title || 'Новый диалог';

      const updated = new Date(Number(chat.updatedAt) || Number(chat.createdAt) || Date.now());
      const date = updated.toLocaleDateString('ru-RU', {day: '2-digit', month: '2-digit', year: 'numeric'});
      const time = updated.toLocaleTimeString('ru-RU', {hour:'2-digit', minute:'2-digit'});
      const count = (chat.messages || []).length;

      item.innerHTML = `
        <div class="history-item-icon">💬</div>
        <div class="history-item-body">
          <div class="history-item-title">${escapeHtml(chat.title || 'Новый диалог')}</div>
          <div class="history-item-meta">
            <span class="history-item-datetime">${date} · ${time}</span>
            <span class="history-item-count">${count} сообщ.</span>
          </div>
        </div>
        <button class="hist-del-btn" type="button" title="Удалить чат">×</button>
      `;

      item.addEventListener('click', (e) => {
        if (e.target.closest('.hist-del-btn')) return;
        loadChat(chat.id);
      });

      item.querySelector('.hist-del-btn').addEventListener('click', (e) => {
        e.stopPropagation();
        deleteChat(chat.id);
      });

      container.appendChild(item);
    }
  }
}

function loadChat(chatId) {
  const store = historyLoad();
  const chat = store.chats.find(c => c.id === chatId);
  if (!chat) return;

  store.activeId = chatId;
  historySave(store);

  historyReplay = true;
  try {
    chatContainer.innerHTML = '';
    for (const msg of (chat.messages || [])) {
      appendMessage(msg.role === 'user' ? 'user' : 'ai', msg.content);
    }
    if (!chat.messages.length) {
      chatContainer.appendChild(welcomeScreen);
      welcomeScreen.style.display = 'flex';
    }
  } finally {
    historyReplay = false;
  }

  msgCount = (chat.messages || []).length;
  renderChatList();
  closeSidebar();
  scrollToBottom();
}

function deleteChat(chatId) {
  const store = historyLoad();
  store.chats = (store.chats || []).filter(c => c.id !== chatId);
  if (store.activeId === chatId) store.activeId = null;
  historySave(store);
  renderChatList();
}

function startNewChat() {
  newChat();
  closeSidebar();
  if (input) input.focus();
}

function loadChatList() {
  renderChatList();
  return Promise.resolve();
}

(function initChatHistory() {
  const run = () => renderChatList();
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run);
  else setTimeout(run, 50);
})();

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
