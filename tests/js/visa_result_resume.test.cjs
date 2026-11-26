// Exercise the actual result-workspace initialization and form event handlers.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(process.env.VISA_RESULT_RESUME_SCRIPT || path.resolve(__dirname,'../../assets/visa-result-workspace.js'),'utf8');

function initialize(stored, hash='', query='') {
  const node=(extra={})=>Object.assign({id:'',dataset:{},hidden:false,listeners:{},parentElement:null,
    addEventListener(type,fn){this.listeners[type]=fn;},classList:{toggle(){}},
    matches(){return false;},closest(){return null;},querySelector(){return null;},querySelectorAll(){return [];}
  },extra);
  const scrolls=[],jumps=[];
  const panel=node({isConnected:true,dataset:{caseId:'1',stage:'prepare'}});
  const group=node({id:'visa-result-group-lodgement',tagName:'DETAILS',open:false,dataset:{resultGroup:'lodgement'},parentElement:panel});
  const row=node({id:'visa-result-item-14',parentElement:group,
    getBoundingClientRect(){return {top:320};},scrollIntoView(){jumps.push(this.id);},
    matches(selector){return selector==='[data-result-item]';}});
  const other=node({id:'visa-result-item-16',parentElement:group,
    getBoundingClientRect(){return {top:500};},scrollIntoView(){jumps.push(this.id);},
    matches(selector){return selector==='[data-result-item]';}});
  const details=node({tagName:'DETAILS',open:false,parentElement:row});
  const editor=node({tagName:'DETAILS',open:false,parentElement:details,
    closest(){return row;},querySelector(){return {name:'legacy_student_document_id',value:'9'};}});
  row.querySelector=selector=>selector==='[data-result-item-details]'?details:null;
  const anchor={value:row.id};
  const form=node({elements:[],closest(){return row;},querySelector(selector){return selector==='[name="return_anchor"]'?anchor:null;}});
  form.elements.namedItem=()=>null;
  const feedback=node(),reset=node();
  panel.querySelector=selector=>selector==='[data-result-feedback]'?feedback:selector==='[data-result-reset]'?reset:null;
  panel.querySelectorAll=selector=>selector==='[id]'?[group,row,other]:selector==='form'?[form]:
    selector==='[data-result-item]'?[row,other]:selector==='[data-result-group]'?[group]:
    selector==='details'?[group,details,editor]:selector==='details[open]'?[group,details,editor].filter(d=>d.open):
    selector==='.visa-document-edit'?[editor]:[];
  const key='aaie.result-workspace.v1:/students/S1/visa-cases/1:1';
  const storage=new Map(stored?[[key,JSON.stringify(stored)]]:[]);
  const context={URL,location:{href:'http://fixture.invalid/students/S1/visa-cases/1'+query+hash,pathname:'/students/S1/visa-cases/1',hash},
    window:{scrollY:800,addEventListener(){},removeEventListener(){},confirm(){return true;},
      scrollBy(x,y){scrolls.push(y);},scrollTo(x,y){scrolls.push(y);}},
    sessionStorage:{getItem(k){return storage.get(k);},setItem(k,v){storage.set(k,v);}},
    document:{querySelectorAll(){return [panel];},addEventListener(){}},requestAnimationFrame(fn){fn();}};
  vm.runInNewContext(source,context);
  return {panel,row,other,editor,details,form,anchor,jumps,scrolls,read:()=>JSON.parse(storage.get(key))};
}

const saved=()=>({stage:'prepare',open:['visa-result-group-lodgement','visa-result-item-14'],
  editOpen:['visa-result-item-14:legacy_student_document_id:9'],
  anchor:'visa-result-item-14',offset:120,scroll:800,resume:true});

test('return to the posted result item restores the prior viewport offset instead of jumping to its top',()=>{
  const h=initialize(saved(),'#visa-result-item-14','?kind=success');
  assert.deepEqual(h.scrolls,[200]);
  assert.deepEqual(h.jumps,[]);
});

test('return reopens the exact file editor and its containing file details',()=>{
  const h=initialize(saved());
  assert.equal(h.details.open,true);
  assert.equal(h.editor.open,true);
});

test('a link to another item keeps its explicit navigation target',()=>{
  const h=initialize(saved(),'#visa-result-item-16');
  assert.deepEqual(h.jumps,['visa-result-item-16']);
  assert.deepEqual(h.scrolls,[]);
});

test('an explicit link to the same item without a successful operation marker still jumps to its top',()=>{
  const h=initialize(saved(),'#visa-result-item-14');
  assert.deepEqual(h.jumps,['visa-result-item-14']);
  assert.deepEqual(h.scrolls,[]);
});

test('document submission uses its containing result item even if another item was focused',()=>{
  const h=initialize();h.editor.open=true;
  h.panel.listeners.focusin({target:{closest(){return h.other;}}});
  h.panel.listeners.submit({target:h.form,defaultPrevented:false,preventDefault(){throw Error('Unexpected submission block');}});
  assert.equal(h.anchor.value,'visa-result-item-14');
  assert.equal(h.read().anchor,'visa-result-item-14');
  assert.deepEqual(h.read().editOpen,['visa-result-item-14:legacy_student_document_id:9']);
});
