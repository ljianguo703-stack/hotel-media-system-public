/* NAS navigation reuses the existing application pages and preserves edited forms. */
const nasNavigationBase = cinemaAdmin;
cinemaAdmin = function(){
  const app=nasNavigationBase();
  Object.defineProperties(app,Object.getOwnPropertyDescriptors({
    nasTab:'sync',
    get nasItems(){return [
      {id:'sync',name:'片库同步'},
      {id:'jellyfin',name:'Jellyfin',service:'jellyfin'},
      {id:'files',name:'文件中心',service:'files'},
      {id:'cockpit',name:'硬盘管理',service:'cockpit',admin:true},
      {id:'dockge',name:'容器管理',service:'dockge',admin:true},
      {id:'shared',name:'电脑共享'},
      {id:'storage',name:'影片接入'},
      {id:'install',name:'应用部署',admin:true},
      {id:'npc',name:'NPS 穿透',admin:true},
      {id:'access',name:'端口与公网入口',admin:true}
    ].filter(item=>!item.admin||this.administrator)},
    nasHref(item){return item.service?(this.serviceUrl(item.service)||'?view=nas#remote-access'):'?view=nas&nas='+item.id},
    readNasLocation(){
      const url=new URL(location.href),legacy={'#npc':'npc','#remote-access':'access'};
      const requested=legacy[url.hash]||url.searchParams.get('nas');
      this.nasTab=this.nasItems.some(item=>!item.service&&item.id===requested)?requested:'sync';
    },
    openNas(id){
      if(!this.nasItems.some(item=>!item.service&&item.id===id))return;
      const entering=this.view!=='nas';
      if(entering)this.navigate('nas');
      this.nasTab=id;
      const url=new URL(location.href);url.searchParams.set('view','nas');url.searchParams.set('nas',id);url.hash='';
      if(entering)history.replaceState({},'',url);else if(url.href!==location.href)history.pushState({},'',url);
      this.mobileNav=false;window.scrollTo(0,0);
    },
    followNas(event,item){
      if(item.service){this.requireService(event,item.service);return}
      // Keep browser open-in-new-tab and modified link clicks available.
      if(event.metaKey||event.ctrlKey||event.shiftKey||event.altKey||event.button>0)return;
      event.preventDefault();this.openNas(item.id);
    },
    async refreshNas(){
      try{await this.reload();if(this.nasTab==='npc')await this.loadNpc();if(this.nasTab==='access')await this.loadAccess()}
      catch(e){this.error=e.message}
    }
  }));
  const init=app.init,login=app.login,nav=app.navigate,requireService=app.requireService;
  app.init=async function(){
    await init.call(this);
    if(this.view==='nas')this.readNasLocation();
    this.$watch('user',()=>{if(!this.nasItems.some(item=>!item.service&&item.id===this.nasTab))this.nasTab='sync'});
    const onLocation=()=>{if(new URL(location.href).searchParams.get('view')==='nas'){this.view='nas';this.readNasLocation()}};
    window.addEventListener('hashchange',onLocation);window.addEventListener('popstate',onLocation);
  };
  app.login=async function(){await login.call(this);if(this.user&&this.view==='nas')this.readNasLocation()};
  app.navigate=function(view){nav.call(this,view);if(view==='nas')this.readNasLocation()};
  app.requireService=function(event,id){
    if(this.serviceUrl(id))return;
    if(!this.administrator){return requireService.call(this,event,id)}
    event.preventDefault();this.openNas('access');this.notify('请在「端口与公网入口」填写该服务的公网地址');
  };
  return app;
};
