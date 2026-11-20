(function () {
  'use strict';
  function valuesChanged(current, original) {
    return current.length !== original.length || current.some((value,index) => value !== original[index]);
  }
  function formLocked(form, dirtyForms) {
    return dirtyForms.length===1 && !dirtyForms.includes(form);
  }
  function initialResultGroup(stage) {
    return stage==='result'?'outcome':stage==='followup'?'s56':'lodgement';
  }
  if(typeof module !== 'undefined' && module.exports) module.exports={valuesChanged,formLocked,initialResultGroup};
  if(typeof document === 'undefined') return;
  const lifecycleCleanup = new Map();
  function initialize(restorePosition = true) {
    for (const [panel, cleanup] of lifecycleCleanup) if (!panel.isConnected) { cleanup(); lifecycleCleanup.delete(panel); }
  document.querySelectorAll('[data-visa-result-workspace]').forEach(panel => {
    if(panel.dataset.resultBound) return;
    panel.dataset.resultBound='true';
    const remove = [];
    const listenWindow = (type, listener) => { window.addEventListener(type, listener); remove.push(() => window.removeEventListener(type, listener)); };
    lifecycleCleanup.set(panel, () => remove.forEach(cleanup => cleanup()));
    const one=s=>panel.querySelector(s), all=s=>Array.from(panel.querySelectorAll(s));
    const forms=all('form');
    forms.forEach(form => { form.dataset.businessSyncManaged = '1'; });
    const controls=form=>Array.from(form.elements).filter(field=>field.name && !['hidden','file','submit','button'].includes(field.type));
    const before=new Map(forms.map(form=>[form,controls(form).map(field=>field.value)]));
    const originallyDisabled=new Map(forms.flatMap(form=>Array.from(form.elements)).map(field=>[field,Boolean(field.disabled)]));
    const dirty=new Set();
    const key='aaie.result-workspace.v1:'+location.pathname+':'+panel.dataset.caseId;
    let lastAnchor='', submitting=false;
    const feedback=one('[data-result-feedback]');
    const stored=(()=>{try{return JSON.parse(sessionStorage.getItem(key)||'null');}catch(_){return null;}})();
    function findTarget(id){return all('[id]').find(element=>element.id===id);}
    function reveal(id, scroll) {
      const target=findTarget(id);
      if(!target) return;
      let parent=target;
      while(parent && parent!==panel){if(parent.tagName==='DETAILS')parent.open=true;parent=parent.parentElement;}
      lastAnchor=id;
      if(scroll)target.scrollIntoView({block:'start',behavior:'auto'});
    }
    function remember(resume) {
      const anchor=findTarget(lastAnchor);
      const data={stage:panel.dataset.stage,open:all('details[open]').map(detail=>detail.id || detail.closest('[data-result-item]')?.id).filter(Boolean),
        anchor:lastAnchor,offset:anchor?anchor.getBoundingClientRect().top:null,scroll:window.scrollY,resume:Boolean(resume)};
      try{sessionStorage.setItem(key,JSON.stringify(data));}catch(_){}
    }
    function update() {
      forms.forEach(form=>{
        const changed=valuesChanged(controls(form).map(field=>field.value),before.get(form));
        if(changed) { dirty.add(form); form.dataset.businessSyncDirty = '1'; }
        else { dirty.delete(form); delete form.dataset.businessSyncDirty; }
      });
      forms.forEach(form=>Array.from(form.elements).forEach(field=>{
        if('disabled' in field && field.type!=='hidden')field.disabled=originallyDisabled.get(field)||formLocked(form,Array.from(dirty));
      }));
      all('[data-result-item]').forEach(row=>{
        row.classList.toggle('is-dirty',Array.from(row.querySelectorAll('select,textarea')).some(field=>{
          const form=field.form, index=form?controls(form).indexOf(field):-1;
          return index>=0 && field.value!==before.get(form)?.[index];
        }));
      });
      const materialForm=one('[data-result-save]'), button=one('[data-result-save-button]');
      if(button)button.disabled=!dirty.has(materialForm)||submitting;
      const status=one('[data-result-save-status]');
      if(status)status.textContent=submitting?'正在保存，请稍候…':dirty.has(materialForm)?'材料状态或备注有未保存修改':'没有未保存的材料修改';
      feedback.textContent=dirty.size?'当前区域有未保存修改，请先保存或撤销，再编辑其他区域。':'';
      one('[data-result-reset]').hidden=dirty.size===0;
      panel.dataset.resultDirty=String(dirty.size>0);
    }
    if (restorePosition) {
    const restoreGroups=stored && stored.stage===panel.dataset.stage && Array.isArray(stored.open);
    all('[data-result-group]').forEach(group=>{
      group.open=restoreGroups?stored.open.includes(group.id):group.dataset.resultGroup===initialResultGroup(panel.dataset.stage);
    });
    if(restoreGroups)stored.open.forEach(id=>{
      const target=findTarget(id);if(!target)return;
      if(target.tagName==='DETAILS')target.open=true;
      const details=target.matches('[data-result-item]')?target.querySelector('[data-result-item-details]'):null;if(details)details.open=true;
    });
    }
    panel.addEventListener('focusin',event=>{
      const target=event.target.closest('[data-result-item], #visa-result-dates, [id="visa-result-lodgement"], #visa-result-case-documents');
      if(target)lastAnchor=target.id;
    });
    panel.addEventListener('input',update);panel.addEventListener('change',update);
    all('details').forEach(detail=>detail.addEventListener('toggle',()=>remember(false)));
    panel.addEventListener('click',event=>{
      if(event.target.closest('[data-result-reset]')){
        dirty.forEach(form=>controls(form).forEach((field,index)=>{field.value=before.get(form)[index];}));
        update();return;
      }
      const link=event.target.closest('[data-result-jump]');
      if(link){event.preventDefault();reveal(link.hash.slice(1),true);remember(false);}
      if(event.target.closest('[data-result-resume]'))remember(true);
    });
    panel.addEventListener('submit',event=>{
      if(event.defaultPrevented)return;
      update();
      if(Array.from(dirty).some(form=>form!==event.target)){
        if(!window.confirm('其他区域仍有未保存修改，继续将放弃这些修改。是否继续？')){
          event.preventDefault();feedback.textContent='未保存的修改已保留，可保存或撤销后再继续。';return;
        }
      }
      if(submitting){event.preventDefault();return;}
      const anchor=event.target.querySelector('[name="return_anchor"]');
      if(anchor && lastAnchor.startsWith('visa-result-item-'))anchor.value=lastAnchor;
      remember(true);submitting=true;
      if(event.target===one('[data-result-save]'))update();
      const materialForm=one('[data-result-save]');
      if(event.target===materialForm && materialForm.elements.namedItem('business_command_key') && window.AAIEBusinessChanges?.submitChecklist){
        event.preventDefault();
        window.AAIEBusinessChanges.submitChecklist(materialForm).then(()=>{
          before.set(materialForm,controls(materialForm).map(field=>field.value));
          submitting=false;update();window.dispatchEvent(new Event('aaie:business-change'));
        }).catch(error=>{submitting=false;update();feedback.textContent=error.message;});
      }
    });
    listenWindow('beforeunload',event=>{if(dirty.size&&!submitting){event.preventDefault();event.returnValue='';}});
    listenWindow('pagehide',()=>remember(true));
    listenWindow('pageshow',event=>{if(event.persisted){submitting=false;update();}});
    update();
    if(restorePosition && !panel.closest('[hidden]'))requestAnimationFrame(()=>{
      const hash=location.hash.slice(1);
      if(hash && findTarget(hash) && hash!=='visa-tab-visa-result'){reveal(hash,true);return;}
      if(stored?.resume){
        lastAnchor=stored.anchor||'';reveal(lastAnchor,false);
        const anchor=findTarget(lastAnchor);
        if(anchor && Number.isFinite(stored.offset))window.scrollBy(0,anchor.getBoundingClientRect().top-stored.offset);
        else if(Number.isFinite(stored.scroll))window.scrollTo(0,Math.max(0,stored.scroll));
        remember(false);
      }
    });
  });
  }
  initialize();
  document.addEventListener('aaie:business-fragment-rendered', () => initialize(false));
})();
