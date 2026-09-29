/**
 * Смоук-тест фронтенда: настоящие index.html, app.js и linux.js
 * выполняются в jsdom и гоняют полный сценарий поиска, «маленький Linux»
 * и агентного режима.
 *
 * Запуск (нужен запущенный сервер на :5000 и установленный jsdom):
 *   npm install jsdom
 *   node tests/frontend/smoke.mjs [http://127.0.0.1:5000]
 *
 * Проверяется ровно то, что видит пользователь:
 *   1. страница чата грузится без JS-ошибок;
 *   2. включение поиска рисует анимированную сцену и подпись «Поищу в интернете»;
 *   3. шаги поиска добавляются в сцену;
 *   4. источники появляются ВНУТРИ пузыря ИИ;
 *   5. с первым токеном сцена схлопывается и печатается ответ;
 *   6. после завершения сцена скрыта, ответ и ссылки остались;
 *   7. Ctrl+K открывает командную панель; тема переключается;
 *   8. компоновка: сайдбар скрыт по умолчанию и открывается бургером,
 *      у чата и сайдбара нет своих ползунков, длинное имя модели
 *      обрезается многоточием с полным названием в подсказке.
 *   9. страница настроек и окно входа не ломаются;
 *  10. панель «маленький Linux»: терминал, файлы, git, задачи;
 *  11. агентный режим, кнопки у блоков кода и шпаргалка хоткеев;
 *  12. поиск в интернете через песочницу: вкладка «Поиск», nova search,
 *      поиск по песочнице и кнопка «Аналог» у блока кода.
 */
import { createRequire } from 'node:module';

// jsdom ищем и в node_modules проекта, и рядом с песочницей
const require_ = createRequire(import.meta.url);
let JSDOM, VirtualConsole;
for (const candidate of ['jsdom', '/tmp/node_modules/jsdom']) {
  try {
    ({ JSDOM, VirtualConsole } = require_(candidate));
    break;
  } catch (_) { /* пробуем следующий путь */ }
}
if (!JSDOM) {
  console.error('Нужен jsdom: npm install jsdom');
  process.exit(2);
}

const BASE = process.argv[2] || 'http://127.0.0.1:5000';
const failures = [];
const check = (name, condition, extra = '') => {
  if (condition) console.log(`  ✓ ${name}`);
  else { console.log(`  ✗ ${name}${extra ? ' — ' + extra : ''}`); failures.push(name); }
};

// ── Синтетический поток поиска: те же события, что отдаёт /api/auto_search_stream
const SEARCH_STREAM = [
  { type: 'stage', scene: 'search', title: 'Поищу в интернете', text: 'Подключаюсь…' },
  { type: 'step', icon: '🧭', text: 'Анализирую запрос' },
  { type: 'step', icon: '🌐', text: 'Ищу через searxng…' },
  { type: 'step', icon: '✅', text: 'searxng: найдено 2 результатов' },
  { type: 'sources', sources: [
    { title: 'Обзор рынка', url: 'https://example.com/a', host: 'example.com' },
    { title: 'Википедия', url: 'https://ru.wikipedia.org/wiki/X', host: 'ru.wikipedia.org' },
  ] },
  { type: 'stage', scene: 'write', title: 'Собираю ответ', text: 'Обрабатываю источники' },
  { type: 'reasoning', token: 'Проверю цены в двух источниках. ' },
  { type: 'token', token: 'Средняя цена — ' },
  { type: 'token', token: '**12 500 ₽**.' },
  { type: 'result', reply: '', searched: true, sources: [
    { title: 'Обзор рынка', url: 'https://example.com/a', host: 'example.com' },
    { title: 'Википедия', url: 'https://ru.wikipedia.org/wiki/X', host: 'ru.wikipedia.org' },
  ], model: 'mock-model' },
  { type: 'done', searched: true },
];

const CHAT_STREAM = [
  { reasoning: 'Сначала проверю условие. ' },
  { token: 'Ответ: ' },
  { token: '42.' },
  { done: true, model: 'mock-model', provider: 'openai_compatible', has_reply: true },
];

// ── Синтетический поток агента: те же события, что отдаёт /api/agent/stream
const AGENT_STREAM = [
  { type: 'stage', scene: 'agent', title: 'Планирую', text: 'Разбираю задачу по шагам…' },
  { type: 'plan', text: '1) посмотреть файлы\n2) запустить пример' },
  { type: 'task', task: { id: 'task-1', title: 'Проверить песочницу', status: 'doing', source: 'agent', steps: [] } },
  { type: 'step', icon: '📂', text: 'Смотрю файлы — ls -la' },
  { type: 'tool', name: 'run', command: 'ls -la', code: 0, output: 'README.md  hello.py' },
  { type: 'step', icon: '✅', text: '$ python3 hello.py — код 0' },
  { type: 'tool', name: 'run', command: 'python3 hello.py', code: 0, output: 'Привет из рабочей папки NovaMind' },
  { type: 'task', task: { id: 'task-1', title: 'Проверить песочницу', status: 'done', source: 'agent',
                           steps: [{ title: '$ python3 hello.py', status: 'done', log: 'ок' }] } },
  { type: 'token', token: 'Скрипт отработал: ' },
  { type: 'token', token: '**привет из папки**.' },
  { type: 'result', reply: 'Скрипт отработал: привет из папки.', steps: 2, task_id: 'task-1', model: 'mock-model' },
  { type: 'done' },
];

// Крошечный API задач в памяти — проверяем, что фронт говорит с ним по-человечески
const taskStore = new Map();
function tasksApi(path, options = {}) {
  const method = (options.method || 'GET').toUpperCase();
  const body = JSON.parse(options.body || '{}');
  const reply = (data) => ({ ok: true, status: 200, async json() { return data; }, async text() { return JSON.stringify(data); } });
  if (path === '/api/tasks' && method === 'GET') return reply({ tasks: [...taskStore.values()] });
  if (path === '/api/tasks' && method === 'POST') {
    const task = { id: 't' + (taskStore.size + 1), title: body.title, detail: '', status: 'todo',
                   source: 'user', steps: [], created_at: Date.now() / 1000, updated_at: Date.now() / 1000 };
    taskStore.set(task.id, task);
    return reply({ task });
  }
  const id = path.split('/')[3];
  const task = taskStore.get(id);
  if (method === 'DELETE') { taskStore.delete(id); return reply({ deleted: id }); }
  if (method === 'PATCH') {
    Object.assign(task, body);
    taskStore.set(id, task);
    return reply({ task });
  }
  if (path.endsWith('/steps')) {
    task.steps = (task.steps || []).concat([{ title: body.title, status: body.status, log: body.log }]);
    return reply({ task });
  }
  return reply({});
}

function streamResponse(events) {
  const payload = events.map((e) => JSON.stringify(e)).join('\n') + '\n';
  const encoder = new TextEncoder();
  let sent = false;
  return {
    ok: true,
    status: 200,
    mimetype: 'application/x-ndjson',
    body: {
      getReader() {
        return {
          async read() {
            if (sent) return { done: true, value: undefined };
            sent = true;
            return { done: false, value: encoder.encode(payload) };
          },
        };
      },
    },
    async json() { return JSON.parse(payload.split('\n')[0]); },
    async text() { return payload; },
  };
}

const pageHtml = await (await fetch(BASE + '/chat')).text();
// jsdom не тянет внешние скрипты сам — подставляем app.js инлайном,
// ровно тем содержимым, которое отдаёт сервер.
const appJs = await (await fetch(BASE + '/static/app.js')).text();
const linuxJs = await (await fetch(BASE + '/static/linux.js')).text();
const html = pageHtml
  .replace('<script src="/static/app.js"></script>', '<script>' + appJs + '</script>')
  .replace('<script src="/static/linux.js"></script>', '<script>' + linuxJs + '</script>');
if (!html.includes('window.Linux')) throw new Error('не удалось встроить /static/linux.js в страницу');

const virtualConsole = new VirtualConsole();
const consoleErrors = [];
virtualConsole.on('jsdomError', (error) => consoleErrors.push('jsdomError: ' + error.message));
virtualConsole.on('error', (...args) => consoleErrors.push('console.error: ' + args.join(' ')));

const dom = new JSDOM(html, {
  url: BASE + '/chat',
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  virtualConsole,
  resources: undefined, // внешние ресурсы не грузим
  beforeParse(window) {
    window.localStorage.setItem('nova_user_nick', 'Smoke');
    window.TextDecoder = TextDecoder;
    window.TextEncoder = TextEncoder;
    window.AbortController = AbortController;
    window.requestAnimationFrame = (cb) => setTimeout(cb, 0);
    window.scrollTo = () => {};
    window.Element.prototype.scrollTo = () => {};
    window.HTMLMediaElement.prototype.play = () => Promise.resolve();
    window.speechSynthesis = { cancel() {}, speak() {} };
    window.SpeechSynthesisUtterance = class { constructor(text) { this.text = text; } };
    window.addEventListener('error', (event) => consoleErrors.push('window.onerror: ' + event.message));
    window.fetch = async (url, options = {}) => {
      const path = String(url).replace(BASE, '');
      const json = (data) => ({ ok: true, status: 200, async json() { return data; }, async text() { return JSON.stringify(data); } });
      if (path.startsWith('/api/auto_search_stream')) return streamResponse(SEARCH_STREAM);
      if (path.startsWith('/send_stream')) return streamResponse(CHAT_STREAM);
      if (path.startsWith('/api/ai/status')) {
        return { ok: true, status: 200, async json() { return { provider: 'openai_compatible', provider_name: 'Local', model: 'mock-model', offline: false, configured: true }; } };
      }
      if (path.startsWith('/api/search/health')) {
        return { ok: true, status: 200, async json() { return { ok: true, alive: ['searxng'], backends: [{ backend: 'searxng', ok: true, count: 2, ms: 12, error: null }] }; } };
      }
      if (path.startsWith('/api/media/')) {
        return { ok: true, status: 200, async json() { return { selection: null }; } };
      }
      if (path.startsWith('/api/terminal/status')) {
        return json({ enabled: true, admin: true, available: true, workspace: 'workspace', git: true,
                      commands: ['ls', 'python3', 'git'] });
      }
      if (path.startsWith('/api/terminal/run')) {
        const command = String(JSON.parse(options.body || '{}').command || '');
        if (command.startsWith('nova search')) {
          return json({ code: 0, stdout: '🔎 «' + command.slice(12) + '» — найдено 2 (0.2 c, searxng)\n'
            + '1. asyncio — документация\n   https://docs.python.org/3/library/asyncio.html\n'
            + '   Event loop, tasks and queues.\n'
            + '2. PEP 492\n   https://peps.python.org/pep-0492/\n   async/await syntax.',
            stderr: '', duration_ms: 200, timed_out: false, cwd: '.' });
        }
        if (command === 'ls -la') {
          return json({ code: 0, stdout: 'README.md  hello.py', stderr: '', duration_ms: 12, timed_out: false, cwd: '.' });
        }
        if (command.startsWith('shutdown')) {
          return { ok: false, status: 400, async json() { return { error: 'Команда «shutdown» не в списке разрешённых' }; } };
        }
        return json({ code: 0, stdout: 'Привет из рабочей папки NovaMind', stderr: '', duration_ms: 40, timed_out: false, cwd: '.' });
      }
      if (path.startsWith('/api/terminal/files')) {
        return json({ files: [
          { path: 'hello.py', name: 'hello.py', size: 220, dir: '.' },
          { path: 'notes/ideas.md', name: 'ideas.md', size: 48, dir: 'notes' },
        ], truncated: false, workspace: 'workspace' });
      }
      if (path.startsWith('/api/terminal/file')) {
        return json({ path: 'hello.py', content: 'print(\"привет\")' });
      }
      if (path.startsWith('/api/terminal/git')) {
        return json({ repo: true, branch: 'main', clean: false, last: 'a1b2c3d первый коммит (Nova, 2026-09-29)',
                      changes: [{ status: 'M', path: 'hello.py' }] });
      }
      if (path.startsWith('/api/linux/search')) {
        const body = JSON.parse(options.body || '{}');
        if (body.mode === 'code') {
          return json({ mode: 'code', query: body.query, count: 1, truncated: false, results: [
            { path: 'hello.py', line: 2, snippet: 'print("привет")', url: '' },
          ] });
        }
        return json({ mode: 'web', query: body.query, count: 2, backend: 'searxng', elapsed_ms: 240, results: [
          { title: 'asyncio — документация', url: 'https://docs.python.org/3/library/asyncio.html',
            host: 'docs.python.org', snippet: 'Event loop, tasks and queues.' },
          { title: 'PEP 492', url: 'https://peps.python.org/pep-0492/',
            host: 'peps.python.org', snippet: 'async/await syntax.' },
        ] });
      }
      if (path.startsWith('/api/linux/save')) {
        return json({ path: 'notes/research/2026-09-30-asyncio-notes.md' });
      }
      if (path.startsWith('/api/linux/read')) {
        return json({ url: 'https://docs.python.org/3/library/asyncio.html',
                      text: 'Текст страницы asyncio', elapsed_ms: 120, bytes: 22 });
      }
      if (path.startsWith('/api/agent/status')) {
        return json({ enabled: true, tools: true, max_steps: 6, admin: true, hint: '' });
      }
      if (path.startsWith('/api/agent/stream')) return streamResponse(AGENT_STREAM);
      if (path.startsWith('/api/tasks')) return tasksApi(path, options);
      if (path.startsWith('/send')) {
        return { ok: true, status: 200, async json() { return { reply: 'Запасной ответ', chat_id: 'x' }; } };
      }
      return { ok: true, status: 200, async json() { return {}; }, async text() { return '{}'; } };
    };
  },
});

const { window } = dom;
const { document } = window;
await new Promise((resolve) => setTimeout(resolve, 250));

console.log('\n1) Загрузка страницы чата');
check('нет JS-ошибок при загрузке', consoleErrors.length === 0, consoleErrors.join(' | '));
check('liquid-glass.css подключён', html.includes('liquid-glass.css'));
check('тема стекла применена', document.documentElement.getAttribute('data-glass-theme') === 'aurora');
check('функции app.js доступны', typeof window.toggleWebSearch === 'function'
  && typeof window.createAssistantTurn === 'function');
check('статус ИИ отрисован в топбаре',
  document.getElementById('statusText').textContent.includes('mock-model'),
  document.getElementById('statusText').textContent);

console.log('\n2) Поиск в интернете: сцена → шаги → источники → ответ');
window.document.getElementById('chat-input').value = 'сколько стоит?';
window.toggleWebSearch();
check('кнопка поиска активна', document.getElementById('btn-web-search').classList.contains('active'));

await window.sendMessage();
await new Promise((resolve) => setTimeout(resolve, 120));

const bubbles = document.querySelectorAll('.message.ai .msg-bubble');
const lastBubble = bubbles[bubbles.length - 1];
check('пузырь ИИ создан', Boolean(lastBubble));
check('источники внутри пузыря', lastBubble.querySelectorAll('.source-item').length === 2,
  `найдено ${lastBubble.querySelectorAll('.source-item').length}`);
check('первый источник — ссылка на сайт',
  lastBubble.querySelector('.source-item')?.getAttribute('href') === 'https://example.com/a');
check('ответ напечатан', lastBubble.querySelector('.answer').textContent.includes('12 500'));
check('жирный текст отформатирован', Boolean(lastBubble.querySelector('.answer strong')));
check('панель рассуждений заполнена',
  lastBubble.querySelector('.reasoning-text')?.textContent.includes('Проверю цены') === true);
check('рассуждение свёрнуто после ответа',
  lastBubble.querySelector('.reasoning')?.classList.contains('done') === true);

// Сцена схлопывается при первом токене
const stage = lastBubble.querySelector('.stage');
check('сцена схлопнута', stage.classList.contains('collapsed'));
await new Promise((resolve) => setTimeout(resolve, 700));
check('сцена убрана из пузыря', stage.classList.contains('lg-hidden'));
check('источники остались видимыми', lastBubble.querySelectorAll('.source-item').length === 2);
check('мета ответа показана', lastBubble.parentElement.querySelector('.msg-meta').textContent.includes('mock-model'),
  lastBubble.parentElement.querySelector('.msg-meta')?.textContent);
check('действия ответа доступны',
  lastBubble.parentElement.querySelectorAll('.msg-actions button').length === 3);
check('сообщение пользователя в чате',
  document.querySelectorAll('.message.user').length === 1);
check('история сохранена', JSON.parse(window.localStorage.getItem('nova_history_v2')).chats.length === 1);

console.log('\n3) Обычный чат (без поиска): пузырь пустой, потом поток');
window.toggleWebSearch();
document.getElementById('chat-input').value = 'сколько будет 6*7';
await window.sendMessage();
await new Promise((resolve) => setTimeout(resolve, 120));
const bubbles2 = document.querySelectorAll('.message.ai .msg-bubble');
const chatBubble = bubbles2[bubbles2.length - 1];
check('ответ получен потоком', chatBubble.querySelector('.answer').textContent.includes('42'));
check('источников нет', chatBubble.querySelectorAll('.source-item').length === 0);

console.log('\n4) Бонусы интерфейса');
window.openCmdk();
check('командная панель открыта', document.getElementById('cmdk').classList.contains('open'));
check('действия в панели есть', document.querySelectorAll('.cmdk-item').length > 5);
window.closeCmdk();
check('командная панель закрыта', !document.getElementById('cmdk').classList.contains('open'));

window.cycleTheme();
check('тема всегда Aurora (единственная палитра)',
  document.documentElement.getAttribute('data-glass-theme') === 'aurora');

window.toggleCalmMotion();
check('спокойный режим включается',
  document.documentElement.getAttribute('data-motion') === 'calm');

window.toggleReasoning();
check('рассуждения переключаются',
  document.getElementById('btn-reasoning').classList.contains('active'));

window.showNotification('Тест', 'ok');
check('тост показан', document.querySelectorAll('#toastStack .lg-toast').length >= 1,
  `в стеке ${document.querySelectorAll('#toastStack .lg-toast').length}`);

// ══════════════════════════════════════════════════════════════
// 5) Компоновка: один ползунок на всё окно, короткие имена моделей
// ══════════════════════════════════════════════════════════════
console.log('\n5) Компоновка чата');
const chatCss = await (await fetch(BASE + '/static/style.css')).text();
const cssBlock = (selector) => (chatCss.match(new RegExp('\\' + selector + '\\s*\\{[^}]*\\}')) || [''])[0];

const appShell = document.querySelector('.app');
check('сайдбар скрыт по умолчанию', appShell.classList.contains('nav-off'));
check('бургер отмечен как закрытый', document.getElementById('btnNav').getAttribute('aria-expanded') === 'false');
window.toggleSidebar();
check('бургер открывает панель', !appShell.classList.contains('nav-off'));
check('состояние панели запомнено', window.localStorage.getItem('nova_nav_open') === '1');
window.toggleSidebar();
check('бургер снова прячет панель', appShell.classList.contains('nav-off'));

check('прокручивается всё окно, а не переписка',
  !/overflow-y:\s*auto/.test(cssBlock('.chat-container')), cssBlock('.chat-container'));
check('у сайдбара нет своего ползунка',
  !/overflow-y:\s*auto/.test(cssBlock('.sidebar-body')), cssBlock('.sidebar-body'));
check('поле ввода прилипает к низу окна', /position:\s*sticky/.test(cssBlock('.input-area')));
check('шапка прилипает к верху окна', /position:\s*sticky/.test(cssBlock('.topbar')));

const probe = document.createElement('span');
window.setShortText(probe, 'meta-llama/Llama-3.3-70B-Instruct-Turbo-Provider', 26);
check('длинное имя модели обрезано многоточием',
  probe.textContent.endsWith('…') && probe.textContent.length <= 27, probe.textContent);
check('полное имя модели — в подсказке', probe.title.startsWith('meta-llama'), probe.title);
check('имя модели в шапке не выталкивает кнопки', /#statusText\s*\{[^}]*text-overflow:\s*ellipsis/.test(chatCss));
check('у модели в шапке есть подсказка с полным именем',
  document.getElementById('statusText').title.length > 0, document.getElementById('statusText').title);

// ══════════════════════════════════════════════════════════════
// 6) Кнопка «стоп», режимы и плашки
// ══════════════════════════════════════════════════════════════
console.log('\n6) Кнопка «стоп» и активный режим');

// Поток, который не отвечает, — как раз тот случай, когда пользователь
// жмёт «стоп» на пустом поле ввода.
const hanging = (signal) => ({
  ok: true, status: 200,
  body: {
    getReader: () => ({
      read: () => new Promise((_, reject) => {
        signal?.addEventListener('abort', () => {
          const error = new Error('aborted');
          error.name = 'AbortError';
          reject(error);
        }, { once: true });
      }),
    }),
  },
});
const realFetch = window.fetch;
window.fetch = async (url, options = {}) =>
  String(url).includes('/send_stream') ? hanging(options?.signal) : realFetch(url, options);

document.getElementById('chat-input').value = 'долгий ответ';
const pending = window.sendMessage();
await new Promise((resolve) => setTimeout(resolve, 60));
check('во время генерации кнопка становится «стоп»',
  document.getElementById('sendBtn').classList.contains('is-stop'));
document.getElementById('chat-input').value = '';     // поле пустое, как у пользователя
await window.sendMessage();
await pending.catch(() => {});
check('«стоп» срабатывает при пустом поле ввода',
  !document.getElementById('sendBtn').classList.contains('is-stop'));
check('после остановки показано уведомление',
  [...document.querySelectorAll('#toastStack .lg-toast')].some((t) => t.textContent.includes('остановлена')));
window.fetch = realFetch;

const modeBar = document.getElementById('modeActiveBar');
check('плашка режима скрыта в обычном чате', modeBar.hidden === true);
window.activateMode({ prefix: '/services weather ', label: '🌤 Погода', placeholder: 'Введите город…' });
check('режим включается и показывается', modeBar.hidden === false
  && document.getElementById('modeActiveText').textContent.includes('Погода'));
check('поле ввода просит город', document.getElementById('chat-input').placeholder.includes('город'));
window.activateMode({ prefix: '/services weather ', label: '🌤 Погода', placeholder: 'Введите город…' });
check('повторный клик по режиму выключает его', modeBar.hidden === true
  && document.getElementById('chat-input').placeholder === 'Напишите сообщение...');
window.activateMode({ prefix: '/services wiki ', label: '📚 Википедия', placeholder: 'Запрос…' });
window.newChat();
check('новый чат снимает активный режим', modeBar.hidden === true);
check('плашка медиа не висит без выбранной модели',
  document.getElementById('mediaActiveBar').hidden === true);

console.log('\n7) Ошибок за весь прогон не появилось');
check('консоль чистая', consoleErrors.length === 0, consoleErrors.join(' | '));

// ══════════════════════════════════════════════════════════════
// 8) Страница настроек: провайдеры, каталог, диагностика поиска
// ══════════════════════════════════════════════════════════════
console.log('\n8) Страница настроек');
const settingsHtml = await (await fetch(BASE + '/settings')).text();
const settingsJs = await (await fetch(BASE + '/static/settings.js')).text();

const settingsErrors = [];
const settingsConsole = new VirtualConsole();
settingsConsole.on('jsdomError', (error) => settingsErrors.push('jsdomError: ' + error.message));
settingsConsole.on('error', (...args) => settingsErrors.push('console.error: ' + args.join(' ')));

const settingsDom = new JSDOM(
  settingsHtml.replace('<script src="/static/settings.js"></script>', '<script>' + settingsJs + '</script>'),
  {
    url: BASE + '/settings',
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    virtualConsole: settingsConsole,
    beforeParse(win) {
      win.TextDecoder = TextDecoder;
      win.requestAnimationFrame = (cb) => setTimeout(cb, 0);
      win.addEventListener('error', (event) => settingsErrors.push('window.onerror: ' + event.message));
      win.fetch = async (url) => {
        const path = String(url).replace(BASE, '');
        const json = (data) => ({ ok: true, status: 200, async json() { return data; } });
        if (path.startsWith('/api/settings/providers')) {
          return json({
            providers: [
              { id: 'groq', name: 'Groq', configured: false, url: 'https://api.groq.com/openai/v1', hint: 'нужен GROQ_API_KEY', offline: false },
              { id: 'local_demo', name: 'Локальная демо-модель', configured: true, url: 'local://', hint: 'работает всегда', offline: true },
            ],
            current_provider: 'local_demo',
            current_model: 'nova-local-1',
          });
        }
        if (path.startsWith('/api/settings/models')) {
          return json({ provider: 'local_demo', configured: true, models: [
            { id: 'nova-local-1', name: 'Nova Local 1', provider: 'local_demo', context_length: 8192,
              free: true, modality: 'text', description: 'офлайн' },
          ] });
        }
        if (path.startsWith('/api/settings/media-models')) {
          return json({ models: [], count: 0 });
        }
        if (path.startsWith('/api/search/health')) {
          return json({ ok: true, alive: ['searxng', 'wiki'], backends: [
            { backend: 'searxng', ok: true, count: 8, ms: 240, error: null },
            { backend: 'ddg', ok: false, count: 0, ms: 12, error: 'HTTP 403' },
            { backend: 'wiki', ok: true, count: 3, ms: 90, error: null },
          ] });
        }
        return json({});
      };
    },
  },
);
await new Promise((resolve) => setTimeout(resolve, 350));
const sdoc = settingsDom.window.document;

check('настройки: нет JS-ошибок', settingsErrors.length === 0, settingsErrors.join(' | '));
check('настройки: liquid-glass подключён', settingsHtml.includes('liquid-glass.css'));
check('провайдеры отрисованы', sdoc.querySelectorAll('.provider-card').length === 2);
check('активный провайдер подсвечен',
  Boolean(sdoc.querySelector('.provider-card.active'))
  && sdoc.querySelector('.provider-card.active').dataset.provider === 'local_demo');
check('текущая модель в пилюле',
  sdoc.getElementById('currentPill').textContent.includes('nova-local-1'),
  sdoc.getElementById('currentPill').textContent);
check('каталог моделей отрисован', sdoc.querySelectorAll('.model-card').length >= 1);
check('диагностика поиска отрисована', sdoc.querySelectorAll('.health-item').length === 3);
check('диагностика: живой бэкенд помечен',
  sdoc.querySelectorAll('.health-item .lg-dot.ok').length === 2);
check('диагностика: упавший бэкенд помечен',
  sdoc.querySelectorAll('.health-item .lg-dot.err').length === 1);
check('статус поиска объясняет итог',
  sdoc.getElementById('healthStatus').textContent.includes('2 из 3'),
  sdoc.getElementById('healthStatus').textContent);
check('кнопка темы держит Aurora', (() => {
  sdoc.getElementById('themeBtn').dispatchEvent(new settingsDom.window.Event('click'));
  return settingsDom.window.document.documentElement.getAttribute('data-glass-theme') === 'aurora';
})());

// ══════════════════════════════════════════════════════════════
// 9) Окно входа
// ══════════════════════════════════════════════════════════════
console.log('\n9) Окно входа');
const authHtml = await (await fetch(BASE + '/')).text();
const authErrors = [];
const authConsole = new VirtualConsole();
authConsole.on('jsdomError', (error) => authErrors.push('jsdomError: ' + error.message));
authConsole.on('error', (...args) => authErrors.push('console.error: ' + args.join(' ')));

const authDom = new JSDOM(authHtml.replace(/<script src="https:[^"]*"[^>]*><\/script>/g, ''), {
  url: BASE + '/',
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  virtualConsole: authConsole,
  beforeParse(win) {
    win.addEventListener('error', (event) => authErrors.push('window.onerror: ' + event.message));
    win.fetch = async () => ({ ok: true, status: 200, async json() { return { success: true }; } });
  },
});
await new Promise((resolve) => setTimeout(resolve, 150));
const adoc = authDom.window.document;

check('вход: нет JS-ошибок', authErrors.length === 0, authErrors.join(' | '));
check('вход: liquid-glass подключён', authHtml.includes('liquid-glass.css'));
check('вход: форма видна', adoc.getElementById('loginForm').classList.contains('active'));
check('вход: переключение на регистрацию', (() => {
  authDom.window.showForm('register');
  return adoc.getElementById('registerForm').classList.contains('active')
    && !adoc.getElementById('loginForm').classList.contains('active');
})());
check('вход: тема всегда Aurora', (() => {
  authDom.window.setAuthTheme('sunset');
  return adoc.documentElement.getAttribute('data-glass-theme') === 'aurora'
    && authDom.window.localStorage.getItem('nova_theme') === 'aurora';
})());
check('вход: гость заполняет форму и логинится', (() => {
  authDom.window.quickLogin('Guest');
  return adoc.getElementById('loginNick').value === 'Guest_User'
    && authDom.window.localStorage.getItem('nova_user_nick') === 'Guest_User';
})());
await new Promise((resolve) => setTimeout(resolve, 150));
// jsdom не умеет реальную навигацию — это ограничение теста, а не страницы
const realErrors = authErrors.filter((item) => !item.includes('Not implemented: navigation'));
check('вход: после входа нет ошибок', realErrors.length === 0, realErrors.join(' | '));

console.log('\n10) Маленький Linux: панель, терминал, файлы, git, задачи');
const linuxPanel = document.getElementById('linuxPanel');
const termOut = document.getElementById('termOut');
const termInput = document.getElementById('termInput');
const press = (key, options = {}) => document.dispatchEvent(
  new window.KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...options })
);

check('панель скрыта до первого открытия', linuxPanel.hidden === true);
press('j', { ctrlKey: true });
await new Promise((resolve) => setTimeout(resolve, 120));
check('Ctrl+J открывает панель', linuxPanel.hidden === false
  && document.body.classList.contains('linux-open')
  && document.getElementById('btnLinux').getAttribute('aria-expanded') === 'true');
check('доступ к песочнице подтверждён, индикатор горит',
  document.getElementById('linuxLed').classList.contains('is-on')
  && document.getElementById('linuxPath').textContent.includes('workspace'));

termInput.value = 'ls -la';
document.getElementById('termForm').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
await new Promise((resolve) => setTimeout(resolve, 120));
check('терминал выполняет команду и печатает вывод',
  termOut.textContent.includes('$ ls -la') && termOut.textContent.includes('README.md')
  && termOut.textContent.includes('код 0'), termOut.textContent.slice(-120));

termInput.value = 'shutdown now';
document.getElementById('termForm').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
await new Promise((resolve) => setTimeout(resolve, 120));
check('команда вне белого списка ругается понятно',
  termOut.textContent.includes('не в списке разрешённых'));

document.querySelector('#linuxTabs .linux-tab[data-tab="files"]').click();
await new Promise((resolve) => setTimeout(resolve, 120));
check('вкладка «Файлы» показывает дерево рабочей папки',
  document.getElementById('fileList').textContent.includes('hello.py')
  && document.getElementById('fileList').textContent.includes('notes'));
document.querySelector('.file-row[data-path="hello.py"]').click();
await new Promise((resolve) => setTimeout(resolve, 120));
check('файл открывается для просмотра',
  document.getElementById('fileView').hidden === false
  && document.getElementById('fileViewBody').textContent.includes('print'));
document.getElementById('btnFileToChat').click();
await new Promise((resolve) => setTimeout(resolve, 120));
check('«В чат» подставляет содержимое файла в поле ввода',
  document.getElementById('chat-input').value.includes('hello.py')
  && linuxPanel.hidden === true);

window.Linux.open('git');
await new Promise((resolve) => setTimeout(resolve, 120));
check('вкладка «Git» показывает ветку и изменения',
  document.getElementById('gitBox').textContent.includes('main')
  && document.getElementById('gitBox').textContent.includes('hello.py'));

window.Linux.open('tasks');
await new Promise((resolve) => setTimeout(resolve, 120));
document.getElementById('taskInput').value = 'Починить смоук';
document.getElementById('taskForm').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
await new Promise((resolve) => setTimeout(resolve, 150));
check('задача добавляется в список и счётчик',
  document.getElementById('taskList').textContent.includes('Починить смоук')
  && document.getElementById('tasksBadge').hidden === false);
document.querySelector('#taskList [data-cycle]').click();
await new Promise((resolve) => setTimeout(resolve, 150));
check('клик по чекбоксу переключает статус задачи',
  document.querySelector('#taskList .task-card').className.includes('is-doing'));
document.querySelector('#taskList [data-del]').click();
await new Promise((resolve) => setTimeout(resolve, 150));
check('задача удаляется', !document.getElementById('taskList').textContent.includes('Починить смоук'));

press('j', { ctrlKey: true });
await new Promise((resolve) => setTimeout(resolve, 100));
check('Ctrl+J закрывает панель', linuxPanel.hidden === true);

console.log('\n11) Агентный режим и бонусы для программиста');
const agentButton = document.getElementById('btnAgent');
agentButton.click();
await new Promise((resolve) => setTimeout(resolve, 120));
check('агентный режим включается и меняет подсказку',
  agentButton.classList.contains('is-on')
  && agentButton.getAttribute('aria-pressed') === 'true'
  && document.getElementById('chat-input').placeholder.includes('агент'));

document.getElementById('chat-input').value = 'запусти hello.py и скажи результат';
await window.sendMessage();
await new Promise((resolve) => setTimeout(resolve, 350));
const aiTurns = document.querySelectorAll('.message.ai');
const agentBubble = aiTurns[aiTurns.length - 1];
check('агент показывает план', !!agentBubble && agentBubble.querySelector('.stage-plan') !== null);
check('агент показывает выполненные команды',
  !!agentBubble && agentBubble.querySelectorAll('.agent-tool').length === 2
  && agentBubble.textContent.includes('python3 hello.py'));
check('агент показывает заведённую задачу',
  !!agentBubble && agentBubble.querySelector('.agent-task') !== null
  && agentBubble.querySelector('.agent-task').textContent.includes('Проверить песочницу'));
check('агент печатает ответ и закрывает сцену',
  !!agentBubble && agentBubble.querySelector('.answer').textContent.includes('привет из папки')
  && agentBubble.querySelector('.stage').classList.contains('collapsed'));
check('мета ответа упоминает агента и шаги',
  !!agentBubble && agentBubble.querySelector('.msg-meta').textContent.includes('агент')
  && agentBubble.querySelector('.msg-meta').textContent.includes('шагов: 2'));

window.Linux.open('terminal');
await new Promise((resolve) => setTimeout(resolve, 100));
press('Escape');   // первый Esc — панель
await new Promise((resolve) => setTimeout(resolve, 100));
press('Escape');   // второй Esc — агентный режим
await new Promise((resolve) => setTimeout(resolve, 100));
check('Escape закрывает панель, затем выключает агентный режим',
  linuxPanel.hidden === true && !agentButton.classList.contains('is-on'));

// кнопки у блока кода
const codeTurn = window.createAssistantTurn();
codeTurn.appendText('Вот функция:\n\n```python\ndef add(a, b):\n    return a + b\n```');
await new Promise((resolve) => setTimeout(resolve, 80));
const codeBlock = codeTurn.element.querySelector('.code-block');
check('у блока кода есть кнопки для программиста',
  !!codeBlock && codeBlock.querySelector('[data-code="explain"]')
  && codeBlock.querySelector('[data-code="tests"]')
  && codeBlock.querySelector('[data-code="optimize"]')
  && codeBlock.querySelector('[data-code="terminal"]'));

codeBlock.querySelector('[data-code="explain"]').dispatchEvent(
  new window.MouseEvent('click', { bubbles: true, cancelable: true }));
await new Promise((resolve) => setTimeout(resolve, 80));
check('«Объясни» подставляет код в поле ввода',
  document.getElementById('chat-input').value.includes('Объясни этот код')
  && document.getElementById('chat-input').value.includes('return a + b'));

codeBlock.querySelector('[data-code="terminal"]').dispatchEvent(
  new window.MouseEvent('click', { bubbles: true, cancelable: true }));
await new Promise((resolve) => setTimeout(resolve, 150));
check('«В терминал» открывает панель и подставляет имя файла',
  linuxPanel.hidden === false && termInput.value.includes('snippets/')
  && termOut.textContent.includes('def add'), termInput.value);

press('Escape');
await new Promise((resolve) => setTimeout(resolve, 100));
check('Escape закрывает панель', linuxPanel.hidden === true);

// в агентном режиме «Объясни» не подставляет текст, а сразу идёт агенту
agentButton.click();
await new Promise((resolve) => setTimeout(resolve, 100));
const turnsBefore = document.querySelectorAll('.message.ai').length;
document.getElementById('chat-input').value = '';
codeBlock.querySelector('[data-code="explain"]').dispatchEvent(
  new window.MouseEvent('click', { bubbles: true, cancelable: true }));
await new Promise((resolve) => setTimeout(resolve, 300));
check('в агентном режиме «Объясни» уходит агенту, а не в поле',
  document.getElementById('chat-input').value === ''
  && document.querySelectorAll('.message.ai').length === turnsBefore + 1);
window.Linux.setAgent(false);
await new Promise((resolve) => setTimeout(resolve, 100));

document.activeElement && document.activeElement.blur();   // «?» работает вне поля ввода
press('?', { keyCode: 63 });
await new Promise((resolve) => setTimeout(resolve, 100));
check('«?» открывает шпаргалку хоткеев',
  document.getElementById('cheatsheet').hidden === false
  && document.querySelector('.cheat-list').textContent.includes('Ctrl')
  && document.querySelector('.cheat-list').textContent.includes('агентный режим'));
press('Escape');
await new Promise((resolve) => setTimeout(resolve, 100));
check('Escape закрывает шпаргалку', document.getElementById('cheatsheet').hidden === true);

console.log('\n12) Поиск в интернете через Linux-окружение');
window.Linux.open('search');
await new Promise((resolve) => setTimeout(resolve, 100));
const searchInput = document.getElementById('searchInput');
const searchList = document.getElementById('searchList');

check('вкладка «Поиск» есть и переключает режим',
  !!document.querySelector('#linuxTabs .linux-tab[data-tab="search"]')
  && document.querySelector('.linux-sec[data-sec="search"]').classList.contains('is-active'));

searchInput.value = 'python asyncio';
document.getElementById('searchForm').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
await new Promise((resolve) => setTimeout(resolve, 200));
check('поиск в интернете возвращает источники',
  searchList.textContent.includes('asyncio — документация')
  && searchList.textContent.includes('peps.python.org')
  && document.getElementById('searchStatus').textContent.includes('searxng'));

searchList.querySelector('[data-act="chat"]').click();
await new Promise((resolve) => setTimeout(resolve, 120));
check('«В чат» подставляет источник в поле ввода',
  document.getElementById('chat-input').value.includes('docs.python.org'));

window.Linux.open('search');
await new Promise((resolve) => setTimeout(resolve, 100));
document.getElementById('searchForm').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
await new Promise((resolve) => setTimeout(resolve, 200));
searchList.querySelector('[data-act="save"]').click();
await new Promise((resolve) => setTimeout(resolve, 200));
check('«Сохранить» уводит выдачу в рабочую папку',
  document.querySelector('.linux-sec[data-sec="files"]').classList.contains('is-active')
  || termOut.textContent.includes('notes/research'));

window.Linux.open('search');
await new Promise((resolve) => setTimeout(resolve, 100));
document.getElementById('searchForm').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
await new Promise((resolve) => setTimeout(resolve, 200));
searchList.querySelector('[data-act="task"]').click();
await new Promise((resolve) => setTimeout(resolve, 200));
check('«В задачу» заводит задачу и открывает список',
  document.querySelector('.linux-sec[data-sec="tasks"]').classList.contains('is-active')
  && document.getElementById('taskList').textContent.includes('Изучить:'));

window.Linux.open('search');
await new Promise((resolve) => setTimeout(resolve, 100));
searchList.querySelector('[data-act="read"]').click();
await new Promise((resolve) => setTimeout(resolve, 200));
check('«Читать» открывает страницу источника в терминале',
  document.querySelector('.linux-sec[data-sec="terminal"]').classList.contains('is-active')
  && termOut.textContent.includes('Текст страницы asyncio'));

// тот же запрос, но по файлам песочницы
window.Linux.open('search');
await new Promise((resolve) => setTimeout(resolve, 100));
document.querySelector('#searchModes .search-mode[data-mode="code"]').click();
searchInput.value = 'asyncio';
document.getElementById('searchForm').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
await new Promise((resolve) => setTimeout(resolve, 200));
check('поиск по песочнице находит вхождения в коде',
  searchList.textContent.includes('hello.py')
  && searchList.textContent.includes('print'));

// «Найди аналог» у блока кода
const simTurn = window.createAssistantTurn();
simTurn.appendText('```python\ndef fetch_users(limit=10):\n    return db.query("users", limit)\n```');
await new Promise((resolve) => setTimeout(resolve, 80));
const simBlock = simTurn.element.querySelector('.code-block');
check('у блока кода есть кнопка «Аналог»', !!simBlock.querySelector('[data-code="similar"]'));
simBlock.querySelector('[data-code="similar"]').dispatchEvent(
  new window.MouseEvent('click', { bubbles: true, cancelable: true }));
await new Promise((resolve) => setTimeout(resolve, 250));
check('«Аналог» ищет по коду в интернете через песочницу',
  linuxPanel.hidden === false
  && document.querySelector('.linux-sec[data-sec="search"]').classList.contains('is-active')
  && searchInput.value.includes('python') && searchInput.value.includes('fetch_users'),
  searchInput.value);
check('поиск по коду сразу показал результаты',
  searchList.textContent.includes('asyncio — документация'));

// терминал умеет тот же поиск
window.Linux.setTab('terminal');
termInput.value = 'nova search asyncio';
document.getElementById('termForm').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
await new Promise((resolve) => setTimeout(resolve, 200));
check('команда nova search в терминале печатает источники',
  termOut.textContent.includes('$ nova search asyncio')
  && termOut.textContent.includes('docs.python.org'));
check('в терминале есть чипы поиска',
  document.getElementById('termChips').textContent.includes('Поиск в интернете')
  && document.getElementById('termChips').textContent.includes('Поиск по песочнице'));
press('Escape');
await new Promise((resolve) => setTimeout(resolve, 80));

if (failures.length) {
  console.error(`\n❌ Провалено проверок: ${failures.length} → ${failures.join(', ')}`);
  process.exit(1);
}
console.log('\n✅ Фронтенд-смоук пройден полностью');
process.exit(0);
