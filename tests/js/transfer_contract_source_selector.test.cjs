const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

test('Transfer selection enables populated choices only; other contract types disable both', () => {
  const source = fs.readFileSync('assets/app.js', 'utf8');
  const start = source.indexOf('const syncPublishedContractFees =');
  const end = source.indexOf('const syncContractFees =', start);
  assert.ok(start >= 0 && end > start);
  const fields = {hidden: true};
  const choices = [{dataset: {sourceEmpty: 'false'}, disabled: true},
                   {dataset: {sourceEmpty: 'true'}, disabled: true},
                   {dataset: {sourceEmpty: 'false', sourceLocked: 'true'}, disabled: true}];
  const radio = {value: 'TransferApplicationContract'};
  const context = {
    q: selector => selector.includes(':checked') ? radio : selector.includes('source-fields') ? fields : null,
    qa: () => choices,
    syncContractPaymentPlan: () => {},
    parseMoney: () => null,
    calculatePublishedContractPricing: () => ({}),
  };
  vm.createContext(context);
  vm.runInContext(source.slice(start, end) + ';this.sync = syncPublishedContractFees;', context);
  const form = {dataset: {contractTransferSourceHold: 'true'}};
  context.sync(form);
  assert.equal(fields.hidden, false);
  assert.equal(choices[0].disabled, false);
  assert.equal(choices[1].disabled, true, 'empty selector must remain disabled');
  assert.equal(choices[2].disabled, true, 'persisted source must not be offered as editable');
  radio.value = 'NewInternationalStudentContract';
  context.sync(form);
  assert.equal(fields.hidden, true);
  assert.ok(choices.every(choice => choice.disabled));
});
