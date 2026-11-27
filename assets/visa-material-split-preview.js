(function () {
  'use strict';
  function previewUrl(value, origin, page) {
    try {
      if (!value) return null;
      const url = new URL(value, origin);
      if (url.origin !== origin || !/^https?:$/.test(url.protocol) || url.username || url.password || !/^\/documents\/\d+\/preview$/.test(url.pathname)) return null;
      url.searchParams.set('size', 'large');
      url.searchParams.set('page', String(Math.max(1, Math.floor(Number(page) || 1))));
      return url.href;
    } catch (_) { return null; }
  }
  function pageTotal(value) {
    const count = Number.parseInt(value, 10);
    return Number.isFinite(count) && count > 0 ? Math.min(count, 1000) : 1;
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = { previewUrl, pageTotal };
  if (typeof document === 'undefined') return;
  const bound = new Map();
  function initialize() {
    for (const [panel, cleanup] of bound) if (!panel.isConnected || panel.closest('[hidden]')) { cleanup(); bound.delete(panel); }
    document.querySelectorAll('[data-visa-material-workspace]').forEach(panel => {
      const view = panel.querySelector('[data-material-preview]');
      if (!view || bound.has(panel) || view.dataset.splitPreviewBound || panel.closest('[hidden]')) return;
      view.dataset.splitPreviewBound = 'true';
      panel.dataset.splitPreviewActive = 'true';
      const one = selector => view.querySelector(selector);
      const rows = Array.from(panel.querySelectorAll('[data-material-item]'));
      const image = one('[data-material-preview-image]');
      const message = one('[data-material-preview-message]');
      const previous = one('[data-material-preview-prev]');
      const next = one('[data-material-preview-next]');
      const full = one('[data-material-preview-fullscreen]');
      const download = one('[data-material-preview-download]');
      const retry = one('[data-material-preview-retry]');
      const storageKey = 'aaie.visa.split-preview:' + location.pathname + ':' + panel.id;
      let stored = null;
      try { stored = JSON.parse(sessionStorage.getItem(storageKey) || 'null'); } catch (_) { /* Optional view state. */ }
      let selected = null, selectedButton = null, current = 1, requested = 1, total = 1, controller = null, generation = 0, objectUrl = '', loading = false;
      function remember(page) {
        try { sessionStorage.setItem(storageKey, JSON.stringify({ item: selected?.dataset.materialItem, source: selectedButton?.dataset.previewSrc || '', page })); } catch (_) { /* Optional view state. */ }
      }
      function clearImage() {
        image.hidden = true; image.removeAttribute('src');
        if (objectUrl) URL.revokeObjectURL(objectUrl);
        objectUrl = '';
      }
      function cancel() { generation++; if (controller) controller.abort(); controller = null; }
      function controls() {
        previous.disabled = loading || !selectedButton || current <= 1;
        next.disabled = loading || !selectedButton || current >= total;
        one('[data-material-preview-page]').textContent = selectedButton ? current + ' / ' + total : '— / —';
      }
      async function load(page) {
        requested = Math.max(1, Math.floor(Number(page) || 1));
        cancel(); clearImage(); retry.hidden = true;
        const url = selectedButton && previewUrl(selectedButton.dataset.previewSrc, location.origin, page);
        if (!url) { loading = false; controls(); return; }
        remember(page);
        const version = generation; controller = new AbortController(); loading = true;
        message.hidden = false; message.textContent = '正在加载原件…'; controls();
        try {
          const response = await fetch(url, { credentials: 'same-origin', redirect: 'error', signal: controller.signal });
          if (!response.ok || !String(response.headers.get('Content-Type') || '').startsWith('image/')) throw new Error('unavailable');
          const status = response.headers.get('X-Preview-Status') || 'ok';
          if (status !== 'ok') throw new Error('unavailable');
          const blob = await response.blob();
          if (version !== generation || !panel.isConnected) return;
          total = pageTotal(response.headers.get('X-Preview-Page-Count'));
          current = Math.min(Math.max(1, page), total);
          objectUrl = URL.createObjectURL(blob); image.src = objectUrl; image.hidden = false;
          message.hidden = true;
        } catch (error) {
          if (version !== generation || error.name === 'AbortError') return;
          message.hidden = false; message.textContent = '原件预览暂不可用，请重试，或使用全屏预览与下载。'; retry.hidden = false;
        } finally {
          if (version === generation) { loading = false; controls(); }
        }
      }
      function select(row, button, startPage = 1) {
        button = button || row.querySelector('[data-preview-open][data-preview-src]');
        if (selected === row && selectedButton === button) return;
        cancel(); clearImage(); selected = row; selectedButton = null; loading = false; current = total = 1;
        rows.forEach(item => {
          item.classList.toggle('is-preview-selected', item === row);
          const choose = item.querySelector('[data-material-select]');
          if (choose) choose.setAttribute('aria-pressed', String(item === row));
        });
        const name = row.querySelector('[data-search-label]').textContent;
        one('[data-material-preview-title]').textContent = button ? button.dataset.previewTitle || name : name;
        const status = row.querySelector('.visa-material-item-status select');
        one('[data-material-preview-context]').textContent = name + (status ? ' · ' + status.options[status.selectedIndex].text : '');
        full.hidden = download.hidden = retry.hidden = true;
        if (button && previewUrl(button.dataset.previewSrc, location.origin, 1)) {
          selectedButton = button; full.hidden = false;
          full.dataset.previewSrc = button.dataset.previewSrc;
          full.dataset.previewTitle = button.dataset.previewTitle || name;
          full.dataset.downloadUrl = button.dataset.downloadUrl || '';
          try {
            const target = new URL(button.dataset.downloadUrl || '', location.origin);
            if (target.origin === location.origin && /^\/documents\/\d+\/download$/.test(target.pathname)) {
              download.href = target.href; download.hidden = false;
            }
          } catch (_) { /* Preserve existing download controls in the material. */ }
          load(startPage);
        } else {
          remember(1); message.hidden = false; message.textContent = '此材料暂无可并排预览的文件。可使用左侧上传、文件与备注或原有查看入口。'; controls();
        }
      }
      function click(event) {
        const row = event.target.closest('[data-material-item]');
        if (!row || !panel.contains(row)) return;
        const preview = event.target.closest('[data-preview-open]');
        if (preview) { event.preventDefault(); event.stopPropagation(); select(row, preview); }
        else if (event.target.closest('[data-material-select]')) { event.preventDefault(); select(row); }
      }
      panel.addEventListener('click', click, true);
      const changed = event => {
        if (selected && selected.contains(event.target)) {
          const status = selected.querySelector('.visa-material-item-status select');
          one('[data-material-preview-context]').textContent = selected.querySelector('[data-search-label]').textContent + (status ? ' · ' + status.options[status.selectedIndex].text : '');
        }
      };
      panel.addEventListener('change', changed);
      previous.addEventListener('click', () => load(current - 1));
      next.addEventListener('click', () => load(current + 1));
      retry.addEventListener('click', () => load(requested));
      const observer = new MutationObserver(() => {
        if (selected && !selected.hidden) return;
        const visible = rows.filter(row => !row.hidden && !row.closest('[data-material-category][hidden]'));
        const row = visible.find(item => item.querySelector('[data-preview-open][data-preview-src]')) || visible[0];
        if (row) select(row);
        else { cancel(); clearImage(); selected = selectedButton = null; full.hidden = download.hidden = retry.hidden = true; loading = false; message.hidden = false; message.textContent = '没有匹配材料，请清除搜索或切换筛选。'; controls(); }
      });
      rows.forEach(row => observer.observe(row, { attributes: true, attributeFilter: ['hidden'] }));
      panel.querySelectorAll('[data-material-category]').forEach(group => observer.observe(group, { attributes: true, attributeFilter: ['hidden'] }));
      const restored = stored && rows.find(row => !row.hidden && row.dataset.materialItem === String(stored.item));
      const initial = restored || rows.find(row => !row.hidden && row.querySelector('[data-preview-open][data-preview-src]')) || rows.find(row => !row.hidden);
      if (initial) {
        const button = restored && Array.from(restored.querySelectorAll('[data-preview-open][data-preview-src]')).find(item => item.dataset.previewSrc === stored.source);
        select(initial, button || undefined, button ? pageTotal(stored.page) : 1);
      }
      bound.set(panel, () => { observer.disconnect(); cancel(); clearImage(); panel.removeEventListener('click', click, true); panel.removeEventListener('change', changed); delete view.dataset.splitPreviewBound; delete panel.dataset.splitPreviewActive; });
    });
  }
  initialize(); document.addEventListener('DOMContentLoaded', initialize);
  document.addEventListener('aaie:business-fragment-rendered', initialize);
})();
