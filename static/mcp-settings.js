(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
  const toast = (message, type = '') => window.showToast ? window.showToast(message, type) : alert(message);
  const authLabel = (auth) => Array.isArray(auth) ? auth.join(', ') : String(auth || 'none');
  function card(item, connected = false) {
    const auth = authLabel(item.auth);
    return `<article class="model-card"><div class="model-card-top"><span class="model-badge ${connected ? 'free' : 'paid'}">${connected ? 'CONNECTED' : 'AVAILABLE'}</span><span class="model-id">${esc(auth)}</span></div><h3>${esc(item.name)}</h3><p>${esc(item.description || item.url || '')}</p>${item.url ? `<p><code>${esc(item.url)}</code></p>` : ''}<div class="model-stats"><span>Авторизация <b>${esc(auth)}</b></span><span>Статус <b>${connected ? 'доступен' : 'не подключён'}</b></span></div>${item.id && connected ? `<button class="select-model" data-mcp-test="${esc(item.id)}" type="button">Проверить tools/list</button>` : ''}</article>`;
  }
  async function loadMcp() {
    const response = await fetch('/api/settings/mcp');
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Не удалось загрузить MCP-каталог');
    const grid = $('mcpGrid');
    const catalog = (data.catalog || []).map(item => card(item)).join('');
    const connections = (data.connections || []).map(item => card({ ...item, auth: item.auth, description: item.last_error || 'Подключённый MCP-сервер' }, item.connected)).join('');
    grid.innerHTML = connections + catalog || '<div class="empty-card">Каталог пуст</div>';
    const connectedCount = (data.connections || []).filter(item => item.connected).length;
    $('mcpStatus').textContent = `${connectedCount} подключений · ${(data.catalog || []).length} шаблонов`;
    grid.querySelectorAll('[data-mcp-test]').forEach(button => button.addEventListener('click', async () => {
      const result = await fetch(`/api/settings/mcp/${encodeURIComponent(button.dataset.mcpTest)}/tools`);
      const body = await result.json();
      toast(result.ok ? `MCP tools/list: ${body.result?.tools?.length || 0} инструментов` : (body.error || 'MCP недоступен'), result.ok ? '' : 'error');
    }));
  }
  $('refreshMcp')?.addEventListener('click', () => loadMcp().catch(error => toast(error.message, 'error')));
  $('connectMcp')?.addEventListener('click', async () => {
    const url = $('mcpUrl').value.trim();
    if (!url) return toast('Укажите URL MCP-сервера', 'error');
    const auth = $('mcpAuth').value;
    const payload = { name: $('mcpName').value.trim() || 'MCP server', url, auth };
    if (auth === 'api_key') payload.api_key = $('mcpSecret').value;
    else if (auth === 'basic') { payload.username = $('mcpName').value.trim(); payload.password = $('mcpSecret').value; }
    else if (auth !== 'none') payload.token = $('mcpSecret').value;
    const button = $('connectMcp'); button.disabled = true;
    try {
      const response = await fetch('/api/settings/mcp/connect', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'MCP-сервер не ответил на initialize');
      $('mcpSecret').value = '';
      toast('MCP-сервер подключён и прошёл initialize');
      await loadMcp();
    } catch (error) { toast(error.message, 'error'); }
    finally { button.disabled = false; }
  });
  loadMcp().catch(error => { if ($('mcpStatus')) $('mcpStatus').textContent = error.message; });
})();
