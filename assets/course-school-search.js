(function () {
  'use strict';
  function matchingSchools(options, query) {
    const words = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
    return options.filter(option => option.value && words.every(word => option.label.toLocaleLowerCase().includes(word)));
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = {matchingSchools};
  if (typeof document === 'undefined') return;
  function initialize() {
    document.querySelectorAll('[data-course-school-picker]').forEach(picker => {
      if (picker.dataset.searchBound) return;
      const input = picker.querySelector('[data-course-school-search]');
      const select = picker.querySelector('[data-course-school-value]');
      const results = picker.querySelector('[role=listbox]');
      const status = picker.querySelector('[data-course-school-status]');
      const options = Array.from(select.options, option => ({value: option.value, label: option.textContent.trim()}));
      let active = -1, matches = [];
      const close = () => { results.hidden = true; input.setAttribute('aria-expanded','false'); input.removeAttribute('aria-activedescendant'); };
      const choose = option => {
        select.value = option.value; input.value = option.label; input.setCustomValidity(''); close();
        status.textContent = '已选择：' + option.label + '（学校 #' + option.value + '）';
        select.dispatchEvent(new Event('change', {bubbles:true}));
      };
      const filter = () => {
        const found = matchingSchools(options, input.value);
        matches = found.slice(0,30); active = -1; results.replaceChildren();
        matches.forEach((option,index) => {
          const button = document.createElement('button'); button.type='button'; button.tabIndex=-1;
          button.setAttribute('role','option'); button.setAttribute('aria-selected','false'); button.id=results.id+'-'+index;
          button.textContent=option.label+' · #'+option.value;
          button.addEventListener('click', () => {input.focus(); choose(option);}); results.append(button);
        });
        results.hidden = false; input.setAttribute('aria-expanded','true'); input.removeAttribute('aria-activedescendant');
        status.textContent = found.length ? `找到 ${found.length} 所学校，请选择${found.length>30?'（显示前30所，请继续输入缩小范围）':''}。` : '未找到学校，请调整名称；不能仅输入文字保存。';
        if (!found.length) { const empty=document.createElement('p'); empty.textContent='没有匹配学校';results.append(empty); }
      };
      picker.dataset.searchBound = 'true';
      const selected = options.find(option => option.value && option.value===select.value);
      if (selected) {input.value=selected.label;status.textContent='当前学校：'+selected.label;}
      input.hidden=false; select.hidden=true; input.required=true;
      input.addEventListener('input', () => {
        select.value=''; input.setCustomValidity(input.value?'请从搜索结果选择CRM学校。':'');
        select.dispatchEvent(new Event('change',{bubbles:true}));filter();
      });
      input.addEventListener('focus', filter);
      input.addEventListener('keydown', event => {
        if (event.key==='Escape') {close();return;}
        if (event.key==='ArrowDown'||event.key==='ArrowUp') {
          event.preventDefault(); if(results.hidden)filter(); if(!matches.length)return;
          active=(active+(event.key==='ArrowDown'?1:-1)+matches.length)%matches.length;
          Array.from(results.children).forEach((button,index)=>button.setAttribute('aria-selected',String(index===active)));
          input.setAttribute('aria-activedescendant',results.children[active].id);
          results.children[active].scrollIntoView({block:'nearest'});
        } else if(event.key==='Enter'&&!results.hidden) {
          event.preventDefault(); if(active>=0)choose(matches[active]); else if(matches.length===1)choose(matches[0]);
        }
      });
      picker.addEventListener('focusout', event => {if(!picker.contains(event.relatedTarget))close();});
      select.addEventListener('change',()=>{if(!input.matches(':focus')){const chosen=options.find(o=>o.value&&o.value===select.value);input.value=chosen?.label||'';input.setCustomValidity('');}});
    });
  }
  initialize();
  document.addEventListener('aaie:business-fragment-rendered',initialize);
})();
