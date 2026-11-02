(() => {
  const panel = document.getElementById('agentWorkspace');
  const list = document.getElementById('workspaceFiles');
  const preview = document.getElementById('workspacePreview');
  const zip = document.getElementById('workspaceZip');
  let selectedChat = '';
  const query = () => `?chat_id=${encodeURIComponent(selectedChat)}`;
  const status = (message) => {
    const node = document.getElementById('workspaceGitStatus');
    if (node) node.textContent = message;
  };
  const getChatId = async () => {
    try {
      const store = typeof historyLoad === 'function' ? historyLoad() : null;
      const chat = store?.chats?.find((item) => item.id === store.activeId);
      if (chat?.serverId) return chat.serverId;
      const response = await fetch('/api/history');
      const data = await response.json();
      return data.chat_id || '';
    } catch (_) { return ''; }
  };
  window.toggleWorkspace = async (open) => {
    panel?.classList.toggle('open', Boolean(open));
    panel?.setAttribute('aria-hidden', String(!open));
    if (!open) return;
    selectedChat = await getChatId();
    if (!selectedChat) { list.textContent = 'Создайте или откройте чат.'; return; }
    const query = `?chat_id=${encodeURIComponent(selectedChat)}`;
    zip.href = `/api/agent/workspace/zip${query}`;
    list.textContent = 'Загружаю…';
    try {
      const response = await fetch(`/api/agent/workspace/tree${query}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Не удалось загрузить файлы');
      list.replaceChildren();
      if (!data.files?.length) { list.textContent = 'В папке пока нет файлов.'; return; }
      for (const item of data.files) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'workspace-file';
        button.textContent = `${item.type === 'directory' ? '📁' : '📄'} ${item.path}`;
        button.disabled = item.type !== 'file';
        button.addEventListener('click', async () => {
          const file = await fetch(`/api/agent/workspace/file${query}&path=${encodeURIComponent(item.path)}`);
          const body = await file.json();
          preview.hidden = false;
          preview.textContent = file.ok ? body.content : (body.error || 'Ошибка чтения');
        });
        list.append(button);
      }
    } catch (error) { list.textContent = error.message || 'Ошибка загрузки workspace.'; }
  };
  window.showWorkspaceTab = (tab) => {
    const files = document.getElementById('workspaceTabFiles');
    const diff = document.getElementById('workspaceTabDiff');
    if (files) files.hidden = tab !== 'files';
    if (diff) diff.hidden = tab !== 'diff';
    if (tab === 'diff') window.refreshWorkspaceDiff();
  };
  window.refreshWorkspaceDiff = async () => {
    if (!selectedChat) selectedChat = await getChatId();
    try {
      const response = await fetch(`/api/agent/github/diff${query()}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Нет рабочей копии GitHub');
      document.getElementById('workspaceDiff').textContent = data.diff || 'Рабочее дерево чистое.';
      status(`Ветка ${data.branch}${data.status ? ` · ${data.status}` : ' · чисто'}`);
    } catch (error) { status(error.message || 'Копия репозитория пока не выбрана.'); }
  };
  document.getElementById('githubCloneForm')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!selectedChat) selectedChat = await getChatId();
    const repository = document.getElementById('githubRepository').value.trim();
    const runId = (window.crypto?.randomUUID?.() || `run-${Date.now()}`).replace(/[^A-Za-z0-9_-]/g, '');
    status('Клонирую репозиторий…');
    try {
      const response = await fetch(`/api/agent/github/clone${query()}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ repository, run_id: runId }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Не удалось склонировать репозиторий');
      status(`Копия готова в ветке ${data.branch}`);
      await window.refreshWorkspaceDiff();
    } catch (error) { status(error.message || 'Ошибка клонирования'); }
  });
  window.commitWorkspaceChanges = async () => {
    const message = window.prompt('Сообщение коммита', 'Изменения NovaMind');
    if (message === null) return;
    try {
      const response = await fetch(`/api/agent/github/commit${query()}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Коммит не создан');
      status(`Коммит ${data.commit.slice(0, 10)} в ${data.branch}`);
      await window.refreshWorkspaceDiff();
    } catch (error) { status(error.message || 'Ошибка коммита'); }
  };
  window.publishWorkspaceChanges = async () => {
    if (!window.confirm('Отправить ветку novamind в GitHub и создать Pull Request?')) return;
    try {
      const response = await fetch(`/api/agent/github/publish${query()}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirmed: true, base: 'main' }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Публикация запрещена');
      status(data.pull_request || data.pull_request_error || `Отправлена ветка ${data.branch}`);
    } catch (error) { status(error.message || 'Ошибка публикации'); }
  };
})();
