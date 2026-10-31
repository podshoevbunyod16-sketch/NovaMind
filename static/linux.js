/**
 * static/linux.js — ИИ-агент внутри главного чата.
 * Файловые запросы на анализ/исправление маршрутизируются в Auto Coding Agent.
 */
(() => {
  'use strict';

  const state = { status: null, ready: null, runId: '' };

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

  async function syncSession() {
    const nick = localStorage.getItem('nova_user_nick');
    if (!nick) return;
    try {
      const check = await getJson('/api/session/check');
      if (check.signed_in) return;
      const login = await getJson('/api/session/login', { method: 'POST', body: JSON.stringify({ nick }) });
      if (login && login.admin && typeof window.loadSettingsAccess === 'function') {
        await window.loadSettingsAccess();
      }
    } catch (_) { /* Без сессии чат всё равно работает — просто без Linux. */ }
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
    if (!status || !status.enabled) return false;
    return status.available === undefined ? !!status.tools : !!status.available;
  }

  function hasLinux() {
    return !!(state.status && state.status.tools);
  }

  function paintStatus() {
    const on = isAvailable();
    document.body.classList.toggle('agent-on', on);
    document.body.classList.toggle('linux-on', on && hasLinux());
    const dot = document.getElementById('statusDot');
    if (!dot) return;
    if (!on) {
      dot.title = (state.status && state.status.hint) || 'ИИ-агент недоступен';
      return;
    }
    const skills = ['ищет в интернете', 'рисует, озвучивает и делает видео',
      'анализирует исходный код и предлагает исправления'];
    if (hasLinux()) skills.unshift('работает в Linux (код, файлы, терминал)');
    dot.title = `ИИ-агент сам выбирает, что делать: ${skills.join(', ')}`;
  }

  async function init() {
    state.ready = (async () => {
      await syncSession();
      const status = await loadStatus();
      if (status && status.hint && !sessionStorage.getItem('nova_linux_hint')) {
        sessionStorage.setItem('nova_linux_hint', '1');
        notify(status.hint, 'info');
      }
      return status;
    })();
    return state.ready;
  }

  function isCodingRequest(message) {
    const text = String(message || '');
    const mentionsProjectFile = /(?:[\w.-]+\/)*[\w.-]+\.(?:py|js|ts|css|html|json|md|yml|yaml|toml)\b/i.test(text);
    const asksForCodeWork = /(найди|исправ|ошиб|баг|проверь|анализ|объясни|рефактор|тест|почему|не работает|предложи|fix|debug|review|refactor|test)/i.test(text);
    return mentionsProjectFile && asksForCodeWork;
  }

  async function runTurn(message, options = {}) {
    if (state.ready) await state.ready.catch(() => null);
    if (!isAvailable()) return false;

    // options.tools — кнопка «Автопоиск». false => вообще без инструментов:
    // ни кодинг-агента, ни поиска, ни терминала — только прямой запрос к модели.
    const tools = options.tools !== false;
    const coding = tools && isCodingRequest(message);
    const search = !!options.search && tools && !coding;
    const turn = createAssistantTurn();
    if (coding) turn.beginIdle('Исследую исходный код…');
    // Не показываем «Поищу в интернете» заранее: флаг поиска только
    // разрешает поиск, а решение должен принять агент. Сцену search
    // сервер отправит после фактического выбора action=search.
    else turn.beginIdle(options.reasoning ? 'Рассуждаю…' : 'Думаю…');

    const controller = new AbortController();
    activeAbort = controller;
    let result = {};
    let failure = null;
    const endpoint = coding ? '/api/agent/coding/stream' : '/api/agent/stream';
    // chat_id открытого диалога — сервер пишет ходы в него же (иначе агент,
    // поиск и обычный чат разъезжаются по разным серверным чатам).
    const chatRef = (typeof window.serverChatRequest === 'function')
      ? window.serverChatRequest() : {};
    try {
      const response = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message, search, tools, reasoning: !!options.reasoning,
                               ...chatRef }),
        signal: controller.signal,
      });
      state.runId = response.headers?.get?.('X-Agent-Run-Id') || '';
      if (!response.ok) {
        if ((response.status === 403 || response.status === 404) && !coding) {
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
        switch (event.type) {
          case 'stage': turn.setStage(event.scene || 'agent', event.title, event.text); break;
          case 'plan': turn.plan(event); break;
          case 'step': turn.step(event.icon, event.text); break;
          case 'tool': turn.tool(event); break;
          case 'task_started': turn.step('🎯', 'Задача принята'); break;
          case 'task_analysis':
            if (event.status === 'started') turn.step('🧠', event.summary || 'Анализирую задачу…');
            else turn.step('✓', event.summary || 'Анализ задачи завершён');
            break;
          case 'plan_created': turn.step('✓', event.summary || 'План создан'); break;
          case 'tool_discovered': turn.step('🧰', `Обнаружено инструментов: ${event.count || 0}`); break;
          case 'tool_call_started': turn.step('▶', `Использую ${event.tool || 'инструмент'}…`); break;
          case 'verification_started': turn.step('🔎', event.summary || 'Проверяю результат…'); break;
          case 'verification_completed': turn.step(event.ok ? '✓' : '⚠️', event.summary || 'Проверка завершена'); break;
          case 'task_completed': turn.step('🏁', 'Задача выполнена'); break;
          // tool_call_completed/failed и observation представлены раскрываемой
          // карточкой legacy `tool`, поэтому не дублируем их отдельной строкой.
          // Динамическое получение инструментов после понимания запроса.
          case 'recall': if (turn.recall) turn.recall(event); break;
          // Думаем перед каждым шагом и ещё раз — после его выполнения.
          case 'think': if (turn.think) turn.think(event); break;
          // «Думал 4 c» — такой же шаг таймлайна, как команда или файл.
          case 'thought': if (turn.thought) turn.thought(event); break;
          case 'confirm': if (turn.confirm) turn.confirm(event); break;
          case 'confirm_result': if (turn.confirmResult) turn.confirmResult(event); break;
          case 'task': turn.taskChip(event.task); break;
          case 'media': turn.addMedia(event.media || {}); break;
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
      if (typeof window.refreshUsage === 'function') window.refreshUsage(true);
      turn.finish({
        model: result.model,
        provider: result.provider,
        // Серверный id чата: по нему после перезагрузки подтягивается
        // панель «Что делал агент» из базы.
        serverChatId: result.chat_id,
        usage: result.usage,
        offline: result.offline,
        searched: result.searched,
        steps: result.steps,
        stopped: !!result.stopped,
        linux: coding ? false : (result.linux === undefined ? (result.steps > 0 || result.searched) : !!result.linux),
      });
    } catch (error) {
      activeAbort = null;
      if (error.name === 'AbortError') {
        turn.collapseStage();
        turn.appendText(turn.text || '_Остановлено._');
        turn.finish({ model: result.model, stopped: true });
      } else {
        turn.fail(error.message || 'ИИ недоступен');
      }
    }
    return true;
  }

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

  /** Кнопка «Стоп»: сервер прекращает шаги по run_id. */
  async function stopRun() {
    const runId = state.runId;
    if (!runId) return false;
    state.runId = '';
    try {
      await getJson('/api/agent/stop', { method: 'POST', body: JSON.stringify({ run_id: runId }) });
      return true;
    } catch (_) {
      return false;      // запуск уже завершился — это нормально
    }
  }

  window.Linux = { state, init, loadStatus, runTurn, isAvailable, hasLinux, stopRun };

  function boot() {
    const chat = document.getElementById('chatContainer');
    if (chat) chat.addEventListener('click', onCodeAction);
    init();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
