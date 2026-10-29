/* One bounded observation window per explicit operation; never submits work. */
((root)=>{
  'use strict';
  function label(status,phase){
    if(status==='running')return phase==='preparing'?'正在准备搜索环境…':'运行中';
    return ({completed:'已完成',failed:'运行失败',partial:'部分完成',pending:'等待中',skipped:'未执行',cancelled:'已取消',uncertain:'状态待确认'})[status] || '状态待确认';
  }
  function watch({read,update,onStop=()=>{},wait=ms=>new Promise(r=>setTimeout(r,ms)),now=()=>Date.now()}){
    let stopped=false,controller=null;const started=now();
    const done=(async()=>{
      for(let count=0;count<60&&!stopped;count++){
        await wait(1000);if(stopped)return;
        if(now()-started>=90000){onStop('limit');return;}
        controller=new AbortController();const timer=setTimeout(()=>controller?.abort(),Math.min(5000,90000-(now()-started)));
        try{const data=await read(controller.signal);if(stopped)return;update(data);}
        catch(_){if(!stopped)onStop('read_error');return;}
        finally{clearTimeout(timer);controller=null;}
      }
      if(!stopped)onStop('limit');
    })();
    return {done,stop(){stopped=true;controller?.abort();}};
  }
  root.AgentReachProgress=Object.freeze({label,watch});
})(window);
