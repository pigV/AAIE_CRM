const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { test } = require('node:test');
const vm = require('node:vm');

const script = readFileSync('assets/app.js', 'utf8');
const search = script.slice(script.indexOf('  const initStudentNameSearch ='), script.indexOf('  initStudentNameSearch(document);') + '  initStudentNameSearch(document);'.length);

function fixture() {
  const submitted = [];
  let scheduled;
  function element() {
    const events = new Map();
    const attributes = new Map();
    return {
      events, attributes, children: [], dataset: {}, value: '', hidden: false,
      setAttribute(key, value) { attributes.set(key, value); },
      removeAttribute(key) { attributes.delete(key); },
      addEventListener(key, callback) { events.set(key, callback); },
      appendChild(child) { this.children.push(child); },
      set innerHTML(_value) { this.children = []; },
      classList: { toggle() {} },
    };
  }
  const input = element(), field = element(), results = element();
  const form = {
    requestSubmit() { submitted.push(input.value); },
  };
  input.dataset.studentSuggestionsId = 'fixture';
  input.closest = selector => selector === 'form' ? form : field;
  const document = {
    getElementById: () => ({ textContent: JSON.stringify([
      { value: 'Hao Chang', student_id: '20260073', search: 'hao chang chang hao 20260073' },
      { value: 'Hao Chang', student_id: '20260195', search: 'hao chang chang hao 20260195' },
    ]) }),
    createElement: element,
  };
  vm.runInNewContext(search, {
    document, qa: (selector, root) => selector === 'button' ? root.children : [input],
    q: () => results, escapeHtml: value => value,
    window: { clearTimeout() { scheduled = null; }, setTimeout(fn) { scheduled = fn; return 1; } },
  });
  return {
    input, results, submitted,
    type(value) {
      input.value = value;
      input.events.get('input')();
      scheduled();
    },
    key(key) { input.events.get('keydown')({ key, preventDefault() {} }); },
    click(index) {
      const option = results.children[index];
      results.events.get('click')?.({ target: { closest: () => option } });
    },
  };
}

test('selects the exact matching student by keyboard when names are identical', () => {
  const page = fixture();
  page.type('chang hao');
  assert.equal(page.results.children.length, 2);
  page.key('ArrowDown');
  page.key('ArrowDown');
  page.key('Enter');
  assert.deepEqual(page.submitted, ['20260195']);
});

test('selects the exact student from a click or touch-generated click', () => {
  const page = fixture();
  page.type('20260073');
  assert.equal(page.results.children.length, 1);
  page.click(0);
  assert.deepEqual(page.submitted, ['20260073']);
});
