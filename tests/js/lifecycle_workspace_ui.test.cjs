const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const ui = require(path.join(__dirname, '../../assets/lifecycle-workspace.js'));

function element(attributes = {}) {
  return {
    attributes: { ...attributes },
    dataset: {},
    hidden: false,
    disabled: false,
    textContent: attributes.textContent || '',
    focusCount: 0,
    setAttribute(name, value) { this.attributes[name] = String(value); },
    getAttribute(name) { return this.attributes[name] ?? null; },
    removeAttribute(name) { delete this.attributes[name]; },
    focus() { this.focusCount += 1; },
  };
}

function workspaceFixture() {
  const tabs = ['overview', 'schools', 'history'].map((key) => {
    const tab = element({ 'aria-selected': 'false', tabindex: '-1' });
    tab.dataset.lifecycleTab = key;
    return tab;
  });
  const panels = ['overview', 'schools', 'history'].map((key) => {
    const panel = element({ id: `transfer-panel-${key}` });
    panel.dataset.lifecyclePanel = key;
    panel.hidden = true;
    return panel;
  });
  const workspace = {
    querySelectorAll(selector) {
      if (selector === '[data-lifecycle-tab]') return tabs;
      if (selector === '[data-lifecycle-panel]') return panels;
      return [];
    },
  };
  return { workspace, tabs, panels };
}

test('selectTab activates exactly one tab and matching panel', () => {
  const { workspace, tabs, panels } = workspaceFixture();
  assert.equal(ui.selectTab(workspace, 'schools'), true);
  assert.deepEqual(tabs.map((tab) => tab.getAttribute('aria-selected')), ['false', 'true', 'false']);
  assert.deepEqual(tabs.map((tab) => tab.getAttribute('tabindex')), ['-1', '0', '-1']);
  assert.deepEqual(panels.map((panel) => panel.hidden), [true, false, true]);
});

test('keyboard navigation wraps and focuses without submitting business actions', () => {
  const { workspace, tabs } = workspaceFixture();
  ui.selectTab(workspace, 'overview');
  let prevented = 0;
  ui.handleTabKeydown({ key: 'ArrowLeft', currentTarget: tabs[0], preventDefault() { prevented += 1; } }, workspace);
  assert.equal(tabs[2].getAttribute('aria-selected'), 'true');
  assert.equal(tabs[2].focusCount, 1);

  ui.handleTabKeydown({ key: 'Home', currentTarget: tabs[2], preventDefault() { prevented += 1; } }, workspace);
  assert.equal(tabs[0].getAttribute('aria-selected'), 'true');
  assert.equal(tabs[0].focusCount, 1);
  assert.equal(prevented, 2);
});

test('hash activation uses a matching panel id and ignores unrelated fragments', () => {
  const { workspace, tabs } = workspaceFixture();
  assert.equal(ui.selectTabFromHash(workspace, '#transfer-panel-history'), true);
  assert.equal(tabs[2].getAttribute('aria-selected'), 'true');
  assert.equal(ui.selectTabFromHash(workspace, '#not-this-workspace'), false);
});

test('submitting state prevents a second submit and gives clear waiting feedback', () => {
  const submitter = element({ textContent: '保存更改' });
  submitter.textContent = '保存更改';
  const otherControl = element();
  const form = {
    dataset: {},
    attributes: {},
    setAttribute(name, value) { this.attributes[name] = String(value); },
    querySelectorAll(selector) {
      assert.equal(selector, 'button:not([type]), button[type="submit"], input[type="submit"]');
      return [submitter];
    },
  };

  assert.equal(ui.setSubmittingState(form, submitter), true);
  assert.equal(form.attributes['aria-busy'], 'true');
  assert.equal(submitter.textContent, '正在保存…');
  assert.equal(submitter.disabled, true);
  assert.equal(otherControl.disabled, false, 'data-bearing controls must remain successful form controls');
  assert.equal(ui.setSubmittingState(form, submitter), false);
});
