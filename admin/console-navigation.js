/* A layout: legacy screens and their existing permission/API paths remain authoritative. */
const consoleBaseFactory=cinemaAdmin;
const consoleEntries=[
 ['overview','运行概览','overview','overview'],
 ['media','影片片库','content','media'],['pending','待上架','content','pending'],['downloads','加片与下载','content','downloads'],
 ['categories','分类管理','content','media','categories'],['sources','影片接入与扫描','content','nas','storage'],['sync','Jellyfin 同步','content','operations','sync',true],['compat','兼容检查','content','operations','compat'],
 ['home','电视首页与指南','television','operations','home'],['topics','专题管理','television','operations','topics'],['channels','频道与节目单','television','operations','channels'],['devices','客房设备','television','devices'],['groups','设备分组','television','operations','groups',true],['fleet','批量运维','television','fleet',null,true],['guest_requests','观众留言','television','operations','guest_requests',true],
 ['ads','广告投放','advertising','ads'],['assets','广告素材库','advertising','operations','assets'],['advertisers','广告主','advertising','operations','advertisers'],['orders','投放单','advertising','operations','orders'],['packages','广告套餐','advertising','operations','packages'],['control','广告总控','advertising','operations','control'],['ad_playback','广告播放规则','advertising','operations','ad_playback'],['reports','投放报表','advertising','reports'],
 ['health','服务器状态','server','operations','health'],['files','文件中心','server','service','files'],['shared','电脑共享','server','nas','shared'],['disks','硬盘管理','server','service','cockpit',true],['containers','容器管理','server','service','dockge',true],['applications','应用部署','server','nas','install',true],['jellyfin','Jellyfin 管理','server','service','jellyfin'],['remote','NPS 穿透','server','nas','npc',true],['ports','端口与公网入口','server','nas','access',true],['software','软件与更新','server','software',null,true],['tasks','任务中心','server','operations','tasks'],
 ['general','酒店与时区','system','settings','hotel'],['users','账号与权限','system','settings','users',true],['passwords','密码与电视 PIN','system','passwords',null,true],['backups','数据库备份','system','operations','backups',true],['releases','配置历史与恢复','system','settings','history',true],['audit','操作审计','system','operations','audit'],['trash','回收站','system','operations','trash']
].map(([id,name,group,view,target,admin])=>({id,name,group,view,target,admin:!!admin}));
const consoleGroupInfo=[['overview','工作台','◈'],['content','片库与下载','▣'],['television','电视与客房','▤'],['advertising','广告经营','◷'],['server','服务器与应用','▦'],['system','系统设置','⚙']];
const consoleAliases={downloads:'搜索与下载 Seerr Radarr Sonarr qBittorrent TMDB 代理',home:'壁纸 背景 wifi 二维码 酒店指南',sync:'同步设置 NAS 元数据',sources:'NAS 影片接入 目录 扫描',remote:'NAS npc nps 内网穿透',ports:'NAS 公网 IP 域名 服务器接入',files:'NAS File Browser 上传',disks:'NAS Cockpit 挂载 存储',containers:'NAS Docker Dockge',passwords:'密码管理 忘记密码 PIN 维护码',ad_playback:'播放规则 中插 间隔',guest_requests:'扫码 留言 求片',releases:'配置历史与备份 恢复 回退'};
cinemaAdmin=function(){
 const app=consoleBaseFactory(),init=app.init,reload=app.reload,navigate=app.navigate,close=app.close,logout=app.logout;
 Object.defineProperties(app,Object.getOwnPropertyDescriptors({
  consoleExpanded:'overview',consoleSearchOpen:false,consoleQuery:'',consoleMarkup:'',dashboardMarkup:'',dashboardData:null,dashboardError:'',dashboardSerial:0,deviceQuery:'',deviceFloor:'',nicId:'',consoleFavoriteIds:['passwords','home','sync','backups'],
  get consoleItems(){return consoleEntries.filter(i=>!i.admin||this.administrator)},
  get consoleGroups(){return consoleGroupInfo.map(([id,name,mark])=>({id,name,mark,items:this.consoleItems.filter(i=>i.group===id)})).filter(g=>g.items.length)},
  get consoleCurrent(){const matches=this.consoleItems.filter(i=>i.view===this.view);if(this.view==='operations')return matches.find(i=>i.target===this.opsTab);if(this.view==='nas')return matches.find(i=>i.target===this.nasTab);if(this.view==='settings')return matches.find(i=>i.target===new URLSearchParams(location.search).get('section'))||matches[0];return matches[0]},
  get consoleGroup(){return this.consoleCurrent?.group||'system'},
  get consoleOpsTabs(){const ids=new Set(this.consoleItems.filter(i=>i.group===this.consoleGroup&&i.view==='operations').map(i=>i.target));return this.opsTabs.filter(t=>ids.has(t.id))},
  get consoleResults(){const q=this.consoleQuery.trim().toLowerCase();return (q?this.consoleItems.filter(i=>(i.name+' '+(consoleAliases[i.id]||'')+' '+(i.view==='operations'?'管理中心':'')).toLowerCase().includes(q)):this.consoleFavoriteIds.map(id=>this.consoleItems.find(i=>i.id===id)).filter(Boolean)).slice(0,41)},
  get consoleFavorites(){return this.consoleFavoriteIds.map(id=>this.consoleItems.find(i=>i.id===id)).filter(Boolean)},
  openConsoleSettings(){this.navigate('console-settings')},openConsoleAll(){this.navigate('console-all')},
  async openConsole(id){const item=this.consoleItems.find(i=>i.id===id);if(!item)return;this.consoleSearchOpen=false;this.mobileNav=false;this.consoleExpanded=item.group;
   if(item.view==='service'){const url=this.serviceUrl(item.target);if(url)window.open(url,'_blank','noopener');else if(this.administrator){this.openNas('access');this.notify('请先配置该应用的访问入口')}else this.notify('请联系管理员配置应用访问入口');return}
   if(item.view==='operations')return this.openOperations(item.target);
   if(item.view==='nas')return this.openNas(item.target);
   this.navigate(item.view);
   if(item.target==='categories'){this.openCategories();return}
   if(item.view==='settings'&&item.target){const url=new URL(location.href);url.searchParams.set('section',item.target);history.replaceState({},'',url);this.$nextTick(()=>document.getElementById('settings-'+item.target)?.scrollIntoView({behavior:'smooth',block:'start'}))}
  },
  consoleOpenSearch(){this.consoleSearchOpen=true;this.consoleQuery='';this.$nextTick(()=>this.$refs.consoleSearch?.focus())},
  toggleConsoleFavorite(id){this.consoleFavoriteIds=this.consoleFavoriteIds.includes(id)?this.consoleFavoriteIds.filter(i=>i!==id):[...this.consoleFavoriteIds,id];try{localStorage.setItem('hotel-console-favorites-v1',JSON.stringify(this.consoleFavoriteIds))}catch{}},
  async loadDashboard(){if(this.view!=='overview'||!this.user)return;const serial=++this.dashboardSerial;try{const data=await this.api('/dashboard');if(serial!==this.dashboardSerial||this.view!=='overview')return;this.dashboardData=data;this.dashboardError='';if(!data.interfaces.some(n=>n.id===this.nicId))this.nicId=data.interfaces[0]?.id||''}catch(e){if(serial===this.dashboardSerial){this.dashboardError=e.message;this.dashboardData=null}}},
  monitorRate(value,places=2){return Number.isFinite(value)?value.toFixed(places):'—'},
  monitorBytes(value){return Number.isFinite(value)?this.bytes(value):'—'},
  monitorDuration(value){if(!Number.isFinite(value))return '等待首轮心跳';const days=Math.floor(value/86400),hours=Math.floor(value%86400/3600),minutes=Math.floor(value%3600/60);return (days?days+' 天 ':'')+(hours?hours+' 小时 ':'')+(minutes?minutes+' 分钟':value<60?value+' 秒':'')},
  monitorTime(value){return value?new Date(value).toLocaleString('zh-CN',{timeZone:this.dashboardData?.timezone||this.data.settings.timezone||'UTC',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}):'等待首轮心跳'},
  get metrics(){const s=this.dashboardData?.server;return {sampledAt:this.dashboardData?.sampled_at?this.monitorTime(this.dashboardData.sampled_at):'正在采集',server:{name:s?.hostname||'—',ip:s?.ip||'—',uptime:s?.uptime_seconds==null?'未采集':this.monitorDuration(s.uptime_seconds),cpu:s?.cpu_percent??'—',memory:s?.memory?.percent??'—',disk:s?.disk?.percent??'—',memoryText:s?.memory?this.bytes(s.memory.used)+' / '+this.bytes(s.memory.total):'未采集',diskText:s?.disk?this.bytes(s.disk.used)+' / '+this.bytes(s.disk.total):'未采集'}}},
  get monitorServerStatus(){return this.dashboardError?'读取失败':!this.dashboardData?'正在采集':this.dashboardData.server?.available?'运行正常':'系统指标不可用'},
  get onlineDevices(){return (this.dashboardData?.devices||[]).map(d=>({...d,floor:d.group||'未分组',since:this.monitorTime(d.online_since),duration:this.monitorDuration(d.online_seconds),rx:Number.isFinite(d.rx_bps)?d.rx_bps*8/1e6:null,tx:Number.isFinite(d.tx_bps)?d.tx_bps*8/1e6:null,received:this.monitorBytes(d.today_rx),sent:this.monitorBytes(d.today_tx)}))},
  get visibleOnlineDevices(){const q=this.deviceQuery.trim().toLowerCase();return this.onlineDevices.filter(d=>(!this.deviceFloor||d.group===this.deviceFloor)&&(!q||(d.room+' '+d.ip).toLowerCase().includes(q)))},
  get monitorDeviceGroups(){return [...new Set(this.onlineDevices.map(d=>d.group).filter(Boolean))]},
  get activeNics(){return (this.dashboardData?.interfaces||[]).map(n=>({...n,name:n.default_route?'默认路由网口':'已连接网口',link:(n.speed_mbps?n.speed_mbps+' Mbps':'速率未报告')+(n.duplex?' · '+(n.duplex==='full'?'全双工':n.duplex==='half'?'半双工':n.duplex):''),rx:Number.isFinite(n.rx_bps)?n.rx_bps*8/1e6:null,tx:Number.isFinite(n.tx_bps)?n.tx_bps*8/1e6:null,received:this.monitorBytes(n.today_rx),sent:this.monitorBytes(n.today_tx),rxLine:n.history.map(p=>p.rx_bps*8/1e6),txLine:n.history.map(p=>p.tx_bps*8/1e6)}))},
  get activeNic(){return this.activeNics.find(n=>n.id===this.nicId)||this.activeNics[0]||{id:'',name:'未取得网口信息',ip:'—',link:'等待主机采集',rx:null,tx:null,received:'—',sent:'—',rxLine:[],txLine:[],history:[]}},
  get lanNic(){return this.activeNic},
  get monitorScale(){return Math.max(1,Math.ceil(Math.max(0,...this.activeNic.rxLine,...this.activeNic.txLine)*1.1))},
  trafficPoints(values){return values.length<2?'':values.map((v,n)=>(360*n/(values.length-1)).toFixed(1)+','+(92-Math.max(0,v)/this.monitorScale*80).toFixed(1)).join(' ')},
  get monitorChartLabels(){const h=this.activeNic.history;if(h.length<2)return [];const times=[h[0],h[Math.floor((h.length-1)/3)],h[Math.floor(2*(h.length-1)/3)],h.at(-1)];return times.map(p=>new Date(p.at).toLocaleTimeString('zh-CN',{timeZone:this.dashboardData?.timezone||'UTC',hour:'2-digit',minute:'2-digit',hour12:false}))}
 }));
 app.nav.push({id:'console-settings',name:'设置中心'},{id:'console-all',name:'全部功能'});
 app.init=async function(){try{const saved=JSON.parse(localStorage.getItem('hotel-console-favorites-v1')||'null');if(Array.isArray(saved))this.consoleFavoriteIds=saved.filter(id=>consoleEntries.some(i=>i.id===id))}catch{}
  await init.call(this);await Promise.all([['consoleMarkup','console-directory.html'],['dashboardMarkup','dashboard.html']].map(async([key,file])=>{const response=await fetch(file);if(!response.ok)throw Error('界面加载失败：'+file);this[key]=await response.text()}));
  this.consoleExpanded=this.consoleGroup;this.$watch('view',()=>this.consoleExpanded=this.consoleGroup);this.$watch('opsTab',()=>this.consoleExpanded=this.consoleGroup);this.$watch('nasTab',()=>this.consoleExpanded=this.consoleGroup);await this.loadDashboard();
  this.dashboardTimer=setInterval(()=>{if(!document.hidden&&this.user&&this.view==='overview')this.loadDashboard()},5000);
 };
 app.reload=async function(...args){await reload.apply(this,args);await this.loadDashboard()};
 app.navigate=function(view){navigate.call(this,view);this.consoleExpanded=this.consoleGroup;if(view!=='overview')this.dashboardSerial++;else this.loadDashboard()};
 app.close=function(){close.call(this);this.consoleSearchOpen=false};
 app.logout=async function(){this.dashboardSerial++;this.dashboardData=null;this.dashboardError='';await logout.call(this)};
 return app;
};
