(function (root, factory) {
  const api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.LifecycleWorkspaceUI = api;
})(typeof window !== 'undefined' ? window : null, function (root) {
  'use strict';

  function tabsFor(workspace) {
    return Array.from(workspace.querySelectorAll('[data-lifecycle-tab]'));
  }

  function panelsFor(workspace) {
    return Array.from(workspace.querySelectorAll('[data-lifecycle-panel]'));
  }

  function selectTab(workspace, key) {
    const tabs = tabsFor(workspace);
    const panels = panelsFor(workspace);
    const target = tabs.find((tab) => tab.dataset.lifecycleTab === key);
    if (!target || !panels.some((panel) => panel.dataset.lifecyclePanel === key)) return false;
    tabs.forEach((tab) => {
      const active = tab.dataset.lifecycleTab === key;
      tab.setAttribute('aria-selected', active ? 'true' : 'false');
      tab.setAttribute('tabindex', active ? '0' : '-1');
    });
    panels.forEach((panel) => {
      panel.hidden = panel.dataset.lifecyclePanel !== key;
    });
    return true;
  }

  function handleTabKeydown(event, workspace) {
    const tabs = tabsFor(workspace);
    const index = tabs.indexOf(event.currentTarget);
    if (index < 0) return false;
    let nextIndex = null;
    if (event.key === 'ArrowRight') nextIndex = (index + 1) % tabs.length;
    if (event.key === 'ArrowLeft') nextIndex = (index - 1 + tabs.length) % tabs.length;
    if (event.key === 'Home') nextIndex = 0;
    if (event.key === 'End') nextIndex = tabs.length - 1;
    if (nextIndex === null) return false;
    event.preventDefault();
    const next = tabs[nextIndex];
    selectTab(workspace, next.dataset.lifecycleTab);
    next.focus();
    return true;
  }

  function selectTabFromHash(workspace, hash) {
    const id = String(hash || '').replace(/^#/, '');
    if (!id) return false;
    const panel = panelsFor(workspace).find((candidate) => candidate.getAttribute('id') === id);
    return panel ? selectTab(workspace, panel.dataset.lifecyclePanel) : false;
  }

  function setSubmittingState(form, submitter) {
    if (form.dataset.lifecycleSubmitting === 'true') return false;
    form.dataset.lifecycleSubmitting = 'true';
    form.setAttribute('aria-busy', 'true');
    const buttons = Array.from(form.querySelectorAll('button:not([type]), button[type="submit"], input[type="submit"]'));
    buttons.forEach((button) => { button.disabled = true; });
    if (submitter) {
      if (!submitter.dataset.lifecycleOriginalLabel) {
        submitter.dataset.lifecycleOriginalLabel = submitter.textContent || submitter.value || '';
      }
      if ('value' in submitter && submitter.tagName === 'INPUT') submitter.value = '正在保存…';
      else submitter.textContent = '正在保存…';
    }
    return true;
  }

  function initialiseWorkspace(workspace) {
    tabsFor(workspace).forEach((tab) => {
      tab.addEventListener('click', function () {
        if (!selectTab(workspace, tab.dataset.lifecycleTab)) return;
        const panel = panelsFor(workspace).find(
          (candidate) => candidate.dataset.lifecyclePanel === tab.dataset.lifecycleTab
        );
        if (panel && typeof history !== 'undefined' && history.replaceState) {
          history.replaceState(null, '', `#${panel.getAttribute('id')}`);
        }
      });
      tab.addEventListener('keydown', (event) => handleTabKeydown(event, workspace));
    });
    selectTabFromHash(workspace, typeof location !== 'undefined' ? location.hash : '');
    workspace.addEventListener('submit', function (event) {
      if (!setSubmittingState(event.target, event.submitter || null)) event.preventDefault();
    });
  }

  function initialiseAll(documentObject) {
    documentObject.querySelectorAll('[data-lifecycle-workspace]').forEach(initialiseWorkspace);
  }

  if (root && root.document) {
    if (root.document.readyState === 'loading') {
      root.document.addEventListener('DOMContentLoaded', () => initialiseAll(root.document));
    } else {
      initialiseAll(root.document);
    }
    root.addEventListener('hashchange', () => {
      root.document.querySelectorAll('[data-lifecycle-workspace]').forEach(
        (workspace) => selectTabFromHash(workspace, root.location.hash)
      );
    });
  }

  return {
    selectTab,
    handleTabKeydown,
    selectTabFromHash,
    setSubmittingState,
    initialiseWorkspace,
    initialiseAll,
  };
});
