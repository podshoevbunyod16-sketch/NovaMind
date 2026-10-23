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
  if (window.NovaSpeech) window.NovaSpeech.onChatChanged();
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

/**
 * Медиа прямо внутри ответа ИИ: картинка, аудиоплеер или видео.
 * ИИ сам решает, что сгенерировать и какой моделью, — здесь только показ.
 */
const MEDIA_ICON = { image: '🖼', audio: '🔊', video: '🎬' };

function turnMediaPlayer(item) {
  const url = escapeHtml(item.url || '');
  const alt = escapeHtml(item.prompt || item.title || 'Сгенерировано ИИ');
  if (item.kind === 'audio') return `<audio controls preload="metadata" src="${url}"></audio>`;
  if (item.kind === 'video') return `<video controls playsinline preload="metadata" src="${url}"></video>`;
  return `<a class="turn-media-open" href="${url}" target="_blank" rel="noopener"><img src="${url}" alt="${alt}" loading="lazy"></a>`;
}

function turnMediaCaption(item) {
  const bits = [`${MEDIA_ICON[item.kind] || '✦'} ${escapeHtml(item.model_name || item.model || 'ИИ')}`];
  if (item.elapsed_ms) bits.push(`${(item.elapsed_ms / 1000).toFixed(1)} c`);
  const name = escapeHtml(item.filename || `novamind_${item.kind || 'media'}`);
  return `<figcaption><span class="turn-media-meta">${bits.join(' · ')}</span>`
    + `<a class="turn-media-dl" href="${escapeHtml(item.url || '')}" download="${name}" title="Скачать">⬇ Скачать</a></figcaption>`;
}

function turnMediaItemHtml(item) {
  return `<figure class="turn-media-item" data-kind="${escapeHtml(item.kind || 'image')}">`
    + `${turnMediaPlayer(item)}${turnMediaCaption(item)}</figure>`;
}

/** Видео у провайдера делается долго: опрашиваем задачу и подменяем карточку плеером. */
async function pollTurnMediaJob(item, figure) {
  const statusEl = figure.querySelector('.turn-media-status');
  for (let attempt = 0; attempt < 120; attempt++) {
    await new Promise((resolve) => setTimeout(resolve, window.NOVA_MEDIA_POLL_MS || 5000));
    if (!figure.isConnected) return;
    let data;
    try {
      const response = await fetch('/api/media/jobs/' + encodeURIComponent(item.job_id));
      data = await response.json();
      if (!response.ok) throw new Error(data.error || ('HTTP ' + response.status));
    } catch (error) {
      if (statusEl) statusEl.textContent = 'Не удалось узнать статус: ' + error.message;
      continue;
    }
    if (data.state === 'succeeded' && data.media) {
      const ready = { ...item, ...data.media, kind: 'video', job_id: null };
      figure.outerHTML = turnMediaItemHtml(ready);
      historyAddMessage('ai', 'MEDIA_RESULT:' + JSON.stringify({ ...ready, title: item.prompt || 'Видео' }));
      return;
    }
    if (['failed', 'cancelled', 'error'].includes(data.state)) {
      if (statusEl) statusEl.textContent = '❌ ' + (data.error || 'Провайдер отменил задачу');
      figure.classList.add('is-error');
      return;
    }
    if (statusEl) statusEl.textContent = `Видео готовится у провайдера… (${data.state}, проверок: ${data.polls})`;
  }
  if (statusEl) statusEl.textContent = 'Видео всё ещё готовится — загляните позже.';
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

// ══════════════════════════════════════════════════════════════════
// ASSISTANT TURN — «живой» пузырь ИИ
//
// Жизненный цикл:
//   beginIdle() / beginSearch() → сцена и шаги
//   reasoningToken()            → рассуждение внутри блока шагов
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
  const mediaData = [];
  // «Что делал агент»: шаги и команды этого хода — переживают перезагрузку.
  const traceData = [];
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
          <span class="stage-elapsed">0.0 c</span>
        </div>`;
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
      if (traceData.length < 60) traceData.push({ kind: 'step', icon: icon || '•', text: text || '' });
      scrollToBottom();
    },

    /** План агента: короткий блок над шагами. */
    /**
     * План и галочки выполнения.
     * Принимает строку (старый формат) или событие {text, steps:[{text,status}], done, total}.
     */
    plan(payload) {
      const event = payload && typeof payload === 'object' ? payload : { text: payload };
      if (!event.text && !(event.steps && event.steps.length)) return;
      ensureStage(null, '');
      let box = stage.querySelector('.stage-plan');
      if (!box) {
        box = document.createElement('div');
        box.className = 'stage-plan';
        stage.insertBefore(box, stepsEl);
      }
      if (Array.isArray(event.steps) && event.steps.length) {
        const done = Number.isFinite(event.done) ? event.done
          : event.steps.filter((step) => step.status === 'done').length;
        const total = event.total || event.steps.length;
        box.innerHTML = `
          <span class="stage-plan-label">План · ${done}/${total}</span>
          <ul class="stage-plan-steps">
            ${event.steps.map((step) => `
              <li class="is-${escapeHtml(step.status || 'todo')}">
                <span class="stage-plan-check">${step.status === 'done' ? '✔' : '•'}</span>
                ${escapeHtml(step.text || '')}
              </li>`).join('')}
          </ul>`;
      } else {
        box.innerHTML = `<span class="stage-plan-label">План</span><span class="stage-plan-text">${escapeHtml(event.text || '')}</span>`;
      }
      scrollToBottom();
    },

    /**
     * Один блок шагов на ответ: команды и запросы разрешений живут вместе.
     * Создаётся лениво — при первом инструменте или подтверждении.
     */
    ensureTerm() {
      let box = bubble.querySelector('.agent-term');
      if (box) return box;
      box = document.createElement('div');
      box.className = 'agent-term';
      box.innerHTML = `
        <button type="button" class="agent-term-head" aria-expanded="false">
          <span class="agent-term-ico">⌨️</span>
          <span class="agent-term-title">Шаги ИИ</span>
          <span class="agent-term-meta"></span>
          <span class="caret">▼</span>
        </button>
        <div class="agent-term-log"></div>`;
      box.querySelector('.agent-term-head').addEventListener('click', () => {
        // Пока висит вопрос, не даём спрятать разрешение вместе с блоком.
        if (box.querySelector('.agent-confirm:not(.is-approved):not(.is-denied)')) {
          api.setTermOpen(box, true);
          return;
        }
        api.setTermOpen(box, !box.classList.contains('is-open'));
      });
      bubble.insertBefore(box, answerEl);
      return box;
    },

    setTermOpen(box, open) {
      if (!box) return;
      box.classList.toggle('is-open', !!open);
      const head = box.querySelector('.agent-term-head');
      if (head) head.setAttribute('aria-expanded', open ? 'true' : 'false');
    },

    /** Подпись в шапке: число шагов, ошибки или «ждёт разрешения». */
    refreshTermMeta(box) {
      if (!box) return;
      const meta = box.querySelector('.agent-term-meta');
      if (!meta) return;
      const rows = box.querySelectorAll('.agent-term-row').length;
      const confirms = box.querySelectorAll('.agent-confirm').length;
      const reasons = box.querySelectorAll('.agent-term-reason').length;
      const count = rows + confirms + reasons;
      const failed = box.querySelectorAll('.agent-term-row.is-error').length;
      const pending = box.querySelectorAll('.agent-confirm:not(.is-approved):not(.is-denied)').length;
      const n10 = count % 10, n100 = count % 100;
      const word = (n10 === 1 && n100 !== 11) ? 'шаг'
        : (n10 >= 2 && n10 <= 4 && (n100 < 12 || n100 > 14)) ? 'шага' : 'шагов';
      if (pending) {
        meta.textContent = `⏳ ждёт разрешения${count ? ` · ${count} ${word}` : ''}`;
      } else {
        meta.textContent = count ? `${count} ${word}${failed ? ` · ошибок: ${failed}` : ''}` : '';
      }
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
      const box = api.ensureTerm();
      // Блок появляется ещё на рассуждении; «Работа в Linux» — только когда
      // реально выполнилась команда в окружении.
      if (window.Linux && window.Linux.hasLinux && window.Linux.hasLinux()) {
        const title = box.querySelector('.agent-term-title');
        if (title) title.textContent = 'Работа в Linux';
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
      api.refreshTermMeta(box);
      if (traceData.length < 60) {
        traceData.push({ kind: 'tool', name: event.name || 'run',
          command: command, code: event.code ?? 0 });
      }
      // Блок остаётся свёрнутым: раскрывается только по нажатию пользователя
      scrollToBottom();
    },

    /** Свернуть блок команд: дальше ИИ пишет ответ, а не работает руками. */
    finishTerm() {
      const box = bubble.querySelector('.agent-term');
      if (!box) return;
      // Незакрытый вопрос важнее: оставляем шаги раскрытыми, пока ждём ответа.
      if (box.querySelector('.agent-confirm:not(.is-approved):not(.is-denied)')) return;
      api.setTermOpen(box, false);
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

    /** ИИ сгенерировал картинку, аудио или видео — показываем прямо в ответе. */
    addMedia(item = {}) {
      if (!item.url && !item.job_id) return;
      let box = bubble.querySelector('.turn-media');
      if (!box) {
        box = document.createElement('div');
        box.className = 'turn-media';
        bubble.insertBefore(box, answerEl);
      }
      const holder = document.createElement('div');
      if (item.job_id && !item.url) {
        holder.innerHTML = `<figure class="turn-media-item is-pending" data-kind="video">
          <div class="turn-media-wait"><span class="turn-media-spin"></span>
          <span class="turn-media-status">Видео готовится у провайдера…</span></div>
          <figcaption><span class="turn-media-meta">🎬 ${escapeHtml(item.model_name || 'видео')}</span></figcaption></figure>`;
        box.appendChild(holder.firstElementChild);
        pollTurnMediaJob(item, box.lastElementChild);
      } else {
        holder.innerHTML = turnMediaItemHtml(item);
        box.appendChild(holder.firstElementChild);
        mediaData.push(item);
      }
      scrollToBottom();
    },

    /**
     * Рассуждение модели — строка внутри блока шагов, а не панель в чате.
     * Свёрнута: пользователь раскрывает её сам, ответу она не мешает.
     */
    reasoningToken(token) {
      if (!token) return;
      reasoningText += token;
      if (!reasoningBox) {
        const term = api.ensureTerm();
        api.setTermOpen(term, true);
        reasoningBox = document.createElement('details');
        reasoningBox.className = 'agent-term-reason';
        reasoningBox.innerHTML = `
          <summary>
            <span class="agent-term-reason-ico">🧠</span>
            <span>Рассуждение</span>
            <span class="caret">▶</span>
          </summary>
          <div class="agent-term-reason-text"></div>`;
        term.querySelector('.agent-term-log').appendChild(reasoningBox);
        api.refreshTermMeta(term);
      }
      reasoningBox.querySelector('.agent-term-reason-text').textContent = reasoningText;
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
      if (!historyReplay && window.NovaSpeech && window.NovaSpeech.streamFeed) window.NovaSpeech.streamFeed(wrap, answerText);
      answerEl.innerHTML = formatContent(answerText);
      scrollToBottom();
    },

    appendText(text) {
      answerText = text || '';
      answerEl.innerHTML = formatContent(answerText);
      scrollToBottom();
    },

    /** Карточка «Разрешить / Отклонить» внутри блока шагов агента. */
    confirm(request) {
      const term = api.ensureTerm();
      const log = term.querySelector('.agent-term-log');
      const box = document.createElement('div');
      box.className = 'agent-confirm is-open';
      box.dataset.id = request.id || '';
      const tool = escapeHtml(request.tool || 'действие');
      const command = escapeHtml(request.command || '');
      const args = request.args ? escapeHtml(JSON.stringify(request.args, null, 2)) : '';
      box.innerHTML = `
        <button type="button" class="agent-confirm-head" aria-expanded="true">
          <span class="agent-confirm-ico">⚠️</span>
          <span class="agent-confirm-title">Нужно разрешение · ${tool}</span>
          <span class="agent-confirm-risk">${escapeHtml(request.risk || 'danger')}</span>
          <span class="agent-confirm-note"></span>
          <span class="caret">▼</span>
        </button>
        <div class="agent-confirm-body">
          ${command ? `<pre class="agent-confirm-cmd">${command}</pre>` : ''}
          ${args ? `<details class="agent-confirm-args"><summary>Аргументы</summary><pre>${args}</pre></details>` : ''}
        </div>
        <div class="agent-confirm-actions">
          <button type="button" class="ok" data-confirm="yes">Разрешить</button>
          <button type="button" class="always" data-confirm="always"
                  title="Больше не спрашивать для таких действий до перезапуска сервера">✅ Разрешить всегда</button>
          <button type="button" class="no" data-confirm="no">Отклонить</button>
        </div>`;
      log.appendChild(box);
      // Пока ждём решение — блок раскрыт, чтобы вопрос был виден сразу.
      api.setTermOpen(term, true);
      api.refreshTermMeta(term);

      const head = box.querySelector('.agent-confirm-head');
      const note = box.querySelector('.agent-confirm-note');
      head.addEventListener('click', () => {
        box.classList.toggle('is-open');
        head.setAttribute('aria-expanded', box.classList.contains('is-open'));
      });

      const settle = (approve, text) => {
        if (box.classList.contains('is-approved') || box.classList.contains('is-denied')) return;
        box.querySelectorAll('[data-confirm]').forEach((button) => { button.disabled = true; });
        box.classList.add(approve ? 'is-approved' : 'is-denied');
        box.classList.remove('is-open');
        head.setAttribute('aria-expanded', 'false');
        note.textContent = text || (approve ? '✔ разрешено' : '✕ отклонено');
        api.refreshTermMeta(term);
      };
      const decide = (approve, always = false) => {
        const alwaysOn = Boolean(approve && always);
        settle(approve, alwaysOn ? '✔ разрешено всегда'
          : (approve ? '✔ разрешено' : '✕ отклонено'));
        box.classList.toggle('is-always', alwaysOn);
        fetch('/api/agent/confirm', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id: request.id, approve, always: alwaysOn }),
        }).then((response) => {
          if (!response.ok) note.textContent = 'решение не принято: время вышло';
        }).catch(() => { note.textContent = 'нет связи с сервером'; });
      };
      box.querySelector('[data-confirm="yes"]').addEventListener('click', () => decide(true));
      box.querySelector('[data-confirm="always"]').addEventListener('click', () => decide(true, true));
      box.querySelector('[data-confirm="no"]').addEventListener('click', () => decide(false));
      box._settle = settle;
      scrollToBottom();
      return box;
    },

    /** Ответ на подтверждение уже обработан сервером (пришёл confirm_result). */
    confirmResult(event) {
      const box = bubble.querySelector(`.agent-confirm[data-id="${event.id}"]`);
      if (!box) return;
      if (box._settle) {
        box.classList.toggle('is-always', Boolean(event.approved && event.always));
        box._settle(!!event.approved, event.approved
          ? (event.always ? '✔ разрешено всегда' : '✔ разрешено')
          : '✕ отклонено');
      } else {
        box.classList.add(event.approved ? 'is-approved' : 'is-denied');
        box.querySelectorAll('button').forEach((button) => { button.disabled = true; });
        const note = box.querySelector('.agent-confirm-note');
        if (note && !note.textContent) note.textContent = event.approved ? 'Разрешено' : 'Отклонено';
      }
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
        reasoningBox.classList.add('is-done');
        reasoningBox.removeAttribute('open');
      }

      const seconds = ((Date.now() - startedAt) / 1000).toFixed(1);
      const bits = [];
      if (meta.model) bits.push(`модель: ${escapeHtml(meta.model)}`);
      if (meta.provider) bits.push(escapeHtml(meta.provider));
      if (meta.offline) bits.push('офлайн');
      if (meta.searched) bits.push('с поиском');
      if (meta.agent || meta.linux) bits.push('Linux');
      if (meta.steps) bits.push(`шагов: ${meta.steps}`);
      if (meta.usage && meta.usage.total_tokens) {
        bits.push(`токенов: ${formatTokens(meta.usage.total_tokens)}`
          + ` (${formatCost(meta.usage.cost_known === false ? null : meta.usage.cost_usd)})`);
      }
      if (meta.stopped) bits.push('остановлено');
      if (sourcesData.length) bits.push(`источников: ${sourcesData.length}`);
      bits.push(`${seconds} c`);
      metaEl.innerHTML = `<span>${bits.join(' · ')}</span>`;

      actionsEl.innerHTML = `
        <button type="button" data-act="copy" title="Скопировать ответ">📋 Копировать</button>
        ${speakButtonHtml()}
        <button type="button" data-act="regen" title="Ответить заново">↻ Заново</button>`;
      actionsEl.querySelector('[data-act="copy"]').addEventListener('click', () => {
        copyText(answerText || bubble.innerText);
      });
      bindSpeakButton(actionsEl, wrap, () => answerText);
      actionsEl.querySelector('[data-act="regen"]').addEventListener('click', () => {
        if (lastUserMessage) sendMessage(lastUserMessage);
      });

      // Автоозвучка: ровно один раз на завершённый ответ, никогда — для
      // истории, ошибки или остановленной генерации (проверяется в контроллере).
      if (window.NovaSpeech) {
        window.NovaSpeech.onTurnFinished(wrap, answerText, {
          stopped: Boolean(meta.stopped), auto: true,
        }).catch?.(() => {});
      }
      updateComposerSpeak();

      if (!historyReplay && answerText) {
        historyAddMessage('ai', answerText, {
          sources: sourcesData.slice(0, 12),
          media: mediaData.length ? mediaData.slice(0, 6) : undefined,
          model: meta.model || null,
          elapsed_ms: Date.now() - startedAt,
          reasoning: reasoningText || null,
          trace: traceData.length ? traceData.slice(0, 60) : undefined,
          usage: meta.usage ? {
            total_tokens: meta.usage.total_tokens,
            cost_usd: meta.usage.cost_usd,
            cost_known: meta.usage.cost_known,
          } : undefined,
          server_chat_id: meta.serverChatId || null,
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
      updateComposerSpeak();
      scrollToBottom();
    },

    remove() {
      stopTimer();
      wrap.remove();
    },

    get text() { return answerText; },
    get sources() { return sourcesData; },
    get media() { return mediaData; },
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


// ══════════════════════════════════════════
// ОЗВУЧКА ОТВЕТОВ (кнопки у сообщений и под полем ввода)
// ══════════════════════════════════════════

/** Разметка кнопки озвучки: одна на сообщение, состояние — в data-speech-state. */
function speakButtonHtml() {
  return '<button type="button" data-act="speak" data-speech-state="idle"'
    + ' title="Озвучить ответ" aria-label="Озвучить ответ">'
    + '<span data-speech-icon>🔊</span> <span data-speech-label>Озвучить</span></button>';
}

/** Привязывает кнопку к конкретному ответу — не к «последнему сообщению в чате». */
function bindSpeakButton(container, element, getText) {
  const button = container.querySelector('[data-act="speak"]');
  if (!button || !window.NovaSpeech) return;
  window.NovaSpeech.bindButton(button, element, getText);
}

/** Кнопка под полем ввода: озвучивает последний готовый ответ, иначе disabled. */
function updateComposerSpeak() {
  const button = document.getElementById('btn-speak');
  if (!button) return;
  const element = lastAssistantElement();
  const playing = window.NovaSpeech && window.NovaSpeech._state().current;
  const same = Boolean(playing && element && playing.element === element);
  button.disabled = !element || !lastAssistantText();
  button.classList.toggle('active', Boolean(same));
  button.dataset.speechState = same
    ? (playing.paused ? 'paused' : 'playing') : 'idle';
  const label = button.querySelector('#speakBtnText');
  if (label) {
    label.textContent = same
      ? (playing.paused ? 'Продолжить' : 'Стоп')
      : 'Озвучить';
  }
  button.title = same ? 'Остановить озвучку' : 'Озвучить последний ответ';
}

/** Клик по кнопке под полем ввода. */
function speakLastAnswer() {
  resetSpeechIdleTimer();
  if (!window.NovaSpeech) {
    showNotification('Озвучка не загрузилась', 'warn');
    return;
  }
  const element = lastAssistantElement();
  if (!element) {
    showNotification('Пока нечего озвучивать', 'warn');
    return;
  }
  const text = lastAssistantText();
  if (!text) return;
  window.NovaSpeech.toggle(element, () => text).catch?.(() => {});
}

/** Раз в несколько секунд обновляем подпись кнопки под полем ввода. */
let speechIdleTimer = null;
function resetSpeechIdleTimer() {
  if (speechIdleTimer) { clearInterval(speechIdleTimer); speechIdleTimer = null; }
  speechIdleTimer = setInterval(updateComposerSpeak, 500);
  setTimeout(() => {
    if (speechIdleTimer) { clearInterval(speechIdleTimer); speechIdleTimer = null; }
    updateComposerSpeak();
  }, 4000);
}

if (typeof window !== 'undefined') {
  window.addEventListener('nova-speech', () => updateComposerSpeak());
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

/**
 * Озвучка: единая точка входа — `static/tts.js` (window.NovaSpeech).
 *
 * Здесь осталась только тонкая обёртка для старых вызовов: сам синтез, очередь,
 * состояния кнопок и остановка живут в контроллере, чтобы не было второй
 * независимой системы озвучки.
 */
function speakText(text, element = null) {
  if (!window.NovaSpeech) {
    showNotification('Озвучка не загрузилась', 'warn');
    return Promise.resolve(false);
  }
  const target = element || lastAssistantElement();
  if (!target) {
    showNotification('Пока нечего озвучивать', 'warn');
    return Promise.resolve(false);
  }
  return window.NovaSpeech.speak(target, text, { auto: false });
}

/** Последний ответ ИИ в текущем диалоге (для кнопки под полем ввода). */
function lastAssistantElement() {
  const turns = chatContainer.querySelectorAll('.message.ai');
  return turns.length ? turns[turns.length - 1] : null;
}

/** Текст последнего ответа ИИ — из пузыря, если контроллер не отдал свой. */
function lastAssistantText() {
  const element = lastAssistantElement();
  if (!element) return '';
  const answer = element.querySelector('.answer');
  return String((answer && answer.textContent) || '').trim();
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
  // Агент работает на сервере: без run_id он продолжил бы шаги после
  // закрытия соединения. Поэтому сначала просим его остановиться.
  if (window.Linux && typeof window.Linux.stopRun === 'function') window.Linux.stopRun();
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

    // ── Главный чат: ИИ-агент ──
    // ИИ сам решает, что делать: ответить сразу, поискать, нарисовать картинку,
    // озвучить текст, сделать видео или поработать в Linux по шагам.
    // Модель генерации медиа тоже выбирает он — кнопок выбора нет.
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
    updateComposerSpeak();
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
  if (input) input.placeholder = input.dataset.defaultPlaceholder || 'Напишите сообщение...';
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

  const savedMedia = isAI && Array.isArray(meta?.media) ? meta.media.filter((m) => m && m.url) : [];
  const mediaHtml = savedMedia.length
    ? `<div class="turn-media">${savedMedia.map(turnMediaItemHtml).join('')}</div>` : '';
  const traceSteps = isAI && Array.isArray(meta?.trace) ? meta.trace : [];
  const traceHtml = (traceSteps.length || meta?.reasoning)
    ? agentTraceHtml(traceSteps, meta?.reasoning) : '';
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
        ${mediaHtml}
        ${traceHtml}
        <div class="answer">${formatted}${imageHtml}</div>
        ${sourcesHtml}
      </div>
      ${isAI && meta?.model ? `<div class="msg-meta"><span>модель: ${escapeHtml(String(meta.model))}${meta.elapsed_ms ? ' · ' + (meta.elapsed_ms / 1000).toFixed(1) + ' c' : ''}</span></div>` : ''}
      ${isAI ? `<div class="msg-actions">
        <button type="button" data-act="copy">📋 Копировать</button>
        ${speakButtonHtml()}
      </div>` : ''}
    </div>`;

  chatContainer.appendChild(wrap);

  const answerText = String(content || '');
  wrap.querySelector('[data-act="copy"]')?.addEventListener('click', () => copyText(answerText));
  if (isAI) bindSpeakButton(wrap, wrap, () => answerText);
  wrap.querySelector('.sources-head')?.addEventListener('click', (event) => {
    event.currentTarget.parentElement.classList.toggle('min');
  });
  // Рассуждение теперь живёт внутри блока шагов (details), отдельный
  // обработчик не нужен — раскрытие делает сам <details>.

  scrollToBottom();

  if (!historyReplay && (role === 'user' || role === 'ai') && content) {
    historyAddMessage(role, content, meta);
  }
}

// ── Счётчик токенов и стоимости (Б3) ─────────────────────────────
function formatTokens(count) {
  const value = Number(count) || 0;
  if (value >= 1_000_000) return (value / 1_000_000).toFixed(1) + ' млн';
  if (value >= 1_000) return (value / 1_000).toFixed(1) + ' тыс.';
  return String(value);
}

function formatCost(cost) {
  if (cost === null || cost === undefined) return 'цена неизвестна';
  if (cost <= 0) return 'бесплатно';
  if (cost < 0.01) return '< $0.01';
  return '$' + cost.toFixed(2);
}

let usageWarned = false;

/** Итоги чата в шапке: токены, деньги и предупреждение о лимите Groq. */
async function refreshUsage(showWarning = false) {
  const chip = document.getElementById('usageChip');
  try {
    const response = await fetch('/api/usage');
    if (!response.ok) return null;
    const data = await response.json();
    const chat = data.chat || {};
    if (chip) {
      chip.textContent = `≈ ${formatTokens(chat.total_tokens)} ток.`;
      chip.title = `За чат: ${chat.total_tokens || 0} токенов · ${formatCost(chat.cost_usd)}`
        + `\nХодов: ${chat.turns || 0}`
        + (data.groq && data.groq.keys_total
          ? `\nGroq: осталось ${data.groq.remaining_tokens ?? '—'} токенов,`
            + ` ${data.groq.remaining_requests ?? '—'} запросов`
          : '');
    }
    if (showWarning && data.groq && data.groq.warning && !usageWarned) {
      usageWarned = true;
      showNotification(data.groq.warning, 'warn');
    }
    return data;
  } catch (_) {
    return null;               // счётчик — не критичная часть интерфейса
  }
}
window.refreshUsage = refreshUsage;

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
        tableHtml += `<${tag} style="text-align:${align}">${cell.trim()}</${tag}>`;
      });
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
  if (window.NovaSpeech) window.NovaSpeech.onChatChanged();
  const store = historyLoad();
  store.activeId = null;
  historySave(store);
  // Новый чат — чистый лист: никаких активных режимов.
  clearInputMode(true);
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
  // Кнопка озвучки смотрит на последний ответ: после очистки она снова неактивна.
  updateComposerSpeak();
}

function clearChat() {
  if (window.NovaSpeech) window.NovaSpeech.onChatChanged();
  updateComposerSpeak();
  newChat();
  fetch('/api/history/clear', { method: 'DELETE' }).catch(() => {});
  showNotification('Чат очищен', 'info');
}

function shareChat() {
  copyText(window.location.href);
}

let scrollFrame = 0;
function scrollToBottom() {
  if (scrollFrame) return;
  scrollFrame = requestAnimationFrame(() => {
    scrollFrame = 0;
    // Прокручивается всё окно, поэтому целимся в нижний край панели ввода.
    // Во время потоковой генерации нельзя запускать smooth-scroll на каждый
    // токен: браузер складывает анимации, из-за чего нижняя кромка bubble
    // визуально дрожит/искажается. Один auto-scroll на кадр этого не делает.
    const tail = document.querySelector('.input-area');
    const anchor = tail || chatContainer;
    const bottom = anchor.getBoundingClientRect().bottom;
    const max = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
    // bottom — координата внутри текущего viewport. Учитываем CSS bottom:
    // 10px на ПК и 0px на телефоне, иначе выравнивание по самому низу
    // viewport создавало разную геометрию и лишний хвост страницы.
    const bottomOffset = Math.max(0, parseFloat(getComputedStyle(anchor).bottom) || 0);
    const viewportHeight = document.documentElement.clientHeight || window.innerHeight;
    const target = Math.min(max, Math.max(0, window.scrollY + bottom - (viewportHeight - bottomOffset)));
    if (Math.abs(target - window.scrollY) < 2) return;
    window.scrollTo({ top: target, behavior: isTyping ? 'auto' : 'smooth' });
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
  if (meta && meta.server_chat_id) chat.serverId = meta.server_chat_id;

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

  // Новый диалог — другая озвучка: старый голос и очередь сбрасываются.
  if (window.NovaSpeech) window.NovaSpeech.onChatChanged();
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
  updateComposerSpeak();
  scrollToBottom();
  attachServerTraces(chat);
}

/**
 * Панель «Что делал агент».
 * Раскрытая details-карточка со шагами и командами — без JS-обработчиков.
 */
function agentTraceHtml(steps, reasoning) {
  const list = Array.isArray(steps) ? steps : [];
  if (!list.length && !reasoning) return '';
  let rows = list.map((step) => {
    if (step.kind === 'tool') {
      const label = step.command || step.name || 'команда';
      return `<div class="agent-trace-row"><span class="agent-trace-ico">⌨️</span>` +
        `<code class="agent-trace-cmd">${escapeHtml(String(label))}</code></div>`;
    }
    if (step.kind === 'plan') {
      return `<div class="agent-trace-row"><span class="agent-trace-ico">🗺</span>` +
        `<span>${escapeHtml(String(step.text || ''))}</span></div>`;
    }
    return `<div class="agent-trace-row"><span class="agent-trace-ico">${escapeHtml(String(step.icon || '•'))}</span>` +
      `<span>${escapeHtml(String(step.text || ''))}</span></div>`;
  }).join('');
  if (reasoning) {
    rows = `<details class="agent-trace-reason">
        <summary>🧠 Рассуждение</summary>
        <div class="agent-trace-reason-text">${escapeHtml(String(reasoning))}</div>
      </details>` + rows;
  }
  const count = list.length + (reasoning ? 1 : 0);
  return `<details class="agent-trace">
      <summary>🧭 Что делал агент · ${count}</summary>
      <div class="agent-trace-steps">${rows}</div>
    </details>`;
}

/**
 * После загрузки чата подтягиваем трассы из БД: локальная история живёт
 * в браузере, а шаги агента хранятся на сервере — так панель видна даже
 * после очистки кеша.
 */
async function attachServerTraces(chat) {
  if (!chat || !chat.serverId) return;
  let traces = [];
  try {
    const response = await fetch(`/api/chats/${encodeURIComponent(chat.serverId)}/traces`);
    if (!response.ok) return;
    const data = await response.json();
    traces = Array.isArray(data.traces) ? data.traces : [];
  } catch (_) { return; }
  if (!traces.length) return;
  const bubbles = Array.from(chatContainer.querySelectorAll('.message.ai .msg-bubble'))
    .filter((bubble) => bubble.querySelector('.answer'));
  const start = Math.max(0, bubbles.length - traces.length);
  traces.forEach((trace, index) => {
    const bubble = bubbles[start + index];
    if (!bubble || bubble.querySelector('.agent-trace')) return;
    const html = agentTraceHtml(trace.steps || []);
    if (!html) return;
    const answer = bubble.querySelector('.answer');
    if (answer) answer.insertAdjacentHTML('beforebegin', html);
  });
}

function deleteChat(chatId) {
  const store = historyLoad();
  store.chats = (store.chats || []).filter((c) => c.id !== chatId);
  if (store.activeId === chatId) store.activeId = null;
  historySave(store);
  renderChatList();
}

function startNewChat() {
  if (window.NovaSpeech) window.NovaSpeech.onChatChanged();
  updateComposerSpeak();
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
  refreshAiStatus();
  checkHealth(false);
  renderChatList();
  updateComposerSpeak();
  // Озвучка: подтягиваем настройки и статус провайдера один раз при загрузке.
  if (window.NovaSpeech) window.NovaSpeech.init().catch(() => {});

  window.addEventListener('online', () => {
    showNotification('Соединение восстановлено', 'ok');
    refreshAiStatus();
    checkHealth(false);
  });
  window.addEventListener('offline', () => showNotification('Нет соединения с интернетом', 'warn'));
})();
