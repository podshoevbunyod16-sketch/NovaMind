const state = { providers: [], activeProvider: '', currentModel: '', models: [] };
const $ = (id) => document.getElementById(id);

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[char]));
}
function formatContext(value) {
  if (!value) return '—';
  return value >= 1000000 ? `${(value / 1000000).toFixed(1)}M` : value >= 1000 ? `${Math.round(value / 1000)}K` : String(value);
}
function formatPrice(value) {
  if (!value || value === '0' || value === '0.0' || value === '0.000000') return t('бесплатно');
  const number = Number(value);
  return Number.isFinite(number) ? t("${v0} / 1M токенов", { v0: number.toFixed(2) }) : t('цена по тарифу');
}
function showToast(message, type = '') {
  const toast = $('toast');
  toast.textContent = message;
  toast.className = `toast visible ${type}`;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => { toast.className = 'toast'; }, 3600);
}
function setCatalogStatus(text, type = '') {
  $('catalogStatus').textContent = text;
  $('catalogStatus').className = type;
}

async function loadProviders() {
  const response = await fetch('/api/settings/providers');
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || t('Не удалось загрузить провайдеров'));
  state.providers = data.providers || [];
  state.activeProvider = data.current_provider;
  state.currentModel = data.current_model;
  renderProviders();
  $('currentPill').textContent = `${providerName(state.activeProvider)} · ${state.currentModel}`;
  if (!state.activeProvider) state.activeProvider = state.providers[0]?.id || '';
  await loadModels(false);
}
function providerName(id) { return t(state.providers.find((item) => item.id === id)?.name || id); }
function renderProviders() {
  $('providerGrid').innerHTML = state.providers.map((provider) => `
    <button class="provider-card ${provider.id === state.activeProvider ? 'active' : ''} ${provider.configured ? '' : 'disabled'}" data-provider="${escapeHtml(provider.id)}" type="button">
      <span class="provider-mark">${provider.id === 'openrouter' ? '◈' : provider.id === 'groq' ? 'G' : provider.id === 'cerebras' ? 'C' : provider.id === 'google_ai_studio' ? '✦' : provider.id === 'pollinations' ? '✺' : provider.id === 'local_demo' ? '◉' : '⌘'}</span>
      <span class="provider-copy"><strong>${escapeHtml(t(provider.name))}</strong><small>${escapeHtml(t(provider.hint) || (provider.configured ? t('Ключ найден · каталог доступен') : t('Нет ключа в .env')))}</small></span>
      <span class="provider-state ${provider.configured ? 'ok' : ''}">${provider.configured ? '●' : '○'}</span>
    </button>`).join('') || `<div class="empty-card">${t("Провайдеры не найдены")}</div>`;
  document.querySelectorAll('.provider-card').forEach((button) => button.addEventListener('click', () => {
    state.activeProvider = button.dataset.provider;
    renderProviders();
    loadModels(false).catch((error) => showToast(error.message, 'error'));
  }));
}
async function loadModels(force) {
  if (!state.activeProvider) return;
  setCatalogStatus(t("Проверяю {v0}…", { v0: providerName(state.activeProvider) }));
  $('modelGrid').innerHTML = `<div class="empty-card loading-card">${t("Проверяю ключ и загружаю каталог моделей…")}</div>`;
  const response = await fetch(`/api/settings/models?provider=${encodeURIComponent(state.activeProvider)}${force ? '&refresh=1' : ''}`);
  const data = await response.json();
  state.models = data.models || [];
  if (data.error) {
    setCatalogStatus(data.error, 'error-text');
    $('modelCount').textContent = `0 ${tn(0, 'модель', 'модели', 'моделей')}`;
    $('modelGrid').innerHTML = `<div class="empty-card error-card">${escapeHtml(data.error)}<br><small>${state.activeProvider === 'openai_compatible' ? t('Проверьте, что llama-server запущен на 127.0.0.1:8080; резервные локальные модели всё равно доступны для выбора.') : t('Проверьте ключ в .env и перезапустите сервер.')}</small></div>`;
    return;
  }
  setCatalogStatus(t("{v0} · каталог получен", { v0: providerName(state.activeProvider) }));
  renderModels();
}
function modelMatchesModality(model, filter) {
  if (filter === 'all') return true;
  const modality = String(model.modality || '').toLowerCase();
  return filter === 'vision' ? modality.includes('image') || modality.includes('vision') : !modality.includes('image') && !modality.includes('vision');
}
function renderModels() {
  const search = $('modelSearch').value.trim().toLowerCase();
  const price = $('priceFilter').value;
  const sort = $('sortFilter').value;
  const modality = $('modalityFilter').value;
  const minContext = Number($('contextFilter').value);
  let models = state.models.filter((model) => {
    const haystack = `${model.id} ${model.name} ${model.description}`.toLowerCase();
    return (!search || haystack.includes(search)) && (price === 'all' || (price === 'free' ? model.free : !model.free)) && modelMatchesModality(model, modality) && (!minContext || Number(model.context_length) >= minContext);
  });
  models.sort((a, b) => {
    if (sort === 'context') return Number(b.context_length) - Number(a.context_length);
    if (sort === 'parameters') return Number(b.parameters_b || 0) - Number(a.parameters_b || 0);
    if (sort === 'free') return Number(b.free) - Number(a.free) || Number(b.context_length) - Number(a.context_length);
    if (sort === 'price') return Number(a.prompt_price || 0) - Number(b.prompt_price || 0);
    if (sort === 'newest') return Number(b.created || 0) - Number(a.created || 0);
    return Number(b.free) - Number(a.free) || Number(b.parameters_b || 0) - Number(a.parameters_b || 0) || Number(b.context_length) - Number(a.context_length);
  });
  $('modelCount').textContent = `${models.length} ${tn(models.length, 'модель', 'модели', 'моделей')}`;
  $('modelGrid').innerHTML = models.length ? models.map(modelCard).join('') : `<div class="empty-card">${t("По этим фильтрам модели не найдены")}</div>`;
  document.querySelectorAll('[data-select-model]').forEach((button) => button.addEventListener('click', () => selectModel(button.dataset.selectModel)));
}
function modelCard(model) {
  const selected = state.activeProvider === model.provider && state.currentModel === model.id;
  const vision = String(model.modality || '').toLowerCase().includes('image') || String(model.modality || '').toLowerCase().includes('vision');
  return `<article class="model-card ${selected ? 'selected' : ''}">
    <div class="model-card-top"><span class="model-badge ${model.free ? 'free' : 'paid'}">${model.free ? 'FREE' : 'PAID'}</span>${vision ? '<span class="model-badge vision">VISION</span>' : ''}<span class="model-id">${escapeHtml(model.id)}</span></div>
    <h3>${escapeHtml(model.name)}</h3>
    <div class="model-stats"><span>${t("⌗ Контекст")} <b>${formatContext(model.context_length)}</b></span><span>${t("◉ Веса")} <b>${model.parameters_b ? `${model.parameters_b}B` : '—'}</b></span><span>${t("◆ Ввод")} <b>${formatPrice(model.prompt_price)}</b></span></div>
    <p>${escapeHtml(model.description || t('Модель доступна через выбранного провайдера.'))}</p>
    <button class="select-model ${selected ? 'selected' : ''}" data-select-model="${escapeHtml(model.id)}" type="button">${selected ? t('✓ Используется в чате') : t('Выбрать модель')}</button>
  </article>`;
}
async function selectModel(model) {
  const response = await fetch('/api/settings/select', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({provider: state.activeProvider, model}) });
  const data = await response.json();
  if (!response.ok) { showToast(data.error || t('Не удалось выбрать модель'), 'error'); return; }
  state.currentModel = data.model;
  $('currentPill').textContent = `${providerName(state.activeProvider)} · ${state.currentModel}`;
  renderModels();
  showToast(t("Выбрано: {model}", { model: data.model }));
}
$('refreshAll').addEventListener('click', () => loadProviders().catch((error) => showToast(error.message, 'error')));
$('refreshModels').addEventListener('click', () => loadModels(true).catch((error) => showToast(error.message, 'error')));
['modelSearch', 'priceFilter', 'sortFilter', 'modalityFilter', 'contextFilter'].forEach((id) => $(id).addEventListener('input', renderModels));
loadProviders().catch((error) => { setCatalogStatus(error.message, 'error-text'); showToast(error.message, 'error'); });

/* ========== GENERATIVE MEDIA CATALOG ========== */
const mediaState = {type:'image', models:[]};
function mediaPriceLabel(model) {
  if (model.pricing_status === 'free') return 'FREE';
  if (model.pricing_status === 'free-tier') return 'FREE TIER';
  if (model.pricing_status === 'paid') return 'PAID';
  return model.free ? 'FREE' : 'PRICE UNKNOWN';
}
function mediaCard(model) {
  const configured = model.configured !== false;
  return `<article class="model-card">
    <div class="model-card-top">
      <span class="model-badge ${model.free ? 'free' : 'paid'}">${escapeHtml(mediaPriceLabel(model))}</span>
      <span class="model-badge vision">${escapeHtml((model.media_types || []).join(', ').toUpperCase())}</span>
      <span class="model-id">${escapeHtml(model.provider_name || model.provider || '')}</span>
    </div>
    <h3>${escapeHtml(model.name || model.id)}</h3>
    <p><code>${escapeHtml(model.id)}</code></p>
    <p>${escapeHtml(model.description || '')}</p>
    <div class="model-stats">
      <span>${t("Цена")} <b>${model.free ? t('есть бесплатный тариф') : model.pricing_status === 'paid' ? t('платный') : t('не определена')}</b></span>
      <span>API <b>${configured ? t('доступен') : t('нужен ключ')}</b></span>
    </div>
    <button class="select-model media-test-btn" type="button"
      data-media-test="${escapeHtml(model.id)}" data-media-provider="${escapeHtml(model.provider)}"
      data-media-type="${escapeHtml(mediaState.type)}">
      ${configured ? t('Проверить доступность') : t('Настроить ключ')}
    </button>
  </article>`;
}
async function loadMediaModels(force=false) {
  const grid = $('mediaGrid');
  if (!grid) return;
  $('mediaStatus').textContent = t('Проверяю актуальный каталог…');
  grid.innerHTML = `<div class="empty-card loading-card">${t("Проверяю провайдеры и цены…")}</div>`;
  try {
    const response = await fetch(`/api/settings/media-models?type=${encodeURIComponent(mediaState.type)}${force ? '&refresh=1' : ''}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || t('Не удалось получить media-каталог'));
    mediaState.models = data.models || [];
    $('mediaCount').textContent = `${mediaState.models.length} ${tn(mediaState.models.length, 'модель', 'модели', 'моделей')}`;
    $('mediaStatus').textContent = mediaState.models.length
      ? t('Цены определены по данным провайдера/официального каталога')
      : t('Для этого типа моделей ничего не найдено');
    grid.innerHTML = mediaState.models.length
      ? mediaState.models.map(mediaCard).join('')
      : `<div class="empty-card">${t("Модели не найдены. Подключите OpenRouter или Google AI Studio.")}</div>`;
    document.querySelectorAll('[data-media-test]').forEach(btn => btn.addEventListener('click', () => testMediaModel(btn)));
  } catch (error) {
    $('mediaStatus').textContent = error.message;
    grid.innerHTML = `<div class="empty-card error-card">${escapeHtml(error.message)}</div>`;
  }
}
async function testMediaModel(button) {
  button.disabled = true;
  const old = button.textContent;
  button.textContent = t('Проверяю…');
  try {
    const qs = new URLSearchParams({
      type: button.dataset.mediaType,
      provider: button.dataset.mediaProvider,
      refresh: '1'
    });
    const response = await fetch('/api/settings/media-models?' + qs.toString());
    const data = await response.json();
    const found = (data.models || []).find(m => m.id === button.dataset.mediaTest);
    if (response.ok && found) {
      const price = found.pricing_status === 'paid' ? t('платный')
        : found.pricing_status === 'free-tier' ? t('есть бесплатный тариф')
        : found.pricing_status === 'free' ? t('бесплатно') : t('цена не определена');
      if (found.configured === false) {
        showToast(t("⚠ {id}: модель найдена, но API-ключ провайдера не настроен · {price}", { id: found.id, price: price }), 'error');
      } else {
        showToast(t("✓ {id}: каталог/API доступен · {price}", { id: found.id, price: price }), 'success');
      }
    } else {
      showToast(data.error || t('Модель сейчас недоступна'), 'error');
    }
  } catch (error) {
    showToast(t('Ошибка проверки: ') + error.message, 'error');
  } finally {
    button.disabled = false;
    button.textContent = old;
  }
}
function initMediaCatalog() {
  document.querySelectorAll('[data-media-type]').forEach(tab => tab.addEventListener('click', () => {
    mediaState.type = tab.dataset.mediaType;
    document.querySelectorAll('[data-media-type]').forEach(x => x.classList.toggle('active', x === tab));
    loadMediaModels(false);
  }));
  $('refreshMedia')?.addEventListener('click', () => loadMediaModels(true));
  loadMediaModels(false);
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initMediaCatalog);
else initMediaCatalog();

/* ========== ТЕМА СТЕКЛА ========== */
// Тема одна — Aurora, как и в чате.
const GLASS_THEMES = ['aurora'];
function applyGlassTheme(theme) {
  const value = GLASS_THEMES.includes(theme) ? theme : 'aurora';
  document.documentElement.setAttribute('data-glass-theme', value);
  localStorage.setItem('nova_theme', value);
}
// Тема одна — Aurora, кнопки смены темы нет.
applyGlassTheme('aurora');

/* ========== ДИАГНОСТИКА ПОИСКА ========== */
async function loadSearchHealth(force = false) {
  const grid = $('healthGrid');
  const status = $('healthStatus');
  if (!grid) return;
  status.textContent = t('Проверяю поисковые бэкенды…');
  grid.innerHTML = `<div class="empty-card loading-card">${t("Опрашиваю SearXNG, DuckDuckGo, Википедию…")}</div>`;
  try {
    const response = await fetch('/api/search/health' + (force ? '?refresh=1' : ''));
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || ('HTTP ' + response.status));
    const alive = (data.alive || []).length;
    status.textContent = data.ok
      ? t("Поиск работает: доступно {alive} из {length} бэкендов", { alive: alive, length: data.backends.length })
      : t('Ни один поисковый бэкенд не ответил. Проверьте интернет или задайте SEARCH_BACKENDS / SEARXNG_INSTANCES в .env');
    grid.innerHTML = (data.backends || []).map((item) => `
      <div class="health-item">
        <div>
          <strong>${escapeHtml(item.backend)}</strong>
          <small>${item.ok ? t("{count} результатов · {ms} мс", { count: item.count, ms: item.ms }) : escapeHtml(item.error || t('нет ответа'))}</small>
        </div>
        <span class="lg-dot ${item.ok ? 'ok' : 'err'}"></span>
      </div>`).join('') || `<div class="empty-card">${t("Бэкенды не настроены")}</div>`;
  } catch (error) {
    status.textContent = error.message;
    grid.innerHTML = `<div class="empty-card error-card">${escapeHtml(error.message)}</div>`;
  }
}
$('refreshHealth')?.addEventListener('click', () => loadSearchHealth(true));
loadSearchHealth(false);
