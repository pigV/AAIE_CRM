const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..', '..');

test('Revenue CSS has responsive no-overflow layout', () => {
  const css = fs.readFileSync(path.join(root, 'assets', 'revenue.css'), 'utf8');
  assert.match(css, /@media \(max-width: 700px\)/);
  assert.match(css, /grid-template-columns: repeat\(2, minmax\(0, 1fr\)\)/);
  assert.match(css, /overflow-x: auto/);
  assert.match(css, /min-width: 0/);
});

test('Revenue view delegates payment preview to one external module', () => {
  const source = fs.readFileSync(path.join(root, 'revenue', 'views.py'), 'utf8');
  assert.match(source, /<script src="\/assets\/revenue-payment\.js\?v=3" defer><\/script>/);
  assert.equal(source.includes('<script>'), false);
  assert.equal(source.includes('data-rate'), false);
  assert.equal(source.includes('fetch('), false);
});

test('Revenue payment preview keeps integer-cent parsing and GST arithmetic', () => {
  const source = fs.readFileSync(path.join(root, 'assets', 'revenue-payment.js'), 'utf8');
  const context = {
    document: {querySelectorAll: () => []},
    window: {},
  };
  vm.runInNewContext(source, context);
  const ui = context.window.RevenuePaymentUI;

  assert.equal(ui.parseMajorUnits('2400'), 240000);
  assert.equal(ui.parseMajorUnits('1,200.00'), 120000);
  assert.equal(ui.parseMajorUnits('1200.001'), null);
  assert.equal(ui.applyBasisPoints(240000, 0), 0);
  assert.equal(ui.applyBasisPoints(240000, 1000), 24000);
  assert.equal(ui.applyBasisPoints(120000, 1000), 12000);
});
