/* DL10: search first; one click sends a selected resource to the appropriate downloader. */
function downloadsUi(){return Object.defineProperties(downloadsBaseUi(),Object.getOwnPropertyDescriptors({
  dlTab:'search',dlSearchId:'',dlSearchKind:'',dlQuality:'',dlYear:'',dlMatch:'all',dlPage:0,dlPageSize:20,dlDownloadDir:'',dlOrganize:false,dlPublish:false,dlImportKind:'movie',dlImportTitle:'',dlImport:null,dlLoaded:false,dlLoadSerial:0,
  dlFolder:{open:false,loading:false,serial:0,roots:[],folders:[],path:'',root:'',parent:'',writable:false,newName:'',target:'compose',error:''},
  get dlResult(){return (this.dl.rows||[]).find(t=>t.id===this.dlSearchId)||null},
  get dlResultRows(){return (this.dlResult?.candidates||[]).filter(c=>this.dlMatch==='all'||c.match===this.dlMatch)},
  get dlResultPages(){return Math.max(1,Math.ceil(this.dlResultRows.length/this.dlPageSize))},
  get dlPagedResults(){return this.dlResultRows.slice(this.dlPage*this.dlPageSize,(this.dlPage+1)*this.dlPageSize)},
  dlSetPage(page){this.dlPage=Math.min(this.dlResultPages-1,Math.max(0,page));document.querySelector?.('.dl-results-head')?.scrollIntoView?.({block:'start'})},
  get dlRows(){const q=this.dlQuery.trim().toLowerCase(),f=this.dlFilter,active=['needs_config','needs_clarify','searching','awaiting_choice','queued','downloading','paused','verifying','organizing','importing','scraping','converting','publishing'];
    return (this.dl.rows||[]).filter(t=>t.intent!=='search'&&(!f||f==='all'||(f==='active'?active.includes(t.state):f==='attention'?['needs_config','needs_clarify','awaiting_choice','failed','no_results'].includes(t.state):t.state===f))&&(!q||(t.request+' '+(t.parsed?.title||'')).toLowerCase().includes(q)))},
  get dlReadyText(){const r=this.dl.ready||{},e=r.engines||{};return (r.sources||0)+' 个搜索源 · '+['qbittorrent','aria2'].map(k=>this.dlEngineNames[k]+(e[k]?' 已连接':' 未连接')).join(' · ')},
  get dlIsLink(){return /^(magnet:|https?:\/\/)/i.test(this.dlText.trim())||!!this.dlManualDraft.torrent_b64},
  dlCandidateTask(c){return (this.dl.rows||[]).find(t=>t.intent!=='search'&&t.chosen?.key===c.key&&!['failed','cancelled','no_results'].includes(t.state))},
  dlKindLabel(c){return ({file:'文件直链',magnet:'磁力链接',torrent:'种子'})[c.resource_type]||(c.protocol==='http'?'文件直链':'BT资源')},
  dlTaskLabel(t){if(t.state!=='downloading')return this.dl.labels[t.state]||t.state;return ({metadata:'等待元数据',stalled:'等待可用节点',queued:'下载器排队',checking:'校验文件',allocating:'准备空间',moving:'移动文件',paused:'下载器已暂停',unknown:'状态待确认'})[t.progress?.engine_state]||(t.progress?.speed>0?'下载中':'等待传输')},
  dlTaskClass(t){return t.state==='downloading'&&t.progress?.speed===0?'warn':this.dlStateClass(t.state)},
  dlPeerText(t){const p=t.progress||{};if(t.engine!=='qbittorrent')return '';return '已连接 '+(p.peers==null?'未知':p.peers)+' 个节点 · '+(p.seeds==null?'未知':p.seeds)+' 个做种者'+(p.waiting_seconds>=60?' · 已等待 '+Math.floor(p.waiting_seconds/60)+' 分钟':'')},
  dlEscape(){if(this.dlFolder.open)this.dlCloseFolder();else this.dlModal=null},
  startDownloadsView(){clearInterval(this.dlTimer);this.loadDownloads();this.dlTimer=setInterval(()=>{
    if(this.view!=='downloads'){clearInterval(this.dlTimer);return}
    if(document.visibilityState==='hidden'||this.dlModal||this.dlFolder.open)return;
    if((this.dl.rows||[]).some(t=>['searching','queued','downloading','verifying','organizing','importing','scraping','converting','publishing'].includes(t.state)))this.loadDownloads()
  },3000)},
  async loadDownloads(){const serial=++this.dlLoadSerial;try{const data=await this.api('/operations?section=downloads&archived=0');if(serial!==this.dlLoadSerial)return;
    const previous=new Map((this.dl.rows||[]).map(t=>[t.id,t]));data.rows=(data.rows||[]).map(t=>{const old=previous.get(t.id);return old&&JSON.stringify(old)===JSON.stringify(t)?old:t});
    for(const [key,value] of Object.entries(data))if(JSON.stringify(this.dl[key])!==JSON.stringify(value))this.dl[key]=value;
    if(!this.dlLoaded){if(!this.dlDownloadDir)this.dlDownloadDir=this.dl.default_dir||'';const last=this.dl.rows.find(t=>t.intent==='search'&&t.created_by===this.user?.name);if(last&&!this.dlSearchId&&!this.dlText&&!this.dlQuality&&!this.dlYear&&!this.dlSearchKind){this.dlSearchId=last.id;this.dlText=last.request;this.dlQuality=last.parsed?.resolution||'';this.dlYear=last.parsed?.year?String(last.parsed.year):'';this.dlSearchKind=last.parsed?.kind||''}this.dlLoaded=true}
    if(this.dlPage>=this.dlResultPages)this.dlPage=this.dlResultPages-1;
    if(this.dlView){const fresh=this.dl.rows.find(t=>t.id===this.dlView.id);if(fresh)this.dlView=fresh}
  }catch(e){this.notify(e.message)}},
  dlOptions(includeSearch=true){const overrides={};if(includeSearch){if(this.dlSearchKind)overrides.kind=this.dlSearchKind;if(this.dlQuality)overrides.resolution=this.dlQuality;if(this.dlYear)overrides.year=Number(this.dlYear)}
    if(this.dlOrganize){overrides.kind=this.dlImportKind;if(this.dlImportTitle.trim())overrides.title=this.dlImportTitle.trim()}
    return {flow_version:2,auto:false,organize:this.dlOrganize,publish:this.dlOrganize&&this.dlPublish,download_dir:this.dlDownloadDir||undefined,overrides}},
  async dlSend(){const text=this.dlText.trim();if(!text&&!this.dlManualDraft.torrent_b64)return this.notify('输入搜索词、下载链接或选择种子文件');
    const direct=this.dlIsLink,body={kind:'download',action:'download_create',request:direct?'':text,...this.dlOptions(!direct)};
    if(direct){body.manual={uri:this.dlManualDraft.torrent_b64?'':text,torrent_b64:this.dlManualDraft.torrent_b64||undefined,title:this.dlImportTitle.trim(),kind:this.dlOrganize?this.dlImportKind:''}}
    const r=await this.action('/operations',body,direct?'已加入下载任务':'正在搜索资源',false);
    if(r){if(direct){this.dlTab='tasks';this.dlText='';this.dlManualDraft={uri:'',title:'',kind:'',torrent_b64:'',torrent_name:''}}else{this.dlSearchId=r.id;this.dlMatch='all';this.dlPage=0;this.dlTab='search'}await this.loadDownloads()}},
  async dlDownload(c){if(this.dlCandidateTask(c))return this.dlOpen(this.dlCandidateTask(c));
    const r=await this.action('/operations',{kind:'download',action:'download_action',id:this.dlResult.id,op:'download_candidate',key:c.key,...this.dlOptions()},'已加入下载任务，下载器自动开始处理',false);
    if(r)await this.loadDownloads()},
  async dlTorrent(file){if(!file)return;if(file.size>700*1024)return this.notify('种子文件不能超过700KB');const buf=new Uint8Array(await file.arrayBuffer());let s='';for(let i=0;i<buf.length;i+=8192)s+=String.fromCharCode.apply(null,buf.subarray(i,i+8192));
    this.dlManualDraft.torrent_b64=btoa(s);this.dlManualDraft.torrent_name=file.name;this.dlManual=true},
  dlClearTorrent(){this.dlManualDraft.torrent_b64='';this.dlManualDraft.torrent_name='';this.dlManual=false},
  async dlAct(t,op,extra={}){const labels={pause:'已暂停',resume:'已继续',cancel:'已取消，下载文件保留',retry:'已重试',choose:'已交给下载器',clarify:'已更新需求',retry_scrape:'已重新刮削',publish:'正在上架',archive:'已移出列表',organize:'开始整理入库',reannounce:'已请求重新查找节点'};
    const r=await this.action('/operations',{kind:'download',action:'download_action',id:t.id,op,...extra},labels[op]||'已完成',false);if(r)await this.loadDownloads();return r},
  dlOpenImport(t){this.dlImport={id:t.id,title:t.parsed?.title||'',kind:t.parsed?.kind||'movie',publish:false};this.dlModal='import'},
  async dlRunImport(){const p=this.dlImport;const r=await this.dlAct({id:p.id},'organize',{overrides:{title:p.title,kind:p.kind},publish:p.publish});if(r)this.dlModal=null},
  async dlOpenFolder(target='compose'){this.dlFolder={...this.dlFolder,open:true,target,newName:'',error:''};const selected=target==='settings'?(this.dlSettings.save_dir||this.dlSettings.staging_dir):this.dlDownloadDir;
    await this.dlBrowse(selected||'')},
  dlCloseFolder(){this.dlFolder.open=false;this.dlFolder.serial++},
  async dlBrowse(path='',root=''){const serial=++this.dlFolder.serial;this.dlFolder.loading=true;this.dlFolder.error='';
    try{const q=new URLSearchParams({section:'download_locations'});if(path)q.set('path',path);if(root)q.set('root',root);const r=await this.api('/operations?'+q);
      if(serial===this.dlFolder.serial&&this.dlFolder.open)this.dlFolder={...this.dlFolder,...r,newName:'',loading:false};
    }catch(e){if(serial===this.dlFolder.serial){this.dlFolder.loading=false;this.dlFolder.error=e.message;if(path){try{const r=await this.api('/operations?section=download_locations');if(serial===this.dlFolder.serial)this.dlFolder={...this.dlFolder,...r,error:e.message}}catch(_){}}}}},
  async dlNewFolder(){const name=this.dlFolder.newName.trim();if(!name)return;const r=await this.action('/operations',{kind:'download',action:'download_mkdir',root:this.dlFolder.root,path:this.dlFolder.path,name},'文件夹已创建',false);
    if(r){this.dlFolder.serial++;this.dlFolder={...this.dlFolder,...r,newName:''}}},
  dlChooseFolder(){if(!this.dlFolder.writable||!this.dlFolder.path)return;if(this.dlFolder.target==='settings')this.dlSettings.save_dir=this.dlFolder.path;else this.dlDownloadDir=this.dlFolder.path;this.dlCloseFolder()},
  async dlUseDefault(){if(!this.dlDownloadDir)return;const r=await this.action('/operations',{kind:'download',action:'download_settings',settings:{save_dir:this.dlDownloadDir}},'已保存为默认下载位置',false);if(r)await this.loadDownloads()}
}));}
