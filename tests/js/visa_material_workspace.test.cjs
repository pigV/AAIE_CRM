const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const file = path.resolve(__dirname, '../../assets/visa-material-workspace.js');
test('material filters combine status, category and literal multilingual search', () => {
  assert.ok(fs.existsSync(file), 'material workspace interaction missing');
  const { matchesMaterial } = require(file);
  const item = {status:'missing', required:'required', category:'0', text:'Passport 护照 陈 <script> [1]'};
  assert.equal(matchesMaterial(item, {filter:'required_unfinished',category:'0',query:'护照'}), true);
  assert.equal(matchesMaterial(item, {filter:'uploaded',category:'0',query:''}), false);
  assert.equal(matchesMaterial(item, {filter:'all',category:'1',query:''}), false);
  assert.equal(matchesMaterial(item, {filter:'all',category:'',query:'<script> [1]'}), true);
  assert.equal(matchesMaterial(item, {filter:'all',category:'',query:'.*'}), false);
  assert.equal(matchesMaterial({...item,status:'not_applicable'}, {filter:'required_unfinished',category:'',query:''}),false);
});
test('counts follow unsaved state changes without calling received reviewed', () => {
  assert.ok(fs.existsSync(file), 'material workspace interaction missing');
  const { materialCounts } = require(file);
  const rows = [{status:'missing',required:'required'}, {status:'missing',required:'conditional'}, {status:'uploaded',required:'required'}, {status:'not_applicable',required:'required'}];
  assert.deepEqual(materialCounts(rows),{all:4,required_unfinished:1,unfinished:2,uploaded:1,not_applicable:1});
  rows[0].status='uploaded';
  assert.deepEqual(materialCounts(rows),{all:4,required_unfinished:0,unfinished:1,uploaded:2,not_applicable:1});
});
test('a successful returned item stays visible until the user changes the view', () => {
  const { materialVisible } = require(file);
  assert.equal(typeof materialVisible,'function');
  const item={status:'uploaded',required:'required',category:'0',text:'Passport 护照'};
  const state={filter:'required_unfinished',category:'0',query:'护照'};
  assert.equal(materialVisible(item,state,{recent:true}),true);
  assert.equal(materialVisible(item,state,{dirty:true}),true);
  assert.equal(materialVisible(item,state,{}),false);
});
