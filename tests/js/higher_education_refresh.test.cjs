const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

class Control {
  constructor(value = '') { this.value = value; this.children = []; this.options = this.children; this.listeners = {}; }
  replaceChildren(...nodes) { this.children = nodes; this.options = nodes; this.value = nodes[0]?.value || ''; }
  add(node) { this.children.push(node); }
  appendChild(node) { this.children.push(node); }
  addEventListener(type, callback) { (this.listeners[type] ||= []).push(callback); }
  change() { for (const callback of this.listeners.change || []) callback({target: this}); }
}
class Option { constructor(text, value) { this.text = text; this.value = value; } }

function workspace(id, name) {
  const controls = Object.fromEntries(['university-select', 'pathway-select', 'course-input', 'course-list', 'course-level']
    .map(key => [`[data-he-${key}]`, new Control()]));
  controls['[data-he-university-select]'].value = String(id);
  const form = {querySelector: key => controls[key]};
  const sources = {
    '[data-he-pathway-source]': {textContent: JSON.stringify([{id, university_id: id, name, final_level: 'Bachelor'}])},
    '[data-he-course-source]': {textContent: JSON.stringify([{university_id: id, name: name + ' DEGREE', level: 'Bachelor'}])}
  };
  return {controls, node: {dataset: {}, matches: key => key === '[data-higher-education-workspace]',
    querySelector: key => sources[key], querySelectorAll: key => key === '[data-he-university-form]' ? [form] : []}};
}

test('replaced Higher Education forms retain their new university, pathway and course interactions', () => {
  const initial = workspace(1, 'FIRST');
  const listeners = {};
  const document = {
    querySelector: () => initial.node,
    querySelectorAll: key => key === '[data-higher-education-workspace]' ? [initial.node] : [],
    createElement: () => new Option('', ''),
    addEventListener: (type, callback) => { (listeners[type] ||= []).push(callback); }
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../../assets/higher-education.js'), 'utf8'), {document, Option});
  assert.equal(initial.controls['[data-he-pathway-select]'].options[1].value, '1');
  const updated = workspace(2, 'UPDATED');
  const root = {matches: () => false, querySelectorAll: () => [updated.node]};
  const notify = () => { for (const callback of listeners['aaie:business-fragment-rendered'] || []) callback({detail: {root}}); };
  notify();
  updated.controls['[data-he-university-select]'].change();
  assert.equal(updated.controls['[data-he-pathway-select]'].options[1]?.value, '2');
  assert.equal(updated.controls['[data-he-course-list]'].children[0]?.value, 'UPDATED DEGREE');
  updated.controls['[data-he-pathway-select]'].value = '2';
  updated.controls['[data-he-pathway-select]'].change();
  assert.equal(updated.controls['[data-he-course-level]'].options[0].value, 'Bachelor');
  assert.equal(updated.controls['[data-he-course-level]'].disabled, false);
  updated.controls['[data-he-course-input]'].value = 'USER DRAFT';
  notify();
  assert.equal(updated.controls['[data-he-course-input]'].value, 'USER DRAFT');
});

test('saved forms notify all views and failed saves retain the same intent and draft', async () => {
  const listeners={},events=[];
  const status={textContent:'',setAttribute(){}};
  const form={dataset:{businessSyncDirty:'1'},querySelector:key=>key==='[data-he-command-fields]'?{}:status,
    appendChild(){},addEventListener:(type,callback)=>{listeners[type]=callback;}};
  const node={matches:()=>true,querySelector:()=>null,
    querySelectorAll:key=>key==='form'?[form]:[]};
  const document={querySelectorAll:()=>[node],createElement:()=>status,addEventListener(){}};
  let reject=true,calls=0;
  const window={AAIEBusinessChanges:{submitChecklist:async received=>{
    assert.equal(received,form);calls++;
    if(reject)throw new Error('版本已改变，输入已保留');
    return {ok:true};
  }},dispatchEvent:event=>events.push(event)};
  class CustomEvent{constructor(type,options={}){this.type=type;this.detail=options.detail;}}
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../../assets/higher-education.js'),'utf8'),{document,window,CustomEvent});
  assert.equal(typeof listeners.submit,'function');
  await listeners.submit({preventDefault(){}});
  assert.equal(form.dataset.businessSyncDirty,'1');
  assert.match(status.textContent,/版本已改变/);
  assert.equal(events.length,0);
  reject=false;
  await listeners.submit({preventDefault(){}});
  assert.equal(calls,2);
  assert.equal(form.dataset.businessSyncDirty,undefined);
  assert.equal(events[0].type,'aaie:business-change');
});
