const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const file=path.resolve(__dirname,'../../assets/visa-result-workspace.js');
test('dirty checks notice date and note changes and reset when restored',()=>{
 assert.ok(fs.existsSync(file),'result workspace interactions missing');
 const {valuesChanged}=require(file);
 assert.equal(valuesChanged(['2026-11-19','note'],['2026-11-19','note']),false);
 assert.equal(valuesChanged(['2026-11-20','note'],['2026-11-19','note']),true);
 assert.equal(valuesChanged(['2026-11-19','<note>'],['2026-11-19','note']),true);
 assert.equal(valuesChanged(['2026-11-19','note'],['2026-11-19','note']),false);
});
test('the first edited area locks other forms until saved or reverted',()=>{
 const {formLocked}=require(file);
 assert.equal(typeof formLocked,'function');
 assert.equal(formLocked('dates',['materials']),true);
 assert.equal(formLocked('materials',['materials']),false);
 assert.equal(formLocked('dates',[]),false);
 // Multiple external/autofilled edits must still allow explicit recovery.
 assert.equal(formLocked('dates',['materials','dates']),false);
});
test('the current stage chooses the first useful document group',()=>{
 const {initialResultGroup}=require(file);
 assert.equal(typeof initialResultGroup,'function');
 assert.equal(initialResultGroup('result'),'outcome');
 assert.equal(initialResultGroup('followup'),'s56');
 assert.equal(initialResultGroup('waiting'),'lodgement');
 assert.equal(initialResultGroup('prepare'),'lodgement');
});
