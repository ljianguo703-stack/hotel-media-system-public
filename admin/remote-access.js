/* NPS tunnels stay in NPS. This module only maintains reviewed navigation addresses. */
const remoteAccessBase = cinemaAdmin;
cinemaAdmin = function(){const app=remoteAccessBase();Object.defineProperties(app,Object.getOwnPropertyDescriptors({
  accessMarkup:'',accessConfig:null,accessDraft:{local_host:'',urls:{}},accessRevision:0,accessDirty:false,
  accessLoading:false,accessBusy:false,accessError:'',accessSerial:0,accessMode:'auto',
  get accessServices(){return this.accessConfig?.services||[]},
  get accessConfigured(){return Object.values(this.accessConfig?.urls||{}).filter(Boolean).length},
  get accessRemote(){
    if(this.accessMode!=='auto')return this.accessMode==='remote';
    const h=location.hostname.toLowerCase().replace(/^\[|\]$/g,'');
    return h!==this.accessConfig?.local_host&&h!=='localhost'&&!h.endsWith('.local')&&!/^(127\.|10\.|192\.168\.|169\.254\.|172\.(1[6-9]|2\d|3[01])\.|::1$|f[cd][0-9a-f]{2}:|fe80:)/.test(h);
  },
  setAccessMode(mode){this.accessMode=mode;try{localStorage.setItem('hotel-access-mode',mode)}catch{}},
  async loadAccess(reset=false){
    if(!this.administrator)return;
    const ticket=++this.accessSerial;this.accessLoading=true;
    try{if(!this.accessMarkup){const response=await fetch('remote-access.html',{credentials:'same-origin'});if(!response.ok)throw Error('入口表单加载失败，请重新读取');this.accessMarkup=await response.text()}
      const result=await this.api('/remote-access');if(ticket!==this.accessSerial||!this.administrator)return;
      this.accessConfig=result;this.accessError='';
      if(reset||!this.accessDirty){this.accessDraft={local_host:result.local_host,urls:{...result.urls}};this.accessRevision=result.revision;this.accessDirty=false;
        if(this.accessRemote&&!this.accessDraft.urls.hotel)this.accessDraft.urls.hotel=location.origin+'/';
      }
    }catch(e){if(ticket===this.accessSerial)this.accessError=e.message}finally{if(ticket===this.accessSerial)this.accessLoading=false}
  },
  async saveAccess(){
    if(this.accessBusy||!this.administrator)return;
    this.accessBusy=true;this.accessError='';++this.accessSerial;
    try{const result=await this.api('/remote-access',{revision:this.accessRevision,local_host:this.accessDraft.local_host,urls:{...this.accessDraft.urls}});
      if(!this.administrator)return;
      this.accessConfig=result;this.accessDraft={local_host:result.local_host,urls:{...result.urls}};this.accessRevision=result.revision;this.accessDirty=false;
      this.notify('入口地址已保存；请在 NPS 服务端完成对应端口映射');
    }catch(e){this.accessError=e.message;this.notify(e.message)}finally{this.accessBusy=false}
  },
  serviceUrl(id){
    const defaults={hotel:[18791,'http','/'],jellyfin:[8096,'http','/'],files:[8095,'http','/'],dockge:[5001,'http','/'],cockpit:[9090,'https','/'],qbittorrent:[8098,'http','/'],jackett:[9117,'http','/UI/Dashboard'],prowlarr:[9696,'http','/search'],ariang:[6801,'http','/'],seerr:[5055,'http','/'],radarr:[7878,'http','/'],sonarr:[8989,'http','/'],bazarr:[6767,'http','/']};
    const fallback=defaults[id];
    const service=this.accessServices.find(s=>s.id===id)||(fallback?{port:fallback[0],scheme:fallback[1],path:fallback[2]}:null);
    if(this.accessRemote)return this.accessConfig?.urls?.[id]||'';
    if(!service)return '';
    const rawHost=this.accessConfig?.local_host||location.hostname.replace(/^\[|\]$/g,'');
    const host=rawHost.includes(':')?'['+rawHost+']':rawHost;
    return `${service.scheme}://${host}:${service.port}${service.path}`;
  },
  requireService(event,id){if(this.serviceUrl(id))return;event.preventDefault();this.navigate('nas');this.notify('请在「远程访问入口」填写该服务的公网地址');this.$nextTick?.(()=>document.getElementById('remote-access')?.scrollIntoView({behavior:'smooth',block:'start'}))},
  accessTarget(service){const host=this.accessDraft.local_host||this.accessConfig?.local_host||'';return `${host.includes(':')?'['+host+']':host}:${service.port}`},
  async copyAccessTargets(service=null){
    const value=service?this.accessTarget(service):this.accessServices.map(s=>`${s.name}\tTCP\t${this.accessTarget(s)}`).join('\n');
    try{if(navigator.clipboard&&window.isSecureContext)await navigator.clipboard.writeText(value);else{
      const field=document.createElement('textarea');field.value=value;field.style.position='fixed';field.style.opacity='0';document.body.appendChild(field);field.select();const ok=document.execCommand('copy');field.remove();if(!ok)throw Error();
    }this.notify(service?'NPS 目标地址已复制':'端口清单已复制')}catch{this.notify('复制未成功，请选中目标地址手动复制')}
  },
  resetAccess(){++this.accessSerial;this.accessConfig=null;this.accessDraft={local_host:'',urls:{}};this.accessDirty=false;this.accessError='';this.accessLoading=false;}
}));
const init=app.init,login=app.login,logout=app.logout,nav=app.navigate;
app.init=async function(){try{const mode=localStorage.getItem('hotel-access-mode');if(['auto','local','remote'].includes(mode))this.accessMode=mode}catch{}await init.call(this);this.$watch('user',u=>{if(!u||u.role!=='admin')this.resetAccess()});if(this.administrator)await this.loadAccess()};
app.login=async function(){await login.call(this);if(this.administrator)await this.loadAccess()};
app.logout=async function(){this.resetAccess();await logout.call(this)};
app.navigate=function(view){nav.call(this,view);if(view==='nas'&&this.administrator)this.loadAccess()};
const passwordUrl=app.passwordManagerUrl;
if(passwordUrl)app.passwordManagerUrl=function(){const url=this.serviceUrl('cockpit');if(url)return new URL('hotel-passwords',url.endsWith('/')?url:url+'/').href;if(this.accessRemote&&this.accessConfig)return '?view=nas#remote-access';return passwordUrl.call(this)};
return app;};
