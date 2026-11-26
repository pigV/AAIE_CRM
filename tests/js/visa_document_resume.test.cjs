// Run the actual material script against a minimal event-capable DOM fixture.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(process.env.VISA_RESUME_SCRIPT || path.resolve(__dirname, '../../assets/visa-material-workspace.js'), 'utf8');

function initialize(stored, query = '') {
  const node = (extra = {}) => Object.assign({dataset:{}, value:'', textContent:'', hidden:false,
    listeners:{}, addEventListener(type, fn){this.listeners[type]=fn;},
    setAttribute(){}, classList:{toggle(){}, add(){}},
    matches(){return false;}, closest(){return null;}, replaceChildren(){}, append(){}}, extra);
  const save = node({elements:{namedItem(){return null;}}});
  const status = node({value:'uploaded',form:save});
  const label = node({textContent:'Demo original.pdf'});
  const row = node({id:'material-checklist-item-14',dataset:{categoryIndex:'0',required:'required',search:'Demo original.pdf'},
    getBoundingClientRect(){return {top:320};},
    querySelector(selector){return selector==='select'?status:selector==='[data-search-label]'?label:node();},
    querySelectorAll(){return [status];}});
  const detail = node({dataset:{materialDetails:'14'},open:false});
  const editor = node({open:false, querySelector(){return {name:'legacy_student_document_id',value:'9'};}});
  const filter = node({value:'all',options:[{value:'all'},{value:'uploaded'},{value:'missing'}]});
  const search = node();
  const single = {'[data-material-search]':search,'[data-material-filter]':filter,
    '[data-category-mobile]':node(),'[data-material-save]':save,
    '[data-material-save-button]':node(),'[data-material-save-status]':node(),
    '[data-material-matches]':node(),'[data-material-empty]':node()};
  const panel = node({id:'material-checklist',isConnected:true,
    querySelector(selector){return single[selector] || null;},
    querySelectorAll(selector){
      return selector==='[data-material-item]'?[row]:selector==='[data-material-details]'?[detail]:
        selector==='[data-material-details][open]'?(detail.open?[detail]:[]):
        selector==='.visa-document-edit'?[editor]:[];
    }});
  const key='aaie.material-workspace.v1:/students/S1/visa-cases/1:material-checklist';
  const storage = new Map(stored?[[key,JSON.stringify(stored)]]:[]);
  const scrolls=[];
  const context={URL,location:{href:'http://fixture.invalid/students/S1/visa-cases/1'+query,pathname:'/students/S1/visa-cases/1',hash:query.includes('#')?'#'+query.split('#')[1]:''},
    window:{scrollY:800,addEventListener(){},removeEventListener(){},scrollBy(x,y){scrolls.push(y);},scrollTo(){}},
    sessionStorage:{getItem(k){return storage.get(k);},setItem(k,v){storage.set(k,v);}},
    document:{querySelectorAll(){return [panel];},addEventListener(){},createElement(){return node();},createTextNode(text){return text;}},
    requestAnimationFrame(fn){fn();}};
  vm.runInNewContext(source,context);
  return {panel,row,detail,editor,filter,search,scrolls,read:()=>JSON.parse(storage.get(key))};
}

test('return restores the same editor, expanded material, search, filter and viewport offset', () => {
  const h=initialize({query:'Demo',filter:'uploaded',category:'',open:['14'],collapsed:[],
    editOpen:['legacy_student_document_id:9'],anchor:'material-checklist-item-14',offset:120,scroll:800,resume:true});
  assert.equal(h.search.value,'Demo');
  assert.equal(h.filter.value,'uploaded');
  assert.equal(h.detail.open,true);
  assert.equal(h.editor.open,true);
  assert.deepEqual(h.scrolls,[200]);
});

test('document submission records its row and current editor state and filter', () => {
  const h=initialize();h.editor.open=true;h.detail.open=true;h.filter.value='uploaded';
  const fields={return_document_tab:{value:'material_checklist'},material_filter:{value:'all'}};
  const form={elements:{namedItem(name){return fields[name] || null;}},closest(){return h.row;}};
  h.panel.listeners.submit({target:form,defaultPrevented:false,preventDefault(){throw Error('unexpected block');}});
  assert.equal(fields.material_filter.value,'uploaded');
  const state=h.read();
  assert.equal(state.anchor,'material-checklist-item-14');
  assert.equal(state.resume,true);
  assert.deepEqual(state.open,['14']);
  assert.deepEqual(state.editOpen,['legacy_student_document_id:9']);
});

test('renamed or removed file does not hide the returned material under the previous filename search', () => {
  const h=initialize({query:'old missing filename',filter:'uploaded',category:'',open:['14'],collapsed:[],resume:false},
    '?kind=success#material-checklist-item-14');
  assert.equal(h.row.hidden,false);
});
