/**
 * static/linux.js — «маленький Linux» поверх чата.
 *
 * Всё живёт в одной правой панели: терминал, файлы рабочей папки,
 * git и список задач. Агентный режим (кнопка в шапке) отправляет
 * сообщение не в чат, а в агентный цикл, который работает этой
 * же песочницей. Бонус для программистов: кнопки прямо у блоков
 * кода в ответах модели.
 *
 * Зависит от app.js (fetch, consumeNdjson, createAssistantTurn),
 * поэтому подключается вторым скриптом.
 */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const state = {
    open: false,
    tab: 'files',
    status: null,
    busy: false,
    file: null,
    searchMode: 'web',
    searchResult: null,
    agent: false,
    agentStatus: null,
  };

  const el = {};

  function cache() {
    [
      'linuxPanel', 'linuxPath', 'linuxLed', 'linuxTabs', 'btnLinuxClose', 'btnLinux', 'btnAgent',
      'fileList', 'filesHint', 'btnFilesRefresh', 'fileView', 'fileViewPath', 'fileViewBody',
      'btnFileToChat', 'btnFileToTerm', 'btnFileClose',
      'searchForm', 'searchModes', 'searchInput', 'btnSearchRun', 'searchStatus', 'searchList',
      'gitBox', 'gitHint', 'btnGitRefresh',
      'taskForm', 'taskInput', 'taskList', 'tasksBadge',
      'cheatsheet', 'btnCheatClose', 'chatContainer', 'chat-input',
    ].forEach((id) => { el[id] = $(id); });
    el.input = el['chat-input'];          // то же поле ввода, короткое имя как в app.js
  }

  // ───────────────────────── утилиты ─────────────────────────

  const esc = (text) => String(text ?? '').replace(/[&<>"']/g, (ch) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
  ));

  async function api(url, options) {
    const response = await fetch(url, {
      headers: { 'Content-Type': 'application/json' },
      ...options,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(data.error || `HTTP ${response.status}`);
      error.status = response.status;
      error.data = data;
      throw error;
    }
    return data;
  }

  function showNotification(message, kind) {
    if (typeof window.showNotification === 'function') window.showNotification(message, kind);
  }

  // Терминала в чате нет по замыслу: им пользуется ИИ во время рассуждений,
// а результат команд приходит в ответе сворачиваемым блоком. Здесь только
// служебный вывод для уведомлений — без панели, поля ввода и истории команд.

// ───────────────────────── статус окружения ─────────────────────────

  async function loadStatus() {
    try {
      const status = await api('/api/terminal/status');
      state.status = status;
      if (el.linuxLed) el.linuxLed.classList.toggle('is-on', !!status.available);
      if (el.linuxPath) el.linuxPath.textContent = `${status.workspace || 'workspace/'}/`;
      paintAccess(status);
    } catch (error) {
      if (el.filesHint) el.filesHint.textContent = error.message;
    }
    try {
      state.agentStatus = await api('/api/agent/status');
    } catch (_) {
      state.agentStatus = { enabled: false };
    }
    paintAgentButton();
  }

  function paintAccess(status) {
    if (!el.filesHint) return;
    el.filesHint.textContent = status.available ? '' : (status.hint || '');
    if (el.termNote) el.termNote.hidden = true;
  }

  // ───────────────────────── файлы ─────────────────────────

  async function loadFiles() {
    if (!el.fileList) return;
    try {
      const data = await api('/api/terminal/files');
      if (!data.files.length) {
        el.fileList.innerHTML = '<div class="sec-empty">Папка пуста. Создайте файл из терминала: touch hello.py</div>';
      } else {
        el.fileList.innerHTML = data.files.map((file) => `
          <button type="button" class="file-row" data-path="${esc(file.path)}" title="${esc(file.path)}">
            <span class="file-ico">${file.path.endsWith('.py') ? '🐍' : file.path.endsWith('.md') ? '📝' : file.path.endsWith('.csv') ? '📊' : '📄'}</span>
            <span class="file-name">${esc(file.name)}</span>
            <span class="file-dir">${esc(file.dir)}</span>
            <span class="file-size">${formatBytes(file.size)}</span>
          </button>`).join('');
      }
      if (el.filesHint) {
        el.filesHint.textContent = data.truncated
          ? `показаны первые ${data.files.length} файлов`
          : `${data.files.length} файл(ов)`;
      }
    } catch (error) {
      el.fileList.innerHTML = `<div class="sec-empty">${esc(error.message)}</div>`;
    }
  }

  function formatBytes(size) {
    if (!size) return '0 Б';
    if (size < 1024) return `${size} Б`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} КБ`;
    return `${(size / 1024 / 1024).toFixed(1)} МБ`;
  }

  async function openFile(path) {
    try {
      const data = await api('/api/terminal/file', { method: 'POST', body: JSON.stringify({ action: 'read', path }) });
      state.file = data;
      el.fileViewPath.textContent = data.path;
      el.fileViewBody.textContent = data.content;
      el.fileView.hidden = false;
    } catch (error) {
      showNotification(error.message, 'warn');
    }
  }

  function closeFile() {
    state.file = null;
    el.fileView.hidden = true;
    el.fileViewBody.textContent = '';
  }

  // ───────────────────────── поиск через окружение ─────────────────────────

function searchStatus(text, kind = 'info') {
  if (!el.searchStatus) return;
  el.searchStatus.hidden = !text;
  el.searchStatus.className = `search-status is-${kind}`;
  el.searchStatus.textContent = text || '';
}

function renderSearch(data) {
  state.searchResult = data;
  const list = el.searchList;
  if (!data.results.length) {
    list.innerHTML = `<div class="sec-empty">${data.mode === 'code'
      ? 'В песочнице таких вхождений нет.'
      : 'Ничего не нашлось. Попробуйте другой запрос или проверьте бэкенды поиска.'}</div>`;
    return;
  }
  if (data.mode === 'code') {
    list.innerHTML = data.results.map((hit) => `
      <button type="button" class="hit hit-code" data-open="${escapeAttr(hit.path)}">
        <span class="hit-path">${escapeHtml(hit.path)}<span class="hit-line">:${hit.line || 1}</span></span>
        <span class="hit-snippet">${escapeHtml(hit.snippet)}</span>
      </button>`).join('');
    return;
  }
  list.innerHTML = data.results.map((item, index) => `
    <article class="hit" data-index="${index}">
      <a class="hit-title" href="${escapeAttr(item.url)}" target="_blank" rel="noopener noreferrer"
         title="${escapeAttr(item.url)}">${index + 1}. ${escapeHtml(item.title || item.host)}</a>
      <span class="hit-host">${escapeHtml(item.host || '')}</span>
      ${item.snippet ? `<p class="hit-snippet">${escapeHtml(item.snippet)}</p>` : ''}
      <div class="hit-acts">
        <button type="button" data-act="chat" title="Спросить об этом в чате">В чат</button>
        <button type="button" data-act="task" title="Завести задачу по этой теме">В задачу</button>
        <button type="button" data-act="save" title="Сохранить выдачу в рабочую папку">Сохранить</button>
        <button type="button" data-act="read" title="Прочитать страницу">Читать</button>
      </div>
    </article>`).join('');
}

const escapeAttr = (text) => String(text ?? '').replace(/[&<>"]/g, (ch) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
));

function resultAt(index) {
  const data = state.searchResult;
  return (data && data.results && data.results[index]) || null;
}

async function runLinuxSearch(query) {
  const text = String(query ?? (el.searchInput ? el.searchInput.value : '')).trim();
  if (!text) return null;
  if (el.btnSearchRun) { el.btnSearchRun.disabled = true; el.btnSearchRun.textContent = 'Ищу…'; }
  searchStatus(state.searchMode === 'code'
    ? `Ищу «${text}» по файлам рабочей папки…`
    : `Ищу «${text}» в интернете…`);
  try {
    const data = await api('/api/linux/search', {
      method: 'POST',
      body: JSON.stringify({ query: text, mode: state.searchMode }),
    });
    renderSearch(data);
    searchStatus(data.mode === 'code'
      ? `Найдено вхождений: ${data.count}${data.truncated ? ' (показаны не все)' : ''}`
      : `${data.count} источников · ${data.backend || 'поиск'} · ${(data.elapsed_ms / 1000).toFixed(1)} c`,
      data.count ? 'ok' : 'warn');
    if (el.searchInput) el.searchInput.value = text;
    return data;
  } catch (error) {
    el.searchList.innerHTML = `<div class="sec-empty">${esc(error.message)}</div>`;
    searchStatus(error.message, 'warn');
    return null;
  } finally {
    if (el.btnSearchRun) { el.btnSearchRun.disabled = false; el.btnSearchRun.textContent = 'Искать'; }
  }
}

/** Открывает найденное в терминале — командой, а не копипастой. */
async function searchInTerminal(query) {
  // Того же поиска через песочницу: команда уходит агенту, а не в поле ввода.
  const text = String(query ?? (el.searchInput ? el.searchInput.value : '')).trim();
  if (!text) return null;
  return runLinuxSearch(text);
}

async function saveSearchResult() {
  const data = state.searchResult;
  if (!data || !data.results.length) return;
  try {
    const saved = await api('/api/linux/save', {
      method: 'POST',
      body: JSON.stringify({ query: data.query, results: data.results }),
    });
    showNotification(`Сохранено: ${saved.path}`, 'info');
    setTab('files');
    loadFiles();
  } catch (error) {
    showNotification(error.message, 'warn');
  }
}

async function readResult(url) {
  searchStatus(`Читаю ${url}…`);
  try {
    const data = await api('/api/linux/read', { method: 'POST', body: JSON.stringify({ url }) });
    showNotification(`Прочитано ${data.bytes} символов за ${data.elapsed_ms} мс`, 'info');
    await saveToWorkspace(`sources/${hostOf(data.url)}.md`,
      `# ${data.url}\n\n${data.text}\n`, 'Прочитанная страница');
  } catch (error) {
    searchStatus(error.message, 'warn');
  }
}

function hostOf(url) {
  try { return new URL(url).hostname.replace(/^www\./, '') || 'source'; }
  catch (_) { return 'source'; }
}

/** Кладёт текст файлом в рабочую папку — без терминала, через API песочницы. */
async function saveToWorkspace(path, content, label) {
  try {
    const data = await api('/api/terminal/file', {
      method: 'POST',
      body: JSON.stringify({ action: 'write', path, content }),
    });
    showNotification(`${label || 'Файл'}: ${data.path || 'рабочая папка'}`, 'info');
    openPanel('files');
    await loadFiles();
    return data.path;
  } catch (error) {
    showNotification(error.message, 'warn');
    return null;
  }
}

/** Выполнить команду песочницы без показа терминала — для кнопок панели. */
async function execCommand(command) {
  try {
    const data = await api('/api/terminal/run', {
      method: 'POST',
      body: JSON.stringify({ command }),
    });
    if (data.code) showNotification(`${command}: код ${data.code}`, 'warn');
    return data.code === 0;
  } catch (error) {
    showNotification(error.message, 'warn');
    return false;
  }
}

// ───────────────────────── git ─────────────────────────

  async function loadGit() {
    if (!el.gitBox) return;
    try {
      const data = await api('/api/terminal/git');
      if (!data.repo) {
        el.gitBox.innerHTML = `
          <div class="sec-empty">Это не git-репозиторий.</div>
          <button class="lg-btn tiny" id="btnGitInit" type="button">Создать репозиторий</button>`;
        const init = $('btnGitInit');
        if (init) init.addEventListener('click', async () => {
          const done = await execCommand('git init');
          if (done) { showNotification('Репозиторий создан', 'info'); loadGit(); }
        });
        if (el.gitHint) el.gitHint.textContent = '';
        return;
      }
      const rows = data.changes.length
        ? data.changes.map((row) => `
            <div class="git-row"><span class="git-flag is-${esc(row.status)}">${esc(row.status)}</span>
            <code>${esc(row.path)}</code></div>`).join('')
        : '<div class="sec-empty">Рабочая папка чистая — незакоммиченных изменений нет.</div>';
      el.gitBox.innerHTML = `
        <div class="git-head">
          <span class="git-branch">⎇ ${esc(data.branch)}</span>
          <span class="git-state ${data.clean ? 'is-clean' : 'is-dirty'}">${data.clean ? 'чисто' : data.changes.length + ' изменений'}</span>
        </div>
        <div class="git-last" title="Последний коммит">${esc(data.last || 'коммитов пока нет')}</div>
        <div class="git-rows">${rows}</div>`;
      if (el.gitHint) el.gitHint.textContent = 'Ctrl+C не нужен — команды выполняются по одной';
    } catch (error) {
      el.gitBox.innerHTML = `<div class="sec-empty">${esc(error.message)}</div>`;
    }
  }

  // ───────────────────────── задачи ─────────────────────────

  const STATUS_FLOW = { todo: 'doing', doing: 'done', done: 'todo' };
  const STATUS_LABEL = { todo: 'К выполнению', doing: 'В работе', done: 'Готово' };

  async function loadTasks() {
    if (!el.taskList) return;
    try {
      const data = await api('/api/tasks');
      paintTasks(data.tasks || []);
    } catch (error) {
      el.taskList.innerHTML = `<div class="sec-empty">${esc(error.message)}</div>`;
    }
  }

  function paintTasks(tasks) {
    const open = tasks.filter((task) => task.status !== 'done').length;
    if (el.tasksBadge) {
      el.tasksBadge.hidden = !open;
      el.tasksBadge.textContent = open;
    }
    if (!tasks.length) {
      el.taskList.innerHTML = '<div class="sec-empty">Задач пока нет. Добавьте вручную или попросите агента.</div>';
      return;
    }
    el.taskList.innerHTML = tasks.map((task) => {
      const steps = (task.steps || []).slice(-6).map((step) => `
        <li class="task-step is-${esc(step.status || 'doing')}">
          <span class="task-step-mark">${step.status === 'done' ? '✔' : step.status === 'error' ? '✕' : '•'}</span>
          <span class="task-step-title">${esc(step.title)}</span>
        </li>`).join('');
      return `
        <article class="task-card is-${esc(task.status)}" data-task="${esc(task.id)}">
          <header class="task-card-head">
            <button type="button" class="task-check" data-cycle="${esc(task.id)}"
                    title="Переключить статус">${task.status === 'done' ? '✔' : '○'}</button>
            <span class="task-title">${esc(task.title)}</span>
            ${task.source === 'agent' ? '<span class="task-tag">агент</span>' : ''}
            <button type="button" class="task-del" data-del="${esc(task.id)}" title="Удалить">✕</button>
          </header>
          ${task.detail ? `<p class="task-detail">${esc(task.detail)}</p>` : ''}
          ${steps ? `<ul class="task-steps">${steps}</ul>` : ''}
          <footer class="task-card-foot">
            <span>${esc(STATUS_LABEL[task.status] || task.status)}</span>
            <span>${timeAgo(task.updated_at)}</span>
          </footer>
        </article>`;
    }).join('');
  }

  function timeAgo(stamp) {
    if (!stamp) return '';
    const seconds = Math.max(0, Date.now() / 1000 - stamp);
    if (seconds < 60) return 'только что';
    if (seconds < 3600) return `${Math.floor(seconds / 60)} мин назад`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)} ч назад`;
    return new Date(stamp * 1000).toLocaleDateString('ru-RU');
  }

  async function addTask(title) {
    const clean = String(title || '').trim();
    if (!clean) return;
    try {
      await api('/api/tasks', { method: 'POST', body: JSON.stringify({ title: clean }) });
      if (el.taskInput) el.taskInput.value = '';
      loadTasks();
    } catch (error) {
      showNotification(error.message, 'warn');
    }
  }

  async function cycleTask(id) {
    const card = el.taskList.querySelector(`[data-task="${CSS.escape(id)}"]`);
    const current = card ? card.className.match(/is-(todo|doing|done)/)?.[1] : 'todo';
    try {
      await api(`/api/tasks/${encodeURIComponent(id)}`, {
        method: 'PATCH',
        body: JSON.stringify({ status: STATUS_FLOW[current] || 'todo' }),
      });
      loadTasks();
    } catch (error) {
      showNotification(error.message, 'warn');
    }
  }

  async function removeTask(id) {
    try {
      await api(`/api/tasks/${encodeURIComponent(id)}`, { method: 'DELETE' });
      loadTasks();
    } catch (error) {
      showNotification(error.message, 'warn');
    }
  }

  // ───────────────────────── панель ─────────────────────────

  async function openPanel(tab) {
    state.open = true;
    el.linuxPanel.hidden = false;
    document.body.classList.add('linux-open');
    if (el.btnLinux) el.btnLinux.setAttribute('aria-expanded', 'true');
    setTab(tab || state.tab);
    await loadStatus();
  }

  function closePanel() {
    state.open = false;
    el.linuxPanel.hidden = true;
    document.body.classList.remove('linux-open');
    if (el.btnLinux) el.btnLinux.setAttribute('aria-expanded', 'false');
  }

  function togglePanel(tab) {
    if (state.open) { closePanel(); return; }
    openPanel(tab);
  }

  function setTab(tab) {
    state.tab = tab;
    document.querySelectorAll('#linuxTabs .linux-tab').forEach((button) => {
      button.classList.toggle('is-active', button.dataset.tab === tab);
    });
    document.querySelectorAll('.linux-sec').forEach((section) => {
      section.classList.toggle('is-active', section.dataset.sec === tab);
    });
    if (tab === 'files') loadFiles();
    if (tab === 'search') el.searchInput && el.searchInput.focus();
    if (tab === 'git') loadGit();
    if (tab === 'tasks') loadTasks();
  }

  // ───────────────────────── агентный режим ─────────────────────────

  function paintAgentButton() {
    const button = el.btnAgent;
    if (!button) return;
    const available = !state.agentStatus || state.agentStatus.enabled !== false;
    button.classList.toggle('is-on', state.agent);
    button.setAttribute('aria-pressed', state.agent ? 'true' : 'false');
    button.title = state.agent
      ? 'Агентный режим включён — задача выполняется в песочнице. Нажмите, чтобы вернуть обычный чат.'
      : (available
        ? 'Агентный режим: задача выполняется в песочнице (терминал, файлы, задачи)'
        : 'Агентный режим выключен: в .env добавьте AGENT_ENABLED=1');
    button.disabled = !available;
    const field = el['chat-input'];
    if (field) {
      const base = field.dataset.defaultPlaceholder || 'Напишите сообщение...';
      field.placeholder = state.agent
        ? 'Опишите задачу — агент выполнит её в песочнице и заведёт задачу…'
        : base;
    }
  }

  function setAgent(on) {
    state.agent = !!on;
    paintAgentButton();
    localStorage.setItem('novamind_agent_mode', state.agent ? '1' : '0');
    if (state.agent) showNotification('Агентный режим: задача уйдёт в песочницу', 'info');
  }

  function toggleAgent() {
    if (state.agentStatus && state.agentStatus.enabled === false) {
      showNotification('Агентный режим выключен (AGENT_ENABLED=1 в .env)', 'warn');
      return;
    }
    setAgent(!state.agent);
  }

  /** Отправка сообщения в агентный цикл вместо обычного чата. */
  async function runAgentTurn(message) {
    const turn = createAssistantTurn({ name: 'NovaMind · агент' });
    turn.setStage('agent', 'Планирую', 'Разбираю задачу по шагам…');

    const controller = new AbortController();
    activeAbort = controller;          // кнопка «Стоп» в app.js останавливает агента

    let result = {};
    try {
      const response = await fetch('/api/agent/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message }),
        signal: controller.signal,
      });
      if (!response.ok) {
        const raw = await response.text();
        let details = `HTTP ${response.status}`;
        try { details = JSON.parse(raw).error || details; } catch (_) { details += ': ' + raw.slice(0, 200); }
        throw new Error(details);
      }
      await consumeNdjson(response, (event) => {
        if (event.type === 'stage') turn.setStage('agent', event.title, event.text);
        else if (event.type === 'plan') turn.plan(event.text);
        else if (event.type === 'step') turn.step(event.icon, event.text);
        else if (event.type === 'tool') turn.tool(event);
        else if (event.type === 'task') { turn.taskChip(event.task); if (state.tab === 'tasks') loadTasks(); }
        else if (event.type === 'token') turn.appendToken(event.token);
        else if (event.type === 'result') result = event;
        else if (event.type === 'error') turn.step('⚠️', event.text);
      });
      if (!turn.text) turn.appendText(result.reply || 'Готово.');
      turn.finish({ model: result.model, steps: result.steps, agent: true });
      if (result.task_id) { loadTasks(); }
    } catch (error) {
      if (error.name === 'AbortError') {
        turn.appendText(turn.text || '_Агент остановлен._');
        turn.finish({ model: result.model, agent: true });
      } else {
        turn.fail(error.message || 'Агент недоступен');
      }
    }
  }

  /** Агент включается одной строкой в обычном sendMessage. */
  const originalSend = window.sendMessage;
  window.sendMessage = function patchedSend(text) {
    const msg = String(text ?? (el.input ? el.input.value : '')).trim();
    if (state.agent && msg && !msg.startsWith('/') && !isTyping) {
      return startAgentMessage(msg);
    }
    return originalSend.apply(this, arguments);
  };

  async function startAgentMessage(msg) {
    hideWelcome();
    appendMessage('user', msg);
    lastUserMessage = msg;
    if (el.input) { el.input.value = ''; el.input.style.height = 'auto'; }
    isTyping = true;
    setSendBusy(true);
    closeCmdk();
    try {
      await runAgentTurn(msg);
    } finally {
      isTyping = false;
      if (typeof setSendBusy === 'function') setSendBusy(false);
    }
  }

  // ───────────────────────── бонус: кнопки у кода ─────────────────────────

  const CODE_ACTIONS = [
    { key: 'explain', icon: '💡', title: 'Объясни код', prompt: 'Объясни этот код по пунктам: что делает, где может сломаться, что улучшить.' },
    { key: 'tests', icon: '🧪', title: 'Напиши тесты', prompt: 'Напиши юнит-тесты к этому коду и запусти их в песочнице.' },
    { key: 'optimize', icon: '⚡', title: 'Оптимизируй', prompt: 'Оптимизируй этот код: читаемость, скорость, обработка ошибок. Покажи diff.' },
    { key: 'similar', icon: '🔎', title: 'Найди аналог в интернете', prompt: null },
    { key: 'terminal', icon: '⌨️', title: 'В терминал', prompt: null },
  ];

  /** По коду строим поисковый запрос: язык + главный идентификатор. */
  function searchQueryForCode(lang, code) {
    const lines = String(code || '').split('\n').map((line) => line.trim()).filter(Boolean);
    const named = lines.find((line) => /^(def|class|function|const|class|func)\s+([A-Za-z_]\w*)/.test(line));
    const symbol = named && (named.match(/([A-Za-z_]\w*)\s*\(/) || [])[1];
    const words = (named || lines[0] || '')
      .replace(/[(){}=;:\[\]]/g, ' ')
      .split(/\s+/).filter((word) => word.length > 2 && !/^(def|class|function|const|let|var|import|from|return|self|this)$/i.test(word));
    const head = words.slice(0, 3).join(' ');
    const prefix = { python: 'python', py: 'python', js: 'javascript', javascript: 'javascript',
                     ts: 'typescript', typescript: 'typescript', bash: 'bash', sh: 'bash',
                     sql: 'sql', go: 'golang', rust: 'rust', java: 'java' }[lang] || 'code';
    return [prefix, symbol || head].filter(Boolean).join(' ').slice(0, 80);
  }

  function codeOf(node) {
    const block = node.closest('.code-block');
    const code = block && block.querySelector('code');
    return { lang: (block && block.dataset.lang) || '', text: code ? code.textContent : '' };
  }

  function onCodeAction(event) {
    const button = event.target.closest('[data-code]');
    if (!button) return;
    const { lang, text } = codeOf(button);
    if (button.dataset.code === 'copy') {
      event.preventDefault();
      if (typeof copyText === 'function') copyText(text);
      else if (navigator.clipboard) navigator.clipboard.writeText(text);
      showNotification('Код скопирован', 'info');
      return;
    }
    const action = CODE_ACTIONS.find((item) => item.key === button.dataset.code);
    if (!action || !text) return;
    event.preventDefault();
    if (action.key === 'similar') {
      const query = searchQueryForCode(lang, text);
      state.searchMode = 'web';
      document.querySelectorAll('.search-mode').forEach((item) => {
        item.classList.toggle('is-active', item.dataset.mode === 'web');
      });
      openPanel('search');
      if (el.searchInput) el.searchInput.value = query;
      runLinuxSearch(query);
      return;
    }
    if (action.key === 'terminal') {
      // Терминала в чате нет: код просто сохраняется файлом в рабочую папку
      const ext = { python: 'py', py: 'py', js: 'js', javascript: 'js', ts: 'ts', typescript: 'ts' }[lang] || 'txt';
      const name = `snippets/${Date.now()}-${(text.split('\n')[0] || 'code')
        .replace(/[^A-Za-z0-9_]+/g, '_').slice(0, 24) || 'snippet'}.${ext}`;
      saveToWorkspace(name, text, 'Код сохранён');
      return;
    }
    if (action.key === 'explain' && state.agent) {
      startAgentMessage(`${action.prompt}\n\n\`\`\`${lang}\n${text}\n\`\`\``);
      return;
    }
    if (el.input) {
      el.input.value = `${action.prompt}\n\n\`\`\`${lang}\n${text}\n\`\`\``;
      el.input.focus();
      el.input.dispatchEvent(new Event('input'));
    }
  }

  // ───────────────────────── шпаргалка ─────────────────────────

  function toggleCheatsheet(force) {
    const show = force == null ? el.cheatsheet.hidden : force;
    el.cheatsheet.hidden = !show;
  }

  // ───────────────────────── события ─────────────────────────

  function wire() {
    el.btnLinux && el.btnLinux.addEventListener('click', () => togglePanel());
    el.btnLinuxClose && el.btnLinuxClose.addEventListener('click', closePanel);
    el.btnAgent && el.btnAgent.addEventListener('click', toggleAgent);
    el.btnCheatClose && el.btnCheatClose.addEventListener('click', () => toggleCheatsheet(false));
    el.cheatsheet && el.cheatsheet.addEventListener('click', (event) => {
      if (event.target === el.cheatsheet) toggleCheatsheet(false);
    });

    el.linuxTabs && el.linuxTabs.addEventListener('click', (event) => {
      const button = event.target.closest('.linux-tab');
      if (button) setTab(button.dataset.tab);
    });

    el.btnFilesRefresh && el.btnFilesRefresh.addEventListener('click', loadFiles);
    el.fileList && el.fileList.addEventListener('click', (event) => {
      const row = event.target.closest('.file-row');
      if (row) openFile(row.dataset.path);
    });
    el.btnFileClose && el.btnFileClose.addEventListener('click', closeFile);
    el.btnFileToChat && el.btnFileToChat.addEventListener('click', () => {
      if (!state.file || !el.input) return;
      el.input.value = `Разбери файл ${state.file.path} из рабочей папки:\n\n\`\`\`\n${state.file.content}\n\`\`\``;
      el.input.focus();
      el.input.dispatchEvent(new Event('input'));
      closePanel();
    });
    el.btnFileToTerm && el.btnFileToTerm.addEventListener('click', () => {
      // Просмотрщик файла вместо консоли: тот же текст, но без терминала
      if (!state.file) return;
      el.fileViewPath.textContent = state.file.path;
      el.fileViewBody.textContent = state.file.content;
      el.fileView.hidden = false;
    });

    el.searchForm && el.searchForm.addEventListener('submit', (event) => {
      event.preventDefault();
      runLinuxSearch();
    });
    el.searchModes && el.searchModes.addEventListener('click', (event) => {
      const button = event.target.closest('.search-mode');
      if (!button) return;
      state.searchMode = button.dataset.mode;
      document.querySelectorAll('.search-mode').forEach((item) => {
        item.classList.toggle('is-active', item === button);
      });
      if (el.searchInput) {
        el.searchInput.placeholder = state.searchMode === 'code'
          ? 'что найти в файлах рабочей папки'
          : 'например: python asyncio task group';
      }
    });
    el.searchList && el.searchList.addEventListener('click', (event) => {
      const hit = event.target.closest('.hit-code');
      if (hit) { setTab('files'); openFile(hit.dataset.open); return; }
      const button = event.target.closest('.hit-acts button');
      if (!button) return;
      const item = resultAt(Number(button.closest('.hit').dataset.index));
      if (!item) return;
      const act = button.dataset.act;
      if (act === 'chat') {
        el.input.value = `Разбери источник «${item.title || item.url}» (${item.url}). Что полезного взять?`;
        el.input.focus();
        el.input.dispatchEvent(new Event('input'));
        closePanel();
      } else if (act === 'task') {
        addTask(`Изучить: ${item.title || item.url}`, item.url);
        setTab('tasks');
      } else if (act === 'save') {
        saveSearchResult();
      } else if (act === 'read') {
        readResult(item.url);
      }
    });

    el.btnGitRefresh && el.btnGitRefresh.addEventListener('click', loadGit);

    el.taskForm && el.taskForm.addEventListener('submit', (event) => {
      event.preventDefault();
      addTask(el.taskInput.value);
    });
    el.taskList && el.taskList.addEventListener('click', (event) => {
      const cycle = event.target.closest('[data-cycle]');
      if (cycle) { cycleTask(cycle.dataset.cycle); return; }
      const del = event.target.closest('[data-del]');
      if (del) removeTask(del.dataset.del);
    });

    el.chatContainer && el.chatContainer.addEventListener('click', onCodeAction);

    document.addEventListener('keydown', (event) => {
      const typing = /^(INPUT|TEXTAREA)$/.test(document.activeElement && document.activeElement.tagName);
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'j' && !event.shiftKey) {
        event.preventDefault();
        togglePanel();
        return;
      }
      if ((event.ctrlKey || event.metaKey) && event.shiftKey && event.key.toLowerCase() === 'a') {
        event.preventDefault();
        toggleAgent();
        return;
      }
      if (event.key === '?' && !typing && !event.ctrlKey && !event.metaKey && !event.altKey) {
        event.preventDefault();
        toggleCheatsheet();
        return;
      }
      if (event.key === 'Escape') {
        if (el.cheatsheet && !el.cheatsheet.hidden) { toggleCheatsheet(false); return; }
        if (state.open) { closePanel(); return; }
        if (state.agent) setAgent(false);
      }
    });
  }

  // Публичный интерфейс — им пользуется всё остальное.
  window.Linux = {
    state,
    open: openPanel,
    close: closePanel,
    toggle: togglePanel,
    setTab,
    run: execCommand,
    loadTasks,
    loadFiles,
    loadGit,
    loadStatus,
    search: runLinuxSearch,
    searchInTerminal,
    setSearchMode(mode) { state.searchMode = mode; },
    setAgent,
    toggleAgent,
    toggleCheatsheet,
    isAgentOn: () => state.agent,
  };

  function init() {
    cache();
    if (!el.linuxPanel) return;
    wire();
    state.agent = localStorage.getItem('novamind_agent_mode') === '1';
    loadStatus();
    loadTasks();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
