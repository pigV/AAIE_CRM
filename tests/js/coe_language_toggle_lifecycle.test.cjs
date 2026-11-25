const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.resolve(__dirname, '../../assets/student-intake-tuition.js'), 'utf8');

class Events {
  constructor() { this.events = {}; }
  addEventListener(name, handler) { (this.events[name] ||= []).push(handler); }
  fire(name, event = {}) { for (const handler of this.events[name] || []) handler(event); }
}
function fixture() {
  const fields = new Events(); fields.dataset = {}; fields.hidden = false;
  const mode = new Events(); mode.value = 'both';
  const type = new Events(); type.value = 'COE';
  function section(kind) {
    const section = {dataset: {schoolCourseKind: kind}};
    const input = extra => Object.assign({disabled:false, required:false, setCustomValidity() {}}, extra);
    const search = input({});
    const school = input({selectedOptions: [{dataset:{}}], matches: q => q === '[data-course-school-value]', closest: () => section});
    const sector = input({value:'', matches: () => false});
    const start = input({});
    const authority = input({value:''});
    const number = input({});
    const picker = {hidden:false, querySelectorAll: () => [search, school]};
    const publicFields = {hidden:false, querySelector: () => authority, querySelectorAll: () => [authority,number]};
    section.querySelector = q => ({'[data-course-sector]':sector,'[data-course-school-value]':school,
      '[data-course-school-picker]':picker,'[data-course-public-fields]':publicFields,'input[type=date]':start})[q] || null;
    section.querySelectorAll = () => [search,school,sector,start,authority,number];
    return {section,sector,school,search,picker,authority,publicFields};
  }
  const language = section('language'), main = section('main');
  fields.querySelector = q => q === '[data-school-course-mode]' ? mode : null;
  fields.querySelectorAll = q => q === '[data-school-course-kind]' ? [language.section,main.section] : [];
  fields.matches = q => q === '[data-school-course-tuition]';
  fields.closest = () => ({querySelector: () => type});
  return {fields,mode,type,language,main};
}
function environment(cards) {
  const document = new Events(), window = new Events();
  document.cards = cards;
  document.querySelectorAll = q => q === '[data-school-course-tuition]' ? document.cards.map(c => c.fields) : [];
  const context = {document,window,WeakMap,WeakSet};
  vm.runInNewContext(source,context);
  return {document,window};
}

test('normal public/private toggles preserve main-school and education-authority requirements', () => {
  const card=fixture(); environment([card]);
  card.language.sector.value='public';card.fields.fire('change',{target:card.language.sector});
  assert.equal(card.language.picker.hidden,true);assert.equal(card.language.search.disabled,true);
  assert.equal(card.language.search.required,false);assert.equal(card.language.authority.required,true);
  assert.equal(card.main.search.required,true,'main course still requires an actual school');
  card.language.sector.value='private';card.fields.fire('change',{target:card.language.sector});
  assert.equal(card.language.picker.hidden,false);assert.equal(card.language.search.required,true);
  assert.equal(card.language.publicFields.hidden,true);
});

test('browser restored public selection is resynchronized on pageshow without a change event', () => {
  const card=fixture();const {window}=environment([card]);
  // Browser form restoration does not promise a user change event.
  card.language.sector.value='public';window.fire('pageshow',{persisted:true});
  assert.equal(card.language.picker.hidden,true);
  assert.equal(card.language.search.disabled,true);assert.equal(card.language.search.required,false);
});

test('a replaced review fragment receives fresh bindings and affects only its own course', () => {
  const first=fixture();const {document}=environment([first]);const replacement=fixture();
  document.cards=[replacement];
  document.fire('aaie:business-fragment-rendered',{detail:{root:replacement.fields}});
  replacement.language.sector.value='public';replacement.fields.fire('change',{target:replacement.language.sector});
  assert.equal(replacement.language.picker.hidden,true);
  assert.equal(first.language.picker.hidden,false);
});

test('input events also synchronize public/private choice before blur', () => {
  const card=fixture();environment([card]);
  card.language.sector.value='public';card.fields.fire('input',{target:card.language.sector});
  assert.equal(card.language.picker.hidden,true);
});

test('repeat refresh initialization does not duplicate field listeners', () => {
  const card=fixture();const {document,window}=environment([card]);
  for(let i=0;i<3;i++) {document.fire('aaie:business-fragment-rendered',{detail:{root:card.fields}});window.fire('pageshow');}
  assert.equal(card.fields.events.change.length,1);
});
