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

  appendMessage('user', `${kind === 'image' ? '📷' : '📁'} ${file.name}${userDesc ? '\n💬 ' + userDesc : ''}`);
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

// ══════════════════════════════════════════
// ЕДИНАЯ ПАНЕЛЬ МЕДИА-МОДЕЛЕЙ (изображения / аудио / видео)
// Одна и та же панель открывается из верхней панели чата и кнопкой «Медиа».
// ══════════════════════════════════════════
let activeMediaModel = null;      // выбранная модель (объект из /api/media/selection)
let pickerType = 'all';
let pickerModels = [];
let mediaSearchTimer = null;

const MEDIA_KIND_ICON = {image: '🖼', audio: '🔊', video: '🎬'};
const PRICING_BADGE = {
  free:    {cls: 'free',    label: 'FREE'},
  trial:   {cls: 'trial',   label: 'ПРОБНЫЕ КРЕДИТЫ'},
  paid:    {cls: 'paid',    label: 'PAID'},
  unknown: {cls: 'unknown', label: 'ЦЕНА НЕ ПОДТВЕРЖДЕНА'},
};

function openMediaPicker() {
  const modal = document.getElementById('mediaPicker');
  if (!modal) return;
  modal.classList.add('open');
  modal.setAttribute('aria-hidden', 'false');
  loadMediaModels(false);
  setTimeout(() => document.getElementById('mediaSearch')?.focus(), 60);
}

function closeMediaPicker() {
  const modal = document.getElementById('mediaPicker');
  if (!modal) return;
  modal.classList.remove('open');
  modal.setAttribute('aria-hidden', 'true');
}

// Кнопка «Медиа» и кнопка в верхней панели открывают один и тот же интерфейс.
function openMediaStudio() { openMediaPicker(); }
function closeMediaStudio() { closeMediaPicker(); }

function setPickerType(type) {
  pickerType = type;
  document.querySelectorAll('[data-picker-type]').forEach((tab) => {
    tab.classList.toggle('active', tab.dataset.pickerType === type);
  });
  loadMediaModels(false);
}

function onMediaSearchInput() {
  window.clearTimeout(mediaSearchTimer);
  mediaSearchTimer = window.setTimeout(() => loadMediaModels(false), 250);
}

async function loadMediaModels(force = false) {
  const list = document.getElementById('pickerList');
  const meta = document.getElementById('pickerMeta');
  if (!list) return;
  list.innerHTML = '<div class="picker-empty">Проверяю подключённых провайдеров…</div>';
  meta.textContent = 'Запрос каталога…';
  const params = new URLSearchParams({
    type: pickerType,
    q: (document.getElementById('mediaSearch')?.value || '').trim(),
    include_trial: '1',
  });
  if (document.getElementById('mediaIncludePaid')?.checked) params.set('include_paid', '1');
  if (force) params.set('refresh', '1');
  try {
    const response = await fetch('/api/media/models?' + params.toString());
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || ('HTTP ' + response.status));
    pickerModels = data.models || [];
    meta.textContent = `${data.count} модел. · бесплатных: ${data.free_count} · ` +
      `пробные кредиты: ${data.trial_count} · цены сверены ${data.pricing_verified_at || '—'}`;
    renderPickerModels();
  } catch (error) {
    meta.textContent = error.message;
    list.innerHTML = `<div class="picker-empty error">Не удалось загрузить каталог: ${escapeHtml(error.message)}</div>`;
  }
}

function renderPickerModels() {
  const list = document.getElementById('pickerList');
  if (!list) return;
  const includeTrial = document.getElementById('mediaIncludeTrial')?.checked !== false;
  const models = pickerModels.filter((m) => includeTrial || m.pricing_status !== 'trial');
  if (!models.length) {
    list.innerHTML = '<div class="picker-empty">По этим фильтрам моделей нет. ' +
      'Для видео бесплатных API сейчас не подтверждено — включите «Платные» или подключите свой endpoint в .env.</div>';
    return;
  }
  list.innerHTML = models.map(pickerCard).join('');
  document.querySelectorAll('[data-pick-model]').forEach((button) => {
    button.addEventListener('click', () => pickMediaModel(button.dataset));
  });
}

function pickerCard(model) {
  const badge = PRICING_BADGE[model.pricing_status] || PRICING_BADGE.unknown;
  const selected = activeMediaModel && activeMediaModel.provider === model.provider && activeMediaModel.model === model.id;
  const connected = model.provider_connected;
  const stateLabel = selected ? '✓ Выбрана'
    : !connected ? 'Нет ключа в .env'
    : model.pricing_status === 'free' ? 'Выбрать'
    : model.pricing_status === 'trial' ? 'Выбрать (кредиты)'
    : model.pricing_status === 'paid' ? 'Выбрать (платно)'
    : 'Выбрать (цена неизвестна)';
  return `<article class="picker-card ${selected ? 'selected' : ''} ${connected ? '' : 'disconnected'}">
    <div class="picker-card-top">
      <span class="picker-kind">${MEDIA_KIND_ICON[model.media_type] || '✦'}</span>
      <span class="picker-badge ${badge.cls}">${badge.label}</span>
      <span class="picker-provider">${escapeHtml(model.provider_name || model.provider)}</span>
      ${model.in_provider_catalog === false ? '<span class="picker-badge unknown">НЕТ В КАТАЛОГЕ</span>' : ''}
    </div>
    <h3>${escapeHtml(model.name || model.id)}</h3>
    <div class="picker-id"><code>${escapeHtml(model.id)}</code></div>
    <p>${escapeHtml(model.description || '')}</p>
    <div class="picker-note">${escapeHtml(model.pricing_note || '')}</div>
    ${model.pricing_source ? `<div class="picker-src">Источник цены: ${escapeHtml(model.pricing_source)}</div>` : ''}
    <button class="picker-select" type="button" data-pick-model="${escapeHtml(model.id)}"
      data-provider="${escapeHtml(model.provider)}" data-pricing="${escapeHtml(model.pricing_status)}"
      data-media-type="${escapeHtml(model.media_type || '')}" ${connected ? '' : 'disabled'}>${stateLabel}</button>
  </article>`;
}

async function pickMediaModel(dataset) {
  const pricing = dataset.pricing;
  const confirm = {};
  if (pricing === 'trial') {
    const ok = window.confirm('Модель доступна только за счёт пробных кредитов провайдера.\n' +
      'Генерация может израсходовать эти кредиты. Продолжить?');
    if (!ok) return;
    confirm.trial = true;
  } else if (pricing === 'paid') {
    const ok = window.confirm('Это платная модель: бесплатный API для неё не подтверждён.\n' +
      'Каждая генерация будет оплачена по тарифу провайдера. Использовать?');
    if (!ok) return;
    confirm.paid = true;
  } else if (pricing === 'unknown') {
    const ok = window.confirm('Цену этой модели подтвердить не удалось (свой endpoint из .env).\n' +
      'Стоимость определяет ваш провайдер. Использовать?');
    if (!ok) return;
    confirm.unknown = true;
  }
  try {
    const response = await fetch('/api/media/select', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({provider: dataset.provider, model: dataset.pickModel, confirm}),
    });
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || ('HTTP ' + response.status));
    activeMediaModel = data.selection;
    updateMediaChip();
    showNotification(`Медиа: ${data.selection.name}`, 'success');
    document.getElementById('pickerHint').textContent =
      `Выбрано: ${data.selection.name}. Напишите промпт в обычном поле чата.`;
    renderPickerModels();
  } catch (error) {
    showNotification(error.message, 'warn');
  }
}

async function clearMediaModel() {
  try {
    await fetch('/api/media/select', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({clear: true}),
    });
  } catch (_) {}
  activeMediaModel = null;
  updateMediaChip();
  showNotification('Обычный текстовый чат', 'info');
  renderPickerModels();
}

async function refreshMediaSelection() {
  try {
    const response = await fetch('/api/media/selection');
    const data = await response.json();
    activeMediaModel = data.selection || null;
  } catch (_) {
    activeMediaModel = null;
  }
  updateMediaChip();
}

function updateMediaChip() {
  const label = document.getElementById('mediaModelLabel');
  const bar = document.getElementById('mediaActiveBar');
  const text = document.getElementById('mediaActiveText');
  const mediaBtn = document.getElementById('btn-media');
  const modelButton = document.getElementById('btnMediaModel');
  if (activeMediaModel) {
    const icon = MEDIA_KIND_ICON[activeMediaModel.media_type] || '🎨';
    if (label) label.textContent = `${icon} ${activeMediaModel.name}`;
    if (bar) bar.hidden = false;
    if (text) {
      const badge = PRICING_BADGE[activeMediaModel.pricing_status] || PRICING_BADGE.unknown;
      text.textContent = `${icon} ${activeMediaModel.name} · ${activeMediaModel.provider_name} · ${badge.label}`;
    }
    if (input) input.placeholder = `Опишите, что создать (${activeMediaModel.media_type}: ${activeMediaModel.name})…`;
    mediaBtn?.classList.add('active');
    modelButton?.classList.add('active');
  } else {
    if (label) label.textContent = 'Медиа';
    if (bar) bar.hidden = true;
    if (input) input.placeholder = 'Напишите сообщение...';
    mediaBtn?.classList.remove('active');
    modelButton?.classList.remove('active');
  }
}

// ══════════════════════════════════════════
// ВЫВОД МЕДИА В ПЕРЕПИСКЕ (изображение / аудиоплеер / видеоплеер + скачивание)
// ══════════════════════════════════════════
function mediaBubbleHtml(payload) {
  const kind = payload.kind || 'image';
  const url = payload.url || '';
  const title = payload.title || (kind === 'image' ? 'Изображение' : kind === 'audio' ? 'Аудио' : 'Видео');
  const provider = payload.provider ? ` · ${escapeHtml(payload.provider)}` : '';
  const seconds = payload.elapsed_ms ? ` · ${(payload.elapsed_ms / 1000).toFixed(1)} c` : '';
  let player;
  if (kind === 'audio') {
    player = `<audio controls preload="metadata" src="${escapeHtml(url)}"></audio>`;
  } else if (kind === 'video') {
    player = `<video controls playsinline preload="metadata" src="${escapeHtml(url)}"></video>`;
  } else {
    player = `<img src="${escapeHtml(url)}" alt="${escapeHtml(title)}" loading="lazy">`;
  }
  const downloadName = payload.filename || `novamind_${kind}`;
  return `<div class="msg-name">NovaMind</div>
    <div class="msg-bubble media-bubble">
      <div class="media-title">${escapeHtml(title)}${provider}${seconds}</div>
      ${player}
      <div class="media-actions">
        <a class="media-download" href="${escapeHtml(url)}" download="${escapeHtml(downloadName)}">⬇ Скачать</a>
        <a href="${escapeHtml(url)}" target="_blank" rel="noopener">Открыть в новой вкладке</a>
      </div>
    </div>`;
}

function appendMediaResult(payload, options = {}) {
  hideWelcome();
  const wrap = document.createElement('div');
  wrap.className = 'message ai';
  wrap.innerHTML = `<div class="msg-avatar">✦</div><div class="msg-body">${mediaBubbleHtml(payload)}</div>`;
  chatContainer.appendChild(wrap);
  scrollToBottom();
  if (!historyReplay && !options.skipHistory && payload.url) {
    historyAddMessage('ai', 'MEDIA_RESULT:' + JSON.stringify(payload));
  }
  return wrap;
}

function appendMediaJobCard(job) {
  hideWelcome();
  const wrap = document.createElement('div');
  wrap.className = 'message ai';
  wrap.innerHTML = `<div class="msg-avatar">✦</div><div class="msg-body">
    <div class="msg-name">NovaMind</div>
    <div class="msg-bubble media-bubble media-job">
      <div class="media-title">🎬 Задача видео у провайдера</div>
      <div class="media-status" data-role="status">Статус: ${escapeHtml(job.state || 'queued')}</div>
      <div class="media-status-sub" data-role="sub">ID задачи: ${escapeHtml(job.job_id || '')}</div>
      <div class="media-status-log" data-role="log"></div>
    </div></div>`;
  chatContainer.appendChild(wrap);
  scrollToBottom();
  return wrap;
}

async function pollMediaJob(jobId, card) {
  const statusEl = card.querySelector('[data-role="status"]');
  const logEl = card.querySelector('[data-role="log"]');
  for (let attempt = 0; attempt < 120; attempt++) {
    await new Promise((resolve) => setTimeout(resolve, 5000));
    let data;
    try {
      const response = await fetch('/api/media/jobs/' + encodeURIComponent(jobId));
      data = await response.json();
      if (!response.ok) throw new Error(data.error || ('HTTP ' + response.status));
    } catch (error) {
      if (statusEl) statusEl.textContent = 'Не удалось опросить задачу: ' + error.message;
      continue;
    }
    if (statusEl) statusEl.textContent = `Статус провайдера: ${data.state} (опросов: ${data.polls})`;
    if (logEl && (data.events || []).length) {
      logEl.textContent = data.events[data.events.length - 1].status;
    }
    if (data.state === 'succeeded' && data.media) {
      card.remove();
      const payload = {...data.media, kind: 'video', title: data.model || 'Видео', provider: data.provider};
      appendMediaResult(payload);
      return;
    }
    if (['failed', 'cancelled', 'error'].includes(data.state)) {
      if (statusEl) statusEl.textContent = '❌ ' + (data.error || 'Провайдер отменил задачу');
      return;
    }
  }
  if (statusEl) statusEl.textContent = 'Опрос остановлен: задача всё ещё выполняется у провайдера.';
}

// Отправка промпта в медиа-движок с реальными статусами выполнения.
async function sendMediaMessage(message) {
  const response = await fetch('/send_stream', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({message, media: true, reasoning: false}),
  });

  if (!response.ok) {
    const raw = await response.text();
    let details = `HTTP ${response.status}`;
    try { details = JSON.parse(raw).error || details; } catch (_) { details += ': ' + raw.slice(0, 300); }
    throw new Error(details);
  }
  if (!response.body) throw new Error('Браузер не поддерживает потоковый ответ');

  removeTyping();
  isTyping = true;
  const started = Date.now();
  const wrap = document.createElement('div');
  wrap.className = 'message ai';
  wrap.innerHTML = '<div class="msg-avatar">✦</div><div class="msg-body"><div class="msg-name">NovaMind</div>' +
    '<div class="msg-bubble media-bubble media-job"><div class="media-status" data-role="status">Подключение…</div>' +
    '<div class="media-status-log" data-role="log"></div></div></div>';
  chatContainer.appendChild(wrap);
  const statusEl = wrap.querySelector('[data-role="status"]');
  const logEl = wrap.querySelector('[data-role="log"]');

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let finished = false;

  const consumeLine = (line) => {
    if (!line.trim()) return;
    const event = JSON.parse(line);
    if (event.error) throw new Error(event.error);
    if (event.status === 'started' || event.status === 'progress') {
      const seconds = ((Date.now() - started) / 1000).toFixed(1);
      if (statusEl) statusEl.textContent = `⏳ ${event.message} · ${seconds} c`;
      if (logEl && event.model) logEl.textContent = `${event.model.provider_name || ''} · ${event.model.pricing_label || ''}`;
      scrollToBottom();
      return;
    }
    if (event.media) {
      finished = true;
      wrap.remove();
      if (event.media.job_id) {
        const card = appendMediaJobCard(event.media);
        pollMediaJob(event.media.job_id, card);
      } else {
        appendMediaResult(event.media);
      }
      return;
    }
    if (event.done) finished = true;
  };

  try {
    while (true) {
      const {value, done} = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), {stream: !done});
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';
      for (const line of lines) consumeLine(line);
      if (done || finished) break;
    }
    if (buffer.trim()) consumeLine(buffer);
  } finally {
    isTyping = false;
    sendBtn.disabled = !input.value.trim();
  }
}


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

  // Если выбрана медиа-модель — промпт уходит в движок генерации
  if (activeMediaModel) {
    try {
      await sendMediaMessage(finalMsg);
    } catch (e) {
      removeTyping();
      appendMessage('ai', '❌ Ошибка генерации: ' + (e.message || 'неизвестная ошибка'));
    }
    return;
  }

  // Если включён АВТО ПОИСК — SSE с реальными шагами в чате
  if (autoSearchOn) {
    try {
      const done = await autoSearchSSE(finalMsg);
      if (done) return;
      // done=false значит поиск не нужен, продолжаем обычный путь
    } catch (e) {
      console.error('Auto search SSE error:', e);
      // При ошибке — продолжаем обычную отправку
    }
  }

  // Если включён ручной поиск — используем web_search_groq
  if (webSearchOn) {
    try {
      const resp = await fetch('/api/web_search_groq', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: finalMsg })
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
// ========== АВТО-ПОИСК С РЕАЛЬНЫМИ ШАГАМИ (SSE) ==========
async function autoSearchSSE(message) {
  // Создаём пузырь "поиска" в чате
  const searchWrap = document.createElement('div');
  searchWrap.className = 'message ai';
  searchWrap.id = 'searchProgressMsg';
  searchWrap.innerHTML = `
    <div class="msg-avatar">🔍</div>
    <div class="msg-body">
      <div class="msg-name">Поиск</div>
      <div class="msg-bubble search-progress-bubble">
        <div id="searchSteps" style="display:flex;flex-direction:column;gap:6px;font-size:13px;"></div>
      </div>
    </div>`;
  messages.appendChild(searchWrap);
  messages.scrollTop = messages.scrollHeight;

  const stepsEl = document.getElementById('searchSteps');

  function addStep(icon, text) {
    const el = document.createElement('div');
    el.style.cssText = 'display:flex;align-items:center;gap:8px;padding:4px 0;border-bottom:1px solid rgba(255,255,255,.06);';
    el.innerHTML = `<span style="font-size:16px;min-width:20px">${icon}</span><span style="color:var(--text-secondary,#aaa)">${escapeHtml(text)}</span>`;
    stepsEl.appendChild(el);
    messages.scrollTop = messages.scrollHeight;
  }

  return new Promise((resolve) => {
    const evtSource = new EventSource('/api/auto_search_stream?' + new URLSearchParams({message}));
    // Используем POST через fetch+ReadableStream т.к. EventSource не поддерживает POST
    evtSource.close();

    // Используем fetch + ReadableStream для SSE с POST
    fetch('/api/auto_search_stream', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message})
    }).then(async resp => {
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, {stream: true});
        const lines = buffer.split('\n');
        buffer = lines.pop();

        for (const line of lines) {
          if (!line.trim()) continue;
          let eventType = 'message', data = '';
          if (line.startsWith('event: ')) {
            eventType = line.slice(7).trim();
          } else if (line.startsWith('data: ')) {
            data = line.slice(6);
            try {
              const payload = JSON.parse(data);
              if (eventType === 'step') {
                addStep(payload.icon || '•', payload.text || '');
              } else if (eventType === 'result') {
                // Убираем прогресс, показываем результат
                searchWrap.remove();
                removeTyping();
                if (payload.reply) {
                  appendMessage('ai', payload.reply);
                  // Показываем источники если есть
                  if (payload.sources && payload.sources.length > 0) {
                    const srcHtml = payload.sources.map((s, i) =>
                      `<a href="${escapeHtml(s.url)}" target="_blank" rel="noopener" style="color:#10b981;font-size:12px;display:block;margin-top:4px">` +
                      `[${i+1}] ${escapeHtml(s.title || s.url)}</a>`
                    ).join('');
                    const srcWrap = document.createElement('div');
                    srcWrap.className = 'message ai';
                    srcWrap.innerHTML = `<div class="msg-avatar">🔗</div><div class="msg-body"><div class="msg-bubble" style="background:rgba(16,185,129,.08);padding:10px 14px">${srcHtml}</div></div>`;
                    messages.appendChild(srcWrap);
                    messages.scrollTop = messages.scrollHeight;
                  }
                }
                resolve(true);
              } else if (eventType === 'error') {
                addStep('❌', payload.text || 'Ошибка');
              } else if (eventType === 'done') {
                if (document.getElementById('searchProgressMsg')) {
                  searchWrap.remove();
                  removeTyping();
                }
                resolve(payload.searched === false ? false : true);
              }
            } catch(e) { /* ignore parse errors */ }
          }
        }
      }
      resolve(false);
    }).catch(e => {
      searchWrap.remove();
      console.error('SSE fetch error:', e);
      resolve(false);
    });
  });
}

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

  // ══ МЕДИА-РЕЗУЛЬТАТ (изображение / аудиоплеер / видеоплеер) ══
  if (isAI && content.startsWith('MEDIA_RESULT:')) {
    try {
      const payload = JSON.parse(content.replace('MEDIA_RESULT:', ''));
      if (payload.url) {
        wrap.innerHTML = `<div class="msg-avatar">✦</div><div class="msg-body">${mediaBubbleHtml(payload)}</div>`;
        chatContainer.appendChild(wrap);
        scrollToBottom();
        return;
      }
    } catch (e) {}
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

  // Таблицы — скролл горизонтально с подсказкой
  html = html.replace(/(\|[^\n]+\|\n\|[-| :]+\|\n(?:\|[^\n]+\|\n?)*)/g, (match) => {
    const rows = match.trim().split('\n');
    const colCount = (rows[0].match(/\|/g) || []).length - 1;
    const needsScroll = colCount > 3;
    let tableHtml = '<table>';
    rows.forEach((row, index) => {
      const cells = row.split('|').filter(c => c.trim() !== '');
      if (index === 1 && cells.every(c => /^[-| :]+$/.test(c))) return;
      const tag = index === 0 ? 'th' : 'td';
      // Определяем выравнивание по разделителю
      const alignRow = rows[1] ? rows[1].split('|').filter(c => c.trim() !== '') : [];
      tableHtml += '<tr>';
      cells.forEach((cell, ci) => {
        const sep = alignRow[ci] || '';
        const align = sep.startsWith(':') && sep.endsWith(':') ? 'center'
                    : sep.endsWith(':') ? 'right' : 'left';
        tableHtml += `<${tag} style="text-align:${align}">${cell.trim()}</${tag}>`;
      });
      tableHtml += '</tr>';
    });
    tableHtml += '</table>';
    const hint = needsScroll
      ? '<div class="tbl-scroll-hint show">← прокрути вправо →</div>'
      : '';
    return `<div class="tbl-wrap">${tableHtml}</div>${hint}`;
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
refreshMediaSelection();
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') closeMediaPicker();
});
