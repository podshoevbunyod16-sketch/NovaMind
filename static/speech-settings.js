/**
 * static/speech-settings.js — панель «Озвучка» в настройках.
 *
 * Всё, что здесь есть, связано с сервером:
 *  • статус (`/api/tts/status`) — реально ли настроен TTS-провайдер;
 *  • модели провайдера (`/api/tts/models`) — живой список TTS-моделей с
 *    голосами, тарифом и лимитом символов: то же окно настроек, без
 *    захардкоженных списков. Каталог перечитывается кнопкой «Обновить модели»;
 *  • переключатель автоозвучки, модель, голос и язык — `POST /api/tts/settings`,
 *    который пишет `runtime_settings.json` (тот же файл, что модель и язык);
 *  • «Прослушать пример» — настоящий запрос синтеза короткой фразы, чтобы
 *    пользователь услышал голос до того, как включит автоозвучку.
 *
 * Ошибки провайдера не пересказываются своими словами: сервер присылает
 * дословный ответ и подсказку (`hint`) + ссылку на действие (`action_url`),
 * а панель показывает и то, и другое.
 */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const toast = (message, type = '') => (window.showToast
    ? window.showToast(message, type)
    : console.log(message));
  const t = (text) => (window.I18N && window.I18N.translateText ? window.I18N.translateText(text) : text);
  const settings = { current: {}, status: null, models: [], catalogError: '' };

  function option(value, label) {
    const element = document.createElement('option');
    element.value = value;
    element.textContent = label;
    return element;
  }

  function currentProvider() {
    return $('speechProvider')?.value || '';
  }

  function paintSwitch(enabled) {
    const toggle = $('autoSpeakToggle');
    if (!toggle) return;
    toggle.checked = Boolean(enabled);
    toggle.setAttribute('aria-checked', enabled ? 'true' : 'false');
    const label = toggle.closest('.switch')?.querySelector('[data-switch-label]');
    if (label) label.textContent = enabled ? t('Включено') : t('Выключено');
  }

  function fillProviders(status) {
    const select = $('speechProvider');
    if (!select) return;
    const current = settings.current.provider || '';
    select.replaceChildren(option('', t('Автоматически')));
    (status.providers || []).forEach((provider) => {
      const element = option(provider.id, provider.available
        ? provider.name
        : `${provider.name} — ${t('нужен ключ')}`);
      element.dataset.available = provider.available ? '1' : '0';
      select.appendChild(element);
    });
    select.value = current;
  }

  function selectedModel() {
    const id = $('speechModel')?.value || '';
    return settings.models.find((model) => model.id === id) || null;
  }

  function modelSuffix(model) {
    const parts = [];
    if (model.paid) parts.push(t('платно'));
    if (model.voices_count) parts.push(`${model.voices_count} ${t('голосов')}`);
    if (model.health === 'degraded') parts.push(t('нестабильна'));
    if (model.health === 'down') parts.push(t('недоступна'));
    if (model.max_input_chars && model.max_input_chars < 300) {
      parts.push(`≤${model.max_input_chars} ${t('симв. за запрос')}`);
    }
    return parts.length ? ` · ${parts.join(' · ')}` : '';
  }

  function fillModels() {
    const select = $('speechModel');
    if (!select) return;
    const provider = currentProvider();
    const current = settings.current.model || '';
    select.replaceChildren(option('', t('По умолчанию (лучшая доступная)')));
    const models = settings.models.filter((model) => !provider || model.provider === provider);
    models.forEach((model) => {
      const prefix = provider ? '' : `${model.provider_name} · `;
      const element = option(model.id, `${prefix}${model.name}${modelSuffix(model)}`);
      element.dataset.provider = model.provider;
      if (!model.available) {
        element.textContent += ` (${t('нужен ключ')})`;
      }
      select.appendChild(element);
    });
    if (!models.length) {
      select.appendChild(option('', t('Модели не найдены — нажмите «Обновить модели»')));
    }
    select.value = models.some((model) => model.id === current) ? current : '';
    paintModelInfo();
  }

  function fillVoices() {
    const select = $('speechVoice');
    if (!select) return;
    const model = selectedModel();
    const provider = currentProvider() || model?.provider || '';
    const current = settings.current.voice || '';
    const voices = model
      ? model.voices || []
      : (settings.status?.voices || []).filter((voice) => !provider || voice.provider === provider);
    select.replaceChildren(option('', t('По умолчанию')));
    voices.forEach((voice) => {
      const label = voice.label || voice.id;
      const element = option(voice.id, voice.default ? `${label} · ${t('по умолчанию')}` : label);
      element.disabled = voice.available === false;
      if (voice.available === false) element.textContent += ` (${t('недоступен')})`;
      select.appendChild(element);
    });
    select.value = voices.some((voice) => voice.id === current) ? current : '';
  }

  function paintModelInfo() {
    const box = $('speechModelInfo');
    if (!box) return;
    const model = selectedModel();
    if (!model) {
      box.textContent = settings.catalogError
        ? `${t('Каталог провайдера недоступен')}: ${settings.catalogError}`
        : t('Список моделей и голосов запрашивается у провайдера.');
      return;
    }
    const rows = [];
    rows.push(`<b>${escapeHtml(model.name)}</b> · <code>${escapeHtml(model.id)}</code>`);
    if (model.description) rows.push(escapeHtml(model.description));
    const facts = [
      model.paid ? t('платная модель') : t('бесплатно'),
      `${model.voices_count} ${t('голосов')}`,
      `${t('запрос до')} ${model.max_input_chars} ${t('символов')}`,
    ];
    if (model.endpoint) facts.push(`endpoint: /v1/${model.endpoint === 'chat' ? 'chat/completions' : 'audio/speech'}`);
    if (model.health) facts.push(`${t('состояние')}: ${model.health}`);
    rows.push(facts.join(' · '));
    if (model.paid) {
      rows.push(`<span class="warn">${t('Платная модель: каждый запрос расходует кредиты провайдера.')}</span>`);
    }
    if (!model.available) {
      rows.push(`<span class="warn">${t('Провайдер не настроен — добавьте ключ в .env.')}</span>`);
    }
    box.innerHTML = rows.join('<br>');
  }

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, (char) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;',
    }[char]));
  }

  function paintStatus(status) {
    settings.status = status;
    const dot = $('speechDot');
    const headline = $('speechHeadline');
    const details = $('speechDetails');
    const note = $('speechNote');
    const action = $('speechActionLink');
    if (!headline) return;
    const available = Boolean(status && status.available);
    if (dot) {
      dot.classList.toggle('ok', available);
      dot.classList.toggle('err', !available);
      dot.title = available ? t('TTS-провайдер подключён') : t('TTS-провайдер не настроен');
    }
    if (available) {
      headline.textContent = t('Озвучка готова: {name}')
        .replace('{name}', `${status.provider_name} · ${status.model_name || status.model}`);
      details.textContent = `${t('Голос')}: ${status.voice || t('по умолчанию')} · `
        + `${t('лимит')}: ${status.limits?.max_chars || '—'} ${t('символов за запрос')}`;
    } else {
      headline.textContent = t('TTS-провайдер не настроен');
      details.textContent = status?.error || t('Добавьте ключ провайдера, чтобы включить озвучку.');
    }
    if (action) {
      if (status?.hint || status?.action_url) {
        action.hidden = false;
        action.textContent = status?.action_url
          ? `${status.hint || t('Открыть настройки провайдера')} →`
          : (status.hint || '');
        action.href = status?.action_url || '#';
      } else {
        action.hidden = true;
        action.removeAttribute('href');
      }
    }
    if (note) {
      note.textContent = available
        ? t('Озвучка идёт через TTS-провайдера на сервере. Ключи в браузер не передаются.')
        : t('Пока провайдер не настроен, кнопка озвучки использует браузерный синтез речи (резерв) — это не модель.');
    }
    const toggle = $('autoSpeakToggle');
    if (toggle) {
      toggle.disabled = false;
      toggle.title = available
        ? t('Автоматически озвучивать ответы ИИ')
        : t('Провайдер не настроен: автоозвучка будет браузерной');
    }
    fillProviders(status || {});
    fillModels();
    fillVoices();
  }

  async function loadModels(refresh = false) {
    const provider = currentProvider();
    const query = new URLSearchParams();
    if (provider) query.set('provider', provider);
    if (refresh) query.set('refresh', '1');
    const button = $('refreshTtsModels');
    if (button && refresh) {
      button.disabled = true;
      button.textContent = t('Обновляю модели…');
    }
    try {
      const response = await fetch(`/api/tts/models${query.toString() ? `?${query}` : ''}`);
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      settings.models = data.models || [];
      settings.catalogError = data.catalog_error || '';
      paintModelsSummary();
      fillModels();
      fillVoices();
      return settings.models;
    } catch (error) {
      settings.models = [];
      settings.catalogError = error.message;
      paintModelsSummary();
      fillModels();
      fillVoices();
      return [];
    } finally {
      if (button) {
        button.disabled = false;
        button.textContent = t('↻ Обновить модели');
      }
    }
  }

  function paintModelsSummary() {
    const box = $('speechModelInfo');
    if (!box || settings.models.length) return;
    box.textContent = settings.catalogError
      ? `${t('Не удалось получить список моделей')}: ${settings.catalogError}`
      : t('Модели не найдены: выберите провайдера или добавьте ключ.');
  }

  async function loadStatus(refresh = false) {
    try {
      const response = await fetch(`/api/tts/status${refresh ? '?refresh=1' : ''}`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const status = await response.json();
      settings.current = status.settings || {};
      settings.lang = settings.current.lang || '';
      paintSwitch(settings.current.auto_speak);
      if ($('readCodeToggle')) $('readCodeToggle').checked = Boolean(settings.current.read_code);
      if ($('speechLang')) $('speechLang').value = settings.lang;
      paintStatus(status);
      // Статус приходит без сети, а список моделей — с каталогом провайдера.
      await loadModels(refresh);
      if (window.NovaSpeech && refresh) window.NovaSpeech.loadStatus(true);
      return status;
    } catch (error) {
      const headline = $('speechHeadline');
      if (headline) headline.textContent = t('Не удалось проверить TTS: {error}')
        .replace('{error}', error.message);
      const dot = $('speechDot');
      if (dot) { dot.classList.add('err'); dot.classList.remove('ok'); }
      return null;
    }
  }

  async function save(patch, message = 'Настройки озвучки сохранены', confirmPaid = false) {
    const body = { ...patch };
    if (confirmPaid) body.confirm_paid = true;
    try {
      const response = await fetch('/api/tts/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      settings.current = data.settings || { ...settings.current, ...patch };
      // Клиент держит ту же копию: /api/tts/settings — источник истины.
      if (window.NovaSpeech) {
        window.NovaSpeech.loadSettings(true).catch(() => {});
      }
      if (message) toast(message);
      paintSwitch(settings.current.auto_speak);
      return settings.current;
    } catch (error) {
      if (!confirmPaid && /платн/i.test(error.message)
          && window.confirm(`${error.message}\n\n${t('Всё равно выбрать эту модель?')}`)) {
        return save(patch, message, true);
      }
      toast(error.message, 'error');
      return null;
    }
  }

  async function playSample() {
    const text = t('Привет! Это пример озвучки ответов в NovaMind. Голос можно выбрать в настройках.');
    const button = $('testSpeech');
    if (button) { button.disabled = true; button.textContent = t('Готовлю аудио…'); }
    try {
      const response = await fetch('/api/tts/speech', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          text,
          voice: settings.current.voice || '',
          provider: settings.current.provider || '',
          model: settings.current.model || '',
          read_code: Boolean(settings.current.read_code),
          lang: settings.current.lang || '',
        }),
      });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.error || `HTTP ${response.status}`);
      }
      const raw = await response.text();
      const events = raw.split('\n').filter(Boolean).map((line) => {
        try { return JSON.parse(line); } catch (_) { return null; }
      }).filter(Boolean);
      const chunks = events.filter((event) => event.type === 'chunk');
      const failure = events.find((event) => event.type === 'error');
      if (!chunks.length) {
        const hint = failure?.hint ? ` — ${failure.hint}` : '';
        const link = failure?.action_url ? ` ${failure.action_url}` : '';
        if (failure?.action_url && $('speechActionLink')) {
          $('speechActionLink').hidden = false;
          $('speechActionLink').href = failure.action_url;
          $('speechActionLink').textContent = `${failure.hint || t('Что сделать')} →`;
        }
        throw new Error(`${failure?.error || t('Провайдер не вернул аудио')}${hint}${link}`);
      }
      const audio = new Audio(chunks[0].url);
      await audio.play().catch(() => {});
      const first = events.find((event) => event.type === 'start') || {};
      toast(`${t('Озвучка работает')}: ${first.model || first.provider || 'TTS'}`);
    } catch (error) {
      toast(error.message, 'error');
    } finally {
      if (button) { button.disabled = false; button.textContent = t('▶ Прослушать пример'); }
    }
  }

  function init() {
    if (!$('speechSectionOwner') && !document.getElementById('speech-section')) return;
    loadStatus();
    $('refreshSpeech')?.addEventListener('click', () => loadStatus(true));
    $('refreshTtsModels')?.addEventListener('click', () => {
      loadModels(true).then((models) => {
        if (models.length) toast(`${t('Модели обновлены')}: ${models.length}`);
      });
    });
    $('autoSpeakToggle')?.addEventListener('change', (event) => {
      const enabled = Boolean(event.target.checked);
      paintSwitch(enabled);
      save({ auto_speak: enabled },
        enabled ? t('Автоозвучка включена') : t('Автоозвучка выключена'));
    });
    $('speechProvider')?.addEventListener('change', (event) => {
      settings.current.provider = event.target.value;
      settings.current.model = '';
      settings.current.voice = '';
      loadModels(false);
    });
    $('speechModel')?.addEventListener('change', () => {
      const model = selectedModel();
      if (model) {
        // Модель из общего списка «Автоматически» сама задаёт провайдера.
        if (!currentProvider() && $('speechProvider')) {
          $('speechProvider').value = model.provider;
          settings.current.provider = model.provider;
        }
        settings.current.model = model.id;
      } else {
        settings.current.model = '';
      }
      paintModelInfo();
      fillVoices();
    });
    $('saveSpeech')?.addEventListener('click', () => {
      const model = selectedModel();
      const provider = currentProvider() || model?.provider || '';
      save({
        provider,
        model: $('speechModel')?.value || '',
        voice: $('speechVoice')?.value || '',
        lang: $('speechLang')?.value || '',
        read_code: Boolean($('readCodeToggle')?.checked),
      }).then((saved) => {
        if (saved) loadStatus();
      });
    });
    $('testSpeech')?.addEventListener('click', playSample);
    $('stopSpeech')?.addEventListener('click', () => {
      if (window.NovaSpeech) window.NovaSpeech.stop('settings');
      if (typeof window.speechSynthesis !== 'undefined') {
        try { window.speechSynthesis.cancel(); } catch (_) { /* нет поддержки */ }
      }
      toast(t('Озвучка остановлена'));
    });
    if (window.NovaSpeech) window.NovaSpeech.loadSettings().catch(() => {});
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
