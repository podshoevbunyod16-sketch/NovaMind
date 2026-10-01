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

      elapsedEl = stage.querySelector('.stage-elapsed');
      startTimer();
    },

    /** Сцена поиска: сначала анимация, через паузу — подпись. */
    beginSearch(title = 'Поищу в интернете') {
      ensureStage('search', title);
      const head = stage.querySelector('.stage-head');
      setTimeout(() => {
        if (head && !stage.classList.contains('collapsed')) {
          head.classList.remove('lg-hidden');
          head.classList.add('lg-fade-in');
          scrollToBottom();
        }
      }, 520);
    },

    /** Смена сцены (search → write) без схлопывания. */
    setStage(scene, title, text) {
      ensureStage(scene, title);
      const sceneEl = stage.querySelector('.stage-scene');
      if (sceneEl) sceneEl.dataset.scene = scene;
      const titleEl = stage.querySelector('.stage-title');
      if (titleEl && title) titleEl.textContent = title;
      const head = stage.querySelector('.stage-head');
      if (head) head.classList.remove('lg-hidden');
      if (text) api.step(scene === 'write' ? '✍️' : '🔎', text);
    },

    step(icon, text) {
      ensureStage(null, '');
      if (!stepsEl) return;
      const previous = stepsEl.querySelector('.is-last');
      if (previous) previous.classList.remove('is-last');
      const row = document.createElement('div');
      row.className = 'stage-step is-last';
      row.innerHTML = `<span class="ico">${icon || '•'}</span><span class="txt">${escapeHtml(text || '')}</span>`;
      stepsEl.appendChild(row);
      while (stepsEl.children.length > 7) stepsEl.firstChild.remove();
      scrollToBottom();
    },

    /** План агента: короткий блок над шагами. */
    plan(text) {
      if (!text) return;
      ensureStage(null, '');
      let box = stage.querySelector('.stage-plan');
      if (!box) {
        box = document.createElement('div');
        box.className = 'stage-plan';
        stage.insertBefore(box, stepsEl);
      }
      box.innerHTML = `<span class="stage-plan-label">План</span><span class="stage-plan-text">${escapeHtml(text)}</span>`;
      scrollToBottom();
    },

    /**
     * Команды, которые ИИ выполняет в песочнице.
     *
     * Отдельного терминала в чате нет: во время работы блок раскрыт,
     * а как только пошёл финальный ответ — сворачивается в одну строку.
     */
    tool(event = {}) {
      const command = event.command || '';
      if (!command) return;
      let box = bubble.querySelector('.agent-term');
      if (!box) {
        box = document.createElement('div');
        box.className = 'agent-term';
        box.innerHTML = `
          <button type="button" class="agent-term-head">
            <span class="agent-term-ico">⌨️</span>
            <span class="agent-term-title">Работа в Linux</span>
            <span class="agent-term-meta"></span>
            <span class="caret">▼</span>
          </button>
          <div class="agent-term-log"></div>`;
        box.querySelector('.agent-term-head').addEventListener('click', () => {
          // Свёрнут по умолчанию, раскрывается только по нажатию
          box.classList.toggle('is-open');
        });
        bubble.insertBefore(box, answerEl);
      }
      const failed = Number(event.code) !== 0;
      const row = document.createElement('div');
      row.className = `agent-term-row${failed ? ' is-error' : ''}`;
      row.innerHTML = `
        <span class="agent-term-sign">${failed ? '✕' : '›'}</span>
        <code class="agent-term-cmd">${escapeHtml(command)}</code>
        <span class="agent-term-code">${escapeHtml(String(event.code ?? 0))}</span>`;
      if (event.output) {
        const out = document.createElement('pre');
        out.className = 'agent-term-out';
        out.textContent = String(event.output).slice(0, 1200);
        row.appendChild(out);
      }
      box.querySelector('.agent-term-log').appendChild(row);
      const count = box.querySelectorAll('.agent-term-row').length;
      const done = box.querySelectorAll('.agent-term-row.is-error').length;
      const n10 = count % 10, n100 = count % 100;
      const word = (n10 === 1 && n100 !== 11) ? 'шаг'
        : (n10 >= 2 && n10 <= 4 && (n100 < 12 || n100 > 14)) ? 'шага' : 'шагов';
      box.querySelector('.agent-term-meta').textContent =
        `${count} ${word}${done ? ` · ошибок: ${done}` : ''}`;
      // Блок остаётся свёрнутым: раскрывается только по нажатию пользователя
      scrollToBottom();
    },

    /** Свернуть блок команд: дальше ИИ пишет ответ, а не работает руками. */
    finishTerm() {
      const box = bubble.querySelector('.agent-term');
      if (!box) return;
      box.classList.remove('is-open');
      box.classList.add('is-done');
    },

    /** Агент завёл задачу — показываем её прямо в ответе. */
    taskChip(task = {}) {
      if (!task.id) return;
      let box = bubble.querySelector('.agent-task');
      if (!box) {
        box = document.createElement('div');
        box.className = 'agent-task';
        bubble.insertBefore(box, answerEl);
      }
      const steps = (task.steps || []).length;
      const statusLabel = { todo: 'к выполнению', doing: 'в работе', done: 'готово' }[task.status] || task.status;
      box.innerHTML = `<span class="agent-task-ico">${task.status === 'done' ? '✔' : '📋'}</span>
        <span class="agent-task-title">${escapeHtml(task.title || 'Задача')}</span>
        <span class="agent-task-meta">${escapeHtml(statusLabel)}${steps ? ` · шагов: ${steps}` : ''}</span>`;
      scrollToBottom();
    },

    reasoningToken(token) {
      if (!token) return;
      reasoningText += token;
      if (!reasoningBox) {
        reasoningBox = document.createElement('div');
        reasoningBox.className = 'reasoning';          // свёрнуто, пока пользователь сам не откроет
        reasoningBox.innerHTML = `
          <div class="reasoning-head">
            <svg class="reasoning-brain" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M9.5 2A2.5 2.5 0 0 0 7 4.5v.5a2.5 2.5 0 0 0-2 4.4A2.5 2.5 0 0 0 6.5 14a2.5 2.5 0 0 0 3 3.9V19a2.5 2.5 0 0 0 5 0v-1.1A2.5 2.5 0 0 0 17.5 14a2.5 2.5 0 0 0 1.5-4.6A2.5 2.5 0 0 0 17 5v-.5A2.5 2.5 0 0 0 14.5 2z"/>
            </svg>
            <span>Рассуждение</span>
            <span class="caret">▶</span>
          </div>
          <div class="reasoning-body"><div class="reasoning-text"></div></div>`;
        reasoningBox.querySelector('.reasoning-head').addEventListener('click', () => {
          reasoningBox.classList.toggle('open');
        });
        bubble.insertBefore(reasoningBox, stage);
      }
      reasoningBox.querySelector('.reasoning-text').textContent = reasoningText;
      scrollToBottom();
    },

    /** Схлопнуть сцену (первый токен ответа уже пошёл). */
    collapseStage() {
      if (stage.classList.contains('collapsed')) return;
      stage.classList.add('collapsed');
      stopTimer();
      setTimeout(() => { if (stage.classList.contains('collapsed')) stage.classList.add('lg-hidden'); }, 560);
    },

    appendToken(token) {
      if (!token) return;
      api.collapseStage();
      api.finishTerm();          // команды показаны — дальше только ответ
      answerText += token;
      answerEl.innerHTML = formatContent(answerText);
      scrollToBottom();
    },

    appendText(text) {
      answerText = text || '';
      answerEl.innerHTML = formatContent(answerText);
      scrollToBottom();
    },

    setSources(sources) {
      sourcesData = Array.isArray(sources) ? sources.filter((s) => s && s.url) : [];
      if (!sourcesData.length) return;
      let box = bubble.querySelector('.sources');
      if (!box) {
        box = document.createElement('div');
        box.className = 'sources min';        // источники свёрнуты по умолчанию
        bubble.appendChild(box);
      }
      box.innerHTML = `
        <div class="sources-head">
          <span>🔗 Источники · ${sourcesData.length}</span>
          <span class="caret">▼</span>
        </div>
        <div class="sources-list">
          ${sourcesData.map((source, index) => sourceItemHtml(source, index)).join('')}
        </div>`;
      box.querySelector('.sources-head').addEventListener('click', () => box.classList.toggle('min'));
      scrollToBottom();
    },

    finish(meta = {}) {
      if (finished) return;
      finished = true;
      api.collapseStage();
      api.finishTerm();
      stopTimer();
      wrap.classList.remove('is-working');
      if (reasoningBox) {
        reasoningBox.classList.add('done');
        reasoningBox.classList.remove('open');
      }

      const seconds = ((Date.now() - startedAt) / 1000).toFixed(1);
      const bits = [];
      if (meta.model) bits.push(`модель: ${escapeHtml(meta.model)}`);
      if (meta.provider) bits.push(escapeHtml(meta.provider));
      if (meta.offline) bits.push('офлайн');
      if (meta.searched) bits.push('с поиском');
      if (meta.agent || meta.linux) bits.push('Linux');
      if (meta.steps) bits.push(`шагов: ${meta.steps}`);
      if (sourcesData.length) bits.push(`источников: ${sourcesData.length}`);
      bits.push(`${seconds} c`);
      metaEl.innerHTML = `<span>${bits.join(' · ')}</span>`;

      actionsEl.innerHTML = `
        <button type="button" data-act="copy" title="Скопировать ответ">📋 Копировать</button>
        <button type="button" data-act="speak" title="Озвучить ответ">🔊 Озвучить</button>
        <button type="button" data-act="regen" title="Ответить заново">↻ Заново</button>`;
      actionsEl.querySelector('[data-act="copy"]').addEventListener('click', () => {
        copyText(answerText || bubble.innerText);
      });
      actionsEl.querySelector('[data-act="speak"]').addEventListener('click', () => speakText(answerText));
      actionsEl.querySelector('[data-act="regen"]').addEventListener('click', () => {
        if (lastUserMessage) sendMessage(lastUserMessage);
      });

      if (!historyReplay && answerText) {
        historyAddMessage('ai', answerText, {
          sources: sourcesData.slice(0, 12),
          model: meta.model || null,
          elapsed_ms: Date.now() - startedAt,
          reasoning: reasoningText || null,
        });
      }
      scrollToBottom();
    },

    fail(message) {
      finished = true;
      api.collapseStage();
      api.finishTerm();
      stopTimer();
      wrap.classList.remove('is-working');
      answerEl.innerHTML = `<span style="color:var(--err)">❌ ${escapeHtml(message || 'Неизвестная ошибка')}</span>`;
      actionsEl.innerHTML = `<button type="button" data-act="copy">📋 Копировать</button>
        <button type="button" data-act="regen">↻ Повторить</button>`;
      actionsEl.querySelector('[data-act="copy"]').addEventListener('click', () => copyText(message || ''));
      actionsEl.querySelector('[data-act="regen"]').addEventListener('click', () => {
        if (lastUserMessage) sendMessage(lastUserMessage);
      });
      scrollToBottom();
    },

    remove() {
      stopTimer();
      wrap.remove();
    },

    get text() { return answerText; },
    get sources() { return sourcesData; },
  };

  return api;
}

function sourceItemHtml(source, index) {
  const host = source.host || hostFromUrl(source.url);
  const letter = (source.title || host || '?').trim().charAt(0).toUpperCase();
  return `
    <a class="source-item" href="${escapeHtml(source.url)}" target="_blank" rel="noopener noreferrer"
       title="${escapeHtml(source.title || source.url)}">
      <span class="source-num">${index + 1}</span>
      <img class="source-fav" alt="" loading="lazy"
           src="https://icons.duckduckgo.com/ip3/${encodeURIComponent(host)}.ico"
           onerror="this.remove()">
      <span class="source-txt">
        <span class="source-title">${escapeHtml(source.title || host)}</span>
        <span class="source-host">${escapeHtml(host)}</span>
      </span>
      <span class="source-open">↗</span>
    </a>`;
}

function hostFromUrl(url) {
  try { return new URL(url).hostname.replace(/^www\./, ''); }
  catch (_) { return String(url || '').slice(0, 40); }
}

function copyText(text) {
  const value = String(text || '');
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(value)
      .then(() => showNotification('Скопировано в буфер обмена', 'ok'))
      .catch(() => fallbackCopy(value));
  } else fallbackCopy(value);
}

function fallbackCopy(value) {
  const area = document.createElement('textarea');
  area.value = value;
  area.style.position = 'fixed';
  area.style.opacity = '0';
  document.body.appendChild(area);
  area.select();
  try { document.execCommand('copy'); showNotification('Скопировано', 'ok'); }
  catch (_) { showNotification('Не удалось скопировать', 'warn'); }
  area.remove();
}

let speechUtterance = null;
function speakText(text) {
  if (!('speechSynthesis' in window)) {
    showNotification('Браузер не поддерживает озвучку', 'warn');
    return;
  }
  window.speechSynthesis.cancel();
  const clean = String(text || '').replace(/[*#`|]/g, '').slice(0, 4000);
  speechUtterance = new SpeechSynthesisUtterance(clean);
  speechUtterance.lang = 'ru-RU';
  speechUtterance.rate = 1.02;
  window.speechSynthesis.speak(speechUtterance);
  showNotification('🔊 Читаю ответ…', 'info');
}

// ══════════════════════════════════════════
// ПОТОКОВАЯ ОБРАБОТКА NDJSON
// ══════════════════════════════════════════
async function consumeNdjson(response, onEvent) {
  if (!response.body) throw new Error('Браузер не поддерживает потоковый ответ');
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  const handle = (line) => {
    if (!line.trim()) return;
    let event;
    try { event = JSON.parse(line); } catch (_) { return; }
    onEvent(event);
  };
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const lines = buffer.split('\n');
    buffer = lines.pop() || '';
    for (const line of lines) handle(line);
    if (done) break;
  }
  if (buffer.trim()) handle(buffer);
}

function setSendBusy(busy) {
  if (!sendBtn) return;
  sendBtn.classList.toggle('is-stop', busy);
  sendBtn.disabled = busy ? false : !input.value.trim();
  sendBtn.title = busy ? 'Остановить генерацию' : 'Отправить (Enter)';
  sendBtn.innerHTML = busy
    ? '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>'
    : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>';
}

function stopGeneration() {
  if (!isTyping) return;
  if (activeAbort) {
    activeAbort.abort();
    activeAbort = null;
  }
  // Кнопка сразу возвращается в обычный вид и разрешает новое сообщение;
  // оборванный хвост бутыря доделывает обработчик AbortError.
  isTyping = false;
  setSendBusy(false);
  showNotification('Генерация остановлена', 'warn');
}

// ══════════════════════════════════════════
// ОБЫЧНЫЙ ЧАТ (поток + рассуждения)
// ══════════════════════════════════════════
async function streamMessage(message, turn) {
  const controller = new AbortController();
  activeAbort = controller;

  const response = await fetch('/send_stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, reasoning: reasoningOn }),
    signal: controller.signal,
  });

  if (!response.ok) {
    const raw = await response.text();
    let details = `HTTP ${response.status}`;
    try {
      const data = JSON.parse(raw);
      details = data.error || data.message || details;
    } catch (_) {
      const plain = raw.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
      if (plain) details = `${details}: ${plain.slice(0, 400)}`;
    }
    throw new Error(details);
  }

  let meta = {};
  let failure = null;
  await consumeNdjson(response, (event) => {
    if (event.reasoning) turn.reasoningToken(event.reasoning);
    if (event.token) turn.appendToken(event.token);
    if (event.error) failure = event.error;
    if (event.done) meta = event;
  });
  activeAbort = null;

  if (failure && !turn.text) throw new Error(failure);
  if (!turn.text) throw new Error('AI не вернул текст ответа');
  turn.finish({ model: meta.model, provider: meta.provider, offline: meta.offline });
  return turn.text;
}

/** Запасной путь: обычный /send, если поток не удалось прочитать. */
async function sendOnce(message, turn) {
  const controller = new AbortController();
  activeAbort = controller;
  const response = await fetch('/send', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, reasoning: reasoningOn }),
    signal: controller.signal,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.error) throw new Error(data.error || ('HTTP ' + response.status));
  turn.collapseStage();
  turn.appendText(data.reply || '');
  turn.finish({ model: data.model });
  activeAbort = null;
  return data.reply;
}

// ══════════════════════════════════════════
// ПОИСК В ИНТЕРНЕТЕ (поток событий)
// ══════════════════════════════════════════
async function runSearchTurn(message, turn) {
  const controller = new AbortController();
  activeAbort = controller;

  const response = await fetch('/api/auto_search_stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, force: true, reasoning: reasoningOn }),
    signal: controller.signal,
  });

  if (!response.ok) {
    const raw = await response.text();
    let details = `HTTP ${response.status}`;
    try { details = JSON.parse(raw).error || details; } catch (_) { details += ': ' + raw.slice(0, 300); }
    throw new Error(details);
  }

  let failure = null;
  let result = {};
  await consumeNdjson(response, (event) => {
    switch (event.type) {
      case 'stage':
        if (event.scene === 'search') turn.setStage('search', event.title, event.text);
        else turn.setStage('write', event.title, event.text);
        break;
      case 'step':
        turn.step(event.icon, event.text);
        break;
      case 'sources':
        turn.setSources(event.sources || []);
        break;
      case 'reasoning':
        turn.reasoningToken(event.token);
        break;
      case 'token':
        turn.appendToken(event.token);
        break;
      case 'error':
        failure = event.text;
        turn.step('❌', event.text);
        break;
      case 'result':
        result = event;
        if (Array.isArray(event.sources) && event.sources.length) turn.setSources(event.sources);
        // Ответ мог прийти без потока (повторный запрос на сервере) —
        // тогда текст лежит в result.reply, иначе пузырь остался бы пустым.
        if (!turn.text && event.reply) turn.appendText(event.reply);
        break;
      case 'done':
        break;
      default:
        break;
    }
  });
  activeAbort = null;

  if (!turn.text) {
    throw new Error(failure || 'Поиск завершился без ответа модели');
  }
  turn.finish({
    model: result.model || aiStatus?.model,
    provider: aiStatus?.provider,
    offline: result.offline,
    searched: result.searched,
  });
  return turn.text;
}

// ══════════════════════════════════════════
// ОТПРАВКА СООБЩЕНИЙ
// ══════════════════════════════════════════
async function sendMessage(text) {
  // Кнопка отправки во время генерации — это «стоп». Проверяем это ДО ввода:
  // поле в это время пустое, и ранний return глушил остановку.
  if (isTyping) { stopGeneration(); return; }
  const msg = String(text ?? input.value).trim();
  if (!msg) return;

  let finalMsg = msg;
  if (inputMode && !msg.startsWith('/')) {
    finalMsg = inputMode.prefix + msg;
    clearInputMode(true);
  }

  hideWelcome();
  appendMessage('user', finalMsg);
  lastUserMessage = finalMsg;

  input.value = '';
  input.style.height = 'auto';
  isTyping = true;
  setSendBusy(true);
  closeCmdk();

  try {
    const isCommand = finalMsg.startsWith('/');

    // ── Команды ──
    if (isCommand) {
      const turn = createAssistantTurn();
      turn.beginIdle('Выполняю команду…');
      try {
        const resp = await fetch('/command', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ command: finalMsg }),
        });
        const data = await resp.json().catch(() => ({}));
        turn.collapseStage();
        if (data.error) turn.appendText('Ошибка: ' + data.error);
        else turn.appendText(data.result || data.reply || 'Готово');
        turn.finish({ model: 'command' });
      } catch (error) {
        turn.fail(error.message || 'Ошибка соединения');
      }
      return;
    }

    // ── Медиа-модель ──
    if (activeMediaModel) {
      try {
        await sendMediaMessage(finalMsg);
      } catch (error) {
        appendMessage('ai', '❌ Ошибка генерации: ' + (error.message || 'неизвестная ошибка'));
      }
      return;
    }

    // ── Главный чат с Linux-окружением ──
    // ИИ сам решает: ответить сразу или работать в Linux по шагам.
    // Кнопка «Поиск» тоже идёт через окружение (nova search → чтение страниц → ответ).
    if (window.Linux && typeof window.Linux.runTurn === 'function') {
      const handled = await window.Linux.runTurn(finalMsg, {
        search: webSearchOn || autoSearchOn,
        reasoning: reasoningOn,
      });
      if (handled) return;
    }

    // ── Запасной путь без окружения: поиск в интернете ──
    if (webSearchOn || autoSearchOn) {
      const turn = createAssistantTurn();
      turn.beginSearch('Поищу в интернете');
      try {
        await runSearchTurn(finalMsg, turn);
      } catch (error) {
        if (error.name === 'AbortError') {
          turn.collapseStage();
          turn.appendText(turn.text || '_Генерация остановлена._');
          turn.finish({ model: aiStatus?.model });
        } else if (turn.sources.length) {
          // Источники нашлись, но модель не ответила. Отвечать «по памяти»
          // здесь нельзя — это выглядит будто свежих данных нет. Честно
          // показываем, что произошло, и оставляем ссылки под рукой.
          turn.collapseStage();
          turn.appendText(
            '_Источники найдены, но ответ модель не отдала._\n\n' +
            'Откройте ссылки ниже или повторите запрос — обычно помогает ' +
            'смена модели или повторный запуск поиска.'
          );
          turn.finish({ model: aiStatus?.model, provider: aiStatus?.provider, searched: true });
          showNotification('Поиск нашёл ссылки, но модель не ответила', 'warn');
        } else {
          turn.step('⚠️', 'Поиск не удался — отвечаю без интернета');
          try {
            await streamMessage(finalMsg, turn);
          } catch (fallbackError) {
            try {
              await sendOnce(finalMsg, turn);
            } catch (lastError) {
              turn.fail(`${error.message} · затем ${lastError.message}`);
            }
          }
        }
      }
      return;
    }

    // ── Обычный чат: пузырь сначала пустой ──
    const turn = createAssistantTurn();
    turn.beginIdle(reasoningOn ? 'Рассуждаю…' : 'Думаю…');
    try {
      await streamMessage(finalMsg, turn);
    } catch (error) {
      if (error.name === 'AbortError') {
        turn.collapseStage();
        turn.appendText(turn.text || '_Генерация остановлена._');
        turn.finish({ model: aiStatus?.model });
        return;
      }
      try {
        await sendOnce(finalMsg, turn);
      } catch (fallbackError) {
        turn.fail(fallbackError.message || error.message);
      }
    }
  } finally {
    isTyping = false;
    setSendBusy(false);
  }
}


// ========== РЕЖИМЫ КОМАНД ==========
/** Плашка активного режима («Погода», «Курс валют»…) с крестиком отмены. */
function paintModeChip() {
  const bar = document.getElementById('modeActiveBar');
  const text = document.getElementById('modeActiveText');
  if (!bar) return;
  bar.hidden = !inputMode;
  if (inputMode && text) setShortText(text, inputMode.label || inputMode.prefix.trim(), 52);
}

function activateMode(mode) {
  // Повторный клик по тому же пункту — выключить режим, а не «залипнуть» в нём.
  if (inputMode && inputMode.prefix === mode.prefix) { clearInputMode(); return; }
  inputMode = mode;
  input.placeholder = mode.placeholder;
  input.value = '';
  paintModeChip();
  input.focus();
}

function clearInputMode(quiet = false) {
  if (!inputMode) return;
  inputMode = null;
  paintModeChip();
  updateMediaChip();          // вернёт подсказку поля: медиа-модель или обычный чат
  if (!quiet) showNotification('Режим выключен — обычный чат', 'info');
}

function hideWelcome() { if (welcomeScreen) welcomeScreen.style.display = 'none'; }

// ══════════════════════════════════════════
// СООБЩЕНИЯ (история, медиа, composio)
// ══════════════════════════════════════════
function appendMessage(role, content, meta = null) {
  msgCount++;
  const isAI = role === 'ai';
  const wrap = document.createElement('div');
  wrap.className = 'message ' + (role === 'user' ? 'user' : 'ai');

  if (isAI && content.startsWith('COMPOSIO_CARDS:')) {
    try {
      const cards = JSON.parse(content.replace('COMPOSIO_CARDS:', ''));
      wrap.innerHTML = `<div class="msg-avatar">✦</div><div class="msg-body"><div class="msg-name">NovaMind</div><div class="msg-bubble">${renderComposioCards(cards)}</div></div>`;
      chatContainer.appendChild(wrap);
      scrollToBottom();
      return;
    } catch (e) {}
  }

  if (isAI && content.startsWith('COMPOSIO_AUTH:')) {
    const withoutPrefix = content.replace('COMPOSIO_AUTH:', '');
    const colonIdx = withoutPrefix.indexOf(':');
    wrap.innerHTML = `<div class="msg-avatar">✦</div><div class="msg-body"><div class="msg-name">NovaMind</div><div class="msg-bubble">${renderComposioAuth(withoutPrefix.substring(0, colonIdx), withoutPrefix.substring(colonIdx + 1))}</div></div>`;
    chatContainer.appendChild(wrap);
    scrollToBottom();
    return;
  }

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

  let formatted = isAI ? formatContent(content) : escapeHtml(content);
  let imageHtml = '';
  const imageMatch = content.match(/!\[Image\]\((.*?)\)/);
  if (imageMatch) {
    imageHtml = `<img src="${imageMatch[1]}" alt="Generated image" style="max-width:100%;border-radius:12px;margin-top:8px;">`;
    formatted = formatted.replace(/!\[Image\]\(.*?\)/, '');
  }

  const sources = Array.isArray(meta?.sources) ? meta.sources.filter((s) => s && s.url) : [];
  const sourcesHtml = sources.length ? `
    <div class="sources min">
      <div class="sources-head"><span>🔗 Источники · ${sources.length}</span><span class="caret">▼</span></div>
      <div class="sources-list">${sources.map(sourceItemHtml).join('')}</div>
    </div>` : '';

  wrap.innerHTML = `
    <div class="msg-avatar">${isAI ? '✦' : '👤'}</div>
    <div class="msg-body">
      <div class="msg-name">${isAI ? 'NovaMind' : 'Вы'}</div>
      <div class="msg-bubble">
        ${meta?.reasoning ? `<div class="reasoning"><div class="reasoning-head"><span>🧠</span><span>Рассуждение</span><span class="caret">▶</span></div><div class="reasoning-body"><div class="reasoning-text">${escapeHtml(meta.reasoning)}</div></div></div>` : ''}
        <div class="answer">${formatted}${imageHtml}</div>
        ${sourcesHtml}
      </div>
      ${isAI && meta?.model ? `<div class="msg-meta"><span>модель: ${escapeHtml(String(meta.model))}${meta.elapsed_ms ? ' · ' + (meta.elapsed_ms / 1000).toFixed(1) + ' c' : ''}</span></div>` : ''}
      ${isAI ? `<div class="msg-actions">
        <button type="button" data-act="copy">📋 Копировать</button>
        <button type="button" data-act="speak">🔊 Озвучить</button>
      </div>` : ''}
    </div>`;

  chatContainer.appendChild(wrap);

  const answerText = String(content || '');
  wrap.querySelector('[data-act="copy"]')?.addEventListener('click', () => copyText(answerText));
  wrap.querySelector('[data-act="speak"]')?.addEventListener('click', () => speakText(answerText));
  wrap.querySelector('.sources-head')?.addEventListener('click', (event) => {
    event.currentTarget.parentElement.classList.toggle('min');
  });
  wrap.querySelector('.reasoning-head')?.addEventListener('click', (event) => {
    event.currentTarget.parentElement.classList.toggle('open');
  });

  scrollToBottom();

  if (!historyReplay && (role === 'user' || role === 'ai') && content) {
    historyAddMessage(role, content, meta);
  }
}

function formatContent(text) {
  let html = escapeHtml(String(text ?? ''));

  // Блоки кода — с кнопками для программиста. Их вынимаем в заглушки,
  // чтобы разметка ниже не трогала код: переносы строк не превращались
  // в <br>, а ** и * внутри кода — в жирный и курсив.
  const codeBlocks = [];
  html = html.replace(/\n*```(\w+)?\n?([\s\S]*?)```\n*/g, (_, lang, code) => {
    codeBlocks.push(
      `<div class="code-block" data-lang="${escapeHtml(lang || '')}">`
      + '<div class="code-bar">'
      + `<span class="code-lang">${escapeHtml(lang || 'код')}</span>`
      + '<span class="code-acts">'
      + '<button type="button" data-code="run" title="ИИ запустит этот код в Linux и покажет результат">▶<span class="code-act-label"> Запустить</span></button>'
      + '<button type="button" data-code="explain" title="Объяснить этот код">💡<span class="code-act-label"> Объясни</span></button>'
      + '<button type="button" data-code="tests" title="Написать и запустить тесты">🧪<span class="code-act-label"> Тесты</span></button>'
      + '<button type="button" data-code="copy" title="Скопировать код">📋</button>'
      + '</span></div>'
      + `<pre><code>${code.replace(/\s+$/, '').replace(/^\n+/, '')}</code></pre>`
      + '</div>');
    return `\u0000${codeBlocks.length - 1}\u0000`;
  });
  // Инлайн-код
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
  // Заголовки
  html = html.replace(/^###\s+(.+)$/gm, '<strong style="font-size:14px">$1</strong>');
  html = html.replace(/^##\s+(.+)$/gm, '<strong style="font-size:15px">$1</strong>');
  // Жирный / курсив
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
  // Ссылки [1]
  html = html.replace(/\[(\d{1,2})\]/g, '<sup class="lg-chip acc" style="padding:1px 5px;font-size:9px">$1</sup>');

  // Таблицы
  html = html.replace(/(\|[^\n]+\|\n\|[-| :]+\|\n(?:\|[^\n]+\|\n?)*)/g, (match) => {
    const rows = match.trim().split('\n');
    const colCount = (rows[0].match(/\|/g) || []).length - 1;
    const needsScroll = colCount > 3;
    let tableHtml = '<table>';
    rows.forEach((row, index) => {
      const cells = row.split('|').filter((c) => c.trim() !== '');
      if (index === 1 && cells.every((c) => /^[-| :]+$/.test(c))) return;
      const tag = index === 0 ? 'th' : 'td';
      const alignRow = rows[1] ? rows[1].split('|').filter((c) => c.trim() !== '') : [];
      tableHtml += '<tr>';
      cells.forEach((cell, ci) => {
        const sep = alignRow[ci] || '';
        const align = sep.startsWith(':') && sep.endsWith(':') ? 'center' : sep.endsWith(':') ? 'right' : 'left';
        tabl

      tableHtml += '</tr>';
    });
    tableHtml += '</table>';
    const hint = needsScroll ? '<div class="tbl-scroll-hint show">← прокрути вправо →</div>' : '';
    return `<div class="tbl-wrap">${tableHtml}</div>${hint}`;
  });

  html = html.replace(/\n\n/g, '<br><br>');
  html = html.replace(/\n/g, '<br>');
  // Возвращаем блоки кода на место — нетронутыми
  html = html.replace(/\u0000(\d+)\u0000/g, (_, index) => codeBlocks[Number(index)] || '');
  return html;
}

function escapeHtml(text) {
  return String(text ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
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
function openSettings() { window.location.href = '/settings'; }

const DRAWER_BREAKPOINT = 860;
// Чуть у́же, чем раньше, чтобы сайдбар не перекрывал чат на телефоне.
const drawerWidth = () => Math.min(300, Math.max(240, window.innerWidth * 0.84));

/** matchMedia может отсутствовать (старые webview, тестовые окружения) — не падаем. */
function isDrawerViewport() {
  try {
    return window.matchMedia(`(max-width: ${DRAWER_BREAKPOINT}px)`).matches;
  } catch (_) {
    return window.innerWidth <= DRAWER_BREAKPOINT;
  }
}

// ══════════════════════════════════════════════════════════════
// СОСТОЯНИЕ БОКОВОЙ ПАНЕЛИ НА КОМПЬЮТЕРЕ
// Панель по умолчанию скрыта: переписка занимает всю ширину,
// а кнопка-бургер в шапке открывает её в один клик. Выбор запоминается.
// ══════════════════════════════════════════════════════════════
const NAV_KEY = 'nova_nav_open';

function navIsOpen() {
  const app = document.querySelector('.app');
  return !!app && !app.classList.contains('nav-off');
}

function setNavOpen(open, save = true) {
  const app = document.querySelector('.app');
  if (!app) return;
  app.classList.toggle('nav-off', !open);
  const btn = document.getElementById('btnNav');
  if (btn) {
    btn.classList.toggle('is-open', open);
    btn.setAttribute('aria-expanded', String(open));
    btn.title = open ? 'Скрыть боковую панель' : 'Открыть боковую панель';
    btn.setAttribute('aria-label', btn.title);
  }
  if (save) {
    try { localStorage.setItem(NAV_KEY, open ? '1' : '0'); } catch (_) {}
  }
}

function initNav() {
  let saved = null;
  try { saved = localStorage.getItem(NAV_KEY); } catch (_) {}
  setNavOpen(saved === '1', false);
}

function setSidebarOffset(offset, animate = false) {
  const app = document.querySelector('.app');
  const sidebar = document.getElementById('sidebar');
  const overlay = document.getElementById('overlay');
  if (!app || !sidebar) return;

  const width = drawerWidth();
  const x = Math.max(0, Math.min(width, Number(offset) || 0));

  app.style.setProperty('--drawer-x', x + 'px');
  app.style.setProperty('--drawer-width', width + 'px');
  app.classList.toggle('drawer-animate', animate);

  const isOpen = x > width * 0.5;
  sidebar.classList.toggle('open', isOpen);
  // Когда сайдбар открыт, чат блокируется — один скролл на экран.
  document.body.classList.toggle('drawer-open', isOpen);

  if (overlay) {
    overlay.classList.toggle('visible', isOpen);
    overlay.style.opacity = String(Math.min(0.7, (x / width) * 0.7));
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
  if (isDrawerViewport()) {
    const width = drawerWidth();
    setSidebarOffset(getSidebarOffset() > width * 0.5 ? 0 : width, true);
    return;
  }
  // На широком экране панель просто убирается, освобождая место под чат.
  setNavOpen(!navIsOpen());
}

function closeSidebar() {
  if (isDrawerViewport()) setSidebarOffset(0, true);
}

function initSidebarSwipe() {
  const app = document.querySelector('.app');
  const sidebar = document.getElementById('sidebar');
  const overlay = document.getElementById('overlay');
  if (!app || !sidebar) return;

  let swipeEnabled = false;

  const enableForViewport = () => {
    swipeEnabled = isDrawerViewport();
    if (!swipeEnabled) {
      app.classList.remove('drawer-animate');
      sidebar.classList.remove('open');
      document.body.classList.remove('drawer-open');
      if (overlay) {
        overlay.classList.remove('visible');
        overlay.style.pointerEvents = 'none';
        overlay.style.opacity = '0';
      }
      app.style.removeProperty('--drawer-x');
      app.style.removeProperty('--drawer-width');
    } else {
      app.style.setProperty('--drawer-width', drawerWidth() + 'px');
      if (!app.style.getPropertyValue('--drawer-x')) app.style.setProperty('--drawer-x', '0px');
    }
  };
  enableForViewport();

  let drag = null;

  const begin = (e) => {
    if (!swipeEnabled) return;
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    const open = getSidebarOffset() > 1;
    const x = e.clientX;
    if (!open && x > 32) return;
    if (open && !sidebar.contains(e.target)) return;
    drag = {
      id: e.pointerId, startX: x, startY: e.clientY,
      startOffset: getSidebarOffset(), lastX: x, lastTime: performance.now(),
      velocityX: 0, horizontal: false,
    };
    app.classList.remove('drawer-animate');
    try { app.setPointerCapture(e.pointerId); } catch (_) {}
  };

  const move = (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    const dx = e.clientX - drag.startX;
    const dy = e.clientY - drag.startY;
    const now = performance.now();
    const dt = Math.max(1, now - drag.lastTime);

    if (!drag.horizontal) {
      if (Math.abs(dx) < 8) return;
      if (Math.abs(dy) > Math.abs(dx) * 1.15) { drag = null; return; }
      drag.horizontal = true;
    }
    e.preventDefault();

    const next = Math.max(0, Math.min(drawerWidth(), drag.startOffset + dx));
    drag.velocityX = (e.clientX - drag.lastX) / dt;
    drag.lastX = e.clientX;
    drag.lastTime = now;
    setSidebarOffset(next, false);
  };

  const end = (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    const currentDrag = drag;
    drag = null;
    try { app.releasePointerCapture(e.pointerId); } catch (_) {}
    if (!currentDrag.horizontal) return;
    const width = drawerWidth();
    const dx = e.clientX - currentDrag.startX;
    const current = getSidebarOffset();
    const shouldOpen = current > width * 0.5 || dx > 70 || currentDrag.velocityX > 0.45;
    setSidebarOffset(shouldOpen ? width : 0, true);
  };

  app.addEventListener('pointerdown', begin, { passive: false });
  app.addEventListener('pointermove', move, { passive: false });
  app.addEventListener('pointerup', end, { passive: false });
  app.addEventListener('pointercancel', end, { passive: false });
  window.addEventListener('resize', enableForViewport);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => { initNav(); initSidebarSwipe(); });
} else {
  initNav();
  initSidebarSwipe();
}

function setActive(el) {
  document.querySelectorAll('.nav-item').forEach((n) => n.classList.remove('active'));
  el.classList.add('active');
}

// ========== ЧАТ ==========
function newChat() {
  const store = historyLoad();
  store.activeId = null;
  historySave(store);
  // Новый чат — чистый лист: никаких активных режимов и медиа-моделей.
  clearInputMode(true);
  clearMediaModel(true);
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
  fetch('/api/history/clear', { method: 'DELETE' }).catch(() => {});
  showNotification('Чат очищен', 'info');
}

function shareChat() {
  copyText(window.location.href);
}

function scrollToBottom() {
  requestAnimationFrame(() => {
    // Прокручивается всё окно, поэтому целимся в нижний край панели ввода
    // (она «прилипает» к низу и всегда закрывает собой хвост переписки).
    const tail = document.querySelector('.input-area');
    const anchor = tail || chatContainer;
    const bottom = anchor.getBoundingClientRect().bottom;
    const max = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
    const target = Math.min(max, Math.max(0, window.scrollY + bottom));
    if (Math.abs(target - window.scrollY) < 2) return;
    window.scrollTo({ top: target, behavior: 'smooth' });
  });
}

// ========== ИСТОРИЯ ЧАТОВ ==========
const HISTORY_KEY = 'nova_history_v2';
const MAX_CHATS = 50;
const MAX_MSGS = 60;

function historyLoad() {
  try {
    const parsed = JSON.parse(localStorage.getItem(HISTORY_KEY) || '{"chats":[],"activeId":null}');
    const chats = Array.isArray(parsed.chats) ? parsed.chats
      .filter((chat) => chat && chat.id)
      .map((chat) => ({
        ...chat,
        title: String(chat.title || 'Новый диалог'),
        messages: Array.isArray(chat.messages) ? chat.messages : [],
        createdAt: Number(chat.createdAt) || Number(chat.updatedAt) || Date.now(),
        updatedAt: Number(chat.updatedAt) || Number(chat.createdAt) || Date.now(),
      })) : [];
    const activeId = chats.some((chat) => chat.id === parsed.activeId) ? parsed.activeId : null;
    return { chats, activeId };
  } catch (e) {
    return { chats: [], activeId: null };
  }
}

function historySave(store) {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(store));
  } catch (e) {
    console.warn('[History] localStorage недоступен или переполнен');
  }
}

function historyAddMessage(role, content, meta = null) {
  if (historyReplay || !content) return;

  const store = historyLoad();
  let chat = store.chats.find((c) => c.id === store.activeId);

  if (!chat) {
    const title = content.slice(0, 55) + (content.length > 55 ? '…' : '');
    chat = {
      id: Date.now().toString(36) + Math.random().toString(36).slice(2, 7),
      title, messages: [], createdAt: Date.now(), updatedAt: Date.now(),
    };
    store.chats.unshift(chat);
    store.activeId = chat.id;
  }

  chat.messages.push(meta ? { role, content, meta } : { role, content });
  chat.updatedAt = Date.now();

  if (role === 'user' && chat.messages.filter((m) => m.role === 'user').length === 1) {
    chat.title = content.slice(0, 55) + (content.length > 55 ? '…' : '');
  }
  if (chat.messages.length > MAX_MSGS) chat.messages = chat.messages.slice(-MAX_MSGS);
  if (store.chats.length > MAX_CHATS) store.chats = store.chats.slice(0, MAX_CHATS);

  historySave(store);
  renderChatList();
}

function renderChatList() {
  const container = document.getElementById('historyContainer');
  if (!container) return;

  const store = historyLoad();
  const chats = [...(store.chats || [])].sort((a, b) => {
    const diff = (Number(b.updatedAt) || 0) - (Number(a.updatedAt) || 0);
    return diff || String(b.id).localeCompare(String(a.id));
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
  const groups = { 'Сегодня': [], 'Вчера': [], 'Последние 7 дней': [], 'Ранее': [] };

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
      const date = updated.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric' });
      const time = updated.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
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
        <button class="hist-del-btn" type="button" title="Удалить чат">×</button>`;

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
  const chat = store.chats.find((c) => c.id === chatId);
  if (!chat) return;

  store.activeId = chatId;
  historySave(store);

  historyReplay = true;
  try {
    chatContainer.innerHTML = '';
    for (const msg of (chat.messages || [])) {
      appendMessage(msg.role === 'user' ? 'user' : 'ai', msg.content, msg.meta || null);
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
  store.chats = (store.chats || []).filter((c) => c.id !== chatId);
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

// ══════════════════════════════════════════
// КОМАНДНАЯ ПАНЕЛЬ (Ctrl+K)
// ══════════════════════════════════════════
const CMDK_ACTIONS = [
  { icon: '🔍', label: 'Включить/выключить поиск в интернете', hint: '', run: () => toggleWebSearch() },
  { icon: '🧠', label: 'Включить/выключить рассуждения', hint: '', run: () => toggleReasoning() },
  { icon: '🎨', label: 'Открыть панель медиа-моделей', hint: '', run: () => openMediaPicker() },
  { icon: '✖️', label: 'Выключить активный режим', hint: '', run: () => clearInputMode() },
  { icon: '✨', label: 'Спокойный режим анимаций', hint: '', run: () => toggleCalmMotion() },
  { icon: '🧹', label: 'Очистить чат', hint: '', run: () => clearChat() },
  { icon: '➕', label: 'Новый диалог', hint: '', run: () => newChat() },
  { icon: '⚙️', label: 'Настройки ИИ и провайдеров', hint: '', run: () => openSettings() },
  { icon: '🩺', label: 'Проверить поиск и модель', hint: '', run: () => checkHealth(true) },
  { icon: '🔊', label: 'Озвучить последний ответ', hint: '', run: () => speakText(lastAnswerText()) },
  { icon: '/help', label: 'Команда: список команд', hint: '/help', run: () => sendMessage('/help') },
  { icon: '🌤', label: 'Режим: погода', hint: '/services weather', run: () => activateMode({ prefix: '/services weather ', label: '🌤 Погода', placeholder: 'Введите город…' }) },
  { icon: '💱', label: 'Режим: курс валют', hint: '/services currency', run: () => activateMode({ prefix: '/services currency ', label: '💱 Курс валют', placeholder: 'USD RUB…' }) },
  { icon: '📚', label: 'Режим: Википедия', hint: '/services wiki', run: () => activateMode({ prefix: '/services wiki ', label: '📚 Википедия', placeholder: 'Запрос…' }) },
  { icon: '💻', label: 'Режим: код', hint: '/code', run: () => activateMode({ prefix: '/code ', label: '💻 Помощник кода', placeholder: 'Какой код создать…' }) },
  { icon: '🖼', label: 'Режим: изображение', hint: '/image', run: () => activateMode({ prefix: '/image ', label: '🎨 Генерация изображений', placeholder: 'Опишите изображение…' }) },
];

let cmdkIndex = 0;
let cmdkFiltered = CMDK_ACTIONS;

function openCmdk() {
  const box = document.getElementById('cmdk');
  if (!box) return;
  box.classList.add('open');
  box.setAttribute('aria-hidden', 'false');
  const field = document.getElementById('cmdkInput');
  if (field) { field.value = ''; field.focus(); }
  filterCmdk();
}

function closeCmdk() {
  const box = document.getElementById('cmdk');
  if (!box) return;
  box.classList.remove('open');
  box.setAttribute('aria-hidden', 'true');
}

function filterCmdk() {
  const query = (document.getElementById('cmdkInput')?.value || '').trim().toLowerCase();
  cmdkFiltered = CMDK_ACTIONS.filter((action) =>
    !query || action.label.toLowerCase().includes(query) || (action.hint || '').toLowerCase().includes(query));
  cmdkIndex = 0;
  renderCmdk();
}

function renderCmdk() {
  const list = document.getElementById('cmdkList');
  if (!list) return;
  if (!cmdkFiltered.length) {
    list.innerHTML = '<div class="cmdk-empty">Ничего не найдено</div>';
    return;
  }
  list.innerHTML = cmdkFiltered.map((action, index) => `
    <div class="cmdk-item ${index === cmdkIndex ? 'sel' : ''}" data-index="${index}">
      <span class="k">${action.icon}</span>
      <span>${escapeHtml(action.label)}</span>
      ${action.hint ? `<span class="hint">${escapeHtml(action.hint)}</span>` : ''}
    </div>`).join('');
  list.querySelectorAll('.cmdk-item').forEach((item) => {
    item.addEventListener('click', () => runCmdk(Number(item.dataset.index)));
  });
}

function runCmdk(index) {
  const action = cmdkFiltered[index];
  closeCmdk();
  if (action) action.run();
}

function cmdkKey(event) {
  if (event.key === 'ArrowDown') {
    event.preventDefault();
    cmdkIndex = (cmdkIndex + 1) % Math.max(1, cmdkFiltered.length);
    renderCmdk();
  } else if (event.key === 'ArrowUp') {
    event.preventDefault();
    cmdkIndex = (cmdkIndex - 1 + cmdkFiltered.length) % Math.max(1, cmdkFiltered.length);
    renderCmdk();
  } else if (event.key === 'Enter') {
    event.preventDefault();
    runCmdk(cmdkIndex);
  } else if (event.key === 'Escape') {
    closeCmdk();
  }
}

function lastAnswerText() {
  const answers = chatContainer.querySelectorAll('.message.ai .answer, .message.ai .msg-bubble');
  return answers.length ? answers[answers.length - 1].innerText : '';
}

// ══════════════════════════════════════════
// СОСТОЯНИЕ ИИ И ПОИСКА (индикаторы)
// ══════════════════════════════════════════
async function refreshAiStatus() {
  try {
    const response = await fetch('/api/ai/status');
    if (!response.ok) throw new Error('HTTP ' + response.status);
    aiStatus = await response.json();
  } catch (error) {
    aiStatus = null;
  }
  paintStatus();
}

function paintStatus() {
  const dot = document.getElementById('statusDot');
  const text = document.getElementById('statusText');
  const providerLabel = document.getElementById('display-provider');
  if (!aiStatus) {
    if (dot) dot.className = 'lg-dot warn';
    setShortText(text, 'AI Ассистент', 26);
    return;
  }
  const model = aiStatus.model || 'модель';
  if (dot) dot.className = 'lg-dot ' + (aiStatus.offline ? 'warn' : 'ok') + ' pulse';
  setShortText(text, aiStatus.offline ? `Офлайн-модель · ${model}` : model, 26);
  setShortText(providerLabel, `${aiStatus.provider_name || aiStatus.provider} · ${model}`, 22);
}

async function checkHealth(notify = false) {
  const dot = document.getElementById('searchHealthDot');
  if (dot) dot.className = 'lg-dot';
  try {
    const response = await fetch('/api/search/health?refresh=1');
    const data = await response.json();
    if (dot) dot.className = 'lg-dot ' + (data.ok ? 'ok' : 'err');
    if (notify) {
      showNotification(data.ok
        ? `Поиск работает: ${data.alive.join(', ')}`
        : 'Поисковые бэкенды недоступны — проверьте интернет или SEARCH_BACKENDS в .env',
        data.ok ? 'ok' : 'warn');
    }
    return data;
  } catch (error) {
    if (dot) dot.className = 'lg-dot err';
    if (notify) showNotification('Не удалось проверить поиск: ' + error.message, 'warn');
    return null;
  }
}

// ══════════════════════════════════════════
// COMPOSIO
// ══════════════════════════════════════════
const COMPOSIO_ICONS = {
  github: '🐙', gmail: '📧', notion: '📝', slack: '💬', googlecalendar: '📅',
  googledrive: '☁️', trello: '📋', twitter: '🐦', discord: '🎮', jira: '🔵',
  linear: '⚡', youtube: '▶️', shopify: '🛒', hubspot: '🟠', airtable: '🗃️',
  dropbox: '📦', figma: '🎨', stripe: '💳', zoom: '📹', asana: '🎯',
};

function renderComposioCards(cards) {
  const grid = cards.map((card) => {
    const icon = COMPOSIO_ICONS[card.slug] || '🔗';
    const statusColor = card.connected ? 'var(--ok)' : 'var(--text-3)';
    const statusText = card.connected ? '✅ Подключено' : 'Нажми — подключить';
    return `
      <div onclick="composioAuthFromChat('${card.slug}')" class="suggestion-card" style="text-align:center;cursor:pointer">
        <div class="card-icon">${icon}</div>
        <div class="card-title">${escapeHtml(card.name || card.slug)}</div>
        <div class="card-desc" style="color:${statusColor}">${statusText}</div>
      </div>`;
  }).join('');

  return `
    <div style="font-weight:700;margin-bottom:10px">🧩 Интеграции Composio — нажми для подключения:</div>
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:8px">${grid}</div>
    <div style="margin-top:10px;font-size:11px;color:var(--text-4)">
      💡 После подключения используй <code>/composio accounts</code> для проверки
    </div>`;
}

function renderComposioAuth(toolkit, url) {
  const icon = COMPOSIO_ICONS[toolkit] || '🔗';
  return `
    <div style="padding:4px 2px">
      <div style="font-size:26px;margin-bottom:8px">${icon}</div>
      <div style="font-size:14px;font-weight:700;margin-bottom:6px">Подключить ${escapeHtml(String(toolkit).toUpperCase())}</div>
      <div style="font-size:12px;color:var(--text-2);margin-bottom:14px">
        Нажми кнопку ниже — откроется страница авторизации.<br>
        После входа вернись и введи <code>/composio accounts</code>
      </div>
      <a class="media-download" href="${escapeHtml(url)}" target="_blank" rel="noopener">🔐 Войти →</a>
      <div style="margin-top:10px;font-size:11px;color:var(--text-4)">
        После: <code>/composio tools ${escapeHtml(toolkit)}</code>
      </div>
    </div>`;
}

function composioAuthFromChat(toolkit) {
  sendMessage(`/composio auth ${toolkit}`);
}

// ══════════════════════════════════════════
// ГОРЯЧИЕ КЛАВИШИ
// ══════════════════════════════════════════
document.addEventListener('keydown', (event) => {
  const key = event.key;
  const mod = event.ctrlKey || event.metaKey;

  if (mod && key.toLowerCase() === 'k') {
    event.preventDefault();
    openCmdk();
    return;
  }
  if (key === 'Escape') {
    closeMediaPicker();
    closeCmdk();
    document.getElementById('attachDropdown')?.classList.remove('open');
    return;
  }
  if (mod && key === '/') {
    event.preventDefault();
    showNotification('Ctrl+K — команды · Enter — отправить · Shift+Enter — новая строка · Esc — закрыть', 'info');
    return;
  }
  if (mod && key.toLowerCase() === 'b') {
    event.preventDefault();
    toggleSidebar();
  }
});

// Клик вне меню «Прикрепить» закрывает его
document.addEventListener('click', (event) => {
  const dropdown = document.getElementById('attachDropdown');
  if (!dropdown || !dropdown.classList.contains('open')) return;
  if (!event.target.closest('#attachDropdown') && !event.target.closest('#btn-attach')) {
    dropdown.classList.remove('open');
  }
});

// ========== ИНИЦИАЛИЗАЦИЯ ==========
(function init() {
  setWebSearch(webSearchOn, true);
  document.getElementById('btn-reasoning')?.classList.toggle('active', reasoningOn);
  input.focus();
  refreshMediaSelection();
  refreshAiStatus();
  checkHealth(false);
  renderChatList();

  window.addEventListener('online', () => {
    showNotification('Соединение восстановлено', 'ok');
    refreshAiStatus();
    checkHealth(false);
  });
  window.addEventListener('offline', () => showNotification('Нет соединения с интернетом', 'warn'));
})();
