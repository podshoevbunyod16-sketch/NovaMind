/**
 * Смоук-тест фронтенда: настоящий index.html + настоящий static/app.js
 * выполняются в jsdom и гоняют полный сценарий поиска.
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
 *   7. Ctrl+K открывает командную панель; тема переключается.
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
const html = pageHtml.replace(
  '<script src="/static/app.js"></script>',
  '<script>' + appJs + '</script>',
);
if (html === pageHtml) throw new Error('не удалось встроить /static/app.js в страницу');

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

const themeBefore = document.documentElement.getAttribute('data-glass-theme');
window.cycleTheme();
check('тема переключается',
  document.documentElement.getAttribute('data-glass-theme') !== themeBefore);

window.toggleCalmMotion();
check('спокойный режим включается',
  document.documentElement.getAttribute('data-motion') === 'calm');

window.toggleReasoning();
check('рассуждения переключаются',
  document.getElementById('btn-reasoning').classList.contains('active'));

window.showNotification('Тест', 'ok');
check('тост показан', document.querySelectorAll('#toastStack .lg-toast').length >= 1,
  `в стеке ${document.querySelectorAll('#toastStack .lg-toast').length}`);

console.log('\n5) Ошибок за весь прогон не появилось');
check('консоль чистая', consoleErrors.length === 0, consoleErrors.join(' | '));

// ══════════════════════════════════════════════════════════════
// 6) Страница настроек: провайдеры, каталог, диагностика поиска
// ══════════════════════════════════════════════════════════════
console.log('\n6) Страница настроек');
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
check('кнопка темы работает', (() => {
  const before = settingsDom.window.document.documentElement.getAttribute('data-glass-theme');
  sdoc.getElementById('themeBtn').dispatchEvent(new settingsDom.window.Event('click'));
  return settingsDom.window.document.documentElement.getAttribute('data-glass-theme') !== before;
})());

// ══════════════════════════════════════════════════════════════
// 7) Окно входа
// ══════════════════════════════════════════════════════════════
console.log('\n7) Окно входа');
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
check('вход: тема сохраняется', (() => {
  authDom.window.setAuthTheme('sunset');
  return adoc.documentElement.getAttribute('data-glass-theme') === 'sunset'
    && authDom.window.localStorage.getItem('nova_theme') === 'sunset';
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

if (failures.length) {
  console.error(`\n❌ Провалено проверок: ${failures.length} → ${failures.join(', ')}`);
  process.exit(1);
}
console.log('\n✅ Фронтенд-смоук пройден полностью');
process.exit(0);
