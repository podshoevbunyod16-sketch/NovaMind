/* ══════════════════════════════════════════════════════════════════
   NovaMind · Khirad — основной скрипт чата (Liquid Glass edition)

   Что нового:
   • AssistantTurn — «живой» пузырь ИИ: сцена поиска, шаги, панель
     рассуждений, потоковая печать ответа и источники внизу пузыря.
   • Поиск и рассуждения работают по потоку NDJSON (без подвисаний).
   • Командная панель Ctrl+K, темы стекла, стоп-генерации, TTS,
     индикатор состояния ИИ и поисковых бэкендов.
   ══════════════════════════════════════════════════════════════════ */

// ========== СОСТОЯНИЕ ==========
let isRecording    = false;
let recognition    = null;
let isTyping       = false;
let webSearchOn    = localStorage.getItem('nova_web_search') === '1';
let reasoningOn    = localStorage.getItem('nova_reasoning') === '1';
let autoSearchOn   = false;
let currentMode    = 'chat';
let msgCount       = 0;
let selectedModelName = 'Nova Ultra';
let inputMode      = null;
let historyReplay  = false;
let activeAbort    = null;      // AbortController текущего запроса
let lastUserMessage = '';
let aiStatus       = null;

// Тема одна — Aurora. Остальные палитры убраны из оборота,
// чтобы интерфейс везде выглядел одинаково.
const THEMES = ['aurora'];
const ONLY_THEME = 'aurora';

// ========== DOM-ЭЛЕМЕНТЫ ==========
const input         = document.getElementById('chat-input');
const sendBtn       = document.getElementById('sendBtn');
const voiceBtn      = document.getElementById('voiceBtn');
const voiceTooltip  = document.getElementById('voiceTooltip');
const chatContainer = document.getElementById('chatContainer');
const welcomeScreen = document.getElementById('welcomeScreen');

/* ══════════════════════════════════════════════════════════════════
   КОРОТКИЕ НАЗВАНИЯ МОДЕЛЕЙ
   Имена моделей бывают на пол-экрана и выталкивают кнопки из шапки.
   Показываем первые два слова (или первые буквы) + многоточие,
   а полное название оставляем в подсказке по наведению.
   ══════════════════════════════════════════════════════════════════ */
function shortModelName(name, max = 26) {
  const text = String(name ?? '').trim();
  if (!text || text.length <= max) return text;
  const words = text.split(/\s+/);
  if (words.length > 2) {
    const head = words.slice(0, 2).join(' ');
    if (head.length + 1 <= max) return head + '…';
  }
  return text.slice(0, max).replace(/[\s·,—-]+$/, '') + '…';
}

/** Записывает короткое имя в элемент, полное — в title. */
function setShortText(el, text, max) {
  if (!el) return;
  const full = String(text ?? '').trim();
  el.textContent = shortModelName(full, max);
  if (full) el.title = full;
  else el.removeAttribute('title');
}

// ══════════════════════════════════════════
// ЗАЩИТА ОТ КОПИРОВАНИЯ ИНТЕРФЕЙСА
// ══════════════════════════════════════════
(function initCopyProtection() {
  document.addEventListener('contextmenu', function (e) {
    if (!e.target.closest('.msg-bubble')) {
      e.preventDefault();
      return false;
    }
  });

  document.addEventListener('keydown', function (e) {
    const selection = window.getSelection();
    const insideMessage =
      selection?.anchorNode?.parentElement?.closest('.msg-bubble') ||
      document.activeElement?.closest('.msg-bubble');
    const inField = ['INPUT', 'TEXTAREA'].includes(document.activeElement?.tagName || '');

    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'c' && !insideMessage && !inField) {
      e.preventDefault();
      showNotification('Копирование интерфейса запрещено. Выделите текст в сообщении.', 'warn');
      return false;
    }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'a' && !insideMessage && !inField) {
      e.preventDefault();
      return false;
    }
  });

  document.addEventListener('dragstart', function (e) {
    if (!e.target.closest('.msg-bubble')) {
      e.preventDefault();
      return false;
    }
  });
})();

// ══════════════════════════════════════════
// УВЕДОМЛЕНИЯ (стеклянные тосты)
// ══════════════════════════════════════════
function showNotification(message, type = 'info') {
  const stack = document.getElementById('toastStack');
  const toast = document.createElement('div');
  const icon = { ok: '✅', success: '✅', warn: '⚠️', err: '❌', error: '❌', info: '✦' }[type] || '✦';
  const cls = { ok: 'ok', success: 'ok', warn: 'warn', err: 'err', error: 'err' }[type] || '';
  toast.className = `lg-toast ${cls}`;
  toast.innerHTML = `<span>${icon}</span><span>${escapeHtml(message)}</span>`;
  if (stack) {
    stack.appendChild(toast);
    while (stack.children.length > 4) stack.firstChild.remove();
  } else {
    document.body.appendChild(toast);
  }
  setTimeout(() => {
    toast.classList.add('out');
    setTimeout(() => toast.remove(), 320);
  }, 3000);
}

// ══════════════════════════════════════════
// ТЕМЫ И АНИМАЦИИ
// ══════════════════════════════════════════
function applyTheme(theme) {
  const value = THEMES.includes(theme) ? theme : ONLY_THEME;
  document.documentElement.setAttribute('data-glass-theme', value);
  localStorage.setItem('nova_theme', value);
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', '#070713');
  return value;
}

function toggleCalmMotion() {
  const calm = document.documentElement.getAttribute('data-motion') === 'calm';
  document.documentElement.setAttribute('data-motion', calm ? 'full' : 'calm');
  localStorage.setItem('nova_motion', calm ? 'full' : 'calm');
  document.getElementById('btn-calm')?.classList.toggle('active', !calm);
  showNotification(calm ? 'Анимации включены' : 'Спокойный режим: фоновые анимации выключены', 'info');
}

(function initAppearance() {
  applyTheme(ONLY_THEME);
  const motion = localStorage.getItem('nova_motion') || 'full';
  document.documentElement.setAttribute('data-motion', motion);
  document.getElementById('btn-calm')?.classList.toggle('active', motion === 'calm');
})();

// ========== ПРОВЕРКА АВТОРИЗАЦИИ ==========
if (!localStorage.getItem('nova_user_nick')) {
  window.location.href = '/';
}

// ========== АДМИН-ПАНЕЛЬ И НИКНЕЙМ ==========
(function () {
  const isAdmin = localStorage.getItem('nova_is_admin') === 'true';
  const adminLink = document.getElementById('admin-link');
  const displayNick = document.getElementById('display-nick');
  if (displayNick) {
    displayNick.textContent = localStorage.getItem('nova_user_nick') || 'Пользователь';
  }
  if (isAdmin && adminLink) adminLink.style.display = 'flex';

  const googleAvatar = localStorage.getItem('nova_user_avatar');
  const avatarEl = document.querySelector('.user-avatar');
  if (googleAvatar && avatarEl) {
    avatarEl.innerHTML = `<img src="${googleAvatar}" alt="" style="width:100%;height:100%;border-radius:50%;object-fit:cover;">`;
    avatarEl.style.background = 'none';
  }
})();

// ========== ВЫХОД ==========
function logout() {
  ['nova_user_nick', 'nova_user_code', 'nova_is_admin', 'nova_google_login', 'nova_user_avatar']
    .forEach((key) => localStorage.removeItem(key));
  window.location.href = '/';
}

// ========== ВВОД ТЕКСТА ==========
input.addEventListener('input', () => {
  if (!isTyping) sendBtn.disabled = !input.value.trim();
});

function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 150) + 'px';
}

function handleKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    if (isTyping) { stopGeneration(); return; }
    if (input.value.trim()) sendMessage();
  }
}

// ========== ПЕРЕКЛЮЧАТЕЛИ ==========
function setWebSearch(state, silent = false) {
  webSearchOn = state;
  localStorage.setItem('nova_web_search', state ? '1' : '0');
  const btn = document.getElementById('btn-web-search');
  btn?.classList.toggle('active', state);
  btn?.classList.toggle('is-live', state);
  const label = document.getElementById('searchBtnText');
  if (label) label.textContent = state ? 'Поиск вкл' : 'Поиск';
  btn?.setAttribute('aria-pressed', state ? 'true' : 'false');
  if (!silent) {
    showNotification(state
      ? '🔍 Автопоиск включён — ИИ ищет в интернете через Linux и отвечает с источниками'
      : 'Автопоиск выключен', state ? 'ok' : 'info');
  }
}

function toggleWebSearch() { setWebSearch(!webSearchOn); }

function toggleReasoning() {
  reasoningOn = !reasoningOn;
  localStorage.setItem('nova_reasoning', reasoningOn ? '1' : '0');
  document.getElementById('btn-reasoning')?.classList.toggle('active', reasoningOn);
  showNotification(reasoningOn
    ? '🧠 Режим рассуждения включён — покажу ход мыслей модели'
    : 'Рассуждение выключено', reasoningOn ? 'ok' : 'info');
}

function toggleAutoSearch() {
  autoSearchOn = !autoSearchOn;
  document.getElementById('btn-auto-search')?.classList.toggle('active', autoSearchOn);
  showNotification(autoSearchOn ? '🔍 Авто-поиск включён' : 'Авто-поиск выключен', 'info');
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
  const turn = createAssistantTurn();
  turn.beginIdle('Читаю файл…');

  const formData = new FormData();
  formData.append('file', file);
  formData.append('description', userDesc.trim() || defaultPrompt);

  try {
    const response = await fetch('/api/attachments/analyze', { method: 'POST', body: formData });
    const raw = await response.text();
    let data;
    try { data = JSON.parse(raw); }
    catch (_) { throw new Error('Сервер вернул не JSON (HTTP ' + response.status + ')'); }
    if (!response.ok || data.error) {
      throw new Error(data.error || ('Ошибка обработки файла: HTTP ' + response.status));
    }
    turn.collapseStage();
    turn.appendText(data.result || 'Анализ завершён, но ответ пустой.');
    turn.finish({ model: aiStatus?.model });
  } catch (error) {
    turn.fail(error.message || 'Ошибка анализа вложения');
  }
}

function attachImage() {
  const el = document.createElement('input');
  el.type = 'file';
  el.accept = 'image/*';
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
    const turn = createAssistantTurn();
    turn.beginIdle('Расшифровываю аудио…');
    const data = new FormData();
    data.append('file', file);
    fetch('/api/media/transcribe', { method: 'POST', body: data })
      .then((response) => response.json())
      .then((result) => {
        if (result.error) { turn.fail(result.error); return; }
        input.value = result.text || '';
        autoResize(input);
        sendBtn.disabled = !input.value.trim();
        turn.collapseStage();
        turn.appendText(`**Расшифровка ${file.name}:**\n\n${result.text || 'Текст не распознан.'}`);
        turn.finish({ model: 'whisper' });
      })
      .catch((error) => turn.fail(error.message || 'Ошибка загрузки аудио'));
  });
}

function attachVideo() {
  pickMediaFile('video/*', (file) => {
    appendMessage('user', `🎬 ${file.name}`);
    const turn = createAssistantTurn();
    turn.beginIdle('Загружаю видео…');
    const data = new FormData();
    data.append('file', file);
    fetch('/api/media/upload', { method: 'POST', body: data })
      .then((response) => response.json())
      .then((result) => {
        turn.remove();
        if (result.error) appendMessage('ai', '❌ ' + result.error);
        else appendMediaMessage('video', result.url, `Видео: ${file.name}`);
      })
      .catch(() => { turn.remove(); appendMessage('ai', '❌ Ошибка загрузки видео'); });
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
  wrap.innerHTML = `<div class="msg-avatar">✦</div><div class="msg-body"><div class="msg-name">NovaMind</div><div class="msg-bubble media-bubble"><div class="media-title">${escapeHtml(title)}</div>${media}<div class="media-actions"><a class="media-download" href="${url}" target="_blank" rel="noopener">Открыть файл</a></div></div></div>`;
  chatContainer.appendChild(wrap);
  scrollToBottom();
}

// ══════════════════════════════════════════
// ЕДИНАЯ ПАНЕЛЬ МЕДИА-МОДЕЛЕЙ
// ══════════════════════════════════════════
let activeMediaModel = null;
let pickerType = 'all';
let pickerModels = [];
let mediaSearchTimer = null;

const MEDIA_KIND_ICON = { image: '🖼', audio: '🔊', video: '🎬' };
const PRICING_BADGE = {
  free:    { cls: 'free',    label: 'FREE' },
  trial:   { cls: 'trial',   label: 'ПРОБНЫЕ КРЕДИТЫ' },
  paid:    { cls: 'paid',    label: 'PAID' },
  unknown: { cls: 'unknown', label: 'ЦЕНА НЕ ПОДТВЕРЖДЕНА' },
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
    if (!window.confirm('Модель доступна только за счёт пробных кредитов провайдера.\nГенерация может израсходовать эти кредиты. Продолжить?')) return;
    confirm.trial = true;
  } else if (pricing === 'paid') {
    if (!window.confirm('Это платная модель: бесплатный API для неё не подтверждён.\nКаждая генерация будет оплачена по тарифу провайдера. Использовать?')) return;
    confirm.paid = true;
  } else if (pricing === 'unknown') {
    if (!window.confirm('Цену этой модели подтвердить не удалось (свой endpoint из .env).\nСтоимость определяет ваш провайдер. Использовать?')) return;
    confirm.unknown = true;
  }
  try {
    const response = await fetch('/api/media/select', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider: dataset.provider, model: dataset.pickModel, confirm }),
    });
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || ('HTTP ' + response.status));
    activeMediaModel = data.selection;
    updateMediaChip();
    showNotification(`Медиа: ${data.selection.name}`, 'success');
    const hint = document.getElementById('pickerHint');
    if (hint) hint.textContent = `Выбрано: ${data.selection.name}. Напишите промпт в обычном поле чата.`;
    renderPickerModels();
  } catch (error) {
    showNotification(error.message, 'warn');
  }
}

async function clearMediaModel(quiet = false) {
  try {
    await fetch('/api/media/select', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ clear: true }),
    });
  } catch (_) {}
  activeMediaModel = null;
  updateMediaChip();
  if (!quiet) showNotification('Обычный текстовый чат', 'info');
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
    setShortText(label, `${icon} ${activeMediaModel.name}`, 22);
    if (bar) bar.hidden = false;
    if (text) {
      const badge = PRICING_BADGE[activeMediaModel.pricing_status] || PRICING_BADGE.unknown;
      setShortText(text, `${icon} ${activeMediaModel.name} · ${activeMediaModel.provider_name} · ${badge.label}`, 44);
    }
    if (input && !inputMode) input.placeholder = `Опишите, что создать (${activeMediaModel.media_type}: ${activeMediaModel.name})…`;
    mediaBtn?.classList.add('active');
    modelButton?.classList.add('active');
  } else {
    if (label) { label.textContent = 'Медиа'; label.removeAttribute('title'); }
    if (bar) bar.hidden = true;
    if (input && !inputMode) input.placeholder = 'Напишите сообщение...';
    mediaBtn?.classList.remove('active');
    modelButton?.classList.remove('active');
  }
}

// ══════════════════════════════════════════
// ВЫВОД МЕДИА В ПЕРЕПИСКЕ
// ══════════════════════════════════════════
function mediaBubbleHtml(payload) {
  const kind = payload.kind || 'image';
  const url = payload.url || '';
  const title = payload.title || (kind === 'image' ? 'Изображение' : kind === 'audio' ? 'Аудио' : 'Видео');
  const provider = payload.provider ? ` · ${escapeHtml(payload.provider)}` : '';
  const seconds = payload.elapsed_ms ? ` · ${(payload.elapsed_ms / 1000).toFixed(1)} c` : '';
  let player;
  if (kind === 'audio') player = `<audio controls preload="metadata" src="${escapeHtml(url)}"></audio>`;
  else if (kind === 'video') player = `<video controls playsinline preload="metadata" src="${escapeHtml(url)}"></video>`;
  else player = `<img src="${escapeHtml(url)}" alt="${escapeHtml(title)}" loading="lazy">`;
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
    if (logEl && (data.events || []).length) logEl.textContent = data.events[data.events.length - 1].status;
    if (data.state === 'succeeded' && data.media) {
      card.remove();
      appendMediaResult({ ...data.media, kind: 'video', title: data.model || 'Видео', provider: data.provider });
      return;
    }
    if (['failed', 'cancelled', 'error'].includes(data.state)) {
      if (statusEl) statusEl.textContent = '❌ ' + (data.error || 'Провайдер отменил задачу');
      return;
    }
  }
  if (statusEl) statusEl.textContent = 'Опрос остановлен: задача всё ещё выполняется у провайдера.';
}

async function sendMediaMessage(message) {
  const response = await fetch('/send_stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, media: true, reasoning: false }),
  });

  if (!response.ok) {
    const raw = await response.text();
    let details = `HTTP ${response.status}`;
    try { details = JSON.parse(raw).error || details; } catch (_) { details += ': ' + raw.slice(0, 300); }
    throw new Error(details);
  }
  if (!response.body) throw new Error('Браузер не поддерживает потоковый ответ');

  isTyping = true;
  setSendBusy(true);
  const started = Date.now();
  const wrap = document.createElement('div');
  wrap.className = 'message ai is-working';
  wrap.innerHTML = '<div class="msg-avatar">✦</div><div class="msg-body"><div class="msg-name">NovaMind</div>' +
    '<div class="msg-bubble media-bubble media-job"><div class="media-status" data-role="status">Подключение…</div>' +
    '<div class="lg-bar" style="margin-top:9px"><span></span></div>' +
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
      if (event.media.job_id) pollMediaJob(event.media.job_id, appendMediaJobCard(event.media));
      else appendMediaResult(event.media);
      return;
    }
    if (event.done) finished = true;
  };

  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';
      for (const line of lines) consumeLine(line);
      if (done || finished) break;
    }
    if (buffer.trim()) consumeLine(buffer);
  } finally {
    isTyping = false;
    setSendBusy(false);
  }
}

// ══════════════════════════════════════════════════════════════════
// ASSISTANT TURN — «живой» пузырь ИИ
//
// Жизненный цикл:
//   beginIdle() / beginSearch() → сцена и шаги
//   reasoningToken()            → панель рассуждений
//   collapseStage()             → сцена схлопывается при первом токене
//   appendToken()/appendText()  → печатающийся ответ
//   setSources()                → источники внизу пузыря
//   finish() / fail()           → метаданные, действия, история
// ══════════════════════════════════════════════════════════════════
function sceneMarkup(scene) {
  const particles = Array.from({ length: 14 }, (_, i) => {
    const left = 6 + ((i * 37) % 88);
    const delay = (i % 7) * 0.42;
    const duration = 3.4 + (i % 5) * 0.5;
    return `<i style="left:${left}%;bottom:0;animation-delay:${delay}s;animation-duration:${duration}s"></i>`;
  }).join('');
  return `
    <div class="stage-scene" data-scene="${scene}">
      <div class="stage-radar"></div>
      <div class="stage-orbit o1"></div>
      <div class="stage-orbit o2"></div>
      <div class="stage-orbit o3"></div>
      <div class="stage-core"></div>
      <div class="stage-particles">${particles}</div>
      <div class="stage-scan"></div>
    </div>`;
}

function createAssistantTurn(options = {}) {
  const name = options.name || 'NovaMind';
  hideWelcome();

  const wrap = document.createElement('div');
  wrap.className = 'message ai is-working';
  wrap.innerHTML = `
    <div class="msg-avatar">✦</div>
    <div class="msg-body">
      <div class="msg-name">${escapeHtml(name)}</div>
      <div class="msg-bubble">
        <div class="stage lg-hidden"></div>
        <div class="answer"></div>
      </div>
      <div class="msg-meta"></div>
      <div class="msg-actions"></div>
    </div>`;
  chatContainer.appendChild(wrap);

  const bubble = wrap.querySelector('.msg-bubble');
  const stage = wrap.querySelector('.stage');
  const answerEl = wrap.querySelector('.answer');
  const metaEl = wrap.querySelector('.msg-meta');
  const actionsEl = wrap.querySelector('.msg-actions');

  let startedAt = Date.now();
  let timer = null;
  let elapsedEl = null;
  let stepsEl = null;
  let reasoningBox = null;
  let reasoningText = '';
  let answerText = '';
  let sourcesData = [];
  let finished = false;

  const startTimer = () => {
    if (timer) return;
    timer = setInterval(() => {
      if (elapsedEl) elapsedEl.textContent = ((Date.now() - startedAt) / 1000).toFixed(1) + ' c';
    }, 100);
  };
  const stopTimer = () => { if (timer) { clearInterval(timer); timer = null; } };

  const ensureStage = (scene, title) => {
    // Пузырь начинался с индикатора «Думаю…», а ИИ пошёл работать —
    // заменяем индикатор полноценной сценой с шагами.
    if (!stage.innerHTML || stage.querySelector('.stage-idle')) {
      stage.innerHTML = `
        ${scene ? sceneMarkup(scene) : ''}
        <div class="stage-head lg-hidden">
          <span class="stage-title">${escapeHtml(title || '')}</span>
          <span class="stage-elapsed">0.0 c</span>
        </div>
        <div class="stage-steps"></div>`;
      elapsedEl = stage.querySelector('.stage-elapsed');
      stepsEl = stage.querySelector('.stage-steps');
    }
    stage.classList.remove('lg-hidden');
    stage.classList.remove('collapsed');
    startTimer();
    scrollToBottom();
  };

  const api = {
    element: wrap,

    /** Пустой пузырь с едва заметным индикатором (поиск выключен). */
    beginIdle(label = 'Думаю…') {
      ensureStage(null, label);
      stage.innerHTML = `
        <div class="stage-idle">
          <span class="stage-idle-label">${escapeHtml(label)}</span>
          <div class="lg-bar"><span></span></div>
          <span clas

