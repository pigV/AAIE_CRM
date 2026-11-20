(function () {
  'use strict';
  function matchesMaterial(item, state) {
    if (state.category && item.category !== state.category) return false;
    if (state.query && !item.text.toLocaleLowerCase().includes(state.query.trim().toLocaleLowerCase())) return false;
    const filter = state.filter || 'all';
    if (filter === 'all') return true;
    if (filter === 'unfinished') return item.status === 'missing';
    if (filter === 'required_unfinished') return item.required === 'required' && item.status === 'missing';
    if (['required', 'optional', 'conditional'].includes(filter)) return item.required === filter;
    return item.status === filter;
  }
  function materialCounts(items) {
    return {
      all: items.length,
      required_unfinished: items.filter(i => i.required === 'required' && i.status === 'missing').length,
      unfinished: items.filter(i => i.status === 'missing').length,
      uploaded: items.filter(i => i.status === 'uploaded').length,
      not_applicable: items.filter(i => i.status === 'not_applicable').length
    };
  }
  function materialVisible(item, state, context) {
    return Boolean(context.dirty || context.recent || matchesMaterial(item, state));
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = { matchesMaterial, materialCounts, materialVisible };
  if (typeof document === 'undefined') return;

  const lifecycleCleanup = new Map();
  function initialize(restorePosition = true) {
    for (const [panel, cleanup] of lifecycleCleanup) if (!panel.isConnected) { cleanup(); lifecycleCleanup.delete(panel); }
  document.querySelectorAll('[data-visa-material-workspace]').forEach(panel => {
    if (panel.dataset.workspaceBound) return;
    panel.dataset.workspaceBound = 'true';
    const remove = [];
    const listenWindow = (type, listener) => { window.addEventListener(type, listener); remove.push(() => window.removeEventListener(type, listener)); };
    lifecycleCleanup.set(panel, () => remove.forEach(cleanup => cleanup()));
    const one = selector => panel.querySelector(selector);
    const all = selector => Array.from(panel.querySelectorAll(selector));
    const rows = all('[data-material-item]');
    const input = one('[data-material-search]');
    const filterSelect = one('[data-material-filter]');
    const categorySelect = one('[data-category-mobile]');
    const saveForm = one('[data-material-save]');
    saveForm.dataset.businessSyncManaged = '1';
    const saveButton = one('[data-material-save-button]');
    const message = one('[data-material-save-status]');
    const initial = new Map();
    const dirty = new Set();
    let category = '', submitting = false, lastItem = '';
    const returnUrl = new URL(location.href);
    let recentItem = ['success', 'ok'].includes(returnUrl.searchParams.get('kind')) ? location.hash.slice(1) : '';
    const key = 'aaie.material-workspace.v1:' + location.pathname + ':' + panel.id;
    const originalNames = new Map(rows.map(row => [row, row.querySelector('[data-search-label]').textContent]));
    const fields = rows.flatMap(row => Array.from(row.querySelectorAll('select, textarea')).filter(field => field.form === saveForm));
    fields.forEach(field => initial.set(field, field.value));
    const stored = (() => { try { return JSON.parse(sessionStorage.getItem(key) || 'null'); } catch (_) { return null; } })();
    const allowedFilters = new Set(Array.from(filterSelect.options).map(o => o.value));
    if (stored && typeof stored === 'object') {
      lastItem = typeof stored.anchor === 'string' ? stored.anchor : '';
      input.value = typeof stored.query === 'string' ? stored.query.slice(0, 200) : '';
      const urlFilter = new URL(location.href).searchParams.get('material_filter');
      if (!urlFilter && allowedFilters.has(stored.filter)) filterSelect.value = stored.filter;
      category = typeof stored.category === 'string' && all('[data-category-choice]').some(b => b.dataset.categoryChoice === stored.category) ? stored.category : '';
      if (restorePosition && Array.isArray(stored.open)) stored.open.forEach(id => {
        const details = all('[data-material-details]').find(d => d.dataset.materialDetails === String(id));
        if (details) details.open = true;
      });
      if (restorePosition && Array.isArray(stored.collapsed)) all('[data-material-category]').forEach(section => {
        section.open = !stored.collapsed.includes(section.dataset.materialCategory);
      });
    }
    function record(row) {
      return { category: row.dataset.categoryIndex, status: row.querySelector('select').value,
        required: row.dataset.required, text: row.dataset.search };
    }
    function remember(resume) {
      const target = rows.find(row => row.id === lastItem);
      const view = { query: input.value.slice(0, 200), filter: filterSelect.value, category,
        open: all('[data-material-details][open]').map(d => d.dataset.materialDetails),
        collapsed: all('[data-material-category]').filter(d => !d.open).map(d => d.dataset.materialCategory),
        scroll: window.scrollY, anchor: lastItem, offset: target ? target.getBoundingClientRect().top : null, resume: Boolean(resume) };
      try { sessionStorage.setItem(key, JSON.stringify(view)); } catch (_) { /* Storage may be disabled. */ }
    }
    function highlight(row) {
      const label = row.querySelector('[data-search-label]');
      const name = originalNames.get(row), query = input.value.trim();
      const index = query ? name.toLocaleLowerCase().indexOf(query.toLocaleLowerCase()) : -1;
      label.replaceChildren();
      if (index < 0) { label.textContent = name; return; }
      const mark = document.createElement('mark'); mark.textContent = name.slice(index, index + query.length);
      label.append(document.createTextNode(name.slice(0, index)), mark, document.createTextNode(name.slice(index + query.length)));
    }
    function render() {
      if (dirty.size) saveForm.dataset.businessSyncDirty = '1'; else delete saveForm.dataset.businessSyncDirty;
      const state = { query: input.value, category, filter: filterSelect.value };
      let visible = 0, pinned = 0;
      rows.forEach(row => {
        const item = record(row);
        row.dataset.status = item.status;
        const matches = matchesMaterial(item, state);
        // Never make the row being edited disappear during a status change.
        const recent = row.id === recentItem;
        const keep = dirty.has(row) && !matches;
        row.hidden = !materialVisible(item, state, {dirty:dirty.has(row), recent});
        row.classList.toggle('is-dirty', dirty.has(row));
        row.classList.toggle('is-located', recent);
        let badge = row.querySelector('[data-material-recent]');
        if (!badge) {
          badge = document.createElement('small'); badge.dataset.materialRecent = '';
          badge.className = 'visa-material-recent'; badge.textContent = '本次操作已保存';
          row.querySelector('.visa-material-item-meta').append(badge);
        }
        badge.hidden = !recent;
        if (!row.hidden) visible++;
        if (keep) pinned++;
        highlight(row);
      });
      const counts = materialCounts(rows.map(record));
      all('[data-quick-filter]').forEach(link => {
        const count = counts[link.dataset.quickFilter];
        link.dataset.count = String(count);
        link.querySelector('[data-filter-count]').textContent = String(count);
        link.setAttribute('aria-current', String(link.dataset.quickFilter === filterSelect.value));
      });
      all('[data-material-category]').forEach(section => {
        const group = Array.from(section.querySelectorAll('[data-material-item]'));
        section.hidden = !group.some(row => !row.hidden);
        if (!section.hidden && (input.value.trim() || category || group.some(row => row.id === recentItem))) section.open = true;
        const completed = group.filter(row => record(row).status !== 'missing').length;
        const index = section.dataset.materialCategory;
        one('[data-category-count="' + index + '"]').textContent = completed + ' / ' + group.length;
        one('[data-group-progress="' + index + '"]').textContent = completed + ' / ' + group.length + ' 已完成';
      });
      all('[data-category-choice]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.categoryChoice === category)));
      categorySelect.value = category;
      one('[data-material-matches]').textContent = '显示 ' + visible + ' / ' + rows.length + ' 项' + (pinned ? '（含 ' + pinned + ' 项未保存修改）' : '');
      one('[data-material-empty]').hidden = visible !== 0;
      all('input[name="material_filter"]').forEach(field => { field.value = filterSelect.value; });
      saveButton.disabled = dirty.size === 0 || submitting;
      message.textContent = submitting ? '正在保存，请稍候…' : dirty.size ? dirty.size + ' 项有未保存修改' : '没有未保存修改';
      panel.dataset.materialDirty = String(dirty.size > 0);
    }
    function edited(field) {
      const row = field.closest('[data-material-item]');
      if (!row || !initial.has(field)) return;
      const changed = fields.filter(f => f.closest('[data-material-item]') === row).some(f => initial.get(f) !== f.value);
      if (changed) dirty.add(row); else dirty.delete(row);
      lastItem = row.id;
      render(); remember(false);
    }
    panel.addEventListener('input', event => {
      if (event.target === input) { recentItem = ''; render(); remember(false); }
      else if (event.target.matches('textarea')) edited(event.target);
    });
    panel.addEventListener('change', event => {
      if (event.target === categorySelect) { recentItem = ''; category = categorySelect.value; render(); remember(false); }
      else if (event.target === filterSelect) { recentItem = ''; render(); remember(false); }
      else if (event.target.matches('select')) edited(event.target);
    });
    panel.addEventListener('focusin', event => {
      const row = event.target.closest('[data-material-item]');
      if (row) lastItem = row.id;
    });
    panel.addEventListener('click', event => {
      const link = event.target.closest('[data-quick-filter]');
      if (link && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) {
        event.preventDefault(); recentItem = ''; filterSelect.value = link.dataset.quickFilter; render(); remember(false);
      }
      const button = event.target.closest('[data-category-choice]');
      if (button) { recentItem = ''; category = button.dataset.categoryChoice; render(); remember(false); }
      if (event.target.closest('[data-material-clear]')) {
        recentItem = ''; input.value = ''; filterSelect.value = 'all'; category = ''; render(); remember(false); input.focus();
      }
      const show = event.target.closest('[data-open-files]');
      if (show) {
        const details = all('[data-material-details]').find(d => d.dataset.materialDetails === show.dataset.openFiles);
        details.open = !details.open; remember(false);
      }
      if (event.target.closest('[data-material-resume]')) remember(true);
    });
    all('[data-material-details]').forEach(details => details.addEventListener('toggle', () => remember(false)));
    all('[data-material-category]').forEach(details => details.addEventListener('toggle', () => remember(false)));
    panel.addEventListener('submit', event => {
      if (event.defaultPrevented) return;
      if (event.target !== saveForm && dirty.size) {
        event.preventDefault();
        message.textContent = '请先保存材料状态和备注，再进行其他操作。';
        saveButton.focus(); return;
      }
      if (event.target === saveForm) {
        if (submitting) { event.preventDefault(); return; }
        const target = rows.find(row => row.id === lastItem && !row.hidden);
        saveForm.querySelector('[name="return_anchor"]').value = target ? target.id : panel.id;
      }
      remember(true); submitting = true;
      if (event.target === saveForm) render();
      if (event.target === saveForm && saveForm.elements.namedItem('business_command_key') && window.AAIEBusinessChanges?.submitChecklist) {
        event.preventDefault();
        window.AAIEBusinessChanges.submitChecklist(saveForm).then(() => {
          fields.forEach(field => initial.set(field, field.value)); dirty.clear();
          recentItem = lastItem; submitting = false; render();
          window.dispatchEvent(new Event('aaie:business-change'));
        }).catch(error => {
          submitting = false; render(); message.textContent = error.message;
        });
      }
    });
    listenWindow('beforeunload', event => {
      if (dirty.size && !submitting) { event.preventDefault(); event.returnValue = ''; }
    });
    listenWindow('pagehide', () => remember(true));
    listenWindow('pageshow', event => { if (event.persisted) { submitting = false; render(); } });
    render();
    if (restorePosition && stored && stored.resume && !panel.closest('[hidden]')) {
      requestAnimationFrame(() => {
        const target = rows.find(row => row.id === stored.anchor && !row.hidden);
        if (target && Number.isFinite(stored.offset)) {
          window.scrollBy(0, target.getBoundingClientRect().top - stored.offset);
          target.classList.add('is-located');
        } else if (Number.isFinite(stored.scroll)) window.scrollTo(0, Math.max(0, stored.scroll));
        remember(false);
      });
    }
  });
  }
  initialize();
  document.addEventListener('aaie:business-fragment-rendered', () => initialize(false));
})();
