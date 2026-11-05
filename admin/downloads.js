/* Smart download (D12): one-sentence requests, live task progress, candidates, controls and connection settings. */
function downloadsBaseUi(){return {
  downloadsMarkup:'',dl:{rows:[],counts:{},labels:{},notices:[],ready:{engines:{}},disk:{}},dlFilter:'active',dlQuery:'',dlText:'',dlAuto:null,dlManual:false,
  dlManualDraft:{uri:'',title:'',kind:'movie',torrent_b64:'',torrent_name:''},dlModal:null,dlView:null,dlClarify:null,dlSettings:null,dlSecrets:null,dlSecretsPresent:{},dlHealth:{},dlTimer:null,
  dlKinds:{movie:'电影',series:'电视剧'},dlEngineNames:{qbittorrent:'qBittorrent',aria2:'aria2'},
  dlSourcePresets:{btsearch:{name:'BTSearch 中文索引',url:'https://www.btsearch.love',hint:'中文标题、系列与合集；按页合并结果'},knaben:{name:'Knaben 聚合搜索',url:'https://api.knaben.org/v1',hint:'多站聚合，兼顾中文与英文'},apibay:{name:'ApiBay',url:'https://apibay.org',hint:'偏英文，中文覆盖有限'},nyaa:{name:'Nyaa',url:'https://nyaa.si',hint:'动漫、亚洲影视及字幕组资源'}},
  dlStateClass(s){return {ready:'online',failed:'offline',cancelled:'neutral',no_results:'offline',needs_config:'warn',needs_clarify:'warn',awaiting_choice:'warn',paused:'neutral'}[s]||'online'},
  dlBytes(n){if(n==null)return '大小未知';const u=['B','KB','MB','GB','TB'];let i=0;while(n>=1024&&i<4){n/=1024;i++}return (i?n.toFixed(1):n)+' '+u[i]},
  dlEta(s){if(s==null)return '剩余时间未知';if(s<60)return '约 '+s+' 秒';if(s<3600)return '约 '+Math.round(s/60)+' 分钟';return '约 '+(s/3600).toFixed(1)+' 小时'},
  dlPercent(t){const p=t.progress||{};return p.total?Math.min(100,Math.floor(p.downloaded*100/p.total)):null},
  dlParsed(t){const p=t.parsed||{},out=[];if(p.title)out.push(['片名',p.title,p.fields?.title]);if(p.kind)out.push(['类型',this.dlKinds[p.kind]||p.kind,p.fields?.kind]);if(p.year)out.push(['年份',p.year,p.fields?.year]);
    if(p.resolution)out.push(['画质',p.resolution,p.fields?.resolution]);if(p.season)out.push(['季',p.season,p.fields?.season]);if(p.episodes)out.push(['集',p.episodes.join('–'),p.fields?.episodes]);
    if((p.audio||[]).length)out.push(['音轨',p.audio.join('、'),'']);if((p.subtitle||[]).length)out.push(['字幕',p.subtitle.join('、'),'']);if(p.max_size_gb)out.push(['体积上限',p.max_size_gb+' GB','']);return out},
  get dlRows(){const q=this.dlQuery.trim().toLowerCase(),f=this.dlFilter,active=['needs_config','needs_clarify','searching','awaiting_choice','queued','downloading','paused','verifying','organizing','importing','scraping','converting','publishing'];
    return (this.dl.rows||[]).filter(t=>(!f||f==='all'||(f==='active'?active.includes(t.state):f==='attention'?['needs_config','needs_clarify','awaiting_choice','failed','no_results'].includes(t.state):t.state===f))&&(!q||(t.request+' '+(t.parsed?.title||'')).toLowerCase().includes(q)))},
  get dlReadyText(){const r=this.dl.ready||{},e=r.engines||{},bits=[(r.sources||0)+' 个搜索源'];for(const k of ['qbittorrent','aria2'])bits.push(this.dlEngineNames[k]+(e[k]?' 就绪':' 未配置'));return bits.join(' · ')},
  async loadDownloads(){try{this.dl=await this.api('/operations?section=downloads&archived=0');if(this.dlView){const fresh=this.dl.rows.find(t=>t.id===this.dlView.id);if(fresh)this.dlView=fresh}}catch(e){this.notify(e.message)}},
  startDownloadsView(){this.loadDownloads();clearInterval(this.dlTimer);this.dlTimer=setInterval(()=>{if(this.view!=='downloads'){clearInterval(this.dlTimer);return}if(document.visibilityState!=='hidden')this.loadDownloads()},3000)},
  async dlSend(){const text=this.dlText.trim();if(!text&&!this.dlManual)return this.notify('请先输入一句需求');const body={kind:'download',action:'download_create',request:text};
    if(this.dlAuto!==null)body.auto=this.dlAuto;
    if(this.dlManual){const d=this.dlManualDraft;if(!d.uri&&!d.torrent_b64)return this.notify('请粘贴链接或选择种子文件');body.manual={uri:d.uri.trim(),title:d.title.trim(),kind:d.kind,torrent_b64:d.torrent_b64||undefined}}
    const r=await this.action('/operations',body,'已提交，后台自动处理',false);if(r){this.dlText='';this.dlManualDraft={uri:'',title:'',kind:'movie',torrent_b64:'',torrent_name:''};this.dlManual=false;await this.loadDownloads()}},
  async dlTorrent(file){if(!file)return;if(file.size>700*1024)return this.notify('种子文件过大');const buf=new Uint8Array(await file.arrayBuffer());let s='';for(let i=0;i<buf.length;i+=8192)s+=String.fromCharCode.apply(null,buf.subarray(i,i+8192));this.dlManualDraft.torrent_b64=btoa(s);this.dlManualDraft.torrent_name=file.name},
  async dlAct(t,op,extra={}){const labels={pause:'已暂停',resume:'已继续',cancel:'已取消（已下载文件保留）',retry:'已重试',choose:'已选择来源',clarify:'已更新需求',retry_scrape:'已重新刮削',publish:'正在上架',archive:'已移出列表'};
    if(op==='cancel'&&!confirm('取消后下载器中的任务会被移除，已下载的文件保留在暂存目录，不会删除原片。确认取消？'))return;
    const r=await this.action('/operations',{kind:'download',action:'download_action',id:t.id,op,...extra},labels[op]||'已完成',false);if(r){await this.loadDownloads();if(op==='choose'||op==='clarify')this.dlModal=this.dlModal==='detail'?'detail':null}},
  dlOpen(t){this.dlView=t;this.dlModal='detail'},
  dlEditRequest(t){const p=t.parsed||{};this.dlClarify={id:t.id,title:p.title||'',kind:p.kind||'movie',year:p.year||'',resolution:p.resolution||'',season:p.season||'',episodes:p.episodes?p.episodes.join('-'):'',accept_existing:false};this.dlModal='clarify'},
  async dlSaveClarify(){const c=this.dlClarify,ep=String(c.episodes||'').split(/[-–~到]/).map(x=>Number(x)).filter(x=>x>0);
    await this.dlAct({id:c.id},'clarify',{overrides:{title:c.title,kind:c.kind,year:c.year?Number(c.year):null,resolution:c.resolution,season:c.season?Number(c.season):null,episodes:ep.length?[ep[0],ep[ep.length-1]]:null},accept_existing:c.accept_existing});this.dlModal=null},
  async dlOpenSettings(){try{const v=await this.api('/operations?section=download_settings');this.dlSettings=copy(v.settings);this.dlSecretsPresent=v.secrets_present;this.dlHealth=v.health||{};
    this.dlSecrets={qbittorrent:{password:''},aria2:{secret:''},sources:{}};this.dlModal='settings'}catch(e){this.notify(e.message)}},
  dlAddSource(type){const p=this.dlSourcePresets[type],n=(this.dlSettings.sources||[]).length+1;
    const existing=p&&this.dlSettings.sources.find(s=>s.type===type&&s.url.replace(/\/$/,'')===p.url);if(existing){existing.enabled=true;return this.notify('已启用现有的 '+p.name+'，保存后生效')}
    this.dlSettings.sources.push({id:type+'-'+n+'-'+newUuid().slice(0,4),type,name:p?.name||(type==='prowlarr'?'Prowlarr':'Torznab '+n),url:p?.url||'',enabled:true,timeout:20})},
  dlSourceChanged(s){const p=this.dlSourcePresets[s.type];if(p){s.url=p.url;s.name=p.name}delete this.dlHealth['source:'+s.id]},
  dlSourceLabel(type){return this.dlSourcePresets[type]?'公开接口地址':type==='prowlarr'?'Prowlarr 地址（如 http://127.0.0.1:9696）':'Torznab 地址（…/api）'},
  async dlSaveSettings(){const sec={};for(const k of ['qbittorrent','aria2'])for(const [f,v] of Object.entries(this.dlSecrets[k]||{}))if(v)(sec[k]=sec[k]||{})[f]=v;
    const src=Object.fromEntries(Object.entries(this.dlSecrets.sources||{}).filter(([k,v])=>v));if(Object.keys(src).length)sec.sources=src;
    const r=await this.action('/operations',{kind:'download',action:'download_settings',settings:this.dlSettings,secrets:sec},'智能下载设置已保存',false);
    if(r){const before=this.dl.default_dir;this.dlSettings=copy(r.settings);this.dlSecretsPresent=r.secrets_present;this.dlHealth=r.health||{};this.dlSecrets={qbittorrent:{password:''},aria2:{secret:''},sources:{}};await this.loadDownloads();if(!this.dlDownloadDir||this.dlDownloadDir===before)this.dlDownloadDir=r.settings.save_dir||r.settings.staging_dir;return r}},
  async dlClearSecret(kind,field,source){const sec=source?{sources:{[source]:''}}:{[kind]:{[field]:''}};const r=await this.action('/operations',{kind:'download',action:'download_settings',settings:{},secrets:sec},'已清除',false);if(r)this.dlSecretsPresent=r.secrets_present},
  async dlTest(target){const r=await this.action('/operations',{kind:'download',action:'download_test',target},'连接测试完成',false);if(r)this.dlHealth={...this.dlHealth,[target]:r}},
  dlHealthText(target){const h=this.dlHealth[target];if(!h)return '未测试';return ({ok:'连接成功',auth_failed:'鉴权失败',unreachable:'不可达',unconfigured:'未配置',unavailable:'不可用'}[h.status]||h.status)+(h.version?' · '+h.version:'')+(h.message&&h.message!=='连接成功'?' · '+h.message:'')},
  dlHealthClass(target){const s=this.dlHealth[target]?.status;return s==='ok'?'online':s==='auth_failed'||s==='unreachable'?'offline':'neutral'}
}}
