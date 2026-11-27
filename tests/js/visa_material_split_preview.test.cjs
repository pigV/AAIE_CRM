const test=require('node:test'),assert=require('node:assert/strict');
const {previewUrl,pageTotal}=require('../../assets/visa-material-split-preview.js');
test('only authenticated same-origin document image routes can be previewed',()=>{
 const origin='http://127.0.0.1:19236';
 assert.equal(previewUrl('/documents/12/preview?size=large',origin,2),origin+'/documents/12/preview?size=large&page=2');
 for(const value of ['https://external.test/documents/12/preview','//external.test/a','javascript:alert(1)','/revenue/documents/1/view','/documents/12/download'])assert.equal(previewUrl(value,origin,1),null);
});
test('page counts use server metadata within a bounded positive range',()=>{
 assert.equal(pageTotal('4'),4);assert.equal(pageTotal('no'),1);assert.equal(pageTotal('-2'),1);assert.equal(pageTotal('999999'),1000);
});
