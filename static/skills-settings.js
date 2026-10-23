/**
 * static/skills-settings.js — панель «Скиллы».
 *
 * Скиллы — отдельная категория: это инструкции (SKILL.md), которые попадают в
 * промпт агента. Выключенный скилл остаётся на диске, но в промпт не попадает
 * (`POST /api/settings/skills/<id>/enabled` → `skills_registry.set_enabled`).
 * Если навык объявляет инструменты в frontmatter (`tools:`), они показываются
 * как справка: разрешения по-прежнему проверяет агент и MCP-политика.
 */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' }[c]));
  const t = (text) => (window.I18N && window.I18N.translateText ? window.I18N.translateText(text) : text);
  const toast = (message, type = '') => (window.showToast ? window.showToast(message, type) : console.log(message));
  const escapeSelector = (value) => (typeof CSS !== 'undefined' && CSS.escape
    ? CSS.escape(value)
    : String(value).replace(/["\\]/g, '\\$&'));

  function card(skill) {
    const enabled = skill.enabled !== false;
    const tools = Array.isArray(skill.tools) ? skill.tools : [];
    return `<article class="model-card skill-card ${enabled ? '' : 'is-disabled'}" data-skill-card="${esc(skill.id)}">
      <div class="model-card-top">
        <span class="model-badge ${enabled ? 'free' : 'paid'}">${enabled ? 'SKILL' : 'OFF'}</span>
        <span class="model-id">${esc(skill.id)}</span>
      </div>
      <h3>${esc(skill.name || skill.id)}</h3>
      <p>${esc(skill.description || '')}</p>
      ${skill.invalid ? `<p class="error-text">${t('Skill повреждён или имеет неверный формат.')}</p>` : ''}
      <div class="model-stats">
        <span>${t('Доступность')} <b>${enabled ? t('включён') : t('выключен')}</b></span>
        <span>${t('Инструменты')} <b>${tools.length ? esc(tools.join(', ')) : '—'}</b></span>
      </div>
      <div class="skill-actions">
        <label class="switch switch--small" title="${t('Включить или выключить скилл')}">
          <input type="checkbox" role="switch" data-skill-toggle="${esc(skill.id)}" ${enabled ? 'checked' : ''}
                 aria-checked="${enabled ? 'true' : 'false'}" aria-label="${t('Скилл включён')}">
          <span class="switch-track" aria-hidden="true"><span class="switch-knob"></span></span>
        </label>
        <button class="select-model" type="button" data-skill-details="${esc(skill.id)}">${t('Подробнее')}</button>
        <button class="select-model danger" type="button" data-skill-delete="${esc(skill.id)}">${t('Удалить')}</button>
      </div>
      <pre class="skill-details" data-skill-body="${esc(skill.id)}" hidden></pre>
    </article>`;
  }

  async function toggleSkill(id, enabled, input) {
    input.disabled = true;
    try {
      const response = await fetch(`/api/settings/skills/${encodeURIComponent(id)}/enabled`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      toast(enabled ? t('Скилл включён') : t('Скилл выключен: агент его не использует'));
      await loadSkills();
    } catch (error) {
      input.checked = !enabled;
      input.setAttribute('aria-checked', String(!enabled));
      toast(error.message, 'error');
    } finally {
      input.disabled = false;
    }
  }

  async function details(id, button) {
    const body = document.querySelector(`[data-skill-body="${escapeSelector(id)}"]`);
    if (!body) return;
    if (!body.hidden) { body.hidden = true; button.textContent = t('Подробнее'); return; }
    button.disabled = true;
    try {
      const response = await fetch(`/api/settings/skills/${encodeURIComponent(id)}`);
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      body.textContent = String(data.content || '').slice(0, 4000);
      body.hidden = false;
      button.textContent = t('Свернуть');
    } catch (error) {
      toast(error.message, 'error');
    } finally {
      button.disabled = false;
    }
  }

  async function loadSkills() {
    const grid = $('skillsGrid');
    if (!grid) return;
    $('skillsStatus').textContent = t('Загружаю сохранённые skills…');
    const response = await fetch('/api/settings/skills');
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || t('Не удалось загрузить skills'));
    const skills = data.skills || [];
    const enabled = skills.filter((skill) => skill.enabled !== false).length;
    $('skillsStatus').textContent = `${skills.length} ${t('всего')} · ${enabled} ${t('включено')}`;
    grid.innerHTML = skills.length ? skills.map(card).join('') : `<div class="empty-card">${t('Skills ещё не добавлены')}</div>`;
    grid.querySelectorAll('[data-skill-toggle]').forEach((input) => {
      input.addEventListener('change', () => toggleSkill(input.dataset.skillToggle, input.checked, input));
    });
    grid.querySelectorAll('[data-skill-details]').forEach((button) => {
      button.addEventListener('click', () => details(button.dataset.skillDetails, button));
    });
    grid.querySelectorAll('[data-skill-delete]').forEach((button) => {
      button.addEventListener('click', async () => {
        if (!window.confirm(`${t('Удалить skill')} «${button.dataset.skillDelete}»?`)) return;
        const response = await fetch('/api/settings/skills/' + encodeURIComponent(button.dataset.skillDelete), { method: 'DELETE' });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) return toast(data.error || t('Не удалось удалить skill'), 'error');
        toast(t('Skill удалён'));
        loadSkills().catch((error) => toast(error.message, 'error'));
      });
    });
  }

  async function uploadZip() {
    const file = $('skillZip')?.files?.[0];
    if (!file) return toast(t('Выберите ZIP-архив'), 'error');
    const body = new FormData();
    body.append('file', file);
    const response = await fetch('/api/settings/skills/zip', { method: 'POST', body });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) return toast(data.error || t('ZIP не принят'), 'error');
    toast(`${t('Skill сохранён')}: ${data.skill.name}`);
    $('skillZip').value = '';
    loadSkills();
  }

  async function saveText() {
    const response = await fetch('/api/settings/skills/text', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: $('skillTextName').value, content: $('skillTextContent').value }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) return toast(data.error || t('Текст skill не принят'), 'error');
    toast(`${t('Skill сохранён')}: ${data.skill.name}`);
    $('skillTextName').value = '';
    $('skillTextContent').value = '';
    loadSkills();
  }

  function init() {
    if (!$('skillsGrid')) return;
    $('refreshSkills')?.addEventListener('click', () => loadSkills().catch((error) => toast(error.message, 'error')));
    $('uploadSkillZip')?.addEventListener('click', () => uploadZip().catch((error) => toast(error.message, 'error')));
    $('saveSkillText')?.addEventListener('click', () => saveText().catch((error) => toast(error.message, 'error')));
    document.querySelectorAll('[data-skill-tab]').forEach((tab) => tab.addEventListener('click', () => {
      document.querySelectorAll('[data-skill-tab]').forEach((node) => node.classList.toggle('active', node === tab));
      $('skillZipPanel').hidden = tab.dataset.skillTab !== 'zip';
      $('skillTextPanel').hidden = tab.dataset.skillTab !== 'text';
    }));
    loadSkills().catch((error) => {
      $('skillsStatus').textContent = error.message;
      toast(error.message, 'error');
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
