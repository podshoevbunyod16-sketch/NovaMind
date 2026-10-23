/**
 * static/accordion.js — единый компонент раскрывающихся разделов.
 *
 * Используется панелью настроек (провайдеры, MCP, скиллы, медиа, поиск,
 * озвучка, язык). Один и тот же код отвечает за раскрытие, анимацию и
 * сохранение состояния — второй, конкурирующей реализации в проекте нет.
 *
 * Разметка:
 *
 *   <section class="acc" data-accordion="providers">
 *     <button class="acc-head" type="button" data-accordion-toggle>
 *       <span class="acc-title">Провайдеры</span><span class="caret">▼</span>
 *     </button>
 *     <div class="acc-panel" data-accordion-panel>…</div>
 *   </section>
 *
 * Правила:
 *  • по умолчанию все разделы свёрнуты;
 *  • состояние хранится в localStorage (ключ nova_accordion_state), поэтому
 *    обновление страницы не сбрасывает открытые разделы;
 *  • содержимое не пересоздаётся: панель просто скрывается, поэтому повторное
 *    открытие не плодит дубликаты и не теряет загруженные данные;
 *  • анимация — короткий fade/сдвиг на transform+opacity (композитор, дешёво
 *    на слабых телефонах), при prefers-reduced-motion отключается.
 */
(() => {
  'use strict';

  const STORAGE_KEY = 'nova_accordion_state';
  const t = (text) => (window.I18N && window.I18N.translateText
    ? window.I18N.translateText(text) : text);

  function loadState() {
    try {
      const raw = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
      return raw && typeof raw === 'object' ? raw : {};
    } catch (_) {
      return {};
    }
  }

  function saveState(state) {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(state)); } catch (_) { /* приватный режим */ }
  }

  const state = loadState();
  const sections = new Map();

  function apply(section, open, { animate = true, save = true } = {}) {
    const head = section.querySelector('[data-accordion-toggle]');
    const panel = section.querySelector('[data-accordion-panel]');
    if (!head || !panel) return;
    section.classList.toggle('is-open', open);
    head.setAttribute('aria-expanded', open ? 'true' : 'false');
    panel.hidden = !open;
    panel.classList.toggle('is-animated', !!animate && open);
    const label = section.querySelector('[data-acc-state]');
    if (label) label.textContent = open ? t('Открыто') : t('Свёрнуто');
    if (save) {
      state[section.dataset.accordion] = open;
      saveState(state);
    }
    document.dispatchEvent(new CustomEvent('accordion:change', {
      detail: { key: section.dataset.accordion, open },
    }));
  }

  function setup(section) {
    const key = section.dataset.accordion;
    if (!key) return null;
    const head = section.querySelector('[data-accordion-toggle]');
    const panel = section.querySelector('[data-accordion-panel]');
    if (!head || !panel) return null;
    if (!panel.id) panel.id = `acc-panel-${key}`;
    head.setAttribute('aria-controls', panel.id);
    if (head.tagName !== 'BUTTON') head.setAttribute('role', 'button');
    head.setAttribute('tabindex', head.getAttribute('tabindex') || '0');
    if (!head.dataset.accordionBound) {
      head.dataset.accordionBound = '1';
      head.addEventListener('click', () => {
        apply(section, !section.classList.contains('is-open'));
      });
      head.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          apply(section, !section.classList.contains('is-open'));
        }
      });
    }
    apply(section, !!state[key], { animate: false, save: false });
    sections.set(key, section);
    return section;
  }

  function init(root = document) {
    const list = root.matches && root.matches('[data-accordion]')
      ? [root] : Array.from(root.querySelectorAll('[data-accordion]'));
    list.forEach(setup);
    return list.length;
  }

  function sectionOf(key) {
    return sections.get(key) || document.querySelector(`[data-accordion="${key}"]`);
  }

  function open(key) {
    const section = sectionOf(key);
    if (section) { setup(section); apply(section, true); }
    return section;
  }

  function close(key) {
    const section = sectionOf(key);
    if (section) apply(section, false);
    return section;
  }

  function toggle(key) {
    const section = sectionOf(key);
    if (!section) return null;
    setup(section);
    apply(section, !section.classList.contains('is-open'));
    return section;
  }

  // Ссылки в навигации настроек открывают нужный раздел, а не просто прыгают.
  function bindAnchors() {
    document.querySelectorAll('a[href^="#"]').forEach((link) => {
      const target = document.querySelector(link.getAttribute('href'));
      if (!target || !target.hasAttribute('data-accordion')) return;
      link.addEventListener('click', (event) => {
        event.preventDefault();
        open(target.dataset.accordion);
        target.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
    });
  }

  function boot() {
    init();
    bindAnchors();
  }

  window.Accordion = { init, open, close, toggle, state: () => ({ ...state }) };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
