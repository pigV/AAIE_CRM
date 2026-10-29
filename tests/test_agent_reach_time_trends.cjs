const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const ctx={window:{},Intl,Date,Math,Number,Set,Map};
for(const f of ['agent-reach-ranking.js','agent-reach-time.js']) {const p=path.join(__dirname,'../assets',f);if(fs.existsSync(p))vm.runInNewContext(fs.readFileSync(p,'utf8'),ctx);}
const rank=ctx.window.AgentReachRanking, time=ctx.window.AgentReachTime;
const now='2026-09-09T00:00:00Z';const DAY=86400000;
function item(id,extra={}) {return {source:'opencli',stable_identity:id,keywords:['美国高中'],published_at:'2026-09-08T00:00:00Z',likes:100,latest_search_rank:1,latest_search_run_at:now,...extra};}
const prep=a=>rank.prepare(a,now),ids=a=>Array.from(a,x=>x.stable_identity);
for(const days of [7,30,90,180,365])test('range inclusive exact boundary '+days,()=>{
 const cutoff=Date.parse(now)-days*DAY;
 const input=prep([item('in',{published_at:new Date(cutoff).toISOString()}),item('out',{published_at:new Date(cutoff-1).toISOString()}),item('unknown',{published_at:null})]);
 assert.deepEqual(ids(rank.select(input,{days},now)),['in']);
});
test('all includes unknown and old; no false date',()=>assert.equal(rank.select(prep([item('u',{published_at:null}),item('old',{published_at:'2020-01-01'})]),{days:0},now).length,2));
for(const sort of ['comprehensive','latest','likes','potential'])test('30 days combined '+sort,()=>assert.equal(rank.select(prep([item('x')]),{days:30,sort},now).length,1));
test('XHS restricted to enabled monitored exact keyword; no external reads',()=>{
 const input=prep([item('yes'),item('paused',{keywords:['暂停']}),item('none',{keywords:[]})]);
 assert.deepEqual(ids(rank.filterXhs(input,{days:30,sort:'potential'},now,['美国高中'])),['yes']);
 assert.equal(rank.filterXhs(input,{days:30,keyword:'暂停'},now,['美国高中']).length,0);
 assert.equal(rank.filterXhs(input,{days:30},now,[]).length,0);
});
test('XHS multi sort uses enabled distinct hits; reuses original score',()=>{
 const input=prep([item('single',{keywords:['美国高中','暂停','暂停']}),item('multi',{keywords:['美国高中','留学','留学']})]);
 const output=rank.filterXhs(input,{days:30,sort:'multi'},now,['美国高中','留学']);
 assert.deepEqual(ids(output),['multi','single']);assert.equal(output[0].monitored_keyword_count,2);
 assert.equal(output[0].viral_potential_score,input[1].viral_potential_score);
});
for(const sort of ['potential','latest','likes','multi'])test('XHS sort '+sort,()=>assert.equal(rank.filterXhs(prep([item('x')]),{days:7,sort},now,['美国高中']).length,1));
test('ranking has zero network primitives',()=>assert.doesNotMatch(fs.readFileSync(path.join(__dirname,'../assets/agent-reach-ranking.js'),'utf8'),/\b(fetch|XMLHttpRequest|WebSocket|sendBeacon)\s*\(/));
for(const [iso,expected] of [
 ['2026-06-01T00:00:00Z','01/06/2026 10:00'],
 ['2026-01-01T00:00:00Z','01/01/2026 11:00'],
 ['2026-09-08T23:30:00Z','09/09/2026 09:30'],
 ['2026-10-03T15:59:00Z','04/10/2026 01:59'],
 ['2026-10-03T16:00:00Z','04/10/2026 03:00'],
 ['2026-04-04T15:59:00Z','05/04/2026 02:59'],
 ['2026-04-04T16:00:00Z','05/04/2026 02:00']])test('Melbourne display '+iso,()=>assert.equal(time?.format(iso),expected));
test('unknown/naive/invalid timestamp is never guessed',()=>{for(const v of [null,'','昨天','2026-09-09T08:00:00','2026-02-30T00:00:00Z','2026-09-09T25:00:00Z'])assert.equal(time?.format(v),'未知');});
test('date-only publication preserves precision without fake midnight',()=>{assert.equal(time?.publication('2026-09-09'),'09/09/2026');assert.equal(time?.publication('2026-02-30'),'未知');});
test('publication full timestamp converts across Melbourne date',()=>assert.equal(time?.publication('2026-09-08T23:30:00Z'),'09/09/2026 09:30'));
test('invalid clock normalized by JS never becomes a known publish time',()=>{
 for(const value of ['2026-09-08T24:00:00Z','2026-09-08T23:60:00Z','2026-09-08T23:59:60Z']) {
  const input=prep([item('invalid',{published_at:value})]);
  assert.equal(time.publication(value),'未知');
  assert.equal(input[0].publication_time,null);
  assert.equal(rank.select(input,{days:7},now).length,0);
  assert.equal(rank.filterXhs(input,{days:7},now,['美国高中']).length,0);
 }
});
