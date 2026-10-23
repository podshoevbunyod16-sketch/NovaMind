/**
 * static/mcp-settings.js — панель «MCP-серверы».
 *
 * Показывает реальные данные `mcp_registry`: каталог шаблонов и сохранённые
 * подключения. Ничего не подключается само — только по кнопке, с проверкой
 * `initialize` на сервере. Для каждого подключения видно транспорт (Streamable
 * HTTP), статус, последнюю ошибку и список инструментов (`tools/list`).
 */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' }[c]));
  const t = (text) => (window.I18N && window.I18N.translateText ? window.I18N.translateText(text) : text);
  const toast = (message, type = '') => (window.showToast ? window.showToast(message, type) : console.log(message));
  const authLabel = (auth) => (Array.isArray(auth) ? auth.join(', ') : String(auth || 'none'));
  const transportLabel = (transport) => (String(transport || 'http').toLowerCase() === 'http'
    ? 'HTTP (Streamable)'
    : String(transport || '').toUpperCase());

  const state = { connections: [], catalog: [], tools: {}, errors: {} };

  function connectedCard(item) {
    const id = esc(item.id);
    const connected = Boolean(item.connected);
    const tools = state.tools[item.id];
    const error = item.last_error || state.errors[item.id] || '';
    return `<article class="model-card mcp-card ${connected ? 'is-connected' : 'is-offline'}" data-mcp-card="${id}">
      <div class="model-card-top">
        <span class="model-badge ${connected ? 'free' : 'paid'}">${connected ? 'CONNECTED' : 'OFFLINE'}</span>
        <span class="model-id">${esc(transportLabel(item.transport))}</span>
      </div>
      <h3>${esc(item.name || item.id)}</h3>
      <p><code>${esc(item.url || '')}</code></p>
      <div class="model-stats">
        <span>Авторизация <b>${esc(authLabel(item.auth))}</b></span>
        <span>Статус <b>${connected ? t('подключён') : t('не подключён')}</b></span>
        <span>Инструменты <b>${Array.isArray(tools) ? tools.length : '—'}</b></span>
      </div>
      ${error ? `<p class="error-text mcp-error">${esc(error)}</p>` : ''}
      ${Array.isArray(tools) && tools.length ? `<ul class="mcp-tools">${tools.slice(0, 12).map((tool) =>
        `<li><code>${esc(tool.name || tool)}</code>${tool.description ? ` — ${esc(String(tool.description).slice(0, 120))}` : ''}</li>`).join('')}</ul>` : ''}
      <div class="mcp-actions">
        <button class="select-model" type="button" data-mcp-test="${id}">${t('Проверить')}</button>
        <button class="select-model" type="button" data-mcp-tools="${id}">${t('Инструменты')}</button>
        <button class="select-model danger" type="button" data-mcp-disconnect="${id}">${t('Отключить')}</button>
      </div>
    </article>`;
  }

  function catalogCard(item) {
    return `<article class="model-card mcp-card is-template">
      <div class="model-card-top">
        <span class="model-badge vision">${t('ШАБЛОН')}</span>
        <span class="model-id">${esc(transportLabel(item.transport))}</span>
      </div>
      <h3>${esc(item.name)}</h3>
      <p>${esc(item.description || 'Любой Streamable HTTP MCP endpoint')}</p>
      <div class="model-stats"><span>Авторизация <b>${esc(authLabel(item.auth))}</b></span><span>Статус <b>${t('не подключён')}</b></span></div>
      <button class="select-model" type="button" data-mcp-fill="${esc(item.name)}">${t('Заполнить форму')}</button>
    </article>`;
  }

  function paintStatus() {
    const status = $('mcpStatus');
    if (!status) return;
    const connected = state.connections.filter((item) => item.connected).length;
    status.textContent = `${t('Подключено')}: ${connected} · ${t('шаблонов')}: ${state.catalog.length}`;
  }

  function render(data) {
    const grid = $('mcpGrid');
    if (!grid) return;
    state.connections = data.connections || [];
    state.catalog = data.catalog || [];
    grid.innerHTML = [
      ...state.connections.map(connectedCard),
      ...state.catalog.map(catalogCard),
    ].join('') || `<div class="empty-card">${t('Каталог пуст')}</div>`;
    paintStatus();
    bind(grid);
  }

  function bind(grid) {
    grid.querySelectorAll('[data-mcp-test]').forEach((button) => button.addEventListener('click', async () => {
      const label = button.textContent;
      button.disabled = true;
      button.textContent = t('Проверяю…');
      try {
        const response = await fetch(`/api/settings/mcp/${encodeURIComponent(button.dataset.mcpTest)}/test`, { method: 'POST' });
        const body = await response.json().catch(() => ({}));
        if (response.ok && body.connected) {
          delete state.errors[button.dataset.mcpTest];
          toast(`${t('MCP отвечает')}: ${body.tools ?? ''}`);
        } else {
          state.errors[button.dataset.mcpTest] = body.error || `HTTP ${response.status}`;
          toast(body.error || t('MCP не отвечает'), 'error');
        }
        await loadMcp();
      } finally {
        button.disabled = false;
        button.textContent = label;
      }
    }));

    grid.querySelectorAll('[data-mcp-tools]').forEach((button) => button.addEventListener('click', async () => {
      const id = button.dataset.mcpTools;
      const label = button.textContent;
      button.disabled = true;
      button.textContent = t('Загружаю…');
      try {
        const response = await fetch(`/api/settings/mcp/${encodeURIComponent(id)}/tools`);
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
        const tools = (body.result && body.result.tools) || [];
        state.tools[id] = tools;
        toast(`${t('MCP tools/list')}: ${tools.length} ${t('инструментов')}`);
        await loadMcp();
      } catch (error) {
        state.errors[id] = error.message;
        toast(error.message, 'error');
        await loadMcp();
      } finally {
        button.disabled = false;
        button.textContent = label;
      }
    }));

    grid.querySelectorAll('[data-mcp-disconnect]').forEach((button) => button.addEventListener('click', async () => {
      const id = button.dataset.mcpDisconnect;
      if (!window.confirm(t('Отключить MCP-сервер? Секреты подключения будут удалены.'))) return;
      button.disabled = true;
      try {
        const response = await fetch(`/api/settings/mcp/${encodeURIComponent(id)}`, { method: 'DELETE' });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        delete state.tools[id];
        delete state.errors[id];
        toast(t('MCP-сервер отключён'));
        await loadMcp();
      } catch (error) {
        toast(error.message, 'error');
        button.disabled = false;
      }
    }));

    grid.querySelectorAll('[data-mcp-fill]').forEach((button) => button.addEventListener('click', () => {
      if ($('mcpName')) $('mcpName').value = button.dataset.mcpFill;
      if ($('mcpUrl')) $('mcpUrl').focus();
      toast(t('Укажите URL MCP-сервера и нажмите «Подключить»'));
    }));
  }

  async function loadMcp() {
    const response = await fetch('/api/settings/mcp');
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || t('Не удалось загрузить MCP-каталог'));
    render(data);
    return data;
  }

  function init() {
    if (!$('mcpGrid')) return;
    $('refreshMcp')?.addEventListener('click', () => loadMcp().catch((error) => toast(error.message, 'error')));
    $('connectMcp')?.addEventListener('click', async () => {
      const url = $('mcpUrl').value.trim();
      if (!url) return toast(t('Укажите URL MCP-сервера'), 'error');
      const auth = $('mcpAuth').value;
      const payload = { name: $('mcpName').value.trim() || 'MCP server', url, auth };
      if (auth === 'api_key') payload.api_key = $('mcpSecret').value;
      else if (auth === 'basic') { payload.username = $('mcpName').value.trim(); payload.password = $('mcpSecret').value; }
      else if (auth !== 'none') payload.token = $('mcpSecret').value;
      const button = $('connectMcp');
      button.disabled = true;
      try {
        const response = await fetch('/api/settings/mcp/connect', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.error || t('MCP-сервер не ответил на initialize'));
        $('mcpSecret').value = '';
        toast(t('MCP-сервер подключён и прошёл initialize'));
        await loadMcp();
      } catch (error) {
        toast(error.message, 'error');
      } finally {
        button.disabled = false;
      }
    });
    loadMcp().catch((error) => {
      if ($('mcpStatus')) $('mcpStatus').textContent = error.message;
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
