/* Display only. Never rewrite source timestamps, schedules or result ordering. */
(function(root) {
  'use strict';
  const timezone='Australia/Melbourne';
  const formatter=new Intl.DateTimeFormat('en-GB',{timeZone:timezone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'});
  function calendar(value) {
    if(typeof value!=='string'||!/^\d{4}-\d{2}-\d{2}$/.test(value))return false;
    const [y,m,d]=value.split('-').map(Number);
    // Validate at UTC solely to reject impossible calendar dates, not to infer a zone.
    const date=new Date(value+'T00:00:00Z');
    return Number.isFinite(date.getTime()) && date.getUTCFullYear()===y && date.getUTCMonth()+1===m && date.getUTCDate()===d;
  }
  function instant(value) {
    if(typeof value!=='string'||!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value)||!calendar(value.slice(0,10)))return null;
    if(+value.slice(11,13)>23||+value.slice(14,16)>59||+value.slice(17,19)>59)return null;
    const t=Date.parse(value);return Number.isFinite(t)?t:null;
  }
  function format(value) {
    const t=instant(value);if(t===null)return '未知';
    const p={};formatter.formatToParts(new Date(t)).forEach(x=>{p[x.type]=x.value;});
    return `${p.day}/${p.month}/${p.year} ${p.hour}:${p.minute}`;
  }
  function publication(value) {
    if(calendar(value))return `${value.slice(8,10)}/${value.slice(5,7)}/${value.slice(0,4)}`;
    return format(value);
  }
  root.AgentReachTime=Object.freeze({format,publication,timezone});
})(window);
