const {test}=require('node:test');
const assert=require('node:assert/strict');
const {createRefreshController,createVersionedReader,fetchSnapshotPacket}=require('../../assets/business-change-refresh.js');
const packet=(version,data=version)=>({versions:[{kind:'student',id:'S1',version}],data,etag:String(data),as_of:'2026-11-19T05:00:00Z'});
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};

test('explicit server busy is retryable and preserves fetch authentication and abort signal',async()=>{
 const signal=new AbortController().signal;let request;
 const fetcher=async(url,options)=>{request=options;return {ok:false,status:503,json:async()=>({code:'snapshot_busy'})};};
 await assert.rejects(()=>fetchSnapshotPacket(fetcher,'/snapshot',{signal}),/busy/);
 assert.equal(request.credentials,'same-origin');assert.equal(request.cache,'no-store');assert.equal(request.signal,signal);
 await assert.rejects(()=>fetchSnapshotPacket(async()=>({ok:false,status:503,json:async()=>({code:'other_error'})}),'/snapshot',{}),/read_failed/);
});

test('busy polling retries automatically and applies a fresh snapshot without manual refresh',async t=>{
 t.mock.timers.enable({apis:['setTimeout']});let reads=0;const states=[],applied=[];
 const c=createRefreshController({intervalMs:5000,random:()=>0,readSnapshot:async()=>{if(++reads===1)throw Error('busy');return packet(2);},applySnapshot:p=>applied.push(p.data),onStatus:s=>states.push(s.state)});
 t.after(()=>c.dispose());await c.start();assert.equal(states.at(-1),'busy');
 t.mock.timers.tick(1600);await new Promise(setImmediate);
 assert.deepEqual(applied,[2]);assert.equal(states.at(-1),'current');
});

test('different page poll timers are staggered to avoid simultaneous full rereads',async t=>{
 t.mock.timers.enable({apis:['setTimeout']});let a=0,b=0;
 const first=createRefreshController({intervalMs:5000,random:()=>0,readSnapshot:async()=>{a++;return packet(1);},applySnapshot:()=>{}});
 const second=createRefreshController({intervalMs:5000,random:()=>1,readSnapshot:async()=>{b++;return packet(1);},applySnapshot:()=>{}});
 t.after(()=>{first.dispose();second.dispose();});await first.start();await second.start();
 t.mock.timers.tick(4000);await new Promise(setImmediate);assert.equal(a,2);assert.equal(b,1);
});

test('repeated busy responses back off rather than contesting the slot every two seconds',async t=>{
 t.mock.timers.enable({apis:['setTimeout']});let reads=0;
 const c=createRefreshController({intervalMs:5000,random:()=>0,readSnapshot:async()=>{reads++;throw Error('busy');},applySnapshot:()=>{}});
 t.after(()=>c.dispose());await c.start();
 t.mock.timers.tick(1600);await new Promise(setImmediate);assert.equal(reads,2);
 t.mock.timers.tick(1600);await new Promise(setImmediate);assert.equal(reads,2);
 t.mock.timers.tick(1600);await new Promise(setImmediate);assert.equal(reads,3);
 t.mock.timers.tick(6400);await new Promise(setImmediate);assert.equal(reads,4);
});

test('server-rendered seed avoids a second full render on start and focus while changes and manual refresh still reload',async t=>{
 let full=0,light=0,version=1;const seed=packet(1);
 const c=createRefreshController({initialSnapshot:seed,lightweightVisibility:true,
  readSnapshot:createVersionedReader({initialSnapshot:seed,readVersions:async()=>{light++;return {versions:packet(version).versions};},readSnapshot:async()=>{full++;return packet(version);}}),applySnapshot:()=>{}});
 t.after(()=>c.dispose());await c.start();await c.visibilityChanged();assert.equal(full,0);assert.equal(light,2);
 version=2;await c.visibilityChanged();assert.equal(full,1);
 await c.refresh('manual');assert.equal(full,2);
 await c.notifyCommitted(packet(3).versions);assert.equal(full,3);
});

test('pageshow and focus during an in-flight visible read do not enqueue another read',async t=>{
 const pending=deferred();let reads=0;
 const c=createRefreshController({lightweightVisibility:true,readSnapshot:async()=>{reads++;return pending.promise;},applySnapshot:()=>{}});t.after(()=>c.dispose());
 const run=c.start();await Promise.resolve();c.visibilityChanged();c.visibilityChanged();
 pending.resolve(packet(1));await run;assert.equal(reads,1);
});

test('non-homepage focus retains forced authorization-checked full reads',async t=>{
 let full=0;const seed=packet(1);const c=createRefreshController({initialSnapshot:seed,
  readSnapshot:createVersionedReader({initialSnapshot:seed,readVersions:async()=>({versions:seed.versions}),readSnapshot:async()=>{full++;throw Error('forbidden');}}),applySnapshot:()=>{}});
 t.after(()=>c.dispose());await c.visibilityChanged();assert.equal(full,1);
});

test('changed homepage permissions invalidate same-version cached content',async()=>{
 let full=0;const seed={...packet(1),authorization_context:'allowed'};
 const read=createVersionedReader({initialSnapshot:seed,readVersions:async()=>({versions:seed.versions,authorization_context:'revoked'}),readSnapshot:async()=>{full++;return {...packet(1),authorization_context:'revoked'};}});
 await read({force:false});assert.equal(full,1);
});

test('a hidden change hint forces a full read on return even when versions are unchanged',async t=>{
 let visible=true,full=0;const seed=packet(1);const c=createRefreshController({initialSnapshot:seed,lightweightVisibility:true,isVisible:()=>visible,
  readSnapshot:createVersionedReader({initialSnapshot:seed,readVersions:async()=>({versions:seed.versions}),readSnapshot:async()=>{full++;return seed;}}),applySnapshot:()=>{}});
 t.after(()=>c.dispose());await c.start();visible=false;await c.notifyCommitted();visible=true;await c.visibilityChanged();assert.equal(full,1);
});

test('a busy forced change read remains forced on retry rather than returning old cached current',async t=>{
 t.mock.timers.enable({apis:['setTimeout']});let full=0;const states=[];const seed=packet(1);
 const c=createRefreshController({initialSnapshot:seed,lightweightVisibility:true,random:()=>0,
  readSnapshot:createVersionedReader({initialSnapshot:seed,readVersions:async()=>({versions:seed.versions}),readSnapshot:async()=>{if(++full===1)throw Error('busy');return packet(1,2);}}),applySnapshot:()=>{},onStatus:x=>states.push(x.state)});
 t.after(()=>c.dispose());await c.start();await c.notifyCommitted();assert.equal(states.at(-1),'busy');
 t.mock.timers.tick(1600);await new Promise(setImmediate);assert.equal(full,2);assert.equal(states.at(-1),'current');
});

test('three ordinary page opens with real BroadcastChannel do not cause any full snapshot request',async()=>{
 const {bootstrapBrowser}=require('../../assets/business-change-refresh.js');const name='perf-'+Math.random();let full=0;
 class Channel extends BroadcastChannel {constructor(){super(name);}}
 const disposers=[],originalWindow=global.window;global.window={BroadcastChannel:Channel};
 for(let i=0;i<3;i++){
  const node={dataset:{businessSyncFragment:'dashboard-overview',businessSyncEndpoint:'/api/business-changes/snapshot?surface=dashboard_overview',businessSyncInitialSnapshot:JSON.stringify(packet(1))},closest:()=>null,querySelectorAll:()=>[]};
  const doc={visibilityState:'visible',querySelectorAll:()=>[node],querySelector:()=>null,addEventListener(){},removeEventListener(){}};
  const win={BroadcastChannel:Channel,location:{href:'http://localhost/',origin:'http://localhost'},addEventListener(){},removeEventListener(){},fetch:async url=>{const light=String(url).includes('/versions?');if(!light)full++;return {ok:true,json:async()=>light?{versions:packet(1).versions}:packet(1)};}};
  disposers.push(bootstrapBrowser(doc,win));
 }
 try {await new Promise(resolve=>setTimeout(resolve,40));assert.equal(full,0);} finally {disposers.forEach(f=>f());if(originalWindow===undefined)delete global.window;else global.window=originalWindow;}
});

test('nested chart refresh uses its own endpoint, deduplicates clicks, and displays the outcome', async t => {
 const {bootstrapBrowser} = require('../../assets/business-change-refresh.js');
 const fragmentSelector = '[data-business-sync-fragment]';
 const endpointSelector = '[data-business-sync-endpoint][data-business-sync-fragment]';
 const seed = packet(1);
 const events = {}, requests = [];
 const dashboard = makeFragment('dashboard-overview', 'dashboard_overview');
 const chart = makeFragment('dashboard-workflow', 'dashboard_workflow');
 chart.dataset.businessSyncLocalRefresh = '';
 const controls = {};
 const status = {};
 const button = {textContent: '刷新', closest: selector => ({
  '[data-business-sync-refresh]': button, [fragmentSelector]: chart, [endpointSelector]: chart
 })[selector] || null};
 controls.closest = status.closest = button.closest;
 const children = new Map([
  ['[data-business-sync-controls]', [controls]], ['[data-business-sync-status]', [status]],
  ['[data-business-sync-refresh]', [button]]
 ]);
 dashboard.querySelectorAll = chart.querySelectorAll = selector => children.get(selector) || [];
 function makeFragment(fragment, surface) {
  return {dataset: {businessSyncFragment: fragment, businessSyncEndpoint: `/api/business-changes/snapshot?surface=${surface}`,
    businessSyncInitialSnapshot: JSON.stringify(seed)}, closest: () => null, querySelector: () => null};
 }
 const doc = {visibilityState:'visible', querySelectorAll: () => [dashboard, chart], querySelector: () => null,
  addEventListener: (name, listener) => { events[name] = listener; }, removeEventListener() {}};
 let response = deferred();
 const win = {location:{href:'http://localhost/',origin:'http://localhost'}, addEventListener() {}, removeEventListener() {},
  fetch: async url => {
   const {pathname, searchParams} = new URL(url);
   if (pathname.endsWith('/versions')) return {ok: true, json: async () => ({versions: seed.versions})};
   requests.push(searchParams.get('surface'));
   return response.promise;
  }};
 const dispose = bootstrapBrowser(doc, win);
 t.after(dispose);
 const settled = () => new Promise(setImmediate);
 await settled();
 assert.deepEqual(requests, []);
 const initiallySynced = status.textContent;
 events.click({target: button});
 assert.equal(button.textContent, '刷新中…');
 assert.equal(button.disabled, true);
 events.click({target: button});
 await settled();
 assert.deepEqual(requests, ['dashboard_workflow']);
 const asOf = '2026-11-19T05:00:05Z';
 response.resolve({ok: true, json: async () => ({...seed, as_of: asOf})});
 await settled();
 assert.equal(button.textContent, '刷新');
 assert.equal(button.disabled, false);
 assert.notEqual(status.textContent, initiallySynced);
 assert.match(status.textContent, /已同步.*上次同步.*:05/);
 assert.deepEqual(requests, ['dashboard_workflow']);

 response = deferred();
 events.click({target: button});
 await settled();
 response.reject(new Error('simulated offline'));
 await settled();
 assert.equal(button.disabled, false);
 assert.match(status.textContent, /更新未完成，点击刷新重试.*上次同步.*:05/);
 assert.deepEqual(requests, ['dashboard_workflow', 'dashboard_workflow']);
});

test('a real POST navigation publishes to other pages after navigation, while GET and logout do not',async()=>{
 const {bootstrapBrowser}=require('../../assets/business-change-refresh.js');const name='post-'+Math.random();
 class Channel extends BroadcastChannel {constructor(){super(name);}}
 const observer=new Channel(),messages=[];observer.onmessage=e=>messages.push(e.data);const storage=new Map();
 const win={BroadcastChannel:Channel,location:{href:'http://localhost/students',origin:'http://localhost'},sessionStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},addEventListener(){},removeEventListener(){}};
 const makeDoc=()=>{const events={};return {events,querySelectorAll:()=>[],addEventListener:(key,f)=>{events[key]=f;},removeEventListener(){}};};
 const source=makeDoc();let dispose=bootstrapBrowser(source,win);
 try {
  source.events.submit({target:{method:'get',action:'/students'}});source.events.submit({target:{method:'post',action:'/logout'}});assert.equal(storage.size,0);
  source.events.submit({target:{method:'post',action:'/students/new'}});assert.equal(storage.size,1);assert.equal(messages.length,0);
  dispose();dispose=bootstrapBrowser(makeDoc(),win);await new Promise(resolve=>setTimeout(resolve,30));
  assert.deepEqual(messages,[{type:'changed'}]);assert.equal(storage.size,0);
 } finally {dispose();observer.close();}
});

test('a valid dashboard read lasting twelve seconds can still apply, with a bounded deadline',async t=>{
 t.mock.timers.enable({apis:['setTimeout']});
 let pending=deferred(),signal;const states=[],applied=[];
 const c=createRefreshController({readSnapshot:request=>{signal=request.signal;return pending.promise;},
  applySnapshot:value=>applied.push(value.data),onStatus:value=>states.push(value.state)});
 t.after(()=>c.dispose());
 const running=c.refresh();await Promise.resolve();
 t.mock.timers.tick(12000);await Promise.resolve();
 assert.equal(signal.aborted,false);
 pending.resolve(packet(1));await running;assert.deepEqual(applied,[1]);
 pending=deferred();const second=c.refresh();await Promise.resolve();t.mock.timers.tick(30000);await second;
 assert.equal(signal.aborted,true);assert.equal(states.at(-1),'error');
});

test('polling checks versions cheaply and rereads full facts on changes or every thirty seconds',async()=>{
 let now=0,version=1,full=0,light=0;
 const read=createVersionedReader({initialSnapshot:packet(1),now:()=>now,
  readVersions:async()=>{light++;return {versions:packet(version).versions};},
  readSnapshot:async()=>{full++;return packet(version);}});
 await read({force:false});assert.equal(full,0);assert.equal(light,1);
 version=2;assert.equal((await read({force:false})).etag,'2');assert.equal(full,1);
 await read({force:false});assert.equal(full,1);
 now=30000;await read({force:false});assert.equal(full,2);
 await read({force:true});assert.equal(full,3);
});

test('a failed version check never returns an old snapshot as newly synchronized',async()=>{
 let failed=true;
 const read=createVersionedReader({initialSnapshot:packet(1),
  readVersions:async()=>{if(failed)throw Error('forbidden');return {versions:packet(2).versions};},
  readSnapshot:async()=>packet(2)});
 await assert.rejects(read({force:false}),/forbidden/);
 failed=false;assert.equal((await read({force:false})).etag,'2');
});

test('initial server snapshot prevents a false new-data warning while editing',async t=>{
 const states=[];let applies=0;
 const c=createRefreshController({initialSnapshot:packet(1),readSnapshot:async()=>packet(1),canApply:()=>false,
  applySnapshot:()=>applies++,onStatus:s=>states.push(s.state)});t.after(()=>c.dispose());
 await c.refresh();assert.equal(applies,0);assert.equal(states.at(-1),'current');
});

test('a newer source still waits for an edit or preview to finish',async t=>{
 let blocked=true;const applied=[];const states=[];
 const c=createRefreshController({initialSnapshot:packet(1),readSnapshot:async()=>packet(2),canApply:()=>!blocked,
  applySnapshot:s=>applied.push(s.data),onStatus:s=>states.push(s.state)});t.after(()=>c.dispose());
 await c.refresh();assert.deepEqual(applied,[]);assert.equal(states.at(-1),'waiting');
 blocked=false;await c.flush();assert.deepEqual(applied,[2]);
});

test('stale response after committed change is ignored and reread',async t=>{
 const first=deferred();let reads=0;const applied=[];
 const controller=createRefreshController({readSnapshot:()=>++reads===1?first.promise:Promise.resolve(packet(2)),applySnapshot:s=>applied.push(s.data),isVisible:()=>true});t.after(()=>controller.dispose());
 const running=controller.refresh();controller.notifyCommitted([{kind:'student',id:'S1',version:2}]);first.resolve(packet(1));await running;
 assert.deepEqual(applied,[2]);assert.equal(reads,2);
});

test('response cannot regress a previously applied subject version',async t=>{
 let value=2;const applied=[];const states=[];
 const c=createRefreshController({readSnapshot:async()=>packet(value),applySnapshot:s=>applied.push(s.data),onStatus:s=>states.push(s.state)});t.after(()=>c.dispose());
 await c.refresh();value=1;await c.refresh();assert.deepEqual(applied,[2]);assert.ok(states.includes('stale'));
});

test('dirty content buffers a snapshot until it can safely apply',async t=>{
 let dirty=true;const applied=[];
 const c=createRefreshController({readSnapshot:async()=>packet(1),applySnapshot:s=>applied.push(s.data),canApply:()=>!dirty});t.after(()=>c.dispose());
 await c.refresh();assert.deepEqual(applied,[]);dirty=false;await c.flush();assert.deepEqual(applied,[1]);
});

test('hidden page makes no background request and catches up on return',async t=>{
 let visible=false,reads=0;
 const c=createRefreshController({readSnapshot:async()=>{reads++;return packet(1);},applySnapshot:()=>{},isVisible:()=>visible});t.after(()=>c.dispose());
 await c.refresh();assert.equal(reads,0);visible=true;await c.visibilityChanged();assert.equal(reads,1);
});

test('repeated same snapshot is not reapplied',async t=>{
 let applies=0;const c=createRefreshController({readSnapshot:async()=>packet(1),applySnapshot:()=>applies++});t.after(()=>c.dispose());
 await c.refresh();await c.refresh();assert.equal(applies,1);
});

test('an unchanged snapshot does not falsely report new data during editing',async t=>{
 let dirty=false;const states=[];
 const c=createRefreshController({readSnapshot:async()=>packet(1),applySnapshot:()=>{},canApply:()=>!dirty,onStatus:s=>states.push(s.state)});t.after(()=>c.dispose());
 await c.refresh();dirty=true;await c.refresh();assert.equal(states.at(-1),'current');
});

test('broadcast only carries a change hint and never subject data',async t=>{
 const messages=[];const c=createRefreshController({readSnapshot:async()=>packet(1),applySnapshot:()=>{},channel:{postMessage:m=>messages.push(m)}});t.after(()=>c.dispose());
 await c.notifyCommitted([{kind:'student',id:'S1',version:1}]);assert.deepEqual(messages,[{type:'changed'}]);
});

test('failed read keeps old last-applied time and reports failure',async t=>{
 let failed=false;const states=[];const c=createRefreshController({readSnapshot:async()=>{if(failed)throw Error('network');return packet(1);},applySnapshot:()=>{},onStatus:s=>states.push(s)});t.after(()=>c.dispose());
 await c.refresh();failed=true;await c.refresh();assert.equal(states.at(-1).state,'error');assert.equal(states.at(-1).lastAppliedAsOf,'2026-11-19T05:00:00Z');
});

test('disposed controller ignores pending response',async()=>{
 const pending=deferred();let applied=false;const c=createRefreshController({readSnapshot:()=>pending.promise,applySnapshot:()=>{applied=true;}});
 const task=c.refresh();c.dispose();pending.resolve(packet(1));await task;assert.equal(applied,false);
});

test('expired session reports login requirement without changing content',async t=>{
 const states=[];let applies=0;
 const c=createRefreshController({readSnapshot:async()=>{throw Error('login_required');},applySnapshot:()=>applies++,onStatus:s=>states.push(s.state)});t.after(()=>c.dispose());
 await c.refresh();assert.equal(applies,0);assert.equal(states.at(-1),'login_required');
});

test('unselecting the last student clears the selection hold, while real drafts remain protected',()=>{
 const {bootstrapBrowser}=require('../../assets/business-change-refresh.js');
 const events={};const doc={querySelectorAll:()=>[],addEventListener:(k,f)=>{events[k]=f;},removeEventListener(){}};
 const win={addEventListener(){},removeEventListener(){},setTimeout:f=>f()};
 const dispose=bootstrapBrowser(doc,win);
 const checkbox={name:'student_ids',checked:true};
 const form={dataset:{businessSyncSelection:'1'},elements:[checkbox]};
 events.change({target:{form}});assert.equal(form.dataset.businessSyncDirty,'1');
 checkbox.checked=false;events.change({target:{form}});assert.equal(form.dataset.businessSyncDirty,undefined);
 const draft={dataset:{},elements:[]};events.input({target:{form:draft}});
 assert.equal(draft.dataset.businessSyncDirty,'1');dispose();
});
