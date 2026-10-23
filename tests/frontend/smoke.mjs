/**
 * Смоук-тест фронтенда: настоящие index.html, app.js и linux.js
 * выполняются в jsdom и гоняют полный сценарий: поиск, обычный чат и
 * главный чат, в котором ИИ сам работает в Linux-окружении.
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
 *   7. Ctrl+K открывает командную панель; кнопки темы нет;
 *   8. компоновка: сайдбар скрыт по умолчанию и открывается бургером,
 *      у чата и сайдбара нет своих ползунков, длинное имя модели
 *      обрезается многоточием с полным названием в подсказке.
 *   9. страница настроек и окно входа не ломаются;
 *  10. лишнего нет: кнопок «Агент», «Linux», «Тема», панели Linux,
 *      шпаргалки и карточек-подсказок; скрытое не всплывает на телефоне;
 *  11. главный чат сам работает в Linux: план, шаги, свёрнутый блок
 *      «Работа в Linux», простые вопросы — сразу текстом;
 *  12. кнопка «Поиск» (автопоиск) идёт через Linux-окружение,
 *      кнопки у блоков кода, откат на обычный чат, «стоп»;
 *  13. ИИ сам генерирует картинки, аудио и видео: кнопок «Медиа» нет,
 *      файл появляется прямо в ответе и остаётся после перезагрузки.
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
  { type: 'stage', scene: 'agent', title: 'Работаю в Linux', text: 'Иду к цели по шагам…' },
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

// Простой вопрос: модель ответила сразу, без окружения
const SIMPLE_STREAM = [
  { type: 'token', token: 'Привет! ' },
  { type: 'token', token: 'Чем помочь?' },
  { type: 'result', reply: 'Привет! Чем помочь?', steps: 0, model: 'mock-model', sources: [], searched: false },
  { type: 'done' },
];

// Модель начала фразой, потом выдала действие — фразу забираем назад
const RETRACT_STREAM = [
  { type: 'token', token: 'Сейчас посмотрю…' },
  { type: 'retract' },
  { type: 'stage', scene: 'agent', title: 'Работаю в Linux', text: 'Иду к цели по шагам…' },
  { type: 'tool', name: 'run', command: 'ls -la', code: 0, output: 'hello.py' },
  { type: 'token', token: 'В папке есть hello.py' },
  { type: 'result', reply: 'В папке есть hello.py', steps: 1, model: 'mock-model' },
  { type: 'done' },
];

// Кнопка «Поиск»: nova search → чтение страниц → ответ со ссылками
const LINUX_SOURCES = [
  { title: 'Обзор рынка', url: 'https://example.com/a', host: 'example.com' },
  { title: 'Википедия', url: 'https://ru.wikipedia.org/wiki/X', host: 'ru.wikipedia.org' },
];
const LINUX_SEARCH_STREAM = [
  { type: 'stage', scene: 'search', title: 'Поищу в интернете', text: 'Ищу через Linux-окружение…' },
  { type: 'step', icon: '🔎', text: 'Нашёл источников: 2 (searxng)' },
  { type: 'tool', name: 'search', command: 'nova search цена ноутбука', code: 0, output: '[1] Обзор рынка' },
  { type: 'sources', sources: LINUX_SOURCES },
  { type: 'tool', name: 'open', command: 'nova read https://example.com/a', code: 0, output: 'Текст страницы' },
  { type: 'step', icon: '💾', text: 'Сохранил выдержки: notes/research/2026-09-29-cena-search.md' },
  { type: 'stage', scene: 'write', title: 'Разбираю найденное', text: 'Решаю, хватает ли данных' },
  { type: 'token', token: 'Средняя цена — **12 500 ₽** [1].' },
  { type: 'result', reply: 'Средняя цена — 12 500 ₽ [1].', steps: 0, searched: true, model: 'mock-model',
    sources: LINUX_SOURCES },
  { type: 'done' },
];

// ИИ сам решил нарисовать картинку — модель выбрал сервер
const IMAGE_STREAM = [
  { type: 'stage', scene: 'media', title: 'Рисую изображение', text: 'a red fox in a snowy forest, watercolor' },
  { type: 'step', icon: '🎨', text: 'Пробую Pollinations Flux (Pollinations)' },
  { type: 'media', media: { kind: 'image', url: '/media/fox.png', filename: 'fox.png', mime: 'image/png',
    model: 'flux', model_name: 'Pollinations Flux', provider_name: 'Pollinations', elapsed_ms: 2300,
    prompt: 'a red fox in a snowy forest, watercolor' } },
  { type: 'token', token: 'Готово! Нарисовал лисичку в зимнем лесу.' },
  { type: 'result', reply: 'Готово! Нарисовал лисичку в зимнем лесу.', steps: 1, linux: false, model: 'mock-model',
    media: [{ kind: 'image', url: '/media/fox.png' }] },
  { type: 'done' },
];
const AUDIO_STREAM = [
  { type: 'stage', scene: 'media', title: 'Озвучиваю', text: 'Добро пожаловать!' },
  { type: 'media', media: { kind: 'audio', url: '/media/hello.mp3', filename: 'hello.mp3',
    model_name: 'Gemini 3.8 Flash TTS', provider_name: 'Google AI Studio', prompt: 'Добро пожаловать!' } },
  { type: 'token', token: 'Озвучил приветствие.' },
  { type: 'result', reply: 'Озвучил приветствие.', steps: 1, linux: false, model: 'mock-model' },
  { type: 'done' },
];
const VIDEO_STREAM = [
  { type: 'stage', scene: 'media', title: 'Делаю видео', text: 'a cat surfing' },
  { type: 'media', media: { kind: 'video', job_id: 'job-1', state: 'queued', model_name: 'Veo 3.1',
    provider_name: 'Google AI Studio', prompt: 'a cat surfing' } },
  { type: 'token', token: 'Видео готовится — появится здесь само.' },
  { type: 'result', reply: 'Видео готовится — появится здесь само.', steps: 1, linux: false, model: 'mock-model' },
  { type: 'done' },
];

// Управление моками: доступно ли окружение и что отвечает агент
const mock = {
  linux: false,            // сначала окружения нет — проверяем старый путь
  agentStream: AGENT_STREAM,
  agentStatus: 200,
  agentRequests: [],
  confirmRequests: [],
  sendStreamCalls: 0,
  sessionLogins: [],
  speech: { auto_speak: false, provider: '', voice: 'Puck', lang: '', read_code: false, model: '' },
  speechRequests: [],
  speechCancels: [],
  speechAborted: 0,
  audioBlocked: false,
};

// Синтетический поток озвучки: ровно те события, что отдаёт /api/tts/speech
const ttsStream = (requests) => [
  { type: 'start', job_id: 'tts-job-1', provider: 'google_ai_studio', provider_name: 'Google AI Studio',
    model: 'gemini-3.8-flash-tts', voice: 'Puck', lang: 'ru', chunks: 2, mode: 'ai' },
  { type: 'chunk', index: 0, url: '/media/tts-1.mp3', filename: 'tts-1.mp3', mime: 'audio/mpeg',
    text: String(requests).slice(0, 40) },
  { type: 'chunk', index: 1, url: '/media/tts-2.mp3', filename: 'tts-2.mp3', mime: 'audio/mpeg', text: '…' },
  { type: 'done', chunks: 2, total: 2, job_id: 'tts-job-1' },
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
const ttsJs = await (await fetch(BASE + '/static/tts.js')).text();
// Функция-заменитель, а не строка: в app.js есть '$' + ... — иначе String.replace
// принимает «$'» за вставку остатка страницы и ломает инлайн-скрипт.
// Скрипты подключаются с версией (?v=…), поэтому встраиваем их по регулярке.
const html = (() => {
  let page = pageHtml;
  const code = { 'app': appJs, 'linux': linuxJs, 'tts': ttsJs };
  for (const [name, source] of Object.entries(code)) {
    page = page.replace(
      new RegExp('<script src="/static/' + name + '\\.js[^"]*"></script>'),
      () => '<script>' + source + '</script>');
  }
  return page;
})();
if (!html.includes('window.Linux')) throw new Error('не удалось встроить /static/linux.js в страницу');
if (!html.includes('window.NovaSpeech')) throw new Error('не удалось встроить /static/tts.js в страницу');

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
    // Настоящее аудио в jsdom не играет: подменяем только конструктор Audio,
    // чтобы вручную управлять событием «дослушали до конца».
    window.__audios = [];
    window.Audio = class FakeAudio {
      constructor(src) {
        this.src = src;
        this.paused = true;
        this.ended = false;
        this._listeners = {};
        window.__audios.push(this);
      }
      addEventListener(name, cb) { (this._listeners[name] = this._listeners[name] || []).push(cb); }
      removeEventListener(name, cb) {
        this._listeners[name] = (this._listeners[name] || []).filter((item) => item !== cb);
      }
      play() { this.paused = false; return Promise.resolve(); }
      pause() { this.paused = true; }
      removeAttribute() { this.src = ''; }
      load() {}
      dispatch(name) { (this._listeners[name] || []).forEach((cb) => cb({ type: name })); }
    };
    window.speechSynthesis = { cancel() {}, speak() {} };
    window.SpeechSynthesisUtterance = class { constructor(text) { this.text = text; } };
    window.addEventListener('error', (event) => consoleErrors.push('window.onerror: ' + event.message));
    window.fetch = async (url, options = {}) => {
      const path = String(url).replace(BASE, '');
      const json = (data) => ({ ok: true, status: 200, async json() { return data; }, async text() { return JSON.stringify(data); } });
      if (path.startsWith('/api/auto_search_stream')) return streamResponse(SEARCH_STREAM);
      if (path.startsWith('/send_stream')) { mock.sendStreamCalls += 1; return streamResponse(CHAT_STREAM); }
      if (path.startsWith('/api/session/check')) return json({ signed_in: false, nick: '', admin: false });
      if (path.startsWith('/api/session/login')) {
        mock.sessionLogins.push(JSON.parse(options.body || '{}').nick);
        return json({ success: true });
      }
      if (path.startsWith('/api/ai/status')) {
        return { ok: true, status: 200, async json() { return { provider: 'openai_compatible', provider_name: 'Local', model: 'mock-model', offline: false, configured: true }; } };
      }
      if (path.startsWith('/api/search/health')) {
        return { ok: true, status: 200, async json() { return { ok: true, alive: ['searxng'], backends: [{ backend: 'searxng', ok: true, count: 2, ms: 12, error: null }] }; } };
      }
      if (path.startsWith('/api/media/jobs/')) {
        mock.jobPolls = (mock.jobPolls || 0) + 1;
        return json(mock.jobPolls < 2
          ? { job_id: 'job-1', state: 'running', polls: mock.jobPolls, events: [] }
          : { job_id: 'job-1', state: 'succeeded', polls: mock.jobPolls, events: [],
              media: { kind: 'video', url: '/media/cat.mp4', filename: 'cat.mp4', mime: 'video/mp4' } });
      }
      if (path.startsWith('/api/media/')) {
        return { ok: true, status: 200, async json() { return { selection: null }; } };
      }
      if (path.startsWith('/api/terminal/status')) {
        return json({ enabled: true, user: true, admin: false, available: true,
                      workspace: 'workspace', git: true, commands: ['ls', 'python3', 'git', 'nova'] });
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
        const body = JSON.parse(options.body || '{}');
        if (body.action === 'write') return json({ path: body.path, bytes: (body.content || '').length });
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
      if (path.startsWith('/api/agent/confirm')) {
        mock.confirmRequests.push(JSON.parse(options.body || '{}'));
        return json({ ok: true });
      }
      if (path.startsWith('/api/agent/status')) {
        if (mock.linux === 'media') {
          return json({ enabled: true, tools: false, available: true, max_steps: 8,
            hint: 'ИИ ищет в интернете и генерирует медиа. Для Linux-окружения добавьте TERMINAL_ENABLED=1' });
        }
        return json(mock.linux
          ? { enabled: true, tools: true, available: true, max_steps: 8, hint: '' }
          : { enabled: false, tools: false, available: false, max_steps: 8,
              hint: 'Linux-окружение выключено: добавьте TERMINAL_ENABLED=1 в .env и перезапустите сервер' });
      }
      if (path.startsWith('/api/agent/stream')) {
        mock.agentRequests.push(JSON.parse(options.body || '{}'));
        if (mock.agentStatus !== 200) {
          return { ok: false, status: mock.agentStatus, async json() { return { error: 'выключено' }; },
                   async text() { return '{"error":"выключено"}'; } };
        }
        return streamResponse(mock.agentStream);
      }
      if (path.startsWith('/api/tts/status')) {
        return json({
          available: true, mode: 'ai', provider: 'google_ai_studio', provider_name: 'Google AI Studio',
          model: 'gemini-3.8-flash-tts', model_name: 'Gemini 3.8 Flash TTS', voice: 'Puck',
          voices: [{ id: 'Puck', label: 'Puck', provider: 'google_ai_studio', languages: ['ru', 'en'], available: true }],
          settings: { ...mock.speech }, limits: { max_chars: 8000, chunks: 12 }, error: '',
        });
      }
      if (path.startsWith('/api/tts/settings')) {
        const method = (options.method || 'GET').toUpperCase();
        if (method === 'POST') {
          Object.assign(mock.speech, JSON.parse(options.body || '{}'));
        }
        return json({ settings: { ...mock.speech }, status: { available: true } });
      }
      if (path.startsWith('/api/tts/speech')) {
        mock.speechRequests.push(JSON.parse(options.body || '{}'));
        if (mock.audioBlocked) {
          return new Promise((resolve, reject) => {
            options.signal?.addEventListener('abort', () => {
              mock.speechAborted += 1;
              const error = new Error('aborted'); error.name = 'AbortError';
              reject(error);
            }, { once: true });
          });
        }
        return streamResponse(ttsStream(mock.speechRequests.length));
      }
      if (path.startsWith('/api/tts/cancel')) {
        mock.speechCancels.push(JSON.parse(options.body || '{}'));
        return json({ cancelled: true });
      }
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
check('один раз подсказали, почему ИИ пока без Linux',
  [...document.querySelectorAll('#toastStack .lg-toast')].some((t) => t.textContent.includes('TERMINAL_ENABLED')));

console.log('\n2) Без окружения: поиск старым путём (сцена → шаги → источники → ответ)');
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
check('рассуждение показано внутри шагов, а не отдельной панелью',
  lastBubble.querySelector('.agent-term-reason-text')?.textContent.includes('Проверю цены') === true
  && !lastBubble.querySelector('.reasoning'));
check('рассуждение свёрнуто после ответа',
  lastBubble.querySelector('.agent-term-reason')?.classList.contains('is-done') === true
  && !lastBubble.querySelector('.agent-term-reason')?.open);

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

console.log('\n3) Без окружения: обычный чат, пузырь пустой, потом поток');
window.toggleWebSearch();
document.getElementById('chat-input').value = 'сколько будет 6*7';
await window.sendMessage();
await new Promise((resolve) => setTimeout(resolve, 120));
const bubbles2 = document.querySelectorAll('.message.ai .msg-bubble');
const chatBubble = bubbles2[bubbles2.length - 1];
check('ответ получен потоком', chatBubble.querySelector('.answer').textContent.includes('42'));
check('источников нет', chatBubble.querySelectorAll('.source-item').length === 0);
check('без окружения сообщения не уходят в Linux', mock.agentRequests.length === 0);

console.log('\n4) Бонусы интерфейса');
window.openCmdk();
check('командная панель открыта', document.getElementById('cmdk').classList.contains('open'));
check('действия в панели есть', document.querySelectorAll('.cmdk-item').length > 5);
window.closeCmdk();
check('командная панель закрыта', !document.getElementById('cmdk').classList.contains('open'));

check('кнопки темы нет, тема всегда Aurora',
  !document.getElementById('btnTheme') && typeof window.cycleTheme === 'undefined'
  && document.documentElement.getAttribute('data-glass-theme') === 'aurora');

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
// Раскладка проверяется для десктопа: правила внутри @media (мобильный
// drawer со своим скроллом) сюда не попадают.
const desktopCss = chatCss.split('@media')[0];
const desktopBlock = (selector) =>
  (desktopCss.match(new RegExp('\\' + selector + '\\s*\\{[^}]*\\}')) || [''])[0];

const appShell = document.querySelector('.app');
check('сайдбар скрыт по умолчанию', appShell.classList.contains('nav-off'));
check('бургер отмечен как закрытый', document.getElementById('btnNav').getAttribute('aria-expanded') === 'false');
window.toggleSidebar();
check('бургер открывает панель', !appShell.classList.contains('nav-off'));
check('состояние панели запомнено', window.localStorage.getItem('nova_nav_open') === '1');
window.toggleSidebar();
check('бургер снова прячет панель', appShell.classList.contains('nav-off'));

check('прокручивается всё окно, а не переписка',
  !/overflow-y:\s*auto/.test(desktopBlock('.chat-container')), desktopBlock('.chat-container'));
check('у сайдбара нет своего ползунка',
  !/overflow-y:\s*auto/.test(desktopBlock('.sidebar-body')), desktopBlock('.sidebar-body'));
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
check('кнопок и панели выбора медиа-модели нет',
  !document.getElementById('btnMediaModel') && !document.getElementById('btn-media')
  && !document.getElementById('mediaActiveBar') && !document.getElementById('mediaPicker'));

console.log('\n7) Ошибок за весь прогон не появилось');
check('консоль чистая', consoleErrors.length === 0, consoleErrors.join(' | '));

// ══════════════════════════════════════════════════════════════
// 8) Страница настроек: провайдеры, каталог, диагностика поиска
// ══════════════════════════════════════════════════════════════
console.log('\n8) Страница настроек');
const settingsHtml = await (await fetch(BASE + '/settings')).text();
const settingsJs = await (await fetch(BASE + '/static/settings.js')).text();
// Остальные скрипты страницы настроек тоже нужны: аккордеон, панели MCP,
// скиллов и озвучки живут в отдельных файлах.
const settingsParts = {};
for (const file of ['i18n', 'accordion', 'tts', 'mcp-settings', 'skills-settings', 'speech-settings']) {
  settingsParts[file] = await (await fetch(BASE + '/static/' + file + '.js')).text();
}

// Счётчики обращений страницы настроек к мокам
const settingsMock = {
  skillRequests: [], skillListCalls: 0, speechSettings: [], speechSampleRequests: [],
  langRequests: [], mcpToolsCalls: 0, mcpTestCalls: 0,
};

const settingsErrors = [];
const settingsConsole = new VirtualConsole();
settingsConsole.on('jsdomError', (error) => settingsErrors.push('jsdomError: ' + error.message));
settingsConsole.on('error', (...args) => settingsErrors.push('console.error: ' + args.join(' ')));

// Скрипты подключаются с версией (?v=…), поэтому встраиваем их по регулярке.
let settingsPage = settingsHtml.replace(
  /<script src="\/static\/settings\.js[^"]*"><\/script>/,
  () => '<script>' + settingsJs + '</script>');
for (const [file, source] of Object.entries(settingsParts)) {
  settingsPage = settingsPage.replace(
    new RegExp('<script src="/static/' + file.replace(/[-]/g, '\\-') + '\\.js[^"]*"></script>'),
    () => '<script>' + source + '</script>');
}
if (!settingsPage.includes("const state")) {
  throw new Error('не удалось встроить /static/settings.js в страницу настроек');
}
for (const file of ['accordion', 'tts', 'mcp-settings', 'skills-settings', 'speech-settings']) {
  if (settingsPage.includes('/static/' + file + '.js')) {
    throw new Error('не удалось встроить /static/' + file + '.js в страницу настроек');
  }
}
const settingsDom = new JSDOM(settingsPage,
  {
    url: BASE + '/settings',
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    virtualConsole: settingsConsole,
    beforeParse(win) {
      win.TextDecoder = TextDecoder;
      win.requestAnimationFrame = (cb) => setTimeout(cb, 0);
      // Язык интерфейса уже выбран: i18n не полезет за ним на сервер и не
      // перезагрузит страницу. Дальше проверим переключение вручную.
      win.localStorage.setItem('nova_ui_lang', 'ru');
      win.__confirmations = [];
      win.confirm = (text) => { win.__confirmations.push(text); return true; };
      win.__audios = [];
      win.Audio = class FakeAudio {
        constructor(src) {
          this.src = src; this.paused = true; this._listeners = {};
          win.__audios.push(this);
        }
        addEventListener(name, cb) { (this._listeners[name] = this._listeners[name] || []).push(cb); }
        play() { this.paused = false; return Promise.resolve(); }
        pause() { this.paused = true; }
      };
      win.addEventListener('error', (event) => settingsErrors.push('window.onerror: ' + event.message));
      win.fetch = async (url, options = {}) => {
        const path = String(url).replace(BASE, '');
        const json = (data) => ({ ok: true, status: 200, async json() { return data; } });
        if (path.startsWith('/api/settings/mcp')) {
          if (path.endsWith('/tools')) {
            settingsMock.mcpToolsCalls += 1;
            return { ok: true, status: 200, async json() {
              // Ответ как у mcp_registry.request(id, 'tools/list')
              return { jsonrpc: '2.0', id: 1, result: { tools: [
                { name: 'search', description: 'Ищет документы' },
                { name: 'fetch', description: 'Читает страницу' },
              ] } };
            } };
          }
          if (path.startsWith('/api/settings/mcp/') && !path.endsWith('/test')) {
            return { ok: true, status: 200, async json() { return { deleted: 'docs' }; } };
          }
          if (path.endsWith('/test')) {
            settingsMock.mcpTestCalls += 1;
            return { ok: true, status: 200, async json() { return { ok: true, tools: 2 }; } };
          }
          if (path.startsWith('/api/settings/mcp/connect')) {
            return { ok: true, status: 200, async json() { return { ok: true, id: 'docs' }; } };
          }
          return { ok: true, status: 200, async json() {
            return {
              connections: [
                { id: 'docs', name: 'Docs MCP', url: 'https://mcp.example.com/mcp',
                  transport: 'http', auth: 'api_key', connected: true, last_error: '',
                  last_check: 1761000000, tools_count: 2 },
                { id: 'legacy', name: 'Legacy MCP', url: 'https://old.example.com/sse',
                  transport: 'sse', auth: 'none', connected: false,
                  last_error: 'HTTP 502: сервер не ответил на initialize',
                  last_check: 1761000000, tools_count: 0 },
              ],
              catalog: [{ id: 'github', name: 'GitHub', description: 'GitHub MCP',
                transport: 'http', auth: 'bearer', url: '' }],
            };
          } };
        }
        if (path.startsWith('/api/settings/skills')) {
          if (path.endsWith('/enabled')) {
            const body = JSON.parse(options.body || '{}');
            settingsMock.skillRequests.push({ id: path.split('/')[4], enabled: body.enabled });
            return { ok: true, status: 200, async json() { return { ok: true, enabled: false }; } };
          }
          if (path.startsWith('/api/settings/skills/') && path !== '/api/settings/skills') {
            // Ответ как у настоящего GET /api/settings/skills/<id>: плоский объект.
            return { ok: true, status: 200, async json() {
              return { id: 'research', name: 'Research helper', description: 'Ищет и цитирует',
                       content: '# Research\nИщи источники и цитируй их.', enabled: true,
                       tools: ['search', 'open'] };
            } };
          }
          settingsMock.skillListCalls += 1;
          return { ok: true, status: 200, async json() {
            return { skills: [
              { id: 'research', name: 'Research helper', description: 'Ищет и цитирует',
                enabled: true, tools: ['search', 'open'], invalid: false },
              { id: 'writer', name: 'Writer', description: 'Пишет тексты',
                enabled: false, tools: ['write'], invalid: false },
            ] };
          } };
        }
        if (path.startsWith('/api/tts/status')) {
          return json({
            available: true, mode: 'ai', provider: 'google_ai_studio', provider_name: 'Google AI Studio',
            model: 'gemini-3.8-flash-tts', model_name: 'Gemini 3.8 Flash TTS', voice: 'Puck',
            voices: [{ id: 'Puck', label: 'Puck', provider: 'google_ai_studio',
              languages: ['ru', 'en'], available: true }],
            providers: [{ id: 'google_ai_studio', name: 'Google AI Studio', available: true, reason: '' }],
            settings: { auto_speak: false, provider: '', voice: 'Puck', lang: 'ru', read_code: false },
            limits: { max_chars: 8000, chunks: 12 }, error: '',
          });
        }
        if (path.startsWith('/api/tts/settings')) {
          if (options.body) settingsMock.speechSettings.push(JSON.parse(options.body));
          return json({ settings: { auto_speak: true, provider: '', voice: 'Puck', lang: 'ru', read_code: false,
            ...(settingsMock.speechSettings[settingsMock.speechSettings.length - 1] || {}) } });
        }
        if (path.startsWith('/api/tts/speech')) {
          settingsMock.speechSampleRequests.push(JSON.parse(options.body || '{}'));
          return { ok: true, status: 200, async json() { return {}; },
                   async text() {
                     return [
                       JSON.stringify({ type: 'start', job_id: 'job-1', provider: 'google_ai_studio',
                         provider_name: 'Google AI Studio', chunks: 1, mode: 'ai' }),
                       JSON.stringify({ type: 'chunk', index: 0, url: '/media/tts-sample.mp3',
                         mime: 'audio/mpeg', text: 'пример' }),
                       JSON.stringify({ type: 'done', chunks: 1, total: 1 }),
                     ].join('\n') + '\n';
                   } };
        }
        if (path.startsWith('/api/settings/lang')) {
          const body = JSON.parse(options.body || '{}');
          if (body.language) settingsMock.langRequests.push(body.language);
          return json({ language: 'ru' });
        }
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
        if (path.startsWith('/api/settings/models') && path.includes('provider=groq')) {
          return { ok: false, status: 401, async json() {
            return { error: 'Groq: HTTP 401 — ключ не принят' };
          } };
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

const settingsWait = (ms = 120) => new Promise((resolve) => setTimeout(resolve, ms));
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
check('настройки: кнопки темы нет, тема Aurora',
  !sdoc.getElementById('themeBtn')
  && sdoc.documentElement.getAttribute('data-glass-theme') === 'aurora');

// ── 8б) Раскрывающиеся разделы ──────────────────────────────────────────
console.log('\n8б) Раскрывающиеся разделы (единый компонент)');
const accSections = [...sdoc.querySelectorAll('section.acc')];
check('все разделы — аккордеоны, язык последним',
  accSections.length === 7 && accSections[accSections.length - 1].dataset.accordion === 'language'
  && accSections[0].dataset.accordion === 'providers',
  accSections.map((a) => a.dataset.accordion).join(','));
check('по умолчанию разделы свёрнуты',
  accSections.every((a) => a.querySelector('[data-accordion-toggle]').getAttribute('aria-expanded') === 'false'
    && a.querySelector('[data-accordion-panel]').hidden));
const providersAcc = sdoc.querySelector('section.acc[data-accordion="providers"]');
const providersHead = providersAcc.querySelector('[data-accordion-toggle]');
const providersPanel = providersAcc.querySelector('[data-accordion-panel]');
check('aria-controls ведёт на свою панель',
  Boolean(providersPanel.id) && providersHead.getAttribute('aria-controls') === providersPanel.id,
  providersHead.getAttribute('aria-controls'));
providersHead.dispatchEvent(new settingsDom.window.MouseEvent('click', { bubbles: true }));
check('раздел раскрывается кликом по шапке',
  providersHead.getAttribute('aria-expanded') === 'true' && providersPanel.hidden === false);
check('состояние раздела запоминается',
  JSON.parse(settingsDom.window.localStorage.getItem('nova_accordion_state')).providers === true,
  settingsDom.window.localStorage.getItem('nova_accordion_state'));
const providerCardsBefore = sdoc.querySelectorAll('#providerGrid .provider-card').length;
const providerBodyNode = providersPanel.firstElementChild;
for (let index = 0; index < 4; index += 1) {
  providersHead.dispatchEvent(new settingsDom.window.MouseEvent('click', { bubbles: true }));
  providersHead.dispatchEvent(new settingsDom.window.MouseEvent('click', { bubbles: true }));
}
check('многократное открытие не дублирует карточки и не пересоздаёт DOM',
  sdoc.querySelectorAll('#providerGrid .provider-card').length === providerCardsBefore
  && providersPanel.firstElementChild === providerBodyNode);
providersHead.dispatchEvent(new settingsDom.window.MouseEvent('click', { bubbles: true }));
check('повторное закрытие снова прячет панель',
  providersHead.getAttribute('aria-expanded') === 'false' && providersPanel.hidden === true);
const settingsCss = await (await fetch(BASE + '/static/settings.css')).text();
check('стрелка поворачивается через класс is-open',
  /\.acc\.is-open \.caret\s*\{[^}]*rotate/.test(settingsCss), settingsCss.slice(0, 0));
check('подпись раздела меняется на «Открыто»/«Свёрнуто»',
  /is-open/.test(settingsCss) && /transition/.test(settingsCss));
check('анимация отключается при reduced-motion',
  /prefers-reduced-motion/.test(await (await fetch(BASE + '/static/settings.css')).text()));

// Отдельная мини-страница: состояние из localStorage применяется при загрузке
const accMini = new JSDOM('<section class="acc" data-accordion="providers">'
  + '<button type="button" data-accordion-toggle></button><div data-accordion-panel></div></section>'
  + '<script>' + settingsParts.accordion + '</script>', {
  url: BASE + '/settings',
  runScripts: 'dangerously',
  beforeParse(win) {
    win.localStorage.setItem('nova_accordion_state', JSON.stringify({ providers: true }));
  },
});
await settingsWait(60);
check('открытый раздел восстанавливается после перезагрузки',
  accMini.window.document.querySelector('[data-accordion-toggle]').getAttribute('aria-expanded') === 'true'
  && accMini.window.document.querySelector('[data-accordion-panel]').hidden === false);
accMini.window.close();

// ── 8в) Провайдеры: настоящая проверка подключения ──────────────────────
console.log('\n8в) Провайдеры: проверка связи с провайдером');
const groqCheck = sdoc.querySelector('[data-check-provider="groq"]');
const localCheck = sdoc.querySelector('[data-check-provider="local_demo"]');
check('у провайдера есть кнопка проверки', Boolean(groqCheck) && Boolean(localCheck));
groqCheck.dispatchEvent(new settingsDom.window.MouseEvent('click', { bubbles: true }));
await settingsWait(120);
check('ошибка провайдера показывается как есть',
  sdoc.querySelector('.provider-card[data-provider="groq"] [data-provider-state]').textContent
    .includes('HTTP 401'),
  sdoc.querySelector('.provider-card[data-provider="groq"] [data-provider-state]').textContent);
check('карточка с ошибкой помечена', sdoc.querySelector('.provider-card[data-provider="groq"]').classList.contains('is-error'));
localCheck.dispatchEvent(new settingsDom.window.MouseEvent('click', { bubbles: true }));
await settingsWait(120);
check('успешная проверка показывает число моделей',
  sdoc.querySelector('.provider-card[data-provider="local_demo"] [data-provider-state]').textContent
    .includes('моделей: 1'),
  sdoc.querySelector('.provider-card[data-provider="local_demo"] [data-provider-state]').textContent);
check('кнопка проверки вернулась в исходное состояние', !localCheck.disabled);

// ── 8г) MCP-серверы ─────────────────────────────────────────────────────
console.log('\n8г) MCP-серверы: транспорт, статус, инструменты');
check('MCP: подключения и шаблоны отрисованы',
  sdoc.querySelectorAll('#mcpGrid .mcp-card').length === 3,
  String(sdoc.querySelectorAll('#mcpGrid .mcp-card').length));
const docsCard = sdoc.querySelector('[data-mcp-card="docs"]');
const legacyCard = sdoc.querySelector('[data-mcp-card="legacy"]');
check('MCP: транспорт показан как HTTP (Streamable)', docsCard.textContent.includes('HTTP (Streamable)'));
check('MCP: другой транспорт не выдаётся за HTTP', legacyCard.textContent.includes('SSE'));
check('MCP: подключённый сервер помечен', docsCard.classList.contains('is-connected')
  && docsCard.textContent.includes('подключён'));
check('MCP: отключённый сервер и причина видны',
  legacyCard.classList.contains('is-offline')
  && legacyCard.querySelector('.mcp-error').textContent.includes('HTTP 502'));
docsCard.querySelector('[data-mcp-tools="docs"]').dispatchEvent(new settingsDom.window.MouseEvent('click', { bubbles: true }));
await settingsWait(120);
check('MCP: список инструментов загружен по кнопке',
  sdoc.querySelectorAll('[data-mcp-card="docs"] .mcp-tools li').length === 2,
  String(sdoc.querySelectorAll('[data-mcp-card="docs"] .mcp-tools li').length));
docsCard.querySelector('[data-mcp-test="docs"]').dispatchEvent(new settingsDom.window.MouseEvent('click', { bubbles: true }));
await settingsWait(120);
check('MCP: проверка connection не теряет карточку',
  Boolean(sdoc.querySelector('[data-mcp-card="docs"]')));
check('MCP: секрет вводится только в скрытое поле',
  sdoc.getElementById('mcpSecret').type === 'password');

// ── 8д) Скиллы ──────────────────────────────────────────────────────────
console.log('\n8д) Скиллы: отдельная категория, включение и выключение');
const skillCards = [...sdoc.querySelectorAll('#skillsGrid .skill-card')];
check('скиллы отрисованы отдельными карточками', skillCards.length === 2);
check('скилл показывает доступность и инструменты',
  skillCards[0].textContent.includes('включён') && skillCards[0].textContent.includes('search, open'));
check('выключенный скилл помечен', skillCards[1].classList.contains('is-disabled')
  && skillCards[1].textContent.includes('выключен'));
check('переключатель скилла отражает состояние',
  skillCards[1].querySelector('[data-skill-toggle]').getAttribute('aria-checked') === 'false');
const researchToggle = sdoc.querySelector('[data-skill-toggle="research"]');
researchToggle.checked = false;
researchToggle.dispatchEvent(new settingsDom.window.Event('change', { bubbles: true }));
await settingsWait(120);
check('выключение скилла уходит на сервер', settingsMock.skillRequests.length >= 1
  && settingsMock.skillRequests[0].id === 'research' && settingsMock.skillRequests[0].enabled === false,
  JSON.stringify(settingsMock.skillRequests));
check('после выключения список перечитан с сервера', settingsMock.skillListCalls >= 2);
sdoc.querySelector('[data-skill-details="research"]').dispatchEvent(new settingsDom.window.MouseEvent('click', { bubbles: true }));
await settingsWait(120);
check('подробности скилла загружаются по кнопке',
  sdoc.querySelector('[data-skill-body="research"]').textContent.includes('Ищи источники')
  && sdoc.querySelector('[data-skill-body="research"]').hidden === false);
sdoc.querySelector('[data-skill-delete="writer"]').dispatchEvent(new settingsDom.window.MouseEvent('click', { bubbles: true }));
await settingsWait(120);
check('удаление спрашивает подтверждение', settingsDom.window.__confirmations.length === 1);

// ── 8е) Озвучка в настройках ────────────────────────────────────────────
console.log('\n8е) Настройки озвучки: статус, автоозвучка, пример голоса');
check('статус TTS взят с сервера',
  sdoc.getElementById('speechHeadline').textContent.includes('Google AI Studio')
  && sdoc.getElementById('speechDetails').textContent.includes('8000'),
  sdoc.getElementById('speechHeadline').textContent);
check('провайдеры и голоса заполнены из ответа сервера',
  [...sdoc.querySelectorAll('#speechProvider option')].some((o) => o.value === 'google_ai_studio')
  && [...sdoc.querySelectorAll('#speechVoice option')].some((o) => o.value === 'Puck'));
const autoToggle = sdoc.getElementById('autoSpeakToggle');
check('автоозвучка по умолчанию выключена и подписана',
  autoToggle.checked === false && autoToggle.getAttribute('aria-checked') === 'false'
  && sdoc.querySelector('[data-switch-label]').textContent === 'Выключено');
autoToggle.checked = true;
autoToggle.dispatchEvent(new settingsDom.window.Event('change', { bubbles: true }));
await settingsWait(120);
check('включение автоозвучки сохраняется на сервере',
  settingsMock.speechSettings.length === 1 && settingsMock.speechSettings[0].auto_speak === true,
  JSON.stringify(settingsMock.speechSettings));
check('подпись переключателя меняется на «Включено»',
  sdoc.querySelector('[data-switch-label]').textContent === 'Включено'
  && autoToggle.getAttribute('aria-checked') === 'true');
sdoc.getElementById('testSpeech').dispatchEvent(new settingsDom.window.MouseEvent('click', { bubbles: true }));
await settingsWait(120);
check('«Прослушать пример» запрашивает реальный синтез',
  settingsMock.speechSampleRequests.length === 1 && settingsMock.speechSampleRequests[0].text.length > 10);
check('пример действительно отдан в плеер',
  settingsDom.window.__audios.some((audio) => audio.src === '/media/tts-sample.mp3'));
sdoc.getElementById('stopSpeech').dispatchEvent(new settingsDom.window.MouseEvent('click', { bubbles: true }));
check('кнопка «Остановить» сбрасывает озвучку',
  settingsDom.window.NovaSpeech._state().current === null);

// ── 8ж) Язык интерфейса ─────────────────────────────────────────────────
console.log('\n8ж) Переключение языка');
check('три языка в выборе', sdoc.querySelectorAll('#langGrid [data-lang]').length === 3);
check('текущий язык подсвечен', sdoc.querySelector('[data-lang="ru"]').classList.contains('active'));
sdoc.querySelector('[data-lang="en"]').dispatchEvent(new settingsDom.window.MouseEvent('click', { bubbles: true }));
await settingsWait(150);
check('язык сохранён локально', settingsDom.window.localStorage.getItem('nova_ui_lang') === 'en',
  settingsDom.window.localStorage.getItem('nova_ui_lang'));
check('заголовок переведён на английский',
  sdoc.querySelector('h1').textContent === 'AI settings', sdoc.querySelector('h1').textContent);
check('названия разделов переведены',
  sdoc.querySelector('[data-accordion="providers"] .acc-title').textContent === 'Providers'
  && sdoc.querySelector('[data-accordion="mcp"] .acc-title').textContent === 'MCP servers'
  && sdoc.querySelector('[data-accordion="speech"] .acc-title').textContent === 'Speech');
check('переключатель автоозвучки переведён',
  sdoc.querySelector('[data-switch-label]').textContent === 'On',
  sdoc.querySelector('[data-switch-label]').textContent);
check('язык ушёл на сервер', settingsMock.langRequests.includes('en'), JSON.stringify(settingsMock.langRequests));
check('переключение языка не ломает страницу',
  settingsErrors.filter((item) => !item.includes('Not implemented: navigation')).length === 0,
  settingsErrors.join(' | '));

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
check('вход: выбора темы нет', !adoc.querySelector('.auth-theme-row'));
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

// ══════════════════════════════════════════════════════════════
// 10) Лишнего нет: кнопки, панель Linux, шпаргалка, подсказки
// ══════════════════════════════════════════════════════════════
console.log('\n10) Убраны кнопки «Агент», «Linux», «Тема», панель и подсказки');
const press = (key, extra = {}) => document.dispatchEvent(new window.KeyboardEvent('keydown', { key, bubbles: true, ...extra }));
const aiCount = () => document.querySelectorAll('.message.ai').length;
const lastAi = () => { const all = document.querySelectorAll('.message.ai'); return all[all.length - 1]; };
const settle = (ms = 150) => new Promise((resolve) => setTimeout(resolve, ms));

check('нет кнопки агента', !document.getElementById('btnAgent'));
check('нет кнопки Linux-окружения', !document.getElementById('btnLinux'));
check('нет кнопки смены темы', !document.getElementById('btnTheme'));
check('нет панели Linux', !document.getElementById('linuxPanel') && !document.querySelector('.linux-panel'));
check('нет шпаргалки хоткеев', !document.getElementById('cheatsheet') && !document.querySelector('.cheatsheet'));
check('нет карточек-подсказок на приветствии', !document.querySelector('.suggestions, .welcome .suggestion-card'));
check('[hidden] всегда скрывает (на телефоне ничего не всплывает)',
  /\[hidden\][^{]*\{[^}]*display:\s*none\s*!important/.test(chatCss));
check('в CSS не осталось панели Linux и шпаргалки', !chatCss.includes('.linux-panel') && !chatCss.includes('.cheatsheet'));
press('j', { ctrlKey: true });
press('?');
await settle(60);
check('старые хоткеи ничего не ломают', consoleErrors.length === 0, consoleErrors.join(' | '));
check('кнопка автопоиска подписана и на телефоне',
  document.getElementById('searchBtnText').textContent.startsWith('Поиск'));
check('серверная сессия восстановлена по нику', mock.sessionLogins.includes('Smoke'), JSON.stringify(mock.sessionLogins));

// ══════════════════════════════════════════════════════════════
// 11) Главный чат сам работает в Linux
// ══════════════════════════════════════════════════════════════
console.log('\n11) Главный чат: ИИ сам работает в Linux по шагам');
mock.linux = true;
await window.Linux.loadStatus();
check('окружение доступно', window.Linux.isAvailable() === true);
check('индикатор в шапке объясняет, что ИИ работает с Linux',
  document.getElementById('statusDot').title.includes('Linux'));

mock.agentStream = SIMPLE_STREAM;
const sendCallsBefore = mock.sendStreamCalls;
document.getElementById('chat-input').value = 'привет';
await window.sendMessage();
await settle();
check('обычное сообщение ушло в главный чат с Linux, без переключателя',
  mock.agentRequests.length === 1 && mock.agentRequests[0].message === 'привет'
  && mock.agentRequests[0].search === false && mock.sendStreamCalls === sendCallsBefore,
  JSON.stringify(mock.agentRequests));
check('простой вопрос — сразу текст, без блока Linux',
  lastAi().querySelector('.answer').textContent.includes('Чем помочь')
  && !lastAi().querySelector('.agent-term'));

mock.agentStream = AGENT_STREAM;
document.getElementById('chat-input').value = 'запусти hello.py и расскажи, что вышло';
await window.sendMessage();
await settle();
const agentBubble = lastAi();
check('задача ушла в Linux', mock.agentRequests.length === 2);
check('ИИ показывает план', agentBubble.querySelector('.stage-plan')?.textContent.includes('посмотреть файлы') === true);
check('шаги видны в блоке «Работа в Linux»',
  agentBubble.querySelector('.agent-term-title')?.textContent === 'Работа в Linux'
  && agentBubble.querySelectorAll('.agent-term-row').length === 2
  && agentBubble.querySelector('.agent-term').textContent.includes('python3 hello.py'));
check('блок шагов стоит над ответом', (() => {
  const box = agentBubble.querySelector('.agent-term');
  const answer = agentBubble.querySelector('.answer');
  return !!box && !!(box.compareDocumentPosition(answer) & window.Node.DOCUMENT_POSITION_FOLLOWING);
})());
check('счётчик шагов по-русски', agentBubble.querySelector('.agent-term-meta')?.textContent.startsWith('2 шага'),
  agentBubble.querySelector('.agent-term-meta')?.textContent);
check('после ответа блок свёрнут в одну строку',
  agentBubble.querySelector('.agent-term').classList.contains('is-done')
  && !agentBubble.querySelector('.agent-term').classList.contains('is-open'));
check('свёрнутый блок раскрывается нажатием', (() => {
  const box = agentBubble.querySelector('.agent-term');
  const head = box.querySelector('.agent-term-head');
  head.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  const opened = box.classList.contains('is-open');
  head.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  return opened && !box.classList.contains('is-open');
})());
check('задача видна прямо в ответе',
  agentBubble.querySelector('.agent-task')?.textContent.includes('Проверить песочницу') === true
  && agentBubble.querySelector('.agent-task').textContent.includes('готово'));
check('ответ напечатан, сцена схлопнута',
  agentBubble.querySelector('.answer').textContent.includes('привет из папки')
  && agentBubble.querySelector('.stage').classList.contains('collapsed'));
check('под ответом видно «Linux» и число шагов',
  agentBubble.querySelector('.msg-meta').textContent.includes('Linux')
  && agentBubble.querySelector('.msg-meta').textContent.includes('шагов: 2'),
  agentBubble.querySelector('.msg-meta').textContent);

mock.agentStream = RETRACT_STREAM;
document.getElementById('chat-input').value = 'что в папке?';
await window.sendMessage();
await settle();
check('если модель пошла работать — начатая фраза убрана',
  lastAi().querySelector('.answer').textContent.trim() === 'В папке есть hello.py'
  && lastAi().querySelectorAll('.agent-term-row').length === 1,
  lastAi().querySelector('.answer').textContent);

console.log('\n11б) Разрешение раскрывается внутри шагов агента');
const confirmTurn = window.createAssistantTurn();
confirmTurn.beginIdle('Думаю…');
const confirmCard = confirmTurn.confirm({
  id: 'confirm-smoke-1', tool: 'run', risk: 'danger',
  command: 'git push origin main',
  args: { action: 'run', command: 'git push origin main' },
});
const confirmTerm = confirmTurn.element.querySelector('.agent-term');
check('карточка разрешения внутри блока шагов',
  !!confirmCard && confirmCard.closest('.agent-term-log') === confirmTerm.querySelector('.agent-term-log'));
check('блок шагов сам раскрылся на вопрос',
  confirmTerm.classList.contains('is-open')
  && confirmTerm.querySelector('.agent-term-head').getAttribute('aria-expanded') === 'true');
check('в шагах видно команду, требующую разрешения',
  confirmTerm.textContent.includes('Нужно разрешение')
  && confirmTerm.textContent.includes('git push origin main'));
check('есть три варианта: разрешить, всегда, отклонить',
  ['yes', 'always', 'no'].every((kind) =>
    Boolean(confirmCard.querySelector(`[data-confirm="${kind}"]`)))
  && confirmCard.querySelector('[data-confirm="always"]').textContent.toLowerCase().includes('всегда'));
check('шапка шагов говорит, что ждёт решения',
  confirmTerm.querySelector('.agent-term-meta').textContent.includes('ждёт разрешения'),
  confirmTerm.querySelector('.agent-term-meta').textContent);
confirmCard.querySelector('[data-confirm="always"]').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
await settle(30);
check('кнопка «Разрешить всегда» отправляет always',
  mock.confirmRequests.length === 1
  && mock.confirmRequests[0].approve === true && mock.confirmRequests[0].always === true,
  JSON.stringify(mock.confirmRequests));
check('после «Разрешить всегда» строка свернулась и помечена',
  confirmCard.classList.contains('is-approved') && confirmCard.classList.contains('is-always')
  && !confirmCard.classList.contains('is-open')
  && confirmCard.querySelector('.agent-confirm-note').textContent.includes('разрешено всегда'));
check('блок шагов остался раскрытым для выполнения',
  confirmTerm.classList.contains('is-open')
  && !confirmTerm.querySelector('.agent-term-meta').textContent.includes('ждёт'));
const deniedCard = confirmTurn.confirm({
  id: 'confirm-smoke-2', tool: 'mcp_call', risk: 'danger',
  command: 'github:delete_repo', args: { repo: 'demo' },
});
confirmTurn.confirmResult({ id: 'confirm-smoke-2', approved: false });
check('confirm_result закрывает вопрос и помечает отказ',
  deniedCard.classList.contains('is-denied')
  && deniedCard.querySelector('.agent-confirm-note').textContent.includes('отклонено'));
confirmTurn.remove();

// ══════════════════════════════════════════════════════════════
// 12) Кнопка «Поиск» через Linux, кнопки у кода, откат без окружения
// ══════════════════════════════════════════════════════════════
console.log('\n12) Автопоиск через Linux, кнопки у кода, откат, «стоп»');
window.setWebSearch(true, true);
mock.agentStream = LINUX_SEARCH_STREAM;
document.getElementById('chat-input').value = 'сколько стоит ноутбук?';
await window.sendMessage();
await settle();
const searchTurn = lastAi();
check('кнопка «Поиск» отправляет запрос в Linux-окружение',
  mock.agentRequests[mock.agentRequests.length - 1].search === true);
check('поиск виден шагами nova search / nova read',
  searchTurn.querySelector('.agent-term')?.textContent.includes('nova search цена ноутбука') === true
  && searchTurn.querySelector('.agent-term').textContent.includes('nova read'));
check('источники внутри пузыря', searchTurn.querySelectorAll('.source-item').length === 2,
  String(searchTurn.querySelectorAll('.source-item').length));
check('ответ напечатан', searchTurn.querySelector('.answer').textContent.includes('12 500'));
check('под ответом «с поиском»', searchTurn.querySelector('.msg-meta').textContent.includes('с поиском'),
  searchTurn.querySelector('.msg-meta').textContent);
window.setWebSearch(false, true);

// Кнопки у блока кода
mock.agentStream = SIMPLE_STREAM;
window.appendMessage('ai', 'Вот код:\n```python\nprint(6 * 7)\n```');
const codeBlock = lastAi().querySelector('.code-block');
const codeButtons = codeBlock ? [...codeBlock.querySelectorAll('[data-code]')].map((b) => b.dataset.code) : [];
check('у кода 4 кнопки: запустить, объяснить, тесты, копировать',
  JSON.stringify(codeButtons) === JSON.stringify(['run', 'explain', 'tests', 'copy']), JSON.stringify(codeButtons));
if (codeBlock) {
  const before = mock.agentRequests.length;
  codeBlock.querySelector('[data-code="run"]').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  await settle();
  const runRequest = mock.agentRequests[mock.agentRequests.length - 1];
  check('«Запустить» просит ИИ выполнить код в Linux',
    mock.agentRequests.length === before + 1
    && runRequest.message.startsWith('Запусти этот код в Linux') && runRequest.message.includes('print(6 * 7)'));
}

// Окружение закрылось посреди работы — ответит обычный чат, пустого пузыря нет
mock.agentStatus = 403;
const bubblesBefore = aiCount();
const sendBefore = mock.sendStreamCalls;
document.getElementById('chat-input').value = 'сколько будет 6*7';
await window.sendMessage();
await settle();
check('при 403 сообщение ушло в обычный чат', mock.sendStreamCalls === sendBefore + 1);
check('пустой пузырь не остался', aiCount() === bubblesBefore + 1
  && lastAi().querySelector('.answer').textContent.includes('42'));
mock.agentStatus = 200;
await window.Linux.loadStatus();

// «Стоп» останавливает и работу в Linux
const hangingAgent = (signal) => ({
  ok: true, status: 200,
  body: { getReader: () => ({ read: () => new Promise((_, reject) => {
    signal?.addEventListener('abort', () => { const e = new Error('aborted'); e.name = 'AbortError'; reject(e); }, { once: true });
  }) }) },
});
const fetchBeforeStop = window.fetch;
window.fetch = async (url, options = {}) =>
  String(url).includes('/api/agent/stream') ? hangingAgent(options?.signal) : fetchBeforeStop(url, options);
document.getElementById('chat-input').value = 'долгая задача';
const longTask = window.sendMessage();
await settle(60);
check('во время работы в Linux кнопка — «стоп»', document.getElementById('sendBtn').classList.contains('is-stop'));
await window.sendMessage();
await longTask.catch(() => {});
await settle(60);
check('«стоп» останавливает работу в Linux',
  !document.getElementById('sendBtn').classList.contains('is-stop')
  && lastAi().querySelector('.answer').textContent.includes('Остановлено'));
window.fetch = fetchBeforeStop;

check('за весь прогон ошибок в консоли нет', consoleErrors.length === 0, consoleErrors.join(' | '));


// ══════════════════════════════════════════════════════════════
// 13) ИИ сам генерирует картинки, аудио и видео
// ══════════════════════════════════════════════════════════════
console.log('\n13) ИИ сам решает, что сгенерировать, и сам выбирает модель');
check('в командной панели нет выбора медиа-модели и режима «изображение»',
  (() => { window.openCmdk(); const text = document.getElementById('cmdk').textContent; window.closeCmdk();
    return !text.includes('медиа-моделей') && !text.includes('Режим: изображение'); })());
check('приветствие говорит, что ИИ сам рисует, озвучивает и делает видео', (() => {
  const text = document.getElementById('welcomeScreen').textContent;
  return text.includes('нарисую') && text.includes('видео');
})());

mock.agentStream = IMAGE_STREAM;
document.getElementById('chat-input').value = 'нарисуй лису в зимнем лесу';
await window.sendMessage();
await settle();
const imageTurn = lastAi();
check('просьба нарисовать ушла агенту как обычное сообщение',
  mock.agentRequests[mock.agentRequests.length - 1].message === 'нарисуй лису в зимнем лесу');
check('картинка прямо в ответе ИИ', imageTurn.querySelector('.turn-media img')?.getAttribute('src') === '/media/fox.png');
check('картинка стоит над текстом ответа', (() => {
  const box = imageTurn.querySelector('.turn-media');
  return !!box && !!(box.compareDocumentPosition(imageTurn.querySelector('.answer')) & window.Node.DOCUMENT_POSITION_FOLLOWING);
})());
check('подпись: какой моделью ИИ нарисовал и сколько ждали',
  imageTurn.querySelector('.turn-media-meta').textContent.includes('Pollinations Flux')
  && imageTurn.querySelector('.turn-media-meta').textContent.includes('2.3 c'));
check('кнопка «Скачать»', imageTurn.querySelector('.turn-media-dl')?.getAttribute('download') === 'fox.png');
check('генерация — не Linux: блока шагов и метки Linux нет',
  !imageTurn.querySelector('.agent-term') && !imageTurn.querySelector('.msg-meta').textContent.includes('Linux'));
check('текст ответа напечатан', imageTurn.querySelector('.answer').textContent.includes('лисичку'));
const savedChat = JSON.parse(window.localStorage.getItem('nova_history_v2'));
const savedAi = savedChat.chats.find((c) => c.id === savedChat.activeId).messages.filter((m) => m.role === 'ai').pop();
check('картинка сохранена в истории вместе с ответом', savedAi.meta?.media?.[0]?.url === '/media/fox.png');

mock.agentStream = AUDIO_STREAM;
document.getElementById('chat-input').value = 'озвучь приветствие';
await window.sendMessage();
await settle();
check('озвучка — плеер прямо в ответе', lastAi().querySelector('.turn-media audio')?.getAttribute('src') === '/media/hello.mp3');

window.NOVA_MEDIA_POLL_MS = 30;
mock.agentStream = VIDEO_STREAM;
document.getElementById('chat-input').value = 'сделай видео с котом на сёрфе';
await window.sendMessage();
const videoTurn = lastAi();
check('видео: пока готовится — карточка ожидания', !!videoTurn.querySelector('.turn-media-item.is-pending'));
await settle(250);
check('видео готово — плеер подменил карточку',
  videoTurn.querySelector('.turn-media video')?.getAttribute('src') === '/media/cat.mp4'
  && !videoTurn.querySelector('.is-pending'));

// Перезагрузка чата из истории: картинка на месте
const activeId = JSON.parse(window.localStorage.getItem('nova_history_v2')).activeId;
window.loadChat(activeId);
await settle(60);
check('после перезагрузки чата картинка на месте',
  [...document.querySelectorAll('.turn-media img')].some((img) => img.getAttribute('src') === '/media/fox.png'));
check('после перезагрузки видео на месте',
  [...document.querySelectorAll('video')].some((video) => video.getAttribute('src') === '/media/cat.mp4'));

// Без Linux агент всё равно работает: ищет и генерирует
mock.linux = 'media';
await window.Linux.loadStatus();
check('без Linux ИИ-агент доступен', window.Linux.isAvailable() === true && window.Linux.hasLinux() === false);
check('индикатор: ИИ рисует и ищет, но без Linux', (() => {
  const title = document.getElementById('statusDot').title;
  return title.includes('рисует') && !title.includes('Linux');
})());
mock.agentStream = LINUX_SEARCH_STREAM;
window.setWebSearch(true, true);
document.getElementById('chat-input').value = 'сколько стоит ноутбук?';
await window.sendMessage();
await settle();
window.setWebSearch(false, true);
check('без Linux блок шагов называется честно — «Шаги ИИ»',
  lastAi().querySelector('.agent-term-title')?.textContent === 'Шаги ИИ');

check('после медиа-сценариев консоль чистая', consoleErrors.length === 0, consoleErrors.join(' | '));

// ══════════════════════════════════════════════════════════════
// 14) Озвучка ответов: кнопки, состояния, стоп, очередь, автоозвучка
// ══════════════════════════════════════════════════════════════
console.log('\n14) Озвучка ответов ИИ');
const speakButton = document.getElementById('btn-speak');
const composerKids = speakButton ? [...speakButton.parentElement.children] : [];
check('кнопка озвучки стоит в поле ввода рядом с микрофоном',
  Boolean(speakButton) && composerKids.indexOf(speakButton) >= 0
  && composerKids.indexOf(speakButton) < composerKids.indexOf(document.getElementById('voiceBtn')));
check('у кнопки озвучки есть подпись и подсказка',
  document.getElementById('speakBtnText').textContent.length > 0 && speakButton.title.length > 0);

window.newChat();
await settle(60);
check('в пустом чате кнопка озвучки недоступна', speakButton.disabled === true);

mock.linux = 'media';
await window.Linux.loadStatus();
mock.agentStream = SIMPLE_STREAM;
document.getElementById('chat-input').value = 'расскажи коротко';
await window.sendMessage();
await settle(120);
const speechTurn = lastAi();
const speechMsgButton = speechTurn.querySelector('.msg-actions [data-act="speak"]');
check('в ответе есть своя кнопка озвучки', Boolean(speechMsgButton));
check('после ответа кнопка под полем ввода доступна', speakButton.disabled === false);
check('кнопка в состоянии ожидания', speechMsgButton.dataset.speechState === 'idle');

// Ручная озвучка: играет именно этот ответ
const speechRequestsBefore = mock.speechRequests.length;
speechMsgButton.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
await settle(80);
const manualRequest = mock.speechRequests[mock.speechRequests.length - 1];
check('ручная озвучка идёт на сервер', mock.speechRequests.length === speechRequestsBefore + 1);
check('озвучивается текст этого ответа, а не последнего в чате',
  manualRequest.text.includes('Чем помочь') && manualRequest.auto !== true);
check('текст уходит как есть — подготовку делает сервер',
  manualRequest.text === 'Привет! Чем помочь?', manualRequest.text);
check('клиент не передаёт ключи и секреты',
  !('api_key' in manualRequest) && !('key' in manualRequest) && !('token' in manualRequest));
check('первая часть сразу отдана в плеер',
  window.__audios.some((audio) => audio.src === '/media/tts-1.mp3'));
check('кнопка ответа показывает воспроизведение',
  speechMsgButton.dataset.speechState === 'playing', speechMsgButton.dataset.speechState);
check('кнопка под полем ввода стала «Стоп»',
  speakButton.dataset.speechState === 'playing'
  && document.getElementById('speakBtnText').textContent === 'Стоп');
const firstAudio = window.__audios[window.__audios.length - 1];
firstAudio.ended = true;
firstAudio.dispatch('ended');
await settle(40);
check('вторая часть играет после первой',
  window.__audios.filter((audio) => audio.src === '/media/tts-2.mp3').length === 1);
const secondAudio = window.__audios[window.__audios.length - 1];
secondAudio.ended = true;
secondAudio.dispatch('ended');
await settle(40);
check('после проигрывания кнопка возвращается в ожидание',
  speechMsgButton.dataset.speechState === 'idle' && window.NovaSpeech._state().current === null);

// Пауза и продолжение
speechMsgButton.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
await settle(40);
speechMsgButton.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
await settle(30);
check('повторный клик ставит на паузу', speechMsgButton.dataset.speechState === 'paused',
  speechMsgButton.dataset.speechState);
speechMsgButton.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
await settle(30);
check('клик на паузе продолжает воспроизведение',
  speechMsgButton.dataset.speechState === 'playing');
window.NovaSpeech.stop('test');
await settle(20);

// Автоозвучка: ровно один раз на завершённый ответ
await window.NovaSpeech.saveSettings({ auto_speak: true });
check('автоозвучка включена в контроллере', window.NovaSpeech.autoSpeakEnabled() === true);
const beforeAuto = mock.speechRequests.length;
document.getElementById('chat-input').value = 'и ещё раз';
await window.sendMessage();
await settle(120);
const autoRequests = mock.speechRequests.length - beforeAuto;
check('после ответа автоозвучка сработала один раз', autoRequests === 1,
  `запросов: ${autoRequests}`);
check('автоозвучка помечена как автоматическая',
  mock.speechRequests[mock.speechRequests.length - 1].auto === true);
const autoTurn = lastAi();
check('ответ сам начал играть', autoTurn.querySelector('[data-act="speak"]').dataset.speechState === 'playing');
window.NovaSpeech.onTurnFinished(autoTurn, 'и ещё раз', { auto: true });
await settle(40);
check('повторный вызов для того же ответа не запускает второй голос',
  mock.speechRequests.length - beforeAuto === 1);
check('автоозвучка не дублирует ручную: ручная кнопка тоже играет', autoTurn.querySelector('[data-act="speak"]')
  .dataset.speechState === 'playing');

// Выключение автоозвучки гасит авто-воспроизведение
await window.NovaSpeech.saveSettings({ auto_speak: false });
await settle(40);
check('выключение автоозвучки остановило текущее воспроизведение',
  window.NovaSpeech._state().current === null
  && autoTurn.querySelector('[data-act="speak"]').dataset.speechState === 'idle');
check('при остановке отправлена команда отмены на сервер', mock.speechCancels.length >= 1,
  JSON.stringify(mock.speechCancels));

// Ручное воспроизведение переживает выключение автоозвучки
const manualTurn = lastAi();
manualTurn.querySelector('[data-act="speak"]').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
await settle(40);
await window.NovaSpeech.saveSettings({ auto_speak: false });
await settle(40);
check('выключение автоозвучки не убивает ручную озвучку',
  window.NovaSpeech._state().current !== null);
window.NovaSpeech.stop('test');
await settle(20);

// Остановка отменяет незавершённый запрос
mock.audioBlocked = true;
const audioCountBeforeStop = window.__audios.length;
manualTurn.querySelector('[data-act="speak"]').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
await settle(40);
check('пока аудио готовится, кнопка показывает загрузку',
  manualTurn.querySelector('[data-act="speak"]').dataset.speechState === 'loading',
  manualTurn.querySelector('[data-act="speak"]').dataset.speechState);
const linuxAbortBefore = window.Linux.activeAbort;
window.NovaSpeech.stop('manual');
await settle(60);
check('остановка отменяет незавершённый запрос', mock.speechAborted >= 1);
check('остановка не оставляет плеер и состояние',
  window.NovaSpeech._state().current === null
  && manualTurn.querySelector('[data-act="speak"]').dataset.speechState === 'idle'
  && window.__audios.length === audioCountBeforeStop);
check('остановка озвучки не трогает генерацию текста в Linux',
  window.Linux.activeAbort === linuxAbortBefore);
mock.audioBlocked = false;

// Один голос за раз: новый запуск останавливает предыдущий
window.appendMessage('ai', 'Первый развёрнутый ответ для очереди.');
const queueTurnA = lastAi();
window.appendMessage('ai', 'Второй ответ, который перебьёт первый.');
const queueTurnB = lastAi();
const queueButtonA = queueTurnA.querySelector('[data-act="speak"]');
const queueButtonB = queueTurnB.querySelector('[data-act="speak"]');
queueButtonA.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
await settle(40);
const audioA = window.__audios[window.__audios.length - 1];
check('первый ответ играет', queueButtonA.dataset.speechState === 'playing',
  queueButtonA.dataset.speechState);
const audiosBeforeB = window.__audios.length;
queueButtonB.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
await settle(50);
check('новый запуск останавливает предыдущий — один голос за раз',
  audioA.paused === true
  && window.__audios.slice(audiosBeforeB).filter((audio) => !audio.paused).length === 1
  && queueButtonA.dataset.speechState === 'idle'
  && window.NovaSpeech._state().current.element === queueTurnB,
  `A на паузе: ${audioA.paused}, новых играют: `
  + `${window.__audios.slice(audiosBeforeB).filter((audio) => !audio.paused).length}, `
  + `A: ${queueButtonA.dataset.speechState}`);

// Переключение диалога останавливает озвучку
window.newChat();
await settle(40);
check('смена диалога останавливает озвучку и очередь', window.NovaSpeech._state().current === null);

// Нечего озвучивать — понятная подсказка, без запроса
const requestsBeforeEmpty = mock.speechRequests.length;
check('пустой текст не запускает синтез', window.NovaSpeech.onTurnFinished(null, '', {}) === false
  && mock.speechRequests.length === requestsBeforeEmpty);
check('остановленный ответ не озвучивается автоматически',
  window.NovaSpeech.onTurnFinished(document.body, 'ответ', { stopped: true }) === false
  && mock.speechRequests.length === requestsBeforeEmpty);

// Браузерный синтез — только как честный резерв
mock.agentStream = SIMPLE_STREAM;
window.NovaSpeech.saveSettings({ auto_speak: true });
window.NovaSpeech.loadStatus(true);
document.getElementById('chat-input').value = 'ещё ответ';
await window.sendMessage();
await settle(120);
check('кнопка снова доступна после нового чата', speakButton.disabled === false);
check('консоль после сценариев озвучки чистая', consoleErrors.length === 0, consoleErrors.join(' | '));

if (failures.length) {
  console.error(`\n❌ Провалено проверок: ${failures.length} → ${failures.join(', ')}`);
  process.exit(1);
}
console.log('\n✅ Фронтенд-смоук пройден полностью');
process.exit(0);
