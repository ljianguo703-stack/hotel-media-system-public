/* Printable merchant report (A03). Renders only what the server returns; values are inserted as text, never as HTML. */
(function(){
  const params=new URLSearchParams(location.search),root=document.getElementById('report'),form=document.getElementById('controls');let current=null;
  const el=(tag,attrs={},...kids)=>{const n=document.createElement(tag);for(const [k,v] of Object.entries(attrs)){if(k==='class')n.className=v;else if(k==='style')n.setAttribute('style',v);else n.setAttribute(k,v)}for(const kid of kids.flat()){if(kid==null||kid===false)continue;n.append(kid instanceof Node?kid:document.createTextNode(String(kid)))}return n};
  const reasons={image_load_failed:'图片加载失败',unsupported_creative:'素材格式不受支持',invalid_media_url:'素材地址无效',startup_timeout:'视频启动超时',playback_timeout:'视频播放超时',player_error:'播放器错误',background:'电视切到后台'};
  const reasonText=r=>reasons[r]?reasons[r]+'（'+r+'）':r;
  const tierName=t=>({1:'一档 · 全屏',2:'二档 · 全天文字',3:'三档 · 分时文字'})[t]||'未知档位';
  const table=(head,rows,numeric=[])=>el('table',{},el('thead',{},el('tr',{},head.map((h,i)=>el('th',{class:numeric.includes(i)?'num':''},h)))),el('tbody',{},rows.map(r=>el('tr',{},r.map((c,i)=>el('td',{class:numeric.includes(i)?'num':''},c))))));
  const stamp=ms=>new Date(ms).toLocaleString('zh-CN',{hour12:false});
  function render(r){
    current=r;document.title=r.advertiser.name+' 投放报告 '+r.from+' 至 '+r.until;form.from.value=r.from;form.until.value=r.until;
    const t=r.totals,sheet=el('article',{class:'sheet'});
    sheet.append(el('p',{class:'eyebrow'},r.hotel||'酒店影院'),el('h1',{},r.advertiser.name+' · 广告投放报告'),
      el('p',{class:'meta'},'报告期间：'+r.from+' 至 '+r.until+'（'+r.timezone+'）'),el('p',{class:'meta'},'生成时间：'+stamp(r.generated_at)+(r.advertiser.contact?' · 联系人：'+r.advertiser.contact:'')),
      el('div',{class:'cards'},[['全屏开始',t.fullscreen_starts??t.starts],['全屏完成',t.fullscreen_completes??t.completes],['播放失败',t.failed],['文字可见（分钟）',t.visible_minutes],['覆盖客房',t.rooms]].map(([k,v])=>el('div',{class:'card'},el('b',{},v),el('span',{},k)))));
    if(r.empty)sheet.append(el('div',{class:'empty'},r.orders.length?'本期间没有收到关联广告的播放记录。':'本期间该广告主没有生效中的投放单。'));
    for(const o of r.orders){
      const p=o.target_progress,pct=p.percent??0;
      sheet.append(el('h2',{},'投放单 '+o.code),
        el('p',{class:'meta'},o.package_name+' · '+tierName(o.tier)+' · '+o.group+' · 投放周期 '+o.start+' 至 '+o.end+' · '+o.state),
        el('p',{},el('span',{class:'bar'},el('i',{style:'width:'+pct+'%'})),'整单目标进度：'+(p.target?p.actual+' / '+p.target+' '+p.unit+'（'+pct+'%）':p.actual+' '+p.unit+'（未设目标）')),
        ...(o.progress_by_tier||[]).map(v=>el('p',{class:'meta'},tierName(v.tier)+'周期累计：'+v.actual+(v.target?' / '+v.target:'')+' '+v.unit+' · 覆盖 '+v.devices+' 台电视')),
        ...o.warnings.map(w=>el('p',{class:'warn'},'提醒：'+w)),
        el('h3',{},'本报告期间'),
        table(['广告任务','全屏开始','全屏完成','失败','中断','文字可见（分钟）','覆盖客房'],
          (o.campaigns.length?o.campaigns:[]).map(c=>[c.name+(c.exists?'':'（已删除）'),c.fullscreen_starts??c.starts,c.fullscreen_completes??c.completes,c.failed,c.interrupted,c.visible_minutes,c.rooms]).concat([['合计',o.range.fullscreen_starts??o.range.starts,o.range.fullscreen_completes??o.range.completes,o.range.failed,o.range.interrupted,o.range.visible_minutes,o.range.rooms]]),[1,2,3,4,5,6]));
    }
    if(r.orders.length){
      sheet.append(el('h2',{},'每日明细'),table(['日期','全屏开始','全屏完成','失败','中断','文字可见（分钟）'],r.timeline.map(d=>[d.date,d.fullscreen_starts??d.starts,d.fullscreen_completes??d.completes,d.failed,d.interrupted,d.visible_minutes]),[1,2,3,4,5]));
      sheet.append(el('h2',{},'失败原因'),r.failures.length?table(['原因（电视上报）','次数'],r.failures.map(f=>[reasonText(f.reason),f.count]),[1]):el('p',{class:'meta'},'本期间没有失败记录。'));
    }
    sheet.append(el('p',{class:'note'},r.basis));
    root.replaceChildren(sheet);
  }
  function fail(message){root.replaceChildren(el('div',{class:'error'},message))}
  async function load(){
    const q=new URLSearchParams({section:'merchant_report'});for(const k of ['order','advertiser','from','until'])if(params.get(k))q.set(k,params.get(k));
    try{const res=await fetch('/api/admin/operations?'+q,{credentials:'same-origin'});const body=await res.json().catch(()=>({}));
      if(res.status===401)return fail('请先在本窗口登录后台，再重新打开报告。');if(!res.ok)return fail(body.error||('报告生成失败（'+res.status+'）'));render(body)}
    catch(e){fail('无法连接后台服务：'+e.message)}
  }
  form.addEventListener('submit',e=>{e.preventDefault();params.set('from',form.from.value);params.set('until',form.until.value);history.replaceState({},'','?'+params);load()});
  document.getElementById('print').addEventListener('click',()=>window.print());
  document.getElementById('csv').addEventListener('click',()=>{if(!current)return;const rows=[['日期','全屏开始','全屏完成','失败','中断','文字可见分钟']].concat(current.timeline.map(d=>[d.date,d.fullscreen_starts??d.starts,d.fullscreen_completes??d.completes,d.failed,d.interrupted,d.visible_minutes]));
    const blob=new Blob(['\ufeff'+rows.map(r=>r.join(',')).join('\r\n')],{type:'text/csv'}),a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=current.advertiser.name+'_'+current.from+'_'+current.until+'.csv';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000)});
  load();
})();
