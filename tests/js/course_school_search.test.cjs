const test=require('node:test');
const assert=require('node:assert/strict');
const {matchingSchools}=require('../../assets/course-school-search.js');
const options=[{value:'',label:'Select school'},{value:'9',label:'East Doncaster Secondary College'},{value:'10',label:'Doncaster Primary School'},{value:'11',label:'同名学校'},{value:'12',label:'同名学校'}];
test('word search is literal, case insensitive and works in either word order',()=>{
 assert.deepEqual(matchingSchools(options,' DONCASTER east ').map(x=>x.value),['9']);
 assert.equal(matchingSchools(options,'.*').length,0);
 assert.equal(matchingSchools(options,'not present').length,0);
});
test('same school names preserve separate CRM identifiers',()=>{
 assert.deepEqual(matchingSchools(options,'同名').map(x=>x.value),['11','12']);
 assert.equal(matchingSchools(options,'').length,4);
});
