/* 1.1.0 media automation center: health, the few owner actions, and the overview attention card.
   Every call goes through the hotel admin API; the browser never sees native API keys. */
function mediaCenterUi(){return {
  mediaCenter:null,mediaCenterLoading:false,mediaCenterError:'',mediaCenterAt:0,mcPolicy:'allow',
  mcSubtitles:{assrt_token:'',opensubtitles_username:'',opensubtitles_password:''},
  mcPolicyHelp:{
    allow:'电视用自身解码器直接播放原片，现场电视已验证能播 HEVC。能下载到的资源最多。',
    prefer_h264:'同一部片有 H.264 版本就先下 H.264，没有再用 HEVC。适合部分电视较老的门店。',
    h264_only:'完全不下 HEVC。对老电视最稳，但不少新片和剧集会找不到资源。'},
  get mediaCenterIssues(){return this.mediaCenter?.issues||[]},
  get mediaCenterChecked_(){return !!this.mediaCenter?.checked_at},
  get mediaCenterAttention(){return this.mediaCenterIssues.filter(i=>i.level==='error'||i.level==='warning').length},
  get mediaCenterChecked(){
    if(this.mediaCenterLoading)return '正在检查各项服务…';
    if(!this.mediaCenter?.checked_at)return '尚未检查';
    return '上次检查 '+this.date(this.mediaCenter.checked_at)+(this.mediaCenter.refreshing?'（正在更新）':'');
  },
  get mcSubtitlesDirty(){const s=this.mcSubtitles;return !!(s.assrt_token||s.opensubtitles_username||s.opensubtitles_password)},
  get mcSubtitleWanted(){const s=this.mediaCenter?.subtitles;if(!s)return '字幕状态尚未读取';const count=n=>Number.isInteger(n)?n+' 项':'未读取';return '待补字幕：电影 '+count(s.wanted_movies)+' · 剧集 '+count(s.wanted_episodes)},
  async loadMediaCenter(refresh=false){
    if(!this.user||!this.data.media_automation||this.mediaCenterLoading)return;
    if(!refresh&&this.mediaCenter&&Date.now()-this.mediaCenterAt<60000)return;
    this.mediaCenterLoading=true;this.mediaCenterError='';
    try{this.mcApply(await this.api('/media-automation'+(refresh&&this.administrator?'?refresh=1':'')))}
    catch(e){this.mediaCenterError=e.message}finally{this.mediaCenterLoading=false}
  },
  mcApply(status){this.mediaCenter=status;this.mediaCenterAt=status?.checked_at?Date.now():0;if(status?.policy?.hevc)this.mcPolicy=status.policy.hevc},
  async mediaCenterAction(action,extra={}){
    if(!this.administrator||this.busy)return;
    this.busy=true;this.mediaCenterError='';
    try{const r=await this.api('/media-automation',{action,...extra});this.mcApply(r.status);this.notify(r.message);return r}
    catch(e){this.notify(e.message);this.mediaCenterError=e.message}finally{this.busy=false}
  },
  async mcSaveSubtitles(){
    const s=this.mcSubtitles,body={};
    if(s.opensubtitles_password&&!s.opensubtitles_username)return this.notify('请同时填写 OpenSubtitles 用户名');
    if(s.assrt_token)body.assrt_token=s.assrt_token;
    if(s.opensubtitles_username||s.opensubtitles_password){body.opensubtitles_username=s.opensubtitles_username;body.opensubtitles_password=s.opensubtitles_password}
    const r=await this.mediaCenterAction('set_subtitles',body);
    if(r)this.mcSubtitles={assrt_token:'',opensubtitles_username:'',opensubtitles_password:''};
  },
  async mcDisableSubtitles(){
    if(!confirm('关闭射手网和 OpenSubtitles 在线字幕源？内嵌字幕仍然保留。'))return;
    await this.mediaCenterAction('set_subtitles',{assrt_token:'',opensubtitles_username:'',opensubtitles_password:''});
  },
  mcAdminAction(action){return ['repair_downloaders','retest_indexers'].includes(action)},
  mcActionLabel(action){
    if(!action)return '';
    if(action==='repair_downloaders')return '修复下载器连接';
    if(action==='retest_indexers')return '重新测试搜索源';
    if(action.startsWith('open:'))return '打开 '+({seerr:'Seerr',radarr:'Radarr',sonarr:'Sonarr',prowlarr:'Prowlarr',bazarr:'Bazarr'}[action.slice(5)]||'应用')+' ↗';
    return ({'view:compat':'查看兼容检查','view:cleanup':'清理冷门影片','view:pending':'去待上架','view:health':'服务器检测','view:add':'去处理'})[action]||'';
  },
  async mcIssueAction(issue){
    const a=issue.action||'';
    if(this.mcAdminAction(a))return this.mediaCenterAction(a);
    if(a.startsWith('open:')){const id=a.slice(5),url=this.serviceUrl(id);if(url)window.open(url,'_blank','noopener,noreferrer');else this.requireService({preventDefault(){}},id);return}
    if(a==='view:compat')return this.openOperations('compat');
    if(a==='view:health')return this.openOperations('health');
    if(a==='view:cleanup'){this.navigate('media');if(typeof this.showColdMedia==='function')await this.showColdMedia();return}
    if(a==='view:pending')return this.navigate('pending');
    if(a==='view:add')return this.mcGoAdd();
  },
  mcUsed(d){return d.total?Math.round((d.total-d.free)*100/d.total):0},
  mcNativeUrl(id,path=''){
    const base=this.serviceUrl(id);
    return base?(path?new URL(path,base.endsWith('/')?base:base+'/').href:base):'#remote-access';
  },
  /* 加片: the owner pastes a magnet link or uploads a .torrent; the built-in qBittorrent downloads it. */
  mcAdd:{link:'',kind:'auto',file:null},mcAddTasks:[],mcAddLoaded:false,mcAddError:'',mcAddTimer:null,mcAddSerial:0,mcFileChoices:{},mcNumbering:{},
  mcPick:{mode:'',target:null,query:'',kind:'auto',results:[],loading:false,error:''},
  get mcAddActive(){return this.mcAddTasks.filter(t=>!['listed','downloaded','removed'].includes(t.stage))},
  async loadMediaAdd(){
    if(!this.user||!this.data.media_automation||this.busy)return;
    const serial=++this.mcAddSerial;
    try{const r=await this.api('/media-add');if(serial===this.mcAddSerial){this.mcAddTasks=r.tasks||[];this.mcAddError=''}}catch(e){if(serial===this.mcAddSerial)this.mcAddError=e.message}finally{this.mcAddLoaded=true}
  },
  mcAddPoll(){
    clearInterval(this.mcAddTimer);
    if(!this.mcAddFocusHandler&&typeof window.addEventListener==='function'){this.mcAddFocusHandler=()=>{if(this.view==='downloads'&&this.user&&!this.busy)this.loadMediaAdd()};window.addEventListener('focus',this.mcAddFocusHandler)}
    this.mcAddTimer=setInterval(()=>{if(this.view!=='downloads'||!this.user){clearInterval(this.mcAddTimer);this.mcAddTimer=null;return}if(!document.hidden&&!this.busy)this.loadMediaAdd()},10000);
  },
  mcGoAdd(){const go=()=>document.getElementById('mc-add')?.scrollIntoView({behavior:'smooth',block:'start'});if(this.view==='downloads')return go();this.navigate('downloads');setTimeout(go,400)},
  mcReadTorrent(file){return new Promise((ok,fail)=>{const r=new FileReader();r.onload=()=>ok(String(r.result).split(',')[1]||'');r.onerror=()=>fail(new Error('种子文件读取失败'));r.readAsDataURL(file)})},
  async mcAddSubmit(){
    if(!this.administrator||this.busy)return;
    const body={action:'add',kind:this.mcAdd.kind},file=this.mcAdd.file;
    if(file){if(file.size>700*1024)return this.notify('种子文件过大（超过 700 KB）');}
    else{const link=this.mcAdd.link.trim();if(!link)return this.notify('请粘贴磁力链接，或选择种子文件');body.link=link}
    this.busy=true;++this.mcAddSerial;
    try{if(file)body.torrent_b64=await this.mcReadTorrent(file);const r=await this.api('/media-add',body);this.mcAddTasks=r.tasks||[];this.mcAdd={link:'',kind:this.mcAdd.kind,file:null};if(this.$refs.mcTorrent)this.$refs.mcTorrent.value='';this.notify(r.message);this.mcAddPoll()}
    catch(e){this.notify(e.message)}finally{this.busy=false}
  },
  async mcTaskAction(t,action){
    if(!this.administrator||this.busy)return;
    if(action==='remove'&&!confirm(this.mcKeepsFiles(t)?'只从列表移除这条记录？文件和片库都不受影响，下载器继续做种。':'移除这个下载，并删除已经下载的文件？它还没有入库，片库不受影响。'))return;
    this.busy=true;++this.mcAddSerial;
    try{const r=await this.api('/media-add',{action,id:t.id});this.mcAddTasks=r.tasks||[];this.mediaCenterAt=0;this.notify(r.message)}catch(e){this.notify(e.message)}finally{this.busy=false}
  },
  async mcSelectFiles(t){
    if(this.busy)return;
    let body={id:t.id,action:'select_files',file:this.mcFileChoices[t.id]};
    if(t.stage==='needs_episodes'){const n=this.mcNumbering[t.id]||{};body={id:t.id,action:'number_episodes',season:Number(n.season||1),start:Number(n.start||1)};if(!confirm('按页面列出的顺序连续编号并导入？请先核对每个文件对应的集数。'))return;}
    this.busy=true;++this.mcAddSerial;
    try{const r=await this.api('/media-add',body);this.mcAddTasks=r.tasks||[];this.notify(r.message)}catch(e){this.notify(e.message)}finally{this.busy=false}
  },
  mcStage(t){
    const done=t.download&&t.download.complete;
    return ({waiting_name:{label:'获取资源信息',cls:'neutral'},identifying:{label:'识别片名中',cls:'neutral'},needs_match:{label:'需要确认片名',cls:'warn'},
      matched:{label:done?'入库中':'已认出 · 下载中',cls:'neutral'},importing:{label:'入库中',cls:'neutral'},imported:{label:t.error?'未进入待上架':'已归档 · 同步中',cls:t.error?'warn':'neutral'},
      needs_file:{label:'请选择正片',cls:'warn'},needs_episodes:{label:'请确认集号',cls:'warn'},listed:{label:t.published?'已上架':'已进入待上架',cls:'live'},file:{label:'普通文件 · 下载中',cls:'neutral'},downloaded:{label:'已下载（不入库）',cls:'neutral'},failed:{label:'失败',cls:'offline'}})[t.stage]||{label:t.stage,cls:'neutral'};
  },
  mcProgress(t){return Math.round(((t.download||{}).progress||0)*100)},
  mcDownloadText(t){
    const d=t.download||{};
    if(t.stage==='listed'&&t.published)return '已上架，可在片库中管理';
    if(t.stage==='listed')return '已进入待上架（未上架），检查海报和简介后点“上架”';
    if(t.stage==='imported')return '已归档到片库，正在同步到“待上架”';
    if(t.stage==='downloaded')return '已下载完成；不是影视文件，只下载、不入库';
    if(d.complete)return '下载完成';
    const words={metadata:'正在获取资源信息',stalled:'暂时没有可连接的做种者，继续等待',queued:'排队中',paused:'已暂停',error:'下载出错',missing:'下载器里找不到这个任务',checking:'正在校验'}[d.state];
    if(words)return words+(d.state==='stalled'&&d.progress?' · 已下载 '+this.mcProgress(t)+'%':'');
    return '已下载 '+this.mcProgress(t)+'%'+(d.size?' / '+this.bytes(d.size):'')+(d.speed?' · '+this.bytes(d.speed)+'/秒':'')+(d.eta?' · 约 '+Math.max(1,Math.round(d.eta/60))+' 分钟':'');
  },
  mcCanPick(t){return ['waiting_name','identifying','needs_match','matched','failed'].includes(t.stage)&&!(t.stage==='matched'&&t.download&&t.download.complete)},
  mcKeepsFiles(t){return ['imported','listed','downloaded'].includes(t.stage)},
  mcPoster(p){return p?'/api/admin/media-add/poster?p='+encodeURIComponent(p):''},
  mcOpenPicker(mode,target){
    const query=mode==='rename'?(target.title||''):((target.match&&target.match.title)||target.query||'');
    const kind=mode==='rename'?(target.kind==='series'?'series':'movie'):(target.kind||'auto');
    this.mcPick={mode,target,query,kind,results:mode==='match'&&target.candidates?target.candidates:[],loading:false,error:''};
    this.modal='title-picker';
    if(query&&!this.mcPick.results.length)this.mcSearch();
  },
  async mcSearch(){
    const p=this.mcPick,q=p.query.trim();if(!q)return;
    p.loading=true;p.error='';
    try{const r=await this.api('/media-add/search?q='+encodeURIComponent(q)+'&kind='+encodeURIComponent(p.kind));p.results=r.results||[];if(!p.results.length)p.error='没有搜到，换个片名或用英文原名试试'}
    catch(e){p.results=[];p.error=e.message}finally{p.loading=false}
  },
  async mcChoose(c){
    const p=this.mcPick;if(this.busy)return;
    this.busy=true;p.error='';
    try{
      let r;
      if(p.mode==='match'){r=await this.api('/media-add',{action:'match',id:p.target.id,media_type:c.media_type,tmdb_id:c.tmdb_id});this.mcAddTasks=r.tasks||[]}
      else r=await this.api('/media-rename',{media_id:p.target.id,media_type:c.media_type,tmdb_id:c.tmdb_id});
      this.modal=null;this.notify(r.message);if(p.mode==='rename')this.reload(false);
    }catch(e){p.error=e.message}finally{this.busy=false}
  },
}}
