(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
  const toast = (message, type='') => window.showToast ? window.showToast(message, type) : alert(message);

  function card(skill) {
    return `<article class="model-card skill-card">
      <div class="model-card-top"><span class="model-badge free">SKILL</span><span class="model-id">${esc(skill.id)}</span></div>
      <h3>${esc(skill.name || skill.id)}</h3>
      <p>${esc(skill.description || '')}</p>
      ${skill.invalid ? '<p class="error-text">Skill повреждён или имеет неверный формат.</p>' : ''}
      <button class="select-model skill-delete" type="button" data-skill-delete="${esc(skill.id)}">Удалить</button>
    </article>`;
  }
  async function loadSkills() {
    const grid = $('skillsGrid');
    if (!grid) return;
    $('skillsStatus').textContent = 'Загружаю сохранённые skills…';
    const response = await fetch('/api/settings/skills');
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Не удалось загрузить skills');
    const skills = data.skills || [];
    $('skillsStatus').textContent = `${skills.length} сохранённых skill${skills.length === 1 ? '' : 's'}`;
    grid.innerHTML = skills.length ? skills.map(card).join('') : '<div class="empty-card">Skills ещё не добавлены</div>';
    grid.querySelectorAll('[data-skill-delete]').forEach(btn => btn.addEventListener('click', async () => {
      if (!confirm(`Удалить skill «${btn.dataset.skillDelete}»?`)) return;
      const response = await fetch('/api/settings/skills/' + encodeURIComponent(btn.dataset.skillDelete), {method:'DELETE'});
      const data = await response.json();
      if (!response.ok) return toast(data.error || 'Не удалось удалить skill', 'error');
      toast('Skill удалён');
      loadSkills().catch(e => toast(e.message, 'error'));
    }));
  }
  async function uploadZip() {
    const file = $('skillZip')?.files?.[0];
    if (!file) return toast('Выберите ZIP-архив', 'error');
    const body = new FormData(); body.append('file', file);
    const response = await fetch('/api/settings/skills/zip', {method:'POST', body});
    const data = await response.json();
    if (!response.ok) return toast(data.error || 'ZIP не принят', 'error');
    toast(`Skill «${data.skill.name}» сохранён`); $('skillZip').value = ''; loadSkills();
  }
  async function saveText() {
    const response = await fetch('/api/settings/skills/text', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({id:$('skillTextName').value, content:$('skillTextContent').value})});
    const data = await response.json();
    if (!response.ok) return toast(data.error || 'Текст skill не принят', 'error');
    toast(`Skill «${data.skill.name}» сохранён`); $('skillTextName').value = ''; $('skillTextContent').value = ''; loadSkills();
  }
  function init() {
    if (!$('skillsGrid')) return;
    $('refreshSkills')?.addEventListener('click', () => loadSkills().catch(e => toast(e.message, 'error')));
    $('uploadSkillZip')?.addEventListener('click', () => uploadZip().catch(e => toast(e.message, 'error')));
    $('saveSkillText')?.addEventListener('click', () => saveText().catch(e => toast(e.message, 'error')));
    document.querySelectorAll('[data-skill-tab]').forEach(tab => tab.addEventListener('click', () => {
      document.querySelectorAll('[data-skill-tab]').forEach(x => x.classList.toggle('active', x === tab));
      $('skillZipPanel').hidden = tab.dataset.skillTab !== 'zip';
      $('skillTextPanel').hidden = tab.dataset.skillTab !== 'text';
    }));
    loadSkills().catch(e => { $('skillsStatus').textContent = e.message; toast(e.message, 'error'); });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
