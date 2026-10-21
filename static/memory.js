/**
 * static/memory.js — панель «Память».
 *
 * Список, поиск и удаление долгосрочных воспоминаний пользователя.
 * Сервер отдаёт только записи текущего пользователя; гостям панель
 * отвечает 403, и тогда показывается подсказка про вход.
 */
(() => {
  'use strict';

  const KIND_LABEL = {
    fact: 'факт',
    preference: 'предпочтение',
    project: 'проект',
    decision: 'решение',
  };

  let items = [];
  let loaded = false;

  function escapeHtml(value) {
    return String(value ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function notify(message, kind) {
    if (typeof window.showNotification === 'function') window.showNotification(message, kind);
  }

  async function request(url, options) {
    const response = await fetch(url, {
      headers: { 'Content-Type': 'application/json' },
      ...options,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  }

  function render() {
    const list = document.getElementById('memoryList');
    const empty = document.getElementById('memoryEmpty');
    if (!list) return;
    if (!items.length) {
      list.innerHTML = '';
      if (empty) {
        empty.hidden = false;
        empty.textContent = loaded
          ? 'Ничего не найдено. Скажите агенту «запомни …» или добавьте запись выше.'
          : 'Пока ничего не запомнено.';
      }
      return;
    }
    if (empty) empty.hidden = true;
    list.innerHTML = items.map((item) => `
      <div class="memory-item" data-id="${escapeHtml(item.id)}">
        <div class="memory-item-top">
          <span class="memory-kind">${escapeHtml(KIND_LABEL[item.kind] || item.kind || 'факт')}</span>
          ${item.tags ? `<span class="memory-tags">${escapeHtml(item.tags)}</span>` : ''}
          <button type="button" class="memory-del" data-del="${escapeHtml(item.id)}"
                  title="Удалить из памяти">Удалить</button>
        </div>
        <div class="memory-text">${escapeHtml(item.text)}</div>
      </div>`).join('');
    list.querySelectorAll('[data-del]').forEach((button) => {
      button.addEventListener('click', () => removeMemory(button.dataset.del));
    });
  }

  async function loadMemory(force) {
    try {
      const data = await request('/api/memory');
      items = Array.isArray(data.items) ? data.items : [];
      loaded = true;
      render();
    } catch (error) {
      loaded = true;
      items = [];
      render();
      const empty = document.getElementById('memoryEmpty');
      if (empty) {
        empty.hidden = false;
        empty.textContent = /вход|403/i.test(error.message)
          ? 'Память доступна после входа — гостям записи не сохраняются.'
          : `Не удалось загрузить память: ${error.message}`;
      }
    }
    return force;
  }

  async function filterMemory() {
    const input = document.getElementById('memoryQuery');
    const query = input ? input.value.trim() : '';
    if (!query) {
      await loadMemory();
      return;
    }
    try {
      const data = await request(`/api/memory?q=${encodeURIComponent(query)}`);
      items = Array.isArray(data.items) ? data.items : [];
      loaded = true;
      render();
    } catch (error) {
      notify(`Поиск по памяти: ${error.message}`, 'warn');
    }
  }

  async function addMemory(event) {
    event.preventDefault();
    const text = document.getElementById('memoryText');
    const kind = document.getElementById('memoryKind');
    const tags = document.getElementById('memoryTags');
    const payload = { text: text ? text.value.trim() : '', kind: kind ? kind.value : 'fact',
                      tags: tags ? tags.value.trim() : '' };
    if (!payload.text) return;
    try {
      const data = await request('/api/memory', { method: 'POST', body: JSON.stringify(payload) });
      items.unshift(data.item);
      if (text) text.value = '';
      if (tags) tags.value = '';
      render();
      notify('Запомнил', 'ok');
    } catch (error) {
      notify(error.message, 'warn');
    }
  }

  async function removeMemory(id) {
    if (!id) return;
    try {
      await request(`/api/memory/${encodeURIComponent(id)}`, { method: 'DELETE' });
      items = items.filter((item) => item.id !== id);
      render();
      notify('Удалено из памяти', 'ok');
    } catch (error) {
      notify(error.message, 'warn');
    }
  }

  function openMemory() {
    const panel = document.getElementById('memoryPanel');
    if (!panel) return;
    panel.classList.add('open');
    panel.setAttribute('aria-hidden', 'false');
    loadMemory();
    const input = document.getElementById('memoryText');
    if (input) setTimeout(() => input.focus(), 30);
  }

  function closeMemory() {
    const panel = document.getElementById('memoryPanel');
    if (!panel) return;
    panel.classList.remove('open');
    panel.setAttribute('aria-hidden', 'true');
  }

  window.openMemory = openMemory;
  window.closeMemory = closeMemory;
  window.loadMemory = loadMemory;
  window.filterMemory = filterMemory;
  window.addMemory = addMemory;
})();
