/* Saved-result views only. No I/O. Scores are heuristic, never a platform index. */
(function (root) {
  'use strict';
  const zone = 'Australia/Melbourne';
  const day = 86400000;
  const validNumber = v => typeof v === 'number' && Number.isFinite(v) && v >= 0 ? v : null;
  const validRank = v => Number.isSafeInteger(v) && v > 0 ? v : null;
  const compareText = (a,b) => a === b ? 0 : (String(a) < String(b) ? -1 : 1);
  function instant(value) {
    if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value)) return null;
    if (+value.slice(11,13)>23 || +value.slice(14,16)>59 || +value.slice(17,19)>59) return null;
    const date=value.slice(0,10);
    const calendar=new Date(date+'T00:00:00Z');
    if(!Number.isFinite(calendar.getTime()) || calendar.toISOString().slice(0,10)!==date) return null;
    const t = Date.parse(value); return Number.isFinite(t) ? t : null;
  }
  function publication(value, asOf) {
    if (typeof value !== 'string') return null;
    let t;
    if (/^\d{4}-\d{2}-\d{2}$/.test(value)) {
      const [y,m,d] = value.split('-').map(Number);
      const midnight = Date.UTC(y,m-1,d);
      if (new Date(midnight).toISOString().slice(0,10) !== value) return null;
      // Resolve the calendar date in the declared zone; never assume +10/+11.
      t = midnight;
      for (let i=0;i<3;i++) {
        const parts = new Intl.DateTimeFormat('en-GB',{timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).formatToParts(new Date(t));
        const p={}; parts.forEach(x=>{p[x.type]=x.value;});
        const local = Date.UTC(+p.year,+p.month-1,+p.day,+p.hour,+p.minute,+p.second);
        t -= local-midnight;
      }
    } else {
      t=instant(value);
      if (t !== null) {
        const date=value.slice(0,10), [y,m,d]=date.split('-').map(Number);
        if(new Date(Date.UTC(y,m-1,d)).toISOString().slice(0,10)!==date) t=null;
      }
    }
    return t !== null && t <= asOf ? t : null;
  }
  function rankContext(item, keyword) {
    if (keyword) {
      const r=(Array.isArray(item.keyword_search_ranks)?item.keyword_search_ranks:[]).find(x=>x.keyword===keyword);
      return {rank:validRank(r?.search_rank),time:instant(r?.run_started_at),run:r?.run_id || ''};
    }
    return {rank:validRank(item.latest_search_rank),time:instant(item.latest_search_run_at),run:item.latest_search_run_id || ''};
  }
  function prepare(items, asOfISO) {
    const asOf=instant(asOfISO); if(asOf===null) throw new Error('invalid_score_reference_time');
    return items.map(item=>{
      const likes=validNumber(item.likes), t=publication(item.published_at,asOf);
      const rank=rankContext(item,'').rank;
      const keywords=[...new Set((Array.isArray(item.keywords)?item.keywords:[]).filter(x=>typeof x==='string'&&x.length))];
      const age=t===null?null:(asOf-t)/day;
      const likeScore=likes===null?0:Math.min(Math.log1p(likes)/Math.log1p(10000),1);
      const fresh=age===null?0:Math.pow(2,-age/30);
      const rankScore=rank===null?0:1/Math.sqrt(rank);
      const multi=Math.min(Math.max(keywords.length-1,0)/3,1);
      const score=Math.round(100*(.55*likeScore+.25*fresh+.15*rankScore+.05*multi));
      const labels=[];
      if(likes!==null&&likes>=1000)labels.push('高赞');
      if(age!==null&&age<=7)labels.push('最近发布');
      if(rank!==null&&rank<=5)labels.push('搜索排名靠前');
      if(keywords.length>1)labels.push('历史命中'+keywords.length+'个关键词');
      return {...item,keywords,parsed_likes:likes,publication_time:t===null?null:new Date(t).toISOString(),original_search_rank:rank,viral_potential_score:score,viral_potential_labels:labels,viral_potential_band:score>=80?'高潜力':score>=60?'值得关注':score>=40?'一般':'较低'};
    });
  }
  const desc=(a,b)=>a===b?0:a===null?1:b===null?-1:b-a;
  const asc=(a,b)=>a===b?0:a===null?1:b===null?-1:a-b;
  function select(items, options, asOfISO) {
    const asOf=instant(asOfISO); if(asOf===null)throw new Error('invalid_score_reference_time');
    const {sort='comprehensive',days=0,keyword=''}=options;
    if(!['comprehensive','latest','likes','potential','multi'].includes(sort)||![0,7,30,90,180,365].includes(days)||typeof keyword!=='string')throw new Error('invalid_result_filter');
    return items.filter(x=>{
      const t=instant(x.publication_time);
      return (!keyword||x.keywords.includes(keyword))&&(!days||(t!==null&&t>=asOf-days*day&&t<=asOf));
    }).slice().sort((a,b)=>{
      const ar=rankContext(a,keyword),br=rankContext(b,keyword);
      const date=desc(instant(a.publication_time),instant(b.publication_time));
      const rank=asc(ar.rank,br.rank);
      const stable=compareText(a.source||'',b.source||'')||compareText(a.stable_identity||'',b.stable_identity||'');
      if(sort==='comprehensive')return desc(ar.time,br.time)||compareText(ar.run,br.run)||rank||stable;
      if(sort==='latest')return date||rank||stable;
      if(sort==='likes')return desc(a.parsed_likes,b.parsed_likes)||date||rank||stable;
      if(sort==='multi')return desc(a.monitored_keyword_count ?? a.keywords.length,b.monitored_keyword_count ?? b.keywords.length)||date||rank||stable;
      return desc(a.viral_potential_score,b.viral_potential_score)||date||rank||stable;
    });
  }
  function filterXhs(items, options, asOfISO, enabledKeywords) {
    if (!Array.isArray(enabledKeywords) || enabledKeywords.some(k => typeof k !== 'string')) throw new Error('invalid_monitored_keywords');
    const enabled = new Set(enabledKeywords);
    const monitored = items.filter(item => item.source === 'opencli').map(item => {
      const hits = [...new Set(item.keywords.filter(k => enabled.has(k)))];
      return {...item, monitored_keywords:hits, monitored_keyword_count:hits.length};
    }).filter(item => item.monitored_keyword_count > 0);
    if (options.keyword && !enabled.has(options.keyword)) return [];
    // Reuse prepared score unchanged: same V1 formula and historical observations.
    return select(monitored, options, asOfISO);
  }
  root.AgentReachRanking=Object.freeze({prepare,select,filterXhs});
})(window);
