(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.AAIEBusinessChanges = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  async function fetchSnapshotPacket(fetcher, url, {signal}) {
    const response = await fetcher(url, {credentials:'same-origin', cache:'no-store', signal, headers:{Accept:'application/json'}});
    if (response.redirected || !response.ok) {
      if (!response.redirected && response.status === 503) {
        let body;
        try { body = await response.json(); } catch (_) { body = null; }
        if (body && body.code === 'snapshot_busy') throw new Error('busy');
      }
      throw new Error(response.redirected || response.status === 401 ? 'login_required' : response.status === 403 ? 'forbidden' : 'read_failed');
    }
    return response.json();
  }

  function vector(rows) {
    if (!Array.isArray(rows)) throw new Error('invalid_versions');
    const result = new Map();
    for (const row of rows) {
      if (!row || typeof row.kind !== 'string' || !row.kind || typeof row.id !== 'string' || !row.id ||
          !Number.isSafeInteger(row.version) || row.version < 0) throw new Error('invalid_version');
      const key = JSON.stringify([row.kind, row.id]);
      if (result.has(key)) throw new Error('duplicate_version');
      result.set(key, row.version);
    }
    return result;
  }

  function createVersionedReader(options) {
    const now = options.now || (() => Date.now());
    let cached = options.initialSnapshot || null, lastFull = now();
    return async request => {
      const elapsed = now() - lastFull;
      if (cached && request.force === false && elapsed >= 0 && elapsed < 30000) {
        const hint = await options.readVersions(request);
        const current = vector(hint.versions), previous = vector(cached.versions);
        if (hint.authorization_context === cached.authorization_context &&
            current.size === previous.size && Array.from(current).every(([key, value]) => previous.get(key) === value)) {
          // A revision check is not a fresh business-fact snapshot. Retain its
          // actual as_of rather than reporting cached content as newly read.
          return cached;
        }
      }
      const packet = await options.readSnapshot(request);
      vector(packet.versions);
      cached = packet; lastFull = now();
      return packet;
    };
  }

  function createRefreshController(options) {
    if (!options || typeof options.readSnapshot !== 'function' || typeof options.applySnapshot !== 'function') {
      throw new Error('snapshot_callbacks_required');
    }
    const visible = options.isVisible || (() => typeof document === 'undefined' || document.visibilityState !== 'hidden');
    const canApply = options.canApply || (() => true);
    const status = options.onStatus || (() => {});
    const interval = options.intervalMs || 30000;
    const timeout = options.timeoutMs || 30000;
    let channel = options.channel || null, ownChannel = false;
    let disposed = false, started = false, timer = null, running = null, pending = false;
    let generation = 0, buffered = null, etag = null, lastAppliedAsOf = null, failures = 0;
    let cancelRead = null, queuedReason = 'start', retryBusy = false, fullReadPending = false;
    const lightweightReason = reason => reason === 'poll' ||
      (options.lightweightVisibility === true && ['start', 'visible'].includes(reason));
    const random = options.random || Math.random;
    const minimum = new Map(), applied = new Map();

    function report(state) { status({state, lastAppliedAsOf}); }

    function check(packet) {
      if (!packet || typeof packet.etag !== 'string' || typeof packet.as_of !== 'string' || !Number.isFinite(Date.parse(packet.as_of))) {
        throw new Error('invalid_snapshot');
      }
      const versions = vector(packet.versions);
      for (const [key, version] of minimum) {
        if (!versions.has(key) || versions.get(key) < version) throw new Error('stale');
      }
      for (const [key, version] of versions) {
        if (version < (applied.get(key) || 0)) throw new Error('stale');
      }
      return versions;
    }

    if (options.initialSnapshot) {
      const versions = check(options.initialSnapshot);
      etag = options.initialSnapshot.etag; lastAppliedAsOf = options.initialSnapshot.as_of;
      for (const [key, version] of versions) applied.set(key, version);
    }

    async function apply(packet) {
      const versions = check(packet);
      if (packet.etag !== etag && (!visible() || !canApply(packet))) {
        buffered = packet; report('waiting'); return;
      }
      if (packet.etag !== etag) await options.applySnapshot(packet);
      if (disposed) return;
      etag = packet.etag; lastAppliedAsOf = packet.as_of; buffered = null;
      for (const [key, version] of versions) applied.set(key, version);
      report('current');
    }

    function schedule() {
      if (timer !== null) clearTimeout(timer);
      timer = null;
      if (!started || disposed || !visible()) return;
      const base = retryBusy ? Math.min(30000, 2000 * 2 ** Math.min(Math.max(0, failures - 1), 4)) :
        failures ? Math.min(120000, interval * 2 ** Math.min(failures, 2)) : interval;
      const delay = base * (0.8 + Math.max(0, Math.min(1, random())) * 0.4);
      timer = setTimeout(() => { timer = null; refresh('poll'); }, delay);
    }

    async function attempt(reason) {
      const captured = generation;
      const abort = typeof AbortController === 'function' ? new AbortController() : null;
      let timeoutTimer;
      try {
        const deadline = new Promise((_, reject) => {
          timeoutTimer = setTimeout(() => { if (abort) abort.abort(); reject(new Error('timeout')); }, timeout);
        });
        const canceled = new Promise((_, reject) => { cancelRead = () => { if (abort) abort.abort(); reject(new Error('disposed')); }; });
        const packet = await Promise.race([
          Promise.resolve().then(() => options.readSnapshot({etag, force: fullReadPending || !lightweightReason(reason), signal: abort ? abort.signal : undefined})), deadline, canceled
        ]);
        if (disposed || captured !== generation) return;
        await apply(packet);
        if (captured === generation) fullReadPending = false;
        failures = 0; retryBusy = false;
      } catch (error) {
        if (!disposed) {
          failures++;
          const code = error && error.message;
          retryBusy = code === 'busy';
          report(['stale', 'login_required', 'forbidden', 'busy'].includes(code) ? code : 'error');
        }
      } finally {
        clearTimeout(timeoutTimer); cancelRead = null;
      }
    }

    function refresh(reason = 'manual') {
      if (!lightweightReason(reason)) fullReadPending = true;
      if (disposed || !visible()) return Promise.resolve();
      if (running && lightweightReason(reason)) return running;
      if (reason !== 'poll') queuedReason = reason;
      if (running) { pending = true; return running; }
      running = (async () => {
        do { pending = false; const reason = queuedReason; queuedReason = 'poll'; await attempt(reason); } while (pending && !disposed && visible());
      })().finally(() => { running = null; schedule(); });
      return running;
    }

    function changed() { generation++; return refresh('change'); }
    function receive(event) { if (event && event.data && event.data.type === 'changed') changed(); }

    return {
      refresh,
      start() {
        if (started || disposed) return Promise.resolve();
        started = true;
        if (!channel && typeof window !== 'undefined' && typeof window.BroadcastChannel === 'function') {
          channel = new window.BroadcastChannel('aaie-business-changes'); ownChannel = true;
        }
        if (channel && channel.addEventListener) channel.addEventListener('message', receive);
        return refresh('start');
      },
      notifyCommitted(versions = []) {
        for (const [key, version] of vector(versions)) minimum.set(key, Math.max(version, minimum.get(key) || 0));
        if (channel && channel.postMessage) channel.postMessage({type: 'changed'});
        return changed();
      },
      visibilityChanged() { schedule(); return visible() ? refresh('visible') : Promise.resolve(); },
      async flush() {
        if (disposed) return;
        if (buffered) {
          try { await apply(buffered); }
          catch (_) { return refresh('flush'); }
        }
      },
      dispose() {
        disposed = true; generation++;
        if (timer !== null) clearTimeout(timer);
        if (cancelRead) cancelRead();
        if (channel && channel.removeEventListener) channel.removeEventListener('message', receive);
        if (ownChannel && channel && channel.close) channel.close();
        buffered = null;
      }
    };
  }

  function bootstrapBrowser(doc, win) {
    const controllers = new Map();
    const pageChannel = typeof win.BroadcastChannel === 'function' ? new win.BroadcastChannel('aaie-business-changes') : null;
    const pendingNavigationKey = 'aaie-business-post-navigation';
    const selector = '[data-business-sync-fragment]';
    const find = name => Array.from(doc.querySelectorAll(selector)).find(node => node.dataset.businessSyncFragment === name);
    const hint = () => {
      if (pageChannel) pageChannel.postMessage({type: 'changed'});
    };
    const scan = () => { for (const initial of doc.querySelectorAll('[data-business-sync-endpoint][data-business-sync-fragment]')) {
      const name = initial.dataset.businessSyncFragment;
      if (controllers.has(name)) { controllers.get(name).visibilityChanged(); continue; }
      const endpoint = new URL(initial.dataset.businessSyncEndpoint, win.location.href);
      if (endpoint.origin !== win.location.origin) continue;
      const initialSnapshot = (() => { try { return JSON.parse(initial.dataset.businessSyncInitialSnapshot || 'null'); } catch (_) { return null; } })();
      const versionEndpoint = new URL(endpoint.href);
      versionEndpoint.pathname = '/api/business-changes/versions';
      const fetchPacket = (url, request) => fetchSnapshotPacket(win.fetch.bind(win), url, request);
      const controller = createRefreshController({
        initialSnapshot,
        channel:pageChannel,
        lightweightVisibility:name === 'dashboard-overview',
        intervalMs:5000,
        isVisible: () => {
          const current = find(name), panel = current && current.closest('[data-panel]');
          return Boolean(current && doc.visibilityState !== 'hidden' && (!panel || panel.classList.contains('active')));
        },
        readSnapshot:createVersionedReader({initialSnapshot,
          readVersions:request=>fetchPacket(versionEndpoint.href,request),
          readSnapshot:request=>fetchPacket(endpoint.href,request)}),
        canApply: () => {
          const current = find(name);
          return Boolean(current && !current.querySelector('form[data-business-sync-dirty="1"]') && !doc.querySelector('dialog[open]'));
        },
        applySnapshot: packet => {
          const current = find(name);
          const html = packet.data && packet.data.fragments && packet.data.fragments[name];
          if (!current || typeof html !== 'string') throw new Error('missing_fragment');
          const parsed = new win.DOMParser().parseFromString(html, 'text/html');
          const incoming = Array.from(parsed.querySelectorAll(selector)).find(node => node.dataset.businessSyncFragment === name);
          if (!incoming) throw new Error('invalid_fragment');
          // Retain explicitly stable form widgets and their existing listeners.
          // A changed action, CSRF token or removed permission replaces them.
          const widgets = new Map(Array.from(current.querySelectorAll('[data-business-sync-preserve]')).map(node => [node.dataset.businessSyncPreserve, node]));
          for (const node of incoming.querySelectorAll('[data-business-sync-preserve]')) {
            const old = widgets.get(node.dataset.businessSyncPreserve);
            const a = old && old.querySelector('form'), b = node.querySelector('form');
            const aToken = a && a.querySelector('[name="csrf"]'), bToken = b && b.querySelector('[name="csrf"]');
            if (a && b && aToken && bToken && aToken.value && aToken.value === bToken.value &&
                a.getAttribute('action') === b.getAttribute('action') && a.method === b.method) node.replaceWith(old);
          }
          const detailSelector = 'details[data-business-sync-key], details[id], details[data-material-category], details[data-material-details]';
          const detailKey = node => node.dataset.businessSyncKey || node.id ||
            (node.dataset.materialCategory ? 'material-category:' + node.dataset.materialCategory : '') ||
            (node.dataset.materialDetails ? 'material-details:' + node.dataset.materialDetails : '');
          const open = new Map(Array.from(current.querySelectorAll(detailSelector)).map(node => [detailKey(node), node.open]).filter(([key]) => key));
          const selected = Array.from(current.querySelectorAll('[data-lifecycle-stage-filter][aria-pressed="true"]')).map(node => node.dataset.lifecycleStageFilter).find(value => value !== 'all');
          for (const node of incoming.querySelectorAll(detailSelector)) if (open.has(detailKey(node))) node.open = open.get(detailKey(node));
          const x = win.scrollX, y = win.scrollY;
          const buttonFocused = current.contains(doc.activeElement) && doc.activeElement.hasAttribute('data-business-sync-refresh');
          current.replaceWith(incoming);
          doc.dispatchEvent(new win.CustomEvent('aaie:business-fragment-rendered', {detail: {root: incoming}}));
          if (selected) {
            const button = Array.from(incoming.querySelectorAll('[data-lifecycle-stage-filter]')).find(node => node.dataset.lifecycleStageFilter === selected);
            if (button) button.click();
          }
          if (buttonFocused) {
            const button = incoming.querySelector('[data-business-sync-refresh]');
            if (button) button.focus({preventScroll: true});
          }
          win.scrollTo(x, y);
        },
        onStatus: ({state, lastAppliedAsOf}) => {
          const current = find(name);
          if (!current) return;
          for (const node of current.querySelectorAll('[data-business-sync-controls]')) node.hidden = false;
          const labels = {current: '已同步', waiting: '有新数据，关闭预览或保存/取消编辑后更新', stale: '数据正在更新，稍后重试', busy: '同步繁忙，稍后自动重试', error: '更新未完成，点击刷新重试', login_required: '登录已过期，请重新登录后同步', forbidden: '当前账号没有查看权限，请核对账号权限'};
          for (const node of current.querySelectorAll('[data-business-sync-discard-current]')) node.hidden = state !== 'waiting' || !current.querySelector('form[data-business-sync-dirty="1"]');
          const stamp = lastAppliedAsOf ? new Date(lastAppliedAsOf).toLocaleTimeString('zh-CN', {hour12: false}) : '';
          for (const node of current.querySelectorAll('[data-business-sync-status]')) {
            node.textContent = (labels[state] || '') + (stamp ? ' · 上次同步 ' + stamp : '');
          }
        }
      });
      controllers.set(name, controller);
      controller.start();
    } };
    scan();
    doc.addEventListener('aaie:business-fragments-ready', scan);
    // Ordinary GET navigation is not a business change. A submitted POST marks
    // only this tab; publish after navigation, when the write has finished.
    try {
      if (win.sessionStorage && win.sessionStorage.getItem(pendingNavigationKey)) {
        win.sessionStorage.removeItem(pendingNavigationKey); hint();
      }
    } catch (_) { /* Storage may be disabled; polling/manual refresh still work. */ }
    const submit = event => {
      const form = event.target;
      if (!form || String(form.method || '').toLowerCase() !== 'post') return;
      const target = new URL(form.action || win.location.href, win.location.href);
      if (target.origin !== win.location.origin || ['/login', '/logout'].includes(target.pathname)) return;
      try { if (win.sessionStorage) win.sessionStorage.setItem(pendingNavigationKey, '1'); } catch (_) {}
    };
    doc.addEventListener('submit', submit);
    const refreshAll = () => { for (const c of controllers.values()) c.visibilityChanged(); };
    const flushAll = () => { for (const c of controllers.values()) c.flush(); };
    const input = event => {
      const form = event.target.form || (event.target.closest && event.target.closest('form'));
      if (!form) return;
      if (form.dataset.businessSyncSelection === '1') {
        if (Array.from(form.elements).some(field => field.name === 'student_ids' && field.checked)) {
          form.dataset.businessSyncDirty = '1';
        } else {
          delete form.dataset.businessSyncDirty;
          win.setTimeout(flushAll, 0);
        }
        return;
      }
      if (form.dataset.businessSyncManaged === '1') {
        if (!form.dataset.businessSyncDirty) win.setTimeout(flushAll, 0);
      } else form.dataset.businessSyncDirty = '1';
    };
    const reset = event => {
      const form = event.target.closest && event.target.closest('form');
      if (form) win.setTimeout(() => { delete form.dataset.businessSyncDirty; for (const c of controllers.values()) c.flush(); }, 0);
    };
    const click = event => {
      const discardCurrent = event.target.closest && event.target.closest('[data-business-sync-discard-current]');
      if (discardCurrent) {
        const current = discardCurrent.closest('[data-business-sync-fragment]');
        if (current) for (const form of current.querySelectorAll('form[data-business-sync-dirty="1"]')) form.reset();
      }
      const discard = event.target.closest && event.target.closest('[data-business-sync-discard]');
      if (discard && discard.closest('[data-business-sync-endpoint][data-business-sync-fragment]')) {
        const dialog = discard.closest('dialog');
        if (dialog) for (const form of dialog.querySelectorAll('form')) form.reset();
      }
      const button = event.target.closest && event.target.closest('[data-business-sync-refresh]');
      if (!button) return;
      const fragment = button.closest('[data-business-sync-endpoint][data-business-sync-fragment]');
      const controller = fragment && controllers.get(fragment.dataset.businessSyncFragment);
      if (controller) controller.refresh('manual');
    };
    const committed = () => {
      if (!controllers.size) hint();
      for (const c of controllers.values()) c.notifyCommitted();
    };
    doc.addEventListener('visibilitychange', refreshAll);
    doc.addEventListener('input', input); doc.addEventListener('change', input);
    doc.addEventListener('reset', reset); doc.addEventListener('click', click);
    doc.addEventListener('close', flushAll, true);
    win.addEventListener('focus', refreshAll); win.addEventListener('pageshow', refreshAll);
    win.addEventListener('aaie:business-change', committed);
    return () => {
      for (const c of controllers.values()) c.dispose();
      if (pageChannel) pageChannel.close();
      doc.removeEventListener('submit', submit);
      doc.removeEventListener('aaie:business-fragments-ready', scan);
      doc.removeEventListener('visibilitychange', refreshAll);
      doc.removeEventListener('input', input); doc.removeEventListener('change', input);
      doc.removeEventListener('reset', reset); doc.removeEventListener('click', click);
      doc.removeEventListener('close', flushAll, true);
      win.removeEventListener('focus', refreshAll); win.removeEventListener('pageshow', refreshAll);
      win.removeEventListener('aaie:business-change', committed);
    };
  }

  async function submitChecklist(form) {
    const body = new URLSearchParams(new FormData(form));
    const fields = Array.from(form.elements).filter(field => field.type !== 'hidden');
    const disabled = fields.map(field => field.disabled);
    fields.forEach(field => { field.disabled = true; });
    const abort = new AbortController();
    const timer = setTimeout(() => abort.abort(), 10000);
    try {
      const response = await fetch(form.action, {method:'POST', body, signal:abort.signal,
        headers:{Accept:'application/json','X-Requested-With':'fetch'}});
      let result;
      try { result = await response.json(); } catch (_) { throw new Error('保存未确认，输入已保留，请重试或核对最新数据。'); }
      if (!response.ok || !result.ok) throw new Error(result.message || '保存失败，输入已保留。');
      for (const [key, value] of Object.entries(result.business_receipt || {})) {
        const field = form.elements.namedItem(key);
        if (field && typeof value === 'string') field.value = value;
      }
      return result;
    } catch (error) {
      if (error.name === 'AbortError' || error instanceof TypeError) throw new Error('连接中断或超时，输入已保留。重试将核对同一保存凭据。');
      throw error;
    } finally {
      clearTimeout(timer);
      fields.forEach((field, index) => { field.disabled = disabled[index]; });
    }
  }

  if (typeof window !== 'undefined' && typeof document !== 'undefined') {
    const start = () => bootstrapBrowser(document, window);
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, {once: true});
    else start();
  }
  return {createRefreshController, createVersionedReader, fetchSnapshotPacket, bootstrapBrowser, submitChecklist};
});
