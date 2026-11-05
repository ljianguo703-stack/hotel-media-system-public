/* Complete operator workflows layered on the existing console. */
function managementUi(){return {
  operationsMarkup:'',dialogsMarkup:'',adsMarkup:'',devicesMarkup:'',reportsMarkup:'',mediaPage:0,mediaPageSize:30,
  opsTab:'advertisers',opsRequest:0,opsLoading:false,opsQuery:'',opsStatus:'',opsSelected:[],opsData:{rows:[],total:0},opsPage:0,opsLimit:30,opsOutcome:null,entityKind:'advertiser',credentials:[],csvDraft:'',csvOverwrite:false,
  deviceStatus:'',deviceGroup:'',deviceView:'table',deviceSelected:[],adStatus:'',adGroup:'',adMerchant:'',adSelected:[],bulkGroup:'全部客房',bulkMerchant:'',
  syncDraft:{url:'http://127.0.0.1:8096',token:'',paused:false,interval_seconds:120},syncDirty:false,syncRevision:0,connectionResult:null,policyDraft:{trash_days:30,cleanup_enabled:true},policyDirty:false,policyRevision:0,
  scheduleAt:'',scheduleGroup:'全部客房',scheduleData:null,releasePreview:null,restoreConfirmed:false,fileHealth:null,overviewTasks:{},serverHealth:null,mediaWarnings:[],
  reportFilters:{from:'',until:'',merchant:'',group:'',placement:'',dimension:'campaign'},reportDirty:false,
  get opsTabs(){return [{id:'advertisers',name:'广告主'},{id:'orders',name:'投放单'},{id:'packages',name:'广告套餐'},{id:'control',name:'广告总控'},{id:'ad_playback',name:'播放规则'},{id:'home',name:'电视首页'},{id:'topics',name:'专题'},{id:'compat',name:'兼容检查'},{id:'channels',name:'频道'},{id:'assets',name:'素材库'},{id:'groups',name:'设备组',admin:true},{id:'trash',name:'回收站'},{id:'tasks',name:'任务中心'},{id:'audit',name:'操作审计'},{id:'backups',name:'数据库备份',admin:true},{id:'sync',name:'同步设置',admin:true},{id:'health',name:'服务器状态'}].filter(t=>!t.admin||this.administrator)},
  get visibleCategories(){return this.data.categories.filter(c=>!c._archived)},
  async moveCategory(c,direction){const all=this.visibleCategories,i=all.findIndex(x=>x.id===c.id),j=i+direction;if(j<0||j>=all.length)return;const edge=all[j].sort,outer=all[j+direction]?.sort;await this.action('/categories/'+encodeURIComponent(c.id),{...c,sort:outer==null?edge+direction*10:outer===edge?edge+direction:(edge+outer)/2},'分类顺序已更新',false)},
  get managementModal(){return ['entity','asset-preview','csv-import','credentials','device-detail','source-edit','release-preview','schedule-choose','schedule-preview','op-result','media-warnings','currency','package-edit','order-edit','order-detail','report-choose','guide-edit','topic-edit','channel-edit','channel-detail'].includes(this.modal)},
  get pagedMedia(){return this.filteredMedia.slice(this.mediaPage*this.mediaPageSize,(this.mediaPage+1)*this.mediaPageSize)},
  get tvUrl(){return `${location.protocol}//${location.hostname}:${this.data.endpoints?.tv_port||18790}`},
  get opsKind(){return {advertisers:'advertiser',groups:'group',assets:'asset'}[this.opsTab]},
  get opsTitle(){return this.opsTabs.find(t=>t.id===this.opsTab)?.name||'管理中心'},
  get opsRows(){const all=({advertisers:this.data.advertisers||[],groups:this.data.device_groups||[],assets:this.data.assets||[]})[this.opsTab]||[];return all.filter(r=>!r._archived&&(!this.opsStatus||(this.opsStatus==='enabled'?r.enabled:!r.enabled))&&`${r.name} ${r.contact||''} ${r.phone||''} ${r.notes||''}`.toLowerCase().includes(this.opsQuery.toLowerCase())).sort((a,b)=>a.name.localeCompare(b.name,'zh-CN'))},
  get visibleTrash(){return (this.opsData.rows||[]).filter(r=>(!this.opsStatus||r.kind===this.opsStatus)&&`${r.name} ${r.type}`.toLowerCase().includes(this.opsQuery.toLowerCase()))},
  get activeGroups(){return (this.data.device_groups||[]).filter(g=>g.enabled&&!g._archived)},
  get activeMerchants(){return [...new Set([this.edit.merchant,...(this.data.advertisers||[]).filter(a=>a.enabled&&!a._archived).map(a=>a.name)].filter(Boolean))]},
  get availableAssets(){return (this.data.assets||[]).filter(a=>a.enabled&&!a._archived&&a.format===this.edit.format)},
  get groups(){return ['全部客房',...new Set([...this.activeGroups.map(g=>g.name),...this.data.devices.filter(d=>!d._archived).map(d=>d.group).filter(g=>g!=='全部客房')])]},
  get zones(){const values=typeof Intl.supportedValuesOf==='function'?Intl.supportedValuesOf('timeZone'):['UTC','Asia/Singapore','Asia/Shanghai','Asia/Bangkok','Asia/Tokyo','Europe/London','America/New_York'];return [...new Set([this.data.settings.timezone,this.edit.timezone,...values].filter(Boolean))]},
  get filteredAds(){return this.data.campaigns.filter(a=>!a._archived&&a.status!=='已归档'&&(this.tier==='全部'||a.tier===Number(this.tier))&&(!this.adStatus||a.status===this.adStatus)&&(!this.adGroup||a.group===this.adGroup)&&(!this.adMerchant||a.merchant===this.adMerchant)&&`${a.name} ${a.merchant}`.toLowerCase().includes(this.search.toLowerCase()))},
  get filteredDevices(){return this.data.devices.filter(d=>!d._archived&&(!this.deviceGroup||d.group===this.deviceGroup)&&(!this.deviceStatus||(this.deviceStatus==='online'?d.enabled&&d.online:this.deviceStatus==='disabled'?!d.enabled:d.enabled&&!d.online))&&`${d.room} ${d.group} ${d.id}`.toLowerCase().includes(this.deviceSearch.toLowerCase()))},
  get activeUsers(){return this.data.users.filter(u=>!u._archived)},
  get reportRows(){return this.report.rows},
  async loadOperations(){
    if(!this.user)return;
    if(this.view==='operations'){
      if(['advertisers','assets','groups','packages'].includes(this.opsTab))return;
      const requestedTab=this.opsTab,requestedView=this.view,ticket=++this.opsRequest;
      const section=this.opsTab==='health'?'health':this.opsTab==='sync'?'sync':this.opsTab==='control'?'ad_control':['home','topics'].includes(this.opsTab)?'home':this.opsTab;
      const q=new URLSearchParams({section,q:this.opsQuery,offset:String(this.opsPage*this.opsLimit),limit:String(this.opsLimit)});
      if(this.opsTab==='tasks'&&this.opsStatus)q.set('status',this.opsStatus);
      if(this.opsTab==='orders')for(const [k,v] of Object.entries(this.orderQueryParams()))q.set(k,v);
      this.opsLoading=true;
      try{const result=await this.api('/operations?'+q);if(this.opsTab!==requestedTab||this.view!==requestedView||this.opsRequest!==ticket)return;this.opsData=result;
      if(this.opsTab==='sync'&&!this.syncDirty){this.syncDraft={...result,token:''};this.syncRevision=this.data.revision}
      if(this.opsTab==='trash'&&!this.policyDirty){this.policyDraft=copy(result.policy);this.policyRevision=this.data.revision}
      if(this.opsTab==='health')this.serverHealth=result;
      if(this.opsTab==='control')this.controlLoaded(result);
      if(this.opsTab==='ad_playback'&&typeof this.playbackLoaded==='function')this.playbackLoaded(result);
      if(['home','topics'].includes(this.opsTab))this.homeLoaded(result);
      if(this.opsTab==='compat')this.compatLoaded(result);
      }finally{if(this.opsRequest===ticket)this.opsLoading=false}
    }
    if(this.view==='overview')this.overviewTasks=await this.api('/operations?section=overview');
  },
  async setOpsTab(id){this.opsRequest++;this.opsLoading=false;this.opsData={rows:[],total:0};this.opsTab=id;this.opsQuery='';this.opsStatus='';this.opsPage=0;this.opsSelected=[];const u=new URL(location.href);u.searchParams.set('tab',id);history.replaceState({},'',u);try{await this.loadOperations()}catch(e){this.notify(e.message)}},
  async openOperations(tab){this.navigate('operations');await this.setOpsTab(tab)},
  toggleSet(field,id){this[field]=this[field].includes(id)?this[field].filter(x=>x!==id):[...this[field],id]},
  selectSet(field,ids){this[field]=ids.length&&ids.every(id=>this[field].includes(id))?this[field].filter(id=>!ids.includes(id)):[...new Set([...this[field],...ids])]},
  async managedAction(payload,message='操作已完成',close=true){const r=await this.action('/operations',payload,message,close);if(r){await this.loadOperations();if(Array.isArray(r.ok)){this.opsOutcome={...r,credentials:undefined};this.opsSelected=this.opsSelected.filter(id=>!r.ok.includes(id));this.deviceSelected=this.deviceSelected.filter(id=>!r.ok.includes(id));this.adSelected=this.adSelected.filter(id=>!r.ok.includes(id));this.notify(`成功 ${r.ok.length} 项，失败 ${r.failed.length} 项`);if(r.credentials?.length){this.credentials=r.credentials;this.modal='credentials'}else if(r.failed.length)this.modal='op-result'}}return r},
  askManaged(payload,message){const p={...payload,_revision:payload._revision??this.data.revision};this.pendingConfirm={message,run:()=>this.managedAction({...p,confirm:true})}},
  lifecycle(kind,action,ids=this.opsSelected){if(!ids.length)return;const data={kind,action,ids:[...ids]};if(['archive','purge'].includes(action)){this.askManaged(data,action==='purge'?`清空 ${ids.length} 项管理记录？编辑信息将无法从回收站恢复；原影片和广告文件保留。${kind==='order'?' 清空后该周期报告将不再归属于此投放单。':''}`:`将 ${ids.length} 项移入回收站？关联播放/投放会停止，可在 ${this.data.management_policy.trash_days||30} 天内恢复。`)}else this.managedAction(data)},
  editEntity(kind,item=null){this.entityKind=kind;this.edit=item?{...copy(item),_revision:this.data.revision}:{id:kind+'-'+newUuid(),name:'',notes:'',enabled:true,contact:'',phone:'',path:'',_revision:this.data.revision};if(kind==='asset')this.edit.path=this.fileLabel(this.edit.path);this.modal='entity'},
  async saveEntity(){await this.managedAction({kind:this.entityKind,action:'save',item:this.edit,_revision:this.edit._revision},'资料已保存')},
  fileLabel(value){try{return decodeURIComponent(value||'')}catch{return value||''}},
  async uploadManagedAsset(file){if(!file||this.uploading)return;this.uploading=true;try{const r=await fetch('/api/admin/uploads?kind=ads',{method:'POST',credentials:'same-origin',headers:{'X-CSRF-Token':this.user.csrf,'X-Filename':encodeURIComponent(file.name)},body:file});const d=await r.json();if(!r.ok)throw Error(d.error||'上传失败');await this.reload(false);this.notify('素材已入库：'+file.name)}catch(e){this.notify(e.message)}finally{this.uploading=false}},
  async discoverAssets(){const r=await this.managedAction({kind:'asset',action:'discover_assets'},'本地广告素材检查完成');if(r)this.notify(`已发现 ${r.scanned} 个文件，新增 ${r.added} 个素材`)},
  assetPreview(item){this.preview=copy(item);this.modal='asset-preview'},
  bulkAds(operation,ids=this.adSelected,value=null){if(!ids.length)return;const p={kind:'campaign',action:'campaign_batch',operation,ids:[...ids],value};if(['publish','archive'].includes(operation))this.askManaged(p,operation==='publish'?`发布 ${ids.length} 组 / 条广告？组合内的已勾选档位将一起发布，请确认客房范围与排期。`:`删除 ${ids.length} 组 / 条广告？组合内各档一起进入回收站，投放停止，素材与历史保留。`);else this.managedAction(p)},
  bulkDevices(operation,ids=this.deviceSelected,value=null){if(!ids.length)return;const p={kind:'device',action:'devices_batch',operation,ids:[...ids],value};if(['rotate','archive','disable'].includes(operation))this.askManaged(p,operation==='rotate'?`轮换 ${ids.length} 台设备凭证？旧凭证会立即失效，需将新凭证填入电视。`:`${operation==='archive'?'归档':'停用'} ${ids.length} 台设备？后续请求将被拒绝。`);else this.managedAction(p)},
  async readCsv(file){if(file)this.csvDraft=await file.text()},
  csvTemplate(){this.download('客房设备导入模板.csv',new Blob(['\ufeffid,room,group,enabled,model,version\nroom-201,201,二楼,true,Android TV,1.0\n'],{type:'text/csv;charset=utf-8'}))},
  async importDevices(){await this.managedAction({kind:'device',action:'devices_import',csv:this.csvDraft,overwrite:this.csvOverwrite,_revision:this.edit._revision},'设备导入已完成')},
  openCsv(){this.csvDraft='';this.csvOverwrite=false;this.edit={_revision:this.data.revision};this.modal='csv-import'},
  async exportDevices(){try{const r=await this.api('/operations?section=devices_csv');this.download('客房设备.csv',new Blob(['\ufeff'+r.csv],{type:'text/csv;charset=utf-8'}))}catch(e){this.notify(e.message)}},
  exportCredentials(){const config={server_url:this.tvUrl,devices:this.credentials};this.download('电视装机配置-'+Date.now()+'.json',new Blob([JSON.stringify(config,null,2)],{type:'application/json'}));this.notify('装机配置已下载；将地址和凭证填写到对应电视')},
  async showDevice(item){try{this.preview=await this.api('/operations?'+new URLSearchParams({section:'device',id:item.id}));this.modal='device-detail'}catch(e){this.notify(e.message)}},
  editSource(source){this.edit={...copy(source),enabled:source.enabled!==false,notes:source.notes||'',_revision:this.data.revision};this.modal='source-edit'},
  async saveManagedSource(){const payload={kind:'source',action:'source_save',item:this.edit,_revision:this.edit._revision};if(!this.edit.enabled&&this.edit.published_count){this.askManaged(payload,`停用将从电视移除 ${this.edit.published_count} 部已上架影片。索引与文件保留，重新启用后可恢复显示。`)}else await this.managedAction(payload,'片库来源已保存')},
  async inspectMediaFiles(){if(!this.edit.id)return;const id=this.edit.id;try{const result=await this.api('/operations?'+new URLSearchParams({section:'media_files',id}));if(this.modal==='media'&&this.edit.id===id)this.fileHealth=result}catch(e){this.notify(e.message)}},
  async oneClickLibrary(){const r=await this.managedAction({kind:'media',action:'library_update'},'已加入任务中心，完成结果请查看步骤明细',false);if(r)await this.openOperations('tasks')},
  async retryTask(job){await this.managedAction({kind:'system',action:'retry_task',id:job.id},'已创建重试任务，原失败记录保留',false)},
  jobName(job){return job.kind==='library-update'?'一键更新片库':job.id.startsWith('jellyfin-')?'Jellyfin 同步':job.id.startsWith('scan-')?'目录扫描':job.id},
  jobStatus(status){return {queued:'排队中',running:'执行中',completed:'已完成',failed:'失败'}[status]||status},
  async saveSync(){const r=await this.managedAction({kind:'system',action:'sync_control',paused:this.syncDraft.paused,interval_seconds:this.syncDraft.interval_seconds,_revision:this.syncRevision},'同步设置已保存',false);if(r){this.syncDirty=false;await this.loadOperations()}},
  async saveConnection(){const r=await this.managedAction({kind:'system',action:'sync_connection',url:this.syncDraft.url,token:this.syncDraft.token,_revision:this.syncRevision},'连接配置已保存',false);if(r){this.syncDirty=false;this.syncDraft.token='';await this.loadOperations()}},
  async testConnection(){const r=await this.managedAction({kind:'system',action:'sync_test'},'连接测试已完成',false);if(r)this.connectionResult=r},
  markSync(){if(!this.syncDirty)this.syncRevision=this.data.revision;this.syncDirty=true},
  markPolicy(){if(!this.policyDirty)this.policyRevision=this.data.revision;this.policyDirty=true},
  async savePolicy(){const r=await this.managedAction({kind:'system',action:'policy',...this.policyDraft,_revision:this.policyRevision},'回收站策略已保存',false);if(r){this.policyDirty=false;await this.loadOperations()}},
  async allowImport(item){this.askManaged({kind:'media',action:'allow_reimport',id:item.id},`允许「${item.name}」再次扫描入库？清空前的编辑信息不会自动恢复。`)},
  clearTrash(mode){this.askManaged({kind:'system',action:'trash_clear',mode},mode==='all'?'清空回收站全部管理记录？无法从回收站恢复编辑信息；投放单的历史归属将移除，仍有引用的记录会保留，原文件不会删除。':'清空已到期且无引用的管理记录？原文件和审计保留。')},
  async exportAudit(){try{const r=await this.api('/operations?'+new URLSearchParams({section:'audit_csv',q:this.opsQuery}));this.download('操作审计.csv',new Blob(['\ufeff'+r.csv],{type:'text/csv;charset=utf-8'}));this.notify('已导出 '+r.count+' 条审计'+(r.truncated?'（最多 10000 条，请缩小条件后继续导出）':''))}catch(e){this.notify(e.message)}},
  async saveMedia(){const r=await this.action('/media/'+encodeURIComponent(this.edit.id),this.edit,'影片已保存');if(r?.warnings?.length){this.mediaWarnings=r.warnings;this.modal='media-warnings'}},
  async createBackup(){await this.managedAction({kind:'backup',action:'backup_create'},'数据库备份已生成',false)},
  async downloadBackup(item){try{const r=await fetch('/api/admin/backups/'+encodeURIComponent(item.id),{credentials:'same-origin'});if(!r.ok)throw Error((await r.json()).error||'下载失败');this.download(item.name,await r.blob())}catch(e){this.notify(e.message)}},
  async restore(release){this.toast='';clearTimeout(this.toastTimer);try{this.releasePreview=await this.api('/operations?'+new URLSearchParams({section:'release_preview',id:release.id}));this.restoreConfirmed=false;this.modal='release-preview'}catch(e){this.notify(e.message)}},
  async confirmRestore(){if(!this.restoreConfirmed)return;await this.action('/releases/'+this.releasePreview.id,{confirm:true,_revision:this.releasePreview.revision},'历史配置已恢复')},
  async previewSchedule(){try{this.scheduleData=await this.api('/operations?'+new URLSearchParams({section:'schedule',at:this.scheduleAt,group:this.scheduleGroup}));this.modal='schedule-preview'}catch(e){this.notify(e.message)}},
  openSchedule(){const parts=Object.fromEntries(new Intl.DateTimeFormat('en-CA',{timeZone:this.data.settings.timezone||'UTC',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(new Date()).map(p=>[p.type,p.value]));this.scheduleAt=`${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;this.scheduleGroup='全部客房';this.modal='schedule-choose'},
  reportQuery(){const q=new URLSearchParams({days:String(this.days),...Object.fromEntries(Object.entries(this.reportFilters).filter(([k,v])=>v))});if(this.tier!=='全部')q.set('tier',this.tier);return q.toString()},
  async updateReports(){const ticket=++this.reportSerial;try{const result=await this.api('/reports?'+this.reportQuery());if(ticket===this.reportSerial){this.report=result;this.reportDirty=false}}catch(e){this.notify(e.message)}},
  async exportReport(){try{const r=await fetch('/api/admin/reports.csv?'+this.reportQuery(),{credentials:'same-origin'});if(!r.ok)throw Error((await r.json()).error||'导出失败');this.download('酒店运营报表.csv',await r.blob())}catch(e){this.notify(e.message)}},
  async deleteCategory(c){this.lifecycle('category','archive',[c.id])},
  async saveCampaign(status){const payload={...this.edit,status};if(status==='投放中')this.pendingConfirm={message:'确认发布此广告？请核对档位、时段与客房范围。',run:()=>this.action('/campaigns/'+payload.id,payload,'广告已发布')};else await this.action('/campaigns/'+payload.id,payload,'草稿已保存')},
  async statusCampaign(ad,status){if(status==='投放中')this.bulkAds('publish',[ad.id]);else this.bulkAds('pause',[ad.id])}
}}
const cinemaBaseFactory=cinemaAdmin;
cinemaAdmin=function(){
  const app=cinemaBaseFactory(),baseInit=app.init,baseReload=app.reload,baseClose=app.close,baseOpenMedia=app.openMedia,baseLogout=app.logout,baseNewCampaign=app.newCampaign,baseOpenCampaign=app.openCampaign;
  Object.defineProperties(app,Object.getOwnPropertyDescriptors(managementUi()));
  if(typeof advertisingUi==='function')Object.defineProperties(app,Object.getOwnPropertyDescriptors(advertisingUi()));
  if(typeof ordersUi==='function')Object.defineProperties(app,Object.getOwnPropertyDescriptors(ordersUi()));
  if(typeof homeUi==='function')Object.defineProperties(app,Object.getOwnPropertyDescriptors(homeUi()));
  if(typeof compatUi==='function')Object.defineProperties(app,Object.getOwnPropertyDescriptors(compatUi()));
  if(typeof channelsUi==='function')Object.defineProperties(app,Object.getOwnPropertyDescriptors(channelsUi()));
  if(typeof downloadsUi==='function')Object.defineProperties(app,Object.getOwnPropertyDescriptors(downloadsUi()));
  if(typeof mediaInsightsUi==='function')Object.defineProperties(app,Object.getOwnPropertyDescriptors(mediaInsightsUi()));
  if(typeof softwareUi==='function')Object.defineProperties(app,Object.getOwnPropertyDescriptors(softwareUi()));
  if(typeof fleetUi==='function')Object.defineProperties(app,Object.getOwnPropertyDescriptors(fleetUi()));
  if(typeof mediaCenterUi==='function')Object.defineProperties(app,Object.getOwnPropertyDescriptors(mediaCenterUi()));
  app.data={...app.data,advertisers:[],device_groups:[],assets:[],ad_packages:[],billing:{currency:''},maintenance:{},management_policy:{trash_days:30,cleanup_enabled:true}};
  app.nav.splice(app.nav.findIndex(n=>n.id==='pending')+1,0,{id:'downloads',name:'搜索与下载',mark:'⇩'});
  app.nav.push({id:'fleet',name:'批量运维',mark:'▤'});
  app.nav.push({id:'software',name:'软件与更新',mark:'⇧'});
  app.nav.push({id:'operations',name:'管理中心',mark:'☷'});
  app.init=async function(){
    await baseInit.call(this);
    try{const files={playbackMarkup:'advertising-playback.html',cardMarkup:'advertising-card.html',cardPreviewMarkup:'advertising-preview.html',operationsMarkup:'management.html',dialogsMarkup:'management-dialogs.html',adsMarkup:'campaigns.html',devicesMarkup:'devices.html',reportsMarkup:'reports.html',downloadsMarkup:'downloads.html',softwareMarkup:'software.html',cleanupMarkup:'media-cleanup.html',fleetMarkup:'fleet.html'};await Promise.all(Object.entries(files).map(async([key,file])=>{const r=await fetch(file);if(!r.ok)throw Error('管理界面未能加载：'+file);this[key]=await r.text()}));const tab=new URLSearchParams(location.search).get('tab');if(this.opsTabs.some(t=>t.id===tab)){this.opsTab=tab;await this.loadOperations()}}
    catch(e){this.error=e.message}
    this.$watch('user',u=>{if(!u||u.role!=='admin'){if(typeof this.resetFleet==='function')this.resetFleet()}if(!u){this.close();this.pendingConfirm=null;this.opsOutcome=null;this.syncDraft.token='';this.mediaStats=null;this.mediaStatsMap={};this.mediaStatsSerial++;this.mediaStatsLoading=false;this.cleanupPlan=null;this.cleanupResult=null;this.cleanupConfirmed=false;this.mediaColdOnly=false;this.mediaCenter=null;this.mediaCenterAt=0;this.mcAddTasks=[];this.mcAddLoaded=false;clearInterval(this.mcAddTimer);this.mcAddTimer=null;this.mcSubtitles={assrt_token:'',opensubtitles_username:'',opensubtitles_password:''}}});
    for(const key of ['search','kind','mediaStatus','mediaCategory','mediaSource','mediaSort'])this.$watch(key,()=>this.mediaPage=0);
  };
  app.reload=async function(showBusy=true){await baseReload.call(this,showBusy);if(this.user){this.selected=this.selected.filter(id=>this.data.media.some(m=>m.id===id));this.adSelected=this.adSelected.filter(id=>this.data.campaigns.some(a=>a.id===id&&!a._archived));this.deviceSelected=this.deviceSelected.filter(id=>this.data.devices.some(d=>d.id===id&&!d._archived));const downloads=this.nav.find(n=>n.id==='downloads');if(downloads)downloads.name=this.data.media_automation?'加片与下载':'搜索与下载';if(typeof this.loadMediaCenter==='function'&&['overview','downloads'].includes(this.view))this.loadMediaCenter();await this.loadOperations();if(typeof this.loadSoftware==='function')await this.loadSoftware();if(typeof this.loadFleet==='function')await this.loadFleet();if(typeof this.loadMediaInsights==='function')await this.loadMediaInsights()}};
  app.newCampaign=function(){baseNewCampaign.call(this);this.beginCampaignBundle()};
  app.openCampaign=function(ad){const original=this.data.campaigns.find(c=>c.id===ad.id)||ad;baseOpenCampaign.call(this,original);this.beginCampaignBundle(original)};
  app.close=function(){if(this.modal==='media-cleanup'&&this.busy)return;baseClose.call(this);this.campaignBundle=null;this.credentials=[];this.connectionResult=null;this.fileHealth=null};
  app.logout=async function(){this.close();this.pendingConfirm=null;this.opsOutcome=null;this.syncDraft.token='';await baseLogout.call(this)};
  app.openMedia=function(item){baseOpenMedia.call(this,item);this.fileHealth=null};
  return app;
};
