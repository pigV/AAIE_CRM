const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..', '..');
const source = fs.readFileSync(path.join(root, 'assets', 'revenue-payment.js'), 'utf8');

class FakeElement {
  constructor({value = '', required = false, tagName = 'INPUT', textContent = ''} = {}) {
    this.value = value;
    this.required = required;
    this.tagName = tagName;
    this.textContent = textContent;
    this.dataset = {};
    this.options = [];
    this.selectedIndex = 0;
    this.hidden = false;
    this.disabled = false;
    this.readOnly = false;
    this.customValidity = '';
    this.listeners = new Map();
    this.attributes = new Map();
    this.focused = false;
  }

  addEventListener(type, listener) {
    const listeners = this.listeners.get(type) || [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  async dispatch(type) {
    const event = {
      type,
      defaultPrevented: false,
      immediatePropagationStopped: false,
      preventDefault() { this.defaultPrevented = true; },
      stopImmediatePropagation() { this.immediatePropagationStopped = true; },
    };
    for (const listener of this.listeners.get(type) || []) {
      await listener(event);
      if (event.immediatePropagationStopped) break;
    }
    return event;
  }

  setCustomValidity(message) { this.customValidity = message; }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  removeAttribute(name) { this.attributes.delete(name); }
  focus() { this.focused = true; }
  isValid() { return !this.customValidity && (!this.required || Boolean(this.value)); }
}

class FakeForm extends FakeElement {
  constructor(elements) {
    super({tagName: 'FORM'});
    this.elementsBySelector = elements;
    this.action = '/revenue/billing/payment';
    this.reportValidityCalls = 0;
  }

  querySelector(selector) { return this.elementsBySelector[selector] || null; }

  checkValidity() {
    let valid = true;
    for (const field of Object.values(this.elementsBySelector)) {
      if (!field || typeof field.isValid !== 'function' || field.isValid()) continue;
      valid = false;
      for (const listener of field.listeners.get('invalid') || []) {
        listener({type: 'invalid'});
      }
    }
    return valid;
  }

  reportValidity() {
    this.reportValidityCalls += 1;
    return this.checkValidity();
  }
}

function option(value, data = {}) {
  return {value, dataset: data};
}

function fixture(fetchImpl) {
  const fee = new FakeElement({value: 'fee-a', required: true, tagName: 'SELECT'});
  fee.options = [
    option('', {}),
    option('fee-a', {outstandingBaseCents: '240000'}),
    option('fee-b', {outstandingBaseCents: '120000'}),
  ];
  fee.selectedIndex = 1;

  const method = new FakeElement({required: true, tagName: 'SELECT'});
  method.options = [
    option('', {}),
    option('alipay', {currency: 'CNY', gstBasisPoints: '0'}),
    option('wechat', {currency: 'CNY', gstBasisPoints: '0'}),
    option('au_personal_account', {currency: 'AUD', gstBasisPoints: '0'}),
    option('au_company_account', {currency: 'AUD', gstBasisPoints: '1000'}),
  ];

  const fields = {
    fee,
    method,
    currency: new FakeElement({required: true}),
    actualMajor: new FakeElement({required: true}),
    actualCents: new FakeElement(),
    actualLabel: new FakeElement(),
    actualPrefix: new FakeElement(),
    baseMajor: new FakeElement({required: true}),
    baseCents: new FakeElement(),
    outstanding: new FakeElement(),
    evidence: new FakeElement({required: true}),
    receivedDate: new FakeElement({required: true}),
    previewBase: new FakeElement(),
    previewGst: new FakeElement(),
    previewGross: new FakeElement(),
    previewGstLabel: new FakeElement(),
    error: new FakeElement(),
    submit: new FakeElement({tagName: 'BUTTON', textContent: '保存收款'}),
  };
  fields.error.hidden = true;

  const form = new FakeForm({
    '[data-fee-item]': fields.fee,
    '[data-payment-method]': fields.method,
    '[data-actual-currency]': fields.currency,
    '[data-actual-major]': fields.actualMajor,
    '[data-actual-cents]': fields.actualCents,
    '[data-actual-amount-label]': fields.actualLabel,
    '[data-actual-prefix]': fields.actualPrefix,
    '[data-base-major]': fields.baseMajor,
    '[data-base-cents]': fields.baseCents,
    '[data-outstanding-base]': fields.outstanding,
    'input[name="payment_evidence"]': fields.evidence,
    'input[name="received_date"]': fields.receivedDate,
    '[data-preview-base]': fields.previewBase,
    '[data-preview-gst]': fields.previewGst,
    '[data-preview-gross]': fields.previewGross,
    '[data-preview-gst-label]': fields.previewGstLabel,
    '[data-payment-error]': fields.error,
    'button[type="submit"], input[type="submit"]': fields.submit,
  });
  const redirects = [];
  const context = {
    document: {querySelectorAll: () => [form]},
    window: {location: {assign: (url) => redirects.push(url)}},
    fetch: fetchImpl || (() => Promise.resolve({ok: true, json: async () => ({ok: true, redirect: '/done'})})),
    FormData: class { constructor(sourceForm) { this.form = sourceForm; } },
  };
  vm.runInNewContext(source, context);
  return {form, fields, redirects};
}

async function selectMethod(state, index) {
  state.fields.method.selectedIndex = index;
  state.fields.method.value = state.fields.method.options[index].value;
  await state.fields.method.dispatch('change');
}

function makeValid(state) {
  state.fields.receivedDate.value = '2026-09-03';
  state.fields.evidence.value = 'payment.pdf';
}

test('initial state and fee switching use the selected outstanding balance', async () => {
  const state = fixture();
  assert.equal(state.fields.baseMajor.value, '2400.00');
  assert.equal(state.fields.baseCents.value, '240000');
  assert.equal(state.fields.outstanding.textContent, '当前未收服务费：AUD $2,400.00');

  state.fields.fee.selectedIndex = 2;
  state.fields.fee.value = 'fee-b';
  await state.fields.fee.dispatch('change');
  assert.equal(state.fields.baseMajor.value, '1200.00');
  assert.equal(state.fields.baseCents.value, '120000');
  assert.equal(state.fields.actualMajor.value, '');
});

test('four payment methods and a partial amount calculate authoritative integer cents', async () => {
  const state = fixture();

  await selectMethod(state, 1);
  state.fields.actualMajor.value = '11794.00';
  await state.fields.actualMajor.dispatch('input');
  assert.equal(state.fields.currency.value, 'CNY');
  assert.equal(state.fields.actualCents.value, '1179400');
  assert.equal(state.fields.previewGross.textContent, 'AUD $2,400.00');

  await selectMethod(state, 2);
  state.fields.actualMajor.value = '11800';
  await state.fields.actualMajor.dispatch('input');
  assert.equal(state.fields.currency.value, 'CNY');
  assert.equal(state.fields.actualCents.value, '1180000');

  await selectMethod(state, 3);
  assert.equal(state.fields.actualMajor.value, '2400.00');
  assert.equal(state.fields.actualCents.value, '240000');
  assert.equal(state.fields.previewGst.textContent, 'AUD $0.00');

  await selectMethod(state, 4);
  assert.equal(state.fields.actualMajor.value, '2640.00');
  assert.equal(state.fields.actualCents.value, '264000');
  assert.equal(state.fields.previewGst.textContent, 'AUD $240.00');

  state.fields.baseMajor.value = '1200.00';
  await state.fields.baseMajor.dispatch('input');
  assert.equal(state.fields.baseCents.value, '120000');
  assert.equal(state.fields.actualMajor.value, '1320.00');
  assert.equal(state.fields.previewGross.textContent, 'AUD $1,320.00');
});

test('invalid submit reports the field and never leaves the button locked', async () => {
  const state = fixture(() => { throw new Error('fetch must not run'); });
  await selectMethod(state, 1);
  makeValid(state);
  state.fields.actualMajor.value = '';
  await state.fields.actualMajor.dispatch('input');

  await state.form.dispatch('submit');
  assert.equal(state.fields.actualMajor.customValidity, '请输入实际收到的人民币金额。');
  assert.equal(state.form.reportValidityCalls, 1);
  assert.equal(state.fields.submit.disabled, false);
  assert.equal(state.fields.submit.textContent, '保存收款');
  assert.equal(state.form.dataset.submitting, 'false');
});

test('duplicate submit is ignored while the first request is in flight', async () => {
  let resolveFetch;
  let calls = 0;
  const state = fixture(() => {
    calls += 1;
    return new Promise((resolve) => { resolveFetch = resolve; });
  });
  await selectMethod(state, 3);
  makeValid(state);

  const first = state.form.dispatch('submit');
  await Promise.resolve();
  const second = state.form.dispatch('submit');
  assert.equal(calls, 1);
  assert.equal(state.fields.submit.disabled, true);
  assert.equal(state.fields.submit.textContent, '处理中...');

  resolveFetch({ok: true, json: async () => ({ok: true, redirect: '/revenue/billing?payment=ok'})});
  await Promise.all([first, second]);
  assert.deepEqual(state.redirects, ['/revenue/billing?payment=ok']);
});

for (const scenario of [
  {
    name: 'upload validation 4xx',
    fetch: async () => ({ok: false, json: async () => ({ok: false, message: '付款凭证无效，请上传 PNG、JPG/JPEG 或 PDF 文件。'})}),
    message: '付款凭证无效，请上传 PNG、JPG/JPEG 或 PDF 文件。',
  },
  {
    name: 'non-JSON server failure',
    fetch: async () => ({ok: false, json: async () => { throw new Error('html'); }}),
    message: '收款未保存，请检查输入或付款凭证后重试。',
  },
  {
    name: 'network failure',
    fetch: async () => { throw new Error('offline'); },
    message: '网络连接失败，收款未保存，请稍后重试。',
  },
]) {
  test(`${scenario.name} restores submit state and shows a safe message`, async () => {
    const state = fixture(scenario.fetch);
    await selectMethod(state, 3);
    makeValid(state);
    await state.form.dispatch('submit');

    assert.equal(state.fields.error.hidden, false);
    assert.equal(state.fields.error.textContent, scenario.message);
    assert.equal(state.fields.error.focused, true);
    assert.equal(state.fields.submit.disabled, false);
    assert.equal(state.fields.submit.textContent, '保存收款');
    assert.equal(state.form.dataset.submitting, 'false');
  });
}
