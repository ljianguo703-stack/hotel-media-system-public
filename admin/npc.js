/* NPC settings use their own revision; normal page polling never replaces an edited form. */
const npcBase = cinemaAdmin;
cinemaAdmin = function(){const app=npcBase();Object.defineProperties(app,Object.getOwnPropertyDescriptors({
  npcState:null,npcDraft:{enabled:true,server:'',vkey:'',type:'tcp',dns:'',extra:''},npcRevision:0,
  npcLoading:false,npcBusy:false,npcDirty:false,npcError:'',npcShowKey:false,npcLoaded:false,npcSerial:0,
  async loadNpc(reset=false){
    if(!this.administrator)return;
    const ticket=++this.npcSerial;this.npcLoading=true;
    try{const result=await this.api('/npc');if(ticket!==this.npcSerial||!this.administrator)return;
      this.npcState=result;this.npcError='';
      if(reset||!this.npcLoaded){this.npcDraft={...result.config,vkey:''};this.npcRevision=result.config.revision;this.npcDirty=false;this.npcShowKey=false;this.npcLoaded=true}
    }catch(e){if(ticket===this.npcSerial)this.npcError=e.message}finally{if(ticket===this.npcSerial)this.npcLoading=false}
  },
  async npcAction(action){
    if(this.npcBusy||!this.administrator)return;
    this.npcBusy=true;this.npcError='';++this.npcSerial;this.npcLoading=false;
    try{const body={action,revision:this.npcRevision};
      if(action==='save')body.config=Object.fromEntries(['enabled','server','vkey','type','dns','extra'].map(k=>[k,this.npcDraft[k]]));
      const result=await this.api('/npc',body);
      if(!this.administrator)return;
      this.npcState=result;this.npcDraft={...result.config,vkey:''};this.npcRevision=result.config.revision;
      this.npcDirty=false;this.npcLoaded=true;this.npcShowKey=false;
      this.notify(action==='save'?'NPC 配置已保存并应用':action==='stop'?'NPC 已停止':action==='start'?'NPC 已启动，等待连接确认':'NPC 已重启，等待连接确认');
    }catch(e){this.npcError=e.message;this.notify(e.message)}finally{this.npcBusy=false}
  },
  get npcStatusLabel(){return ({waiting_config:'等待配置',running:'运行中',stopped:'已停止',error:'运行异常',unavailable:'状态不可用'})[this.npcState?.runtime?.state]||'尚未检测'},
  get npcStatusHint(){const state=this.npcState?.runtime?.state;return state==='waiting_config'?'填写服务端地址和验证密钥后，保存并应用。':state==='running'?'客户端正在运行；是否通过认证，请以 NPS 服务端的在线状态为准。':state==='stopped'?'客户端已停止，店内播放和本地管理继续可用。':'刷新状态可重新检测客户端。'}
}));
const init=app.init,nav=app.navigate,logout=app.logout;
app.init=async function(){await init.call(this);this.$watch('user',u=>{if(!u||u.role!=='admin'){++this.npcSerial;this.npcDraft.vkey='';this.npcState=null;this.npcLoaded=false;this.npcShowKey=false}});if(this.view==='nas'&&this.administrator)await this.loadNpc()};
app.navigate=function(view){nav.call(this,view);if(view==='nas'&&this.administrator)this.loadNpc()};
const login=app.login;app.login=async function(){await login.call(this);if(this.view==='nas'&&this.administrator)await this.loadNpc()};
app.logout=async function(){++this.npcSerial;this.npcDraft.vkey='';this.npcState=null;this.npcLoaded=false;this.npcDirty=false;this.npcShowKey=false;await logout.call(this)};
return app;};
