/* Single-hotel administration. Persistent data comes exclusively from the server. */
const copy = value => JSON.parse(JSON.stringify(value));
// getRandomValues works on plain LAN HTTP; randomUUID requires a secure context.
function newUuid(){
  const bytes=crypto.getRandomValues(new Uint8Array(16));
  bytes[6]=(bytes[6]&15)|64;bytes[8]=(bytes[8]&63)|128;
  const hex=Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('');
  return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
}

const placements = {channel:'频道节目间',home:'进入首页',preroll:'影片开始前',midroll:'观影中插播',pause:'暂停时',postroll:'播放结束后',all:'首页与观影',playback:'正片播放中'};
function cinemaAdmin(){return {
  user:null,ready:false,reloadSerial:0,reportSerial:0,busy:false,error:'',username:'admin',password:'',view:'overview',mobileNav:false,
  data:{revision:0,settings:{},jellyfin:{},storage:{},media:[],categories:[],sources:[],devices:[],campaigns:[],audit:[],jobs:[],releases:[],users:[]},
  report:{rows:[],timeline:[],content_sessions:0,basis:''},days:7,mediaStatus:'active',search:'',kind:'全部',tier:'全部',deviceSearch:'',modal:null,edit:{},toast:'',toastTimer:null,uploading:false,oneTimeToken:'',preview:null,
  nav:[{id:'overview',name:'运营概览',mark:'◈'},{id:'media',name:'影片片库',mark:'▣'},{id:'pending',name:'待上架',mark:'▧'},{id:'ads',name:'广告投放',mark:'◷'},{id:'devices',name:'客房设备',mark:'▤'},{id:'reports',name:'投放报表',mark:'▥'},{id:'nas',name:'NAS 与应用',mark:'▦'},{id:'passwords',name:'密码管理',mark:'⌘'},{id:'settings',name:'系统设置',mark:'⚙'}],
  tierNames:{1:'全屏广告',2:'全天滚动文字',3:'分时滚动文字'},dayNames:['一','二','三','四','五','六','日'],placements,
  get writable(){return this.user&&this.user.role!=='viewer'},get administrator(){return this.user?.role==='admin'},
  mediaView:'list',mediaCategory:'',mediaSource:'',mediaSort:'created',
  get filteredMedia(){
    const rows=this.data.media.filter(m=>(this.view==='pending'?!m._archived:this.mediaStatus==='archived'?!!m._archived:!m._archived)&&(!this.mediaColdOnly||this.mediaInsight?.(m)?.eligible)&&
      (this.view==='pending'?((!m.published||(m.episodes||[]).some(e=>e._pending_publish))&&m.available):this.mediaStatus==='published'?m.published&&m.available:this.mediaStatus==='unpublished'?!m.published:this.mediaStatus==='missing'?!m.available:true)&&
      (!this.mediaCategory||m.category===this.mediaCategory)&&(!this.mediaSource||m.source_id===this.mediaSource)&&
      (this.kind==='全部'||m.kind===(this.kind==='电影'?'movie':'series'))&&`${m.title} ${m.category}`.toLowerCase().includes(this.search.trim().toLowerCase()));
    const name=(a,b)=>(a.title||'').localeCompare(b.title||'','zh-CN')||a.id.localeCompare(b.id);
    return rows.sort((a,b)=>this.mediaSort==='cold'?(this.mediaInsight?.(a)?.plays_30??Infinity)-(this.mediaInsight?.(b)?.plays_30??Infinity)||(this.mediaInsight?.(b)?.size_bytes||0)-(this.mediaInsight?.(a)?.size_bytes||0)||name(a,b):this.mediaSort==='name'?name(a,b):this.mediaSort==='year'?(Number(b.year)||0)-(Number(a.year)||0)||name(a,b):this.mediaSort==='modified'?(b._updated_at||0)-(a._updated_at||0)||name(a,b):(b._created_at||0)-(a._created_at||0)||name(a,b));
  },
  get pendingCount(){return this.data.media.filter(m=>(!m.published||(m.episodes||[]).some(e=>e._pending_publish))&&m.available&&!m._archived).length},
  mediaState(m){if(m.published&&(m.episodes||[]).some(e=>e._pending_publish))return '已上架 · 新增分集待审核';if(m.compat_ready===false)return m.compat_label||'等待兼容检查';return m._archived?'已归档':!m.available?'源文件缺失':m.published?'已上架':'未上架'},
  mediaTime(value){return value?this.date(value):'—'},
  resetMediaFilters(){this.mediaColdOnly=false;this.search='';this.kind='全部';this.mediaStatus='active';this.mediaCategory='';this.mediaSource='';this.mediaSort='created'},
  get filteredAds(){return this.data.campaigns.filter(a=>(this.tier==='全部'||a.tier===Number(this.tier))&&`${a.name} ${a.merchant}`.toLowerCase().includes(this.search.toLowerCase()))},
  get filteredDevices(){return this.data.devices.filter(d=>`${d.room} ${d.group} ${d.id}`.toLowerCase().includes(this.deviceSearch.toLowerCase()))},
  get online(){return this.data.devices.filter(d=>d.online&&d.enabled).length},get published(){return this.data.media.filter(m=>m.published&&m.available).length},
  get liveAds(){return this.data.campaigns.filter(a=>a.status==='投放中')},get groups(){return ['全部客房',...new Set(this.data.devices.map(d=>d.group).filter(g=>g!=='全部客房'))]},
  get scanning(){return this.data.jobs.some(j=>['queued','running'].includes(j.status))},get totalStarts(){return this.report.rows.reduce((n,r)=>n+r.starts,0)},
  get reportRows(){return this.report.rows.filter(r=>this.tier==='全部'||r.tier===Number(this.tier))},
  get tvUrl(){return `${location.protocol}//${location.hostname}:18790`},
  async init(){
    const q=new URLSearchParams(location.search);this.view=this.nav.some(n=>n.id===q.get('view'))?q.get('view'):'overview';
    try{const s=await this.api('/session');this.user=s.user;if(this.user)await this.reload()}catch(e){this.error=e.message}finally{this.ready=true}
    this.pollTimer=setInterval(async()=>{if(!this.user||document.hidden)return;try{await this.reload(false)}catch(e){this.error=e.message}},10000);
  },
  async api(path,body,options={}){
    const headers={};if(body!==undefined)headers['Content-Type']='application/json';if(this.user?.csrf)headers['X-CSRF-Token']=this.user.csrf;
    const response=await fetch('/api/admin'+path,{method:body===undefined?'GET':'POST',credentials:'same-origin',headers,body:body===undefined?undefined:JSON.stringify(body),...options});
    if(!response.ok){let payload;try{payload=await response.json()}catch{payload={error:'服务器请求失败'}}if(response.status===401&&path!=='/login')this.user=null;throw new Error(payload.error||'操作失败')}
    return response.json();
  },
  async login(){if(this.busy)return;this.busy=true;this.error='';try{this.user=(await this.api('/login',{username:this.username,password:this.password})).user;this.password='';await this.reload()}catch(e){this.error=e.message}finally{this.busy=false}},
  async logout(){try{await this.api('/logout',{})}catch{}this.user=null;this.data.media=[];this.oneTimeToken='';this.modal=null},
  async reload(showBusy=true){const serial=++this.reloadSerial;if(showBusy)this.busy=true;try{const d=await this.api('/state');if(serial!==this.reloadSerial)return;this.data=d;if(!this.modal&&!this.settingsDirty)this.settingsDraft=copy(d.settings);if(['overview','reports'].includes(this.view)&&!(this.view==='reports'&&this.reportDirty)){const query=this.view==='reports'&&this.reportQuery?this.reportQuery():'days='+(this.view==='overview'?7:Number(this.days));const reportTicket=++this.reportSerial,report=await this.api('/reports?'+query);if(serial===this.reloadSerial&&reportTicket===this.reportSerial)this.report=report}this.error=''}finally{if(showBusy)this.busy=false}},
  settingsDraft:{hotel:'',timezone:'Asia/Singapore',midroll_interval_seconds:1200},settingsDirty:false,settingsRevision:0,
  markSettings(){if(!this.settingsDirty)this.settingsRevision=this.data.revision;this.settingsDirty=true},
  async action(path,body,message,close=true){if(this.busy)return;this.busy=true;try{const r=await this.api(path,{...body,revision:body._revision??this.data.revision});if(close)this.modal=null;await this.reload(false);this.notify(message);return r}catch(e){this.error=e.message;this.notify(e.message)}finally{this.busy=false}},
  navigate(view){if(view==='pending'){this.selected=[];this.mediaStatus='active';this.mediaCategory='';this.mediaSource='';this.mediaColdOnly=false;}this.view=view;this.mobileNav=false;this.search='';this.tier='全部';this.modal=null;const u=new URL(location.href);u.searchParams.set('view',view);history.replaceState({},'',u);this.reload().catch(e=>this.error=e.message);window.scrollTo(0,0)},
  notify(message){this.toast=message;clearTimeout(this.toastTimer);this.toastTimer=setTimeout(()=>this.toast='',5000)},
  date(value){return value?new Date(value).toLocaleString('zh-CN',{hour12:false}):'尚未收到'},
  bytes(value){if(value==null)return '—';const units=['B','KB','MB','GB','TB'];let n=Number(value),i=0;while(n>=1024&&i<4){n/=1024;i++}return `${n.toFixed(i?1:0)} ${units[i]}`},
  title(view){return this.nav.find(n=>n.id===view)?.name||''},
  sourceName(id){return this.data.sources.find(s=>s.id===id)?.name||id},
  openSource(){this.edit={name:'本地电影库',path:'/mnt/',_revision:this.data.revision};this.modal='source'},
  async saveSource(){await this.action('/sources',this.edit,'服务器目录已接入，可开始扫描')},
  passwordManagerUrl(){return 'https://'+location.hostname+':9090/hotel-passwords'},
  nasUrl(port){return `${location.protocol}//${location.hostname}:${port}`},
  async syncJellyfin(refresh=false){await this.action('/jellyfin/sync',{refresh},refresh?'已通知 Jellyfin 扫描；酒店片库每 2 分钟自动同步':'已开始同步 Jellyfin，新作品需在影片片库上架',false)},
  async scan(source){await this.action('/scan',{source_id:source.id},'已开始后台扫描；新作品先保存为未上架',false)},
  selected:[],batchCategory:'',batchResult:null,pendingConfirm:null,
  async confirmPending(){const pending=this.pendingConfirm;this.pendingConfirm=null;if(pending)await pending.run()},
  get selectedCount(){return this.selected.length},
  toggleSelected(id){this.selected=this.selected.includes(id)?this.selected.filter(x=>x!==id):[...this.selected,id]},
  get allFilteredSelected(){return this.filteredMedia.length>0&&this.filteredMedia.every(m=>this.selected.includes(m.id))},
  selectFiltered(){const ids=this.filteredMedia.map(m=>m.id);this.selected=this.allFilteredSelected?this.selected.filter(id=>!ids.includes(id)):[...new Set([...this.selected,...ids])]},
  async batchMedia(action,ids=this.selected,value=undefined,confirmed=false){if(!ids.length)return;if(action==='archive'&&!confirmed){this.pendingConfirm={message:`归档 ${ids.length} 部影片？影片会下架，原文件保留，可在已归档筛选中恢复。`,run:()=>this.batchMedia(action,[...ids],value,true)};return;}const r=await this.action('/media/batch',{ids:[...ids],action,value},'',false);if(!r)return;this.selected=this.selected.filter(id=>!r.ok.includes(id));this.notify(`成功 ${r.ok.length} 部，失败 ${r.failed.length} 部`);if(r.failed.length){this.batchResult=r;this.modal='batch-result'}},
  categoryEdit:null,categoryInto:'',
  get mediaCategories(){return this.data.categories.filter(c=>c.name===this.edit.category||(c.enabled&&['all',this.edit.kind].includes(c.kind)))},
  openCategories(){this.categoryEdit=null;this.modal='categories'},
  editCategory(c=null){this.categoryEdit=c?{...copy(c),aliasText:c.aliases.join('，'),_revision:this.data.revision}:{id:'cat-'+newUuid(),name:'',kind:'all',enabled:true,sort:Math.max(-10,...this.data.categories.map(c=>c.sort))+10,aliasText:'',_revision:this.data.revision};this.categoryInto=''},
  async saveCategory(){const c=this.categoryEdit;const r=await this.action('/categories/'+encodeURIComponent(c.id),{...c,aliases:c.aliasText.split(/[，,\n]/).map(a=>a.trim()).filter(Boolean)},'分类已保存',false);if(r)this.categoryEdit=null},
  async moveCategory(c,direction){const all=this.data.categories,i=all.findIndex(x=>x.id===c.id),j=i+direction;if(j<0||j>=all.length)return;const edge=all[j].sort,outer=all[j+direction]?.sort;const sort=outer==null?edge+direction*10:(edge+outer)/2;await this.action('/categories/'+encodeURIComponent(c.id),{...c,sort},'分类顺序已更新',false)},
  async mergeCategory(){const c=copy(this.categoryEdit),target=this.data.categories.find(x=>x.id===this.categoryInto);if(!target)return;this.pendingConfirm={message:`将「${c.name}」全部影片合并到「${target.name}」？原名称将保留为别名。`,run:async()=>{const r=await this.action('/categories/'+encodeURIComponent(c.id)+'/merge',{into:target.id,_revision:c._revision},'分类已合并',false);if(r)this.categoryEdit=null}}},
  async deleteCategory(c){this.pendingConfirm={message:`删除空分类「${c.name}」？`,run:async()=>{const r=await this.action('/categories/'+encodeURIComponent(c.id)+'/delete',{},'空分类已删除',false);if(r)this.categoryEdit=null}}},
  openMedia(item){this.edit={...copy(item),_revision:this.data.revision};this.modal='media'},
  async saveMedia(){await this.action('/media/'+encodeURIComponent(this.edit.id),this.edit,'影片已保存，电视约 30 秒内读取更新')},
  async toggleMedia(item){await this.action('/media/'+encodeURIComponent(item.id),{published:!item.published},item.published?'影片已下架，原文件保留':'影片已上架')},
  newCampaign(){this.edit={id:'ad-'+Date.now().toString(36)+'-'+Array.from(crypto.getRandomValues(new Uint8Array(8)),b=>b.toString(16).padStart(2,'0')).join(''),name:'',merchant:'酒店自营',tier:1,format:'图文',creative_card:typeof sponsorCardDefaults==='function'?sponsorCardDefaults():{},rotation_order:100,rotation_enabled:true,path:'',text:'',placements:['preroll'],group:'全部客房',status:'草稿',priority:50,duration:15,gap_seconds:300,cap:2,start:'00:00',end:'00:00',from:'',until:'',timezone:this.data.settings.timezone,days:[1,2,3,4,5,6,7],_revision:this.data.revision};this.modal='campaign'},
  openCampaign(ad){this.edit={...copy(ad),creative_card:{...(typeof sponsorCardDefaults==='function'?sponsorCardDefaults():{}),...(ad.creative_card||{})},rotation_order:ad.rotation_order??100,rotation_enabled:ad.rotation_enabled!==false,_revision:this.data.revision};this.modal='campaign'},
  changeTier(){this.edit.tier=Number(this.edit.tier);this.edit.format=this.edit.tier===1?'图文':'文字';this.edit.placements=this.edit.tier===1?['preroll']:['all'];if(this.edit.tier===1&&typeof this.adFormatChanged==='function')this.adFormatChanged()},
  togglePlacement(key){this.edit.placements=this.edit.placements.includes(key)?this.edit.placements.filter(p=>p!==key):[...this.edit.placements,key]},
  toggleDay(day){this.edit.days=this.edit.days.includes(day)?this.edit.days.filter(n=>n!==day):[...this.edit.days,day]},
  async saveCampaign(status){const c={...this.edit,status};if(status==='投放中'&&!confirm('确认发布到真实电视？客户端约 30 秒内读取此广告。'))return;await this.action('/campaigns/'+c.id,c,status==='投放中'?'广告已发布到真实电视':'广告已保存')},
  async statusCampaign(ad,status){if(status==='投放中'&&!confirm('确认将此广告发布到真实电视？'))return;await this.action('/campaigns/'+ad.id,{...ad,status},'广告状态已更新')},
  placementLabel(ad){return (ad.placements||[]).map(p=>placements[p]||p).join(' / ')},
  schedule(ad){return ad.tier===2?'全天':ad.start===ad.end?'全天':`${ad.start}–${ad.end}${ad.start>ad.end?' · 跨午夜':''}`},
  async upload(file,kind){if(!file||this.uploading)return;this.uploading=true;try{const response=await fetch('/api/admin/uploads?kind='+kind,{method:'POST',credentials:'same-origin',headers:{'X-CSRF-Token':this.user.csrf,'X-Filename':encodeURIComponent(file.name)},body:file});const r=await response.json();if(!response.ok)throw Error(r.error||'上传失败');if(r.previous_revision===this.edit._revision)this.edit._revision=r.revision;if(kind==='posters')this.edit.poster=r.path;else{this.edit.path=r.path;this.edit.format=/\.(jpg|jpeg|png|webp)$/i.test(file.name)?'图片':'视频'}this.notify('素材已上传到 Ubuntu')}catch(e){this.notify(e.message)}finally{this.uploading=false}},
  openDevice(device=null){this.edit=device?{...copy(device),_existing:true}:{id:'room-',room:'',group:'全部客房',enabled:true,model:'',version:'',_existing:false};this.edit._revision=this.data.revision;this.modal='device'},
  async saveDevice(){const r=await this.action('/devices/'+encodeURIComponent(this.edit.id),this.edit,'客房设备已保存');if(r?.token){this.oneTimeToken=r.token;this.modal='token'}},
  async saveSettings(){const r=await this.action('/settings',{...this.settingsDraft,_revision:this.settingsDirty?this.settingsRevision:this.data.revision},'设置已保存');if(r){this.settingsDirty=false;this.settingsDraft=copy(this.data.settings)}},
  async updateReports(){this.report=await this.api('/reports?days='+Number(this.days))},
  download(name,blob){const u=URL.createObjectURL(blob),a=document.createElement('a');a.href=u;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(u),2000)},
  async exportReport(){try{const r=await fetch('/api/admin/reports.csv?days='+Number(this.days),{credentials:'same-origin'});if(!r.ok)throw Error('导出失败');this.download('酒店广告真实报表.csv',await r.blob())}catch(e){this.notify(e.message)}},
  async backup(){if(this.busy)return;this.busy=true;try{const r=await fetch('/api/admin/backups',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':this.user.csrf},body:JSON.stringify({revision:this.data.revision})});if(!r.ok)throw Error('备份失败');this.download('hotel-backup-'+Date.now()+'.sqlite3',await r.blob());this.notify('数据库备份已下载，包含账号与设备凭证，请妥善保管')}catch(e){this.notify(e.message)}finally{this.busy=false}},
  async restore(release){if(!confirm(`确认恢复「${release.action}」时保存的影片、广告和系统配置？原视频、账号、设备及历史事件保持保留。`))return;await this.action('/releases/'+release.id,{},'历史配置已恢复')},
  newUser(){this.edit={name:'',password:'',role:'operator',enabled:true,_existing:false,_revision:this.data.revision};this.modal='user'},
  async saveUser(){await this.action('/users',this.edit,'账号已保存；权限修改或密码更新会使其旧登录失效')},
  openUser(u){this.edit={...u,password:'',enabled:!!u.enabled,_existing:true,_revision:this.data.revision};this.modal='user'},
  previewMedia(item){this.preview={title:item.title,path:item.path||item.episodes?.[0]?.path,poster:item.poster};this.modal='preview'},
  previewAd(ad){this.preview={...ad,title:ad.name};this.modal='ad-preview'},
  bar(value){const max=Math.max(1,...this.report.timeline.map(d=>d.ad_starts));return Math.max(2,value/max*100)+'%'},
  close(){this.modal=null;this.preview=null;this.oneTimeToken=''}
}}
document.addEventListener('alpine:init',()=>Alpine.data('cinemaAdmin',cinemaAdmin));
