/**
 * static/linux.js — Linux-окружение внутри главного чата.
 *
 * Отдельных кнопок «Агент» и «Linux» больше нет. Любое сообщение в главном
 * чате уходит в /api/agent/stream: ИИ сам решает — ответить сразу или
 * работать в Linux по шагам (команды, файлы, поиск, чтение страниц),
 * пока цель не достигнута. Кнопка «Поиск» тоже идёт через окружение.
 *
 * Если окружение выключено (нет TERMINAL_ENABLED=1) или пользователь
 * не вошёл, app.js спокойно отвечает старым путём — через обычный чат.
 *
 * Зависит от app.js (createAssistantTurn, consumeNdjson, activeAbort…),
 * поэтому подключается вторым скриптом.
 */
(() => {
  'use strict';

  const state = {
    status: null,        // ответ /api/agent/status
    ready: null,         // Promise первой проверки
  };

  function notify(message, kind) {
    if (typeof window.showNotification === 'function') window.showNotification(message, kind);
  }

  async function getJson(url, options) {
    const response = await fetch(url, {
      headers: { 'Content-Type': 'application/json' },
      ...options,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(data.error || `HTTP ${response.status}`);
      error.status = response.status;
      throw error;
    }
    return data;
  }

  // ───────────────────────── сессия и статус ─────────────────────────

  /**
   * Вход по нику живёт в localStorage, а сервер забывает сессию после
   * перезапуска (частая история в Termux). Без серверной сессии ИИ не
   * пустят в окружение — поэтому тихо восстанавливаем её при загрузке.
   */
  async function syncSession() {
    const nick = localStorage.getItem('nova_user_nick');
    if (!nick) return;
    try {
      const check = await getJson('/api/session/check');
      if (check.signed_in) return;
      await getJson('/api/session/login', { method: 'POST', body: JSON.stringify({ nick }) });
    } catch (_) { /* без сессии чат всё равно работает — просто без Linux */ }
  }

  async function loadStatus() {
    try {
      state.status = await getJson('/api/agent/status');
    } catch (_) {
      state.status = { enabled: false, tools: false, available: false };
    }
    paintStatus();
    return state.status;
  }

  function isAvailable() {
    const status = state.status;
    return !!(status && status.enabled && status.tools);
  }

  /** Подсказка у индикатора в шапке: работает ли Linux у ИИ. */
  function paintStatus() {
    const on = isAvailable();
    document.body.classList.toggle('linux-on', on);
    const dot = document.getElementById('statusDot');
    if (dot) {
      dot.title = on
        ? 'ИИ работает с Linux-окружением: сам запускает команды, пишет файлы и ищет в интернете'
        : (state.status && state.status.hint) || 'Linux-окружение недоступно';
    }
  }

  async function init() {
    state.ready = (async () => {
      await syncSession();
      const status = await loadStatus();
      // Один раз подскажем, почему ИИ отвечает без Linux
      if (status && !isAvailable() && status.hint && !sessionStorage.getItem('nova_linux_hint')) {
        sessionStorage.setItem('nova_linux_hint', '1');
        notify(status.hint, 'info');
      }
      return status;
    })();
    return state.ready;
  }

  // ───────────────────────── ход ИИ через окружение ─────────────────────────

  /**
   * Отправляет сообщение в главный чат с Linux-окружением.
   *
   * Возвращает true, если ответ получен (или остановлен пользователем),
   * и false, если окружение недоступно — тогда app.js ответит старым путём.
   */
  async function runTurn(message, options = {}) {
    if (state.ready) await state.ready.catch(() => null);
    if (!isAvailable()) return false;

    const search = !!options.search;
    const turn = createAssistantTurn();
    if (search) turn.beginSearch('Поищу в интернете');
    else turn.beginIdle(options.reasoning ? 'Рассуждаю…' : 'Думаю…');

    const controller = new AbortController();
    activeAbort = controller;           // кнопка «Стоп» в app.js останавливает и ИИ

    let result = {};
    let failure = null;
    try {
      const response = await fetch('/api/agent/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message, search, reasoning: !!options.reasoning }),
        signal: controller.signal,
      });
      if (!response.ok) {
        // Окружение закрылось (перезапуск сервера, выход из аккаунта) —
        // убираем пустой пузырь и отдаём сообщение обычному чату.
        if (response.status === 403 || response.status === 404) {
          turn.remove();
          activeAbort = null;
          state.status = { ...(state.status || {}), available: false, tools: false };
          paintStatus();
          loadStatus();
          return false;
        }
        const raw = await response.text();
        let details = `HTTP ${response.status}`;
        try { details = JSON.parse(raw).error || details; } catch (_) { details += ': ' + raw.slice(0, 200); }
        throw new Error(details);
      }

      await consumeNdjson(response, (event) => {
<<<<<<< HEAD
        switch (event.type) {
          case 'stage': turn.setStage(event.scene || 'agent', event.title, event.text); break;
          case 'plan': turn.plan(event.text); break;
          case 'step': turn.step(event.icon, event.text); break;
          case 'tool': turn.tool(event); break;
          case 'task': turn.taskChip(event.task); break;
          case 'sources': turn.setSources(event.sources || []); break;
          case 'reasoning': turn.reasoningToken(event.token); break;
          case 'token': turn.appendToken(event.token); break;
          case 'retract': turn.appendText(''); break;
          case 'error': failure = event.text; turn.step('⚠️', event.text); break;
          case 'result':
            result = event;
            if (Array.isArray(event.sources) && event.sources.length) turn.setSources(event.sources);
            if (!turn.text && event.reply) turn.appendText(event.reply);
            break;
          default: break;
        }
      });
      activeAbort = null;
      if (!turn.text) throw new Error(failure || 'ИИ не вернул ответ');
      turn.finish({
        model: result.model,
        provider: result.provider,
        offline: result.offline,
        searched: result.searched,
        steps: result.steps,
        linux: result.steps > 0 || result.searched,
=======
        if (event.type === 'stage') turn.setStage('agent', event.title, event.text);
        else if (event.type === 'plan') turn.plan(event.text);
        else if (event.type === 'step') turn.step(event.icon, event.text);
        else if (event.type === 'tool') turn.tool(event);
        else if (event.type === 'task') { turn.taskChip(event.task); if (state.tab === 'tasks') loadTasks(); }
        else if (event.type === 'sources') turn.setSources(event.sources || []);
        else if (event.type === 'token') turn.appendToken(event.token);
        else if (event.type === 'result') result = event;
        else if (event.type === 'error') turn.step('⚠️', event.text);
>>>>>>> d42dfa25f0f2c0d68742da19cdd015831aee836a
      });
    } catch (error) {
      activeAbort = null;
      if (error.name === 'AbortError') {
        turn.collapseStage();
        turn.appendText(turn.text || '_Остановлено._');
        turn.finish({ model: result.model });
      } else {
        turn.fail(error.message || 'ИИ недоступен');
      }
    }
    return true;
  }

  // ───────────────────────── кнопки у блоков кода ─────────────────────────

  const CODE_PROMPTS = {
    run: 'Запусти этот код в Linux-окружении и покажи, что он выводит. Если есть ошибки — исправь и запусти снова.',
    explain: 'Объясни этот код по пунктам: что делает, где может сломаться, что улучшить.',
    tests: 'Напиши тесты к этому коду, запусти их в Linux-окружении и покажи результат.',
  };

  function codeOf(node) {
    const block = node.closest('.code-block');
    const code = block && block.querySelector('code');
    return { lang: (block && block.dataset.lang) || '', text: code ? code.textContent : '' };
  }

  function onCodeAction(event) {
    const button = event.target.closest('[data-code]');
    if (!button) return;
    event.preventDefault();
    const { lang, text } = codeOf(button);
    if (!text) return;
    if (button.dataset.code === 'copy') {
      if (typeof copyText === 'function') copyText(text);
      else if (navigator.clipboard) navigator.clipboard.writeText(text);
      return;
    }
    const prompt = CODE_PROMPTS[button.dataset.code];
    if (!prompt) return;
    if (typeof isTyping !== 'undefined' && isTyping) {
      notify('Дождитесь окончания ответа', 'warn');
      return;
    }
    window.sendMessage(`${prompt}\n\n\`\`\`${lang}\n${text}\n\`\`\``);
  }

  // Публичный интерфейс — им пользуется app.js
  window.Linux = {
    state,
    init,
    loadStatus,
    runTurn,
    isAvailable,
  };

  function boot() {
    const chat = document.getElementById('chatContainer');
    if (chat) chat.addEventListener('click', onCodeAction);
    init();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
