/**
 * static/tts.js — озвучка ответов ИИ в интерфейсе.
 *
 * Что делает
 * ----------
 * • Просит сервер озвучить текст ответа (`POST /api/tts/speech`, NDJSON):
 *   первая часть приходит и играет, пока генерируются следующие;
 * • держит одно активное воспроизведение на вкладку: новый запуск останавливает
 *   предыдущий, поэтому два голоса одновременно не звучат;
 * • умеет паузу/продолжение, остановку, отмену очереди и отмену запроса
 *   (AbortController + `POST /api/tts/cancel` по job_id);
 * • запоздавшие части отменённого запуска не играют: у каждого запуска свой
 *   generation-счётчик, старые события отбрасываются;
 * • автоматическая озвучка — после завершения ответа, ровно один раз на ответ;
 * • если TTS-провайдер не настроен, честно переходит на браузерный Web Speech
 *   API и помечает это в состоянии (кнопка показывает «резерв»), а не выдаёт
 *   синтез браузера за ответ модели.
 *
 * Настройки
 * ---------
 * Источник истины — сервер (`/api/tts/settings` → runtime_settings.json).
 * localStorage хранит только копию на случай недоступного сервера.
 *
 * События
 * -------
 * `window.dispatchEvent(new CustomEvent('nova-speech', {detail}))` где detail —
 * {state, element, mode, ...}. По ним интерфейс красит кнопки.
 */
(() => {
  'use strict';

  const SETTINGS_CACHE = 'nova_speech_settings';
  const STATES = {
    idle: 'Озвучить',
    loading: 'Готовлю аудио…',
    playing: 'Играет — пауза',
    paused: 'Пауза — продолжить',
    error: 'Ошибка озвучки',
    fallback: 'Браузерная озвучка',
  };

  const t = (text) => (window.I18N && window.I18N.translateText
    ? window.I18N.translateText(text) : text);

  const state = {
    status: null,
    settings: readCachedSettings(),
    settingsLoaded: false,
    loadingPromise: null,
    current: null,        // активный запуск
    generation: 0,        // защита от запоздавших событий
    spoken: new WeakSet(), // элементы, которые уже озвучивались автоматически
    audioFactory: defaultAudioFactory,
    listeners: new Set(),
  };

  // ─────────────────────────── настройки ───────────────────────────
  function readCachedSettings() {
    try {
      const raw = JSON.parse(localStorage.getItem(SETTINGS_CACHE) || '{}');
      return raw && typeof raw === 'object' ? raw : {};
    } catch (_) {
      return {};
    }
  }

  function writeCachedSettings(settings) {
    try { localStorage.setItem(SETTINGS_CACHE, JSON.stringify(settings || {})); } catch (_) { /* приватный режим */ }
  }

  async function loadSettings(force = false) {
    if (state.settingsLoaded && !force) return state.settings;
    if (state.loadingPromise && !force) return state.loadingPromise;
    state.loadingPromise = fetch('/api/tts/settings')
      .then((response) => (response.ok ? response.json() : null))
      .then((data) => {
        if (data && data.settings) {
          state.settings = data.settings;
          state.settingsLoaded = true;
          writeCachedSettings(data.settings);
        }
        if (data && data.status) state.status = data.status;
        return state.settings;
      })
      .catch(() => state.settings)
      .finally(() => { state.loadingPromise = null; });
    return state.loadingPromise;
  }

  async function saveSettings(patch) {
    const body = { ...state.settings, ...patch };
    const response = await fetch('/api/tts/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(data.error || `HTTP ${response.status}`);
    }
    state.settings = data.settings || body;
    state.settingsLoaded = true;
    writeCachedSettings(state.settings);
    // Выключили автоозвучку — гасим только автоматическую, ручную не трогаем.
    if (!state.settings.auto_speak && state.current && state.current.auto) stop('auto-off');
    emit({ state: 'settings', settings: state.settings });
    return state.settings;
  }

  function autoSpeakEnabled() {
    return Boolean(state.settings && state.settings.auto_speak);
  }

  // ─────────────────────────── статус провайдера ───────────────────────────
  async function loadStatus(force = false) {
    if (state.status && !force) return state.status;
    try {
      const response = await fetch('/api/tts/status');
      if (response.ok) {
        state.status = await response.json();
        if (state.status.settings) {
          state.settings = state.status.settings;
          state.settingsLoaded = true;
          writeCachedSettings(state.settings);
        }
      }
    } catch (_) { /* статус не критичен: озвучка попробует сервер и честно упадёт */ }
    return state.status;
  }

  function available() {
    return Boolean(state.status && state.status.available);
  }

  // ─────────────────────────── аудио ───────────────────────────
  function defaultAudioFactory(url) {
    // eslint-disable-next-line no-undef
    return new Audio(url);
  }

  function setAudioFactory(factory) {
    state.audioFactory = typeof factory === 'function' ? factory : defaultAudioFactory;
  }

  function emit(detail) {
    state.listeners.forEach((listener) => {
      try { listener(detail); } catch (_) { /* подписчик не должен ломать озвучку */ }
    });
    if (typeof window.dispatchEvent === 'function' && typeof CustomEvent === 'function') {
      window.dispatchEvent(new CustomEvent('nova-speech', { detail }));
    }
  }

  function paint(element, status, extra = {}) {
    if (!element) return;
    const button = element.querySelector('[data-act="speak"]') || element;
    const label = extra.label || STATES[status] || STATES.idle;
    button.dataset.speechState = status;
    button.setAttribute('aria-label', t(label));
    button.title = t(extra.title || label);
    button.classList.toggle('is-speaking', status === 'playing');
    button.classList.toggle('is-loading', status === 'loading');
    button.classList.toggle('is-error', status === 'error');
    button.disabled = false;
    const text = button.querySelector('[data-speech-label]');
    if (text) text.textContent = t(label);
    const icon = button.querySelector('[data-speech-icon]');
    if (icon) icon.textContent = extra.icon || iconFor(status);
    emit({ state: status, element, ...extra });
  }

  function iconFor(status) {
    return {
      idle: '🔊', loading: '⏳', playing: '⏸', paused: '▶️', error: '⚠️', fallback: '🗣',
    }[status] || '🔊';
  }

  function stopAudio(audio) {
    if (!audio) return;
    try {
      audio.pause();
      audio.removeAttribute('src');
      audio.load?.();
    } catch (_) { /* некоторые браузеры бросают на пустом src */ }
  }

  function browserSpeech(text) {
    if (!('speechSynthesis' in window) || typeof SpeechSynthesisUtterance !== 'function') {
      return null;
    }
    const clean = String(text || '')
      .replace(/```[\s\S]*?```/g, ' ')
      .replace(/[*#`|>]/g, '')
      .replace(/\s+/g, ' ')
      .trim()
      .slice(0, 4000);
    if (!clean) return null;
    const utterance = new SpeechSynthesisUtterance(clean);
    const lang = String(state.settings.lang || '');
    utterance.lang = lang === 'en' ? 'en-US' : 'ru-RU';
    utterance.rate = 1.02;
    return utterance;
  }

  // ─────────────────────────── основной запуск ───────────────────────────
  function stop(reason = 'manual') {
    const current = state.current;
    state.generation += 1;           // всё, что придёт от старых запросов, игнорируем
    state.current = null;
    if (!current) {
      if (typeof window.speechSynthesis !== 'undefined' && window.speechSynthesis.cancel) {
        try { window.speechSynthesis.cancel(); } catch (_) { /* не поддерживается */ }
      }
      return false;
    }
    try { current.controller.abort(); } catch (_) { /* уже завершён */ }
    (current.queue || []).length = 0;
    stopAudio(current.audio);
    if (current.jobId) {
      fetch('/api/tts/cancel', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ job_id: current.jobId }),
      }).catch(() => { /* отмена best-effort: сервер всё равно проверит флаг */ });
    }
    if (typeof window.speechSynthesis !== 'undefined' && window.speechSynthesis.cancel) {
      try { window.speechSynthesis.cancel(); } catch (_) { /* не поддерживается */ }
    }
    const element = current.element;
    if (element) paint(element, reason === 'error' ? 'error' : 'idle', { reason });
    emit({ state: 'stopped', element, reason });
    return true;
  }

  function pause(element) {
    const current = state.current;
    if (!current || (element && current.element !== element)) return false;
    if (current.audio && !current.audio.paused) {
      try {
        current.audio.pause();
        current.paused = true;
        paint(current.element, 'paused');
        return true;
      } catch (_) { return false; }
    }
    return false;
  }

  function resume(element) {
    const current = state.current;
    if (!current || (element && current.element !== element)) return false;
    if (current.audio && current.paused) {
      current.paused = false;
      paint(current.element, 'playing');
      const playing = current.audio.play();
      if (playing && playing.catch) playing.catch(() => paint(current.element, 'error', { title: 'Браузер не дал продолжить воспроизведение' }));
      return true;
    }
    return false;
  }

  /**
   * Озвучивает текст и привязывает состояние к элементу сообщения.
   *
   * @param {Element} element элемент сообщения ИИ (кнопка берётся внутри)
   * @param {string} text исходный ответ (Markdown)
   * @param {object} options {auto: boolean, force: boolean}
   */
  async function speak(element, text, options = {}) {
    const clean = String(text || '').trim();
    if (!clean) {
      if (element) paint(element, 'error', { title: 'Нечего озвучивать' });
      return false;
    }
    if (options.auto && !autoSpeakEnabled()) return false;
    if (options.auto) {
      if (state.spoken.has(element)) return false;      // один ответ — одна авто-озвучка
      state.spoken.add(element);
    }
    const started = startPlayback(element, clean, options);
    return started;
  }

  function startPlayback(element, text, options) {
    stop('replaced');                                  // один голос за раз
    const generation = state.generation;
    const controller = new AbortController();
    const current = {
      element, controller, queue: [], audio: null, jobId: '',
      paused: false, mode: 'ai', auto: Boolean(options.auto),
      chunksPlayed: 0, text,
    };
    state.current = current;
    paint(element, 'loading', { icon: '⏳' });

    const isAlive = () => state.current === current && state.generation === generation;

    const playNext = () => {
      if (!isAlive()) return;
      const next = current.queue.shift();
      if (!next) {
        if (current.finished) finish();
        return;
      }
      const audio = state.audioFactory(next.url);
      current.audio = audio;
      current.paused = false;
      const advance = () => {
        if (!isAlive()) return;
        current.chunksPlayed += 1;
        paint(element, 'playing');
        playNext();
      };
      audio.addEventListener('ended', advance);
      audio.addEventListener('error', () => {
        if (!isAlive()) return;
        paint(element, 'error', { title: 'Не удалось прочитать часть аудио' });
        emit({ state: 'audio-error', element, url: next.url });
        stopAudio(audio);
        advance();
      });
      // play() возвращает Promise почти во всех браузерах, поэтому состояние
      // «играет» ставим сразу, а отклонение промиса обрабатываем отдельно —
      // иначе кнопка висела бы в «Готовлю аудио…» до конца первой части.
      paint(element, 'playing');
      const playing = audio.play();
      if (playing && playing.catch) {
        playing.catch(() => {
          if (!isAlive()) return;
          paint(element, 'error', { title: 'Браузер заблокировал автозапуск — нажмите кнопку' });
          emit({ state: 'blocked', element });
        });
      }
    };

    const finish = () => {
      if (!isAlive()) return;
      state.current = null;
      paint(element, 'idle', { icon: '🔊', title: 'Озвучить ещё раз' });
      emit({ state: 'finished', element, chunks: current.chunksPlayed });
    };

    fetch('/api/tts/speech', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text,
        voice: state.settings.voice || '',
        provider: state.settings.provider || '',
        model: state.settings.model || '',
        read_code: Boolean(state.settings.read_code),
        lang: state.settings.lang || '',
        auto: Boolean(options.auto),
      }),
      signal: controller.signal,
    }).then(async (response) => {
      if (!response.ok) {
        const raw = await response.text().catch(() => '');
        let message = `HTTP ${response.status}`;
        try { message = JSON.parse(raw).error || message; } catch (_) { /* не JSON */ }
        throw new Error(message);
      }
      if (!response.body || !response.body.getReader) {
        throw new Error('Браузер не поддерживает потоковый ответ');
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let finished = false;
      while (true) {
        const { value, done } = await reader.read();
        if (!isAlive()) { try { reader.cancel(); } catch (_) {} return; }
        buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';
        for (const line of lines) {
          const event = safeParse(line);
          if (event) handleEvent(event);
        }
        if (done) break;
      }
      if (buffer.trim()) {
        const event = safeParse(buffer);
        if (event) handleEvent(event);
      }
      if (!finished && isAlive()) {
        current.finished = true;
        if (!current.queue.length && !current.audio) finish();
      }

      function handleEvent(event) {
        if (!isAlive()) return;
        switch (event.type) {
          case 'start':
            current.jobId = event.job_id || '';
            current.mode = event.mode || 'ai';
            emit({ state: 'start', element, ...event });
            break;
          case 'chunk':
            current.queue.push({ url: event.url, index: event.index });
            emit({ state: 'chunk', element, ...event });
            if (!current.audio || (current.audio.ended && !current.paused)) playNext();
            break;
          case 'chunk_error':
            emit({ state: 'chunk-error', element, error: event.error, index: event.index });
            break;
          case 'error':
            handleFailure(event.error || 'Озвучка недоступна', event.mode || '');
            finished = true;
            break;
          case 'cancelled':
            finished = true;
            break;
          case 'done':
            finished = true;
            current.finished = true;         // последняя часть доиграет и завершит
            current.total = event.total || 0;
            if (!current.queue.length && !current.audio) finish();
            break;
          default: break;
        }
      }
    }).catch((error) => {
      if (!isAlive()) return;                       // отменённый запуск: молчим
      if (error.name === 'AbortError') return;
      handleFailure(error.message || 'Озвучка недоступна', '');
    });

    function handleFailure(message, mode) {
      if (!isAlive()) return;
      // Провайдер не настроен — честный резерв: браузерный синтез, если он есть.
      const utterance = mode === 'browser' ? browserSpeech(text) : null;
      if (utterance) {
        state.current = null;
        current.mode = 'browser';
        paint(element, 'fallback', { icon: '🗣', title: `Браузерная озвучка (резерв): ${message}` });
        try {
          utterance.onend = () => {
            paint(element, 'idle', { icon: '🔊', title: 'Озвучить ещё раз' });
            emit({ state: 'finished', element, mode: 'browser' });
          };
          window.speechSynthesis.cancel();
          window.speechSynthesis.speak(utterance);
        } catch (_) {
          paint(element, 'error', { title: message });
        }
        emit({ state: 'fallback', element, message });
        return;
      }
      state.current = null;
      paint(element, 'error', { title: message, icon: '⚠️' });
      emit({ state: 'error', element, error: message });
    }

    return true;
  }

  function safeParse(line) {
    if (!line || !line.trim()) return null;
    try { return JSON.parse(line); } catch (_) { return null; }
  }

  // ─────────────────────────── кнопки ───────────────────────────
  function isSame(element) {
    return Boolean(state.current && element && state.current.element === element);
  }

  /** Клик по кнопке сообщения: играет / пауза / продолжить / заново. */
  function toggle(element, getText) {
    if (isSame(element)) {
      const current = state.current;
      if (current.paused) return resume(element);
      return pause(element) || stop('manual');
    }
    const text = typeof getText === 'function' ? getText() : getText;
    return speak(element, text, { auto: false });
  }

  /** Привязывает кнопку к сообщению; повторная привязка не дублирует обработчик. */
  function bindButton(button, element, getText) {
    if (!button || !element) return;
    if (button.dataset.speechBound === '1') return;
    button.dataset.speechBound = '1';
    button.dataset.speechState = button.dataset.speechState || 'idle';
    button.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      toggle(element, getText);
    });
    paint(element, button.dataset.speechState || 'idle');
  }

  /** Хук после завершения ответа: автоматическая озвучка ровно один раз. */
  function onTurnFinished(element, text, meta = {}) {
    if (!element || !String(text || '').trim()) return false;
    if (state.stream && state.stream.element === element) {
      if (meta.stopped || meta.error) { stop('stopped'); return false; }
      return streamEnd(element, text);
    }
    if (meta.stopped || meta.error) return false;         // остановили или ошибка
    if (!autoSpeakEnabled()) return false;
    return speak(element, text, { auto: true });
  }

  /** Останавливает озвучку при смене диалога/выходе — очередь не «догоняет». */
  function onChatChanged() {
    return stop('chat-changed');
  }

  function settingsSnapshot() {
    return { ...state.settings, status: state.status, available: available() };
  }

  async function init() {
    await loadSettings();
    loadStatus();
  }


  // ───────── потоковая озвучка: речь идёт, пока ответ ещё печатается ─────────
  const STREAM_MIN_CHARS = 350;   // меньше запросов к провайдеру и ровнее речь
  state.stream = null;

  function streamAlive(s) {
    return Boolean(s) && state.current === s.current && state.generation === s.generation;
  }

  /** Конец предложения/абзаца, где можно резать текст (не внутри ```-блока). */
  function streamCut(text, from) {
    if (text.length - from < STREAM_MIN_CHARS) return -1;
    const tail = text.slice(from);
    const re = /(?:[.!?…]+["»”)\]]*|\n{2,})(?=\s)/g;
    let cut = -1;
    let match;
    while ((match = re.exec(tail)) !== null) {
      const end = from + match.index + match[0].length;
      if (end - from < STREAM_MIN_CHARS) continue;
      const fences = (text.slice(0, end).match(/```/g) || []).length;
      if (fences % 2 === 0) cut = end;
    }
    return cut;
  }

  function streamStart(element) {
    stop('replaced');
    const controller = new AbortController();
    const current = {
      element, controller, queue: [], audio: null, jobId: '',
      paused: false, mode: 'ai', auto: true, chunksPlayed: 0, text: '', idle: false,
    };
    state.current = current;
    state.spoken.add(element);
    const s = {
      element, current, controller, generation: state.generation,
      sent: 0, pending: [], fetching: false, ended: false,
    };
    state.stream = s;
    paint(element, 'loading', { icon: '⏳' });
    return s;
  }

  function streamCheckDone(s) {
    if (!streamAlive(s) || !s.ended || s.fetching || s.pending.length) return;
    const c = s.current;
    if (c.queue.length || (c.audio && !c.idle)) return;
    state.current = null;
    state.stream = null;
    paint(s.element, 'idle', { icon: '🔊', title: 'Озвучить ещё раз' });
    emit({ state: 'finished', element: s.element, chunks: c.chunksPlayed });
  }

  function streamFail(s, message) {
    s.pending.length = 0;
    if (!streamAlive(s)) return;
    const c = s.current;
    if (c.audio || c.queue.length || c.chunksPlayed) {   // часть речи уже есть — не рвём её
      emit({ state: 'chunk-error', element: s.element, error: message });
      return;
    }
    state.current = null;
    state.stream = null;
    try { s.controller.abort(); } catch (_) { /* уже завершён */ }
    paint(s.element, 'error', { title: message, icon: '⚠️' });
    emit({ state: 'error', element: s.element, error: message });
  }

  function streamPlayNext(s) {
    if (!streamAlive(s)) return;
    const c = s.current;
    const next = c.queue.shift();
    if (!next) {
      c.idle = true;
      streamCheckDone(s);
      return;
    }
    c.idle = false;
    const audio = state.audioFactory(next.url);
    c.audio = audio;
    c.paused = false;
    const advance = () => {
      if (!streamAlive(s)) return;
      c.chunksPlayed += 1;
      streamPlayNext(s);
    };
    audio.addEventListener('ended', advance);
    audio.addEventListener('error', () => {
      if (!streamAlive(s)) return;
      paint(s.element, 'error', { title: 'Не удалось прочитать часть аудио' });
      stopAudio(audio);
      advance();
    });
    paint(s.element, 'playing');
    const playing = audio.play();
    if (playing && playing.catch) {
      playing.catch(() => {
        if (!streamAlive(s)) return;
        paint(s.element, 'error', { title: 'Браузер заблокировал автозапуск — нажмите кнопку' });
        emit({ state: 'blocked', element: s.element });
      });
    }
  }

  function streamHandle(s, event) {
    const c = s.current;
    switch (event.type) {
      case 'start':
        c.jobId = event.job_id || c.jobId;
        emit({ state: 'start', element: s.element, ...event });
        break;
      case 'chunk':
        c.queue.push({ url: event.url, index: event.index });
        emit({ state: 'chunk', element: s.element, ...event });
        if (c.idle || !c.audio) streamPlayNext(s);
        break;
      case 'chunk_error':
        emit({ state: 'chunk-error', element: s.element, error: event.error, index: event.index });
        break;
      case 'error':
        streamFail(s, event.error || 'Озвучка недоступна');
        break;
      default: break;
    }
  }

  async function streamFetch(s, segment) {
    try {
      const response = await fetch('/api/tts/speech', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          text: segment,
          voice: state.settings.voice || '',
          provider: state.settings.provider || '',
          model: state.settings.model || '',
          read_code: Boolean(state.settings.read_code),
          lang: state.settings.lang || '',
          auto: true,
        }),
        signal: s.controller.signal,
      });
      if (!response.ok) {
        const raw = await response.text().catch(() => '');
        let message = `HTTP ${response.status}`;
        try { message = JSON.parse(raw).error || message; } catch (_) { /* не JSON */ }
        throw new Error(message);
      }
      if (!response.body || !response.body.getReader) {
        throw new Error('Браузер не поддерживает потоковый ответ');
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      while (true) {
        const { value, done } = await reader.read();
        if (!streamAlive(s)) { try { reader.cancel(); } catch (_) { /* ok */ } return; }
        buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';
        for (const line of lines) {
          const event = safeParse(line);
          if (event) streamHandle(s, event);
        }
        if (done) break;
      }
      if (buffer.trim()) {
        const event = safeParse(buffer);
        if (event) streamHandle(s, event);
      }
    } catch (error) {
      if (!streamAlive(s) || error.name === 'AbortError') return;
      streamFail(s, error.message || 'Озвучка недоступна');
    }
  }

  async function streamPump(s) {
    if (s.fetching) return;
    s.fetching = true;
    try {
      while (streamAlive(s) && s.pending.length) {
        await streamFetch(s, s.pending.shift());
      }
    } finally {
      s.fetching = false;
      streamCheckDone(s);
    }
  }

  function streamQueue(s, text) {
    const clean = String(text || '').trim();
    if (!clean) return;
    s.pending.push(clean);
    streamPump(s);
  }

  /** Вызывается на каждый токен ответа: озвучивает готовые предложения. */
  function streamFeed(element, fullText) {
    if (!element || !autoSpeakEnabled()) return false;
    const text = String(fullText || '');
    let s = state.stream;
    if (!s || s.element !== element) {
      if (state.spoken.has(element) || text.length < STREAM_MIN_CHARS) return false;
      s = streamStart(element);
    } else if (!streamAlive(s)) {
      return false;               // остановили вручную — для этого ответа больше не стартуем
    }
    const cut = streamCut(text, s.sent);
    if (cut > 0) {
      streamQueue(s, text.slice(s.sent, cut));
      s.sent = cut;
    }
    return true;
  }

  /** Ответ дописан: озвучиваем остаток и завершаем поток. */
  function streamEnd(element, fullText) {
    const s = state.stream;
    if (!s || s.element !== element) return false;
    if (!streamAlive(s)) { state.stream = null; return false; }
    const text = String(fullText || '');
    const rest = text.slice(s.sent);
    s.sent = text.length;
    s.ended = true;
    streamQueue(s, rest);
    streamCheckDone(s);
    return true;
  }

  window.NovaSpeech = {
    init,
    speak,
    toggle,
    bindButton,
    stop,
    pause,
    resume,
    onTurnFinished,
    streamFeed,
    streamEnd,
    onChatChanged,
    loadSettings,
    loadStatus,
    saveSettings,
    settings: settingsSnapshot,
    available,
    autoSpeakEnabled,
    setAudioFactory,
    states: STATES,
    _state: () => ({ current: state.current, generation: state.generation }),
    translate: t,
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => { init(); });
  } else {
    init();
  }
})();
