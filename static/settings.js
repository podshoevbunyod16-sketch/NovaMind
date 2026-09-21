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
  if (!value || value === '0' || value === '0.0' || value === '0.000000') return 'бесплатно';
  const number = Number(value);
  return Number.isFinite(number) ? `$${number.toFixed(2)} / 1M токенов` : 'цена по тарифу';
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
  if (!response.ok) throw new Error(data.error || 'Не удалось загрузить провайдеров');
  state.providers = data.providers || [];
  state.activeProvider = data.current_provider;
  state.currentModel = data.current_model;
  renderProviders();
  $('currentPill').textContent = `${providerName(state.activeProvider)} · ${state.currentModel}`;
  if (!state.activeProvider) state.activeProvider = state.providers[0]?.id || '';
  await loadModels(false);
}
function providerName(id) { return state.providers.find((item) => item.id === id)?.name || id; }
function renderProviders() {
  $('providerGrid').innerHTML = state.providers.map((provider) => `
    <button class="provider-card ${provider.id === state.activeProvider ? 'active' : ''} ${provider.configured ? '' : 'disabled'}" data-provider="${escapeHtml(provider.id)}" type="button">
      <span class="provider-mark">${provider.id === 'openrouter' ? '◈' : provider.id === 'groq' ? 'G' : provider.id === 'cerebras' ? 'C' : '⌘'}</span>
      <span class="provider-copy"><strong>${escapeHtml(provider.name)}</strong><small>${provider.configured ? 'Ключ найден · каталог доступен' : 'Нет ключа в .env'}</small></span>
      <span class="provider-state ${provider.configured ? 'ok' : ''}">${provider.configured ? '●' : '○'}</span>
    </button>`).join('') || '<div class="empty-card">Провайдеры не найдены</div>';
  document.querySelectorAll('.provider-card').forEach((button) => button.addEventListener('click', () => {
    state.activeProvider = button.dataset.provider;
    renderProviders();
    loadModels(false).catch((error) => showToast(error.message, 'error'));
  }));
}
async function loadModels(force) {
  if (!state.activeProvider) return;
  setCatalogStatus(`Проверяю ${providerName(state.activeProvider)}…`);
  $('modelGrid').innerHTML = '<div class="empty-card loading-card">Проверяю ключ и загружаю каталог моделей…</div>';
  const response = await fetch(`/api/settings/models?provider=${encodeURIComponent(state.activeProvider)}${force ? '&refresh=1' : ''}`);
  const data = await response.json();
  state.models = data.models || [];
  if (data.error) {
    setCatalogStatus(data.error, 'error-text');
    $('modelCount').textContent = '0 моделей';
    $('modelGrid').innerHTML = `<div class="empty-card error-card">${escapeHtml(data.error)}<br><small>Проверьте ключ в .env и перезапустите сервер.</small></div>`;
    return;
  }
  setCatalogStatus(`${providerName(state.activeProvider)} · каталог получен`);
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
  $('modelCount').textContent = `${models.length} ${models.length === 1 ? 'модель' : 'моделей'}`;
  $('modelGrid').innerHTML = models.length ? models.map(modelCard).join('') : '<div class="empty-card">По этим фильтрам модели не найдены</div>';
  document.querySelectorAll('[data-select-model]').forEach((button) => button.addEventListener('click', () => selectModel(button.dataset.selectModel)));
}
function modelCard(model) {
  const selected = state.activeProvider === model.provider && state.currentModel === model.id;
  const vision = String(model.modality || '').toLowerCase().includes('image') || String(model.modality || '').toLowerCase().includes('vision');
  return `<article class="model-card ${selected ? 'selected' : ''}">
    <div class="model-card-top"><span class="model-badge ${model.free ? 'free' : 'paid'}">${model.free ? 'FREE' : 'PAID'}</span>${vision ? '<span class="model-badge vision">VISION</span>' : ''}<span class="model-id">${escapeHtml(model.id)}</span></div>
    <h3>${escapeHtml(model.name)}</h3>
    <div class="model-stats"><span>⌗ Контекст <b>${formatContext(model.context_length)}</b></span><span>◉ Веса <b>${model.parameters_b ? `${model.parameters_b}B` : '—'}</b></span><span>◆ Ввод <b>${formatPrice(model.prompt_price)}</b></span></div>
    <p>${escapeHtml(model.description || 'Модель доступна через выбранного провайдера.')}</p>
    <button class="select-model ${selected ? 'selected' : ''}" data-select-model="${escapeHtml(model.id)}" type="button">${selected ? '✓ Используется в чате' : 'Выбрать модель'}</button>
  </article>`;
}
async function selectModel(model) {
  const response = await fetch('/api/settings/select', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({provider: state.activeProvider, model}) });
  const data = await response.json();
  if (!response.ok) { showToast(data.error || 'Не удалось выбрать модель', 'error'); return; }
  state.currentModel = data.model;
  $('currentPill').textContent = `${providerName(state.activeProvider)} · ${state.currentModel}`;
  renderModels();
  showToast(`Выбрано: ${data.model}`);
}
$('refreshAll').addEventListener('click', () => loadProviders().catch((error) => showToast(error.message, 'error')));
$('refreshModels').addEventListener('click', () => loadModels(true).catch((error) => showToast(error.message, 'error')));
['modelSearch', 'priceFilter', 'sortFilter', 'modalityFilter', 'contextFilter'].forEach((id) => $(id).addEventListener('input', renderModels));
loadProviders().catch((error) => { setCatalogStatus(error.message, 'error-text'); showToast(error.message, 'error'); });
