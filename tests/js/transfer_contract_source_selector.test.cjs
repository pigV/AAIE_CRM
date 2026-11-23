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

test('an explicit verified intake choice releases HOLD and enables only its school confirmation', () => {
  const source = fs.readFileSync('assets/app.js', 'utf8');
  const start = source.indexOf('const syncPublishedContractFees =');
  const end = source.indexOf('const syncContractFees =', start);
  const fields = {hidden: true};
  const first = {dataset: {transferIntakeToken:'first'}, inputs:[{disabled:true}]};
  const second = {dataset: {transferIntakeToken:'second'}, inputs:[{disabled:true}]};
  const intake = {value:'second',dataset:{sourceEmpty:'false'},selectedOptions:[{dataset:{transferSourceFee:'2400.00',transferSourceVerified:'true'}}]};
  const existing = {value:'',dataset:{sourceEmpty:'true'},selectedOptions:[]};
  const total = {value:''};
  const context = {
    q: selector => selector.includes(':checked') ? {value:'TransferApplicationContract'}
      : selector.includes('source-fields') ? fields
      : selector.includes('source_parent_form_token') ? intake
      : selector.includes('source_existing_evidence') ? existing
      : selector.includes('final-total') ? total : null,
    qa: (selector, parent) => selector.includes('current-confirmation') ? [first,second]
      : selector.includes('data-source-empty') ? [intake,existing]
      : parent?.inputs || [],
    syncContractPaymentPlan:()=>{}, parseMoney:value=>value?Number(value):null,
    calculatePublishedContractPricing: ({standardFee})=>({net:standardFee,gst:0,total:standardFee,treatment:'NOT_APPLICABLE'}),
  };
  vm.createContext(context);
  vm.runInContext(source.slice(start,end)+';this.sync=syncPublishedContractFees;',context);
  const form={dataset:{contractTransferSourceHold:'true',contractTransferFeeAuthority:''}};
  context.sync(form);
  assert.equal(form.dataset.contractTransferSourceHold,'false');
  assert.equal(total.value,'AUD 2400.00');
  assert.equal(first.hidden,true);assert.equal(first.inputs[0].disabled,true);
  assert.equal(second.hidden,false);assert.equal(second.inputs[0].disabled,false);
  intake.selectedOptions=[{dataset:{}}];context.sync(form);
  assert.equal(form.dataset.contractTransferSourceHold,'true','unverified selection must remain blocked');
});
