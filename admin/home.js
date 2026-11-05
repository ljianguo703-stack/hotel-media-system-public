/* MD01: TV home appearance, read-only guide cards and timed topics. */
function homeUi(){const app={
  homeDraft:null,homeDirty:false,guideDraft:null,topicDraft:null,topicSearch:'',homeUploading:'',
  get homeData(){return this.opsData&&this.opsData.home?this.opsData:{home:null,guide:[],topics:[]}},
  get guideRows(){return (this.homeData.guide||[]).filter(g=>!g._archived).sort((a,b)=>(a.sort-b.sort)||a.name.localeCompare(b.name))},
  get topicRows(){return (this.homeData.topics||[]).filter(t=>!t._archived).sort((a,b)=>(a.sort-b.sort)||a.name.localeCompare(b.name))},
  get topicCandidates(){const q=this.topicSearch.trim().toLowerCase(),chosen=new Set(this.topicDraft?.item_ids||[]);return (this.data.media||[]).filter(m=>!m._archived&&!chosen.has(m.id)&&(!q||(m.title||'').toLowerCase().includes(q)||(m.category||'').toLowerCase().includes(q))).slice(0,30)},
  mediaTitle(id){return (this.data.media||[]).find(m=>m.id===id)?.title||id},
  mediaPlayable(id){const m=(this.data.media||[]).find(x=>x.id===id);return !!(m&&m.published&&m.available&&!m._archived)},
  homeLoaded(result){if(!this.homeDirty)this.homeDraft={...result.home};if(!this.menuDirty)this.menuLoaded(result)},
  selectDefaultBackground(image){if(!this.writable||!this.homeDraft)return;const selected=(this.homeData.default_backgrounds||[]).find(row=>row.id===image.id);if(!selected)return;this.homeDraft.background=selected.path;this.homeDirty=true},
  topicState(t){if(t._archived)return '已归档';if(!t.enabled)return '已停用';if(t.live)return '展示中';const now=new Date().toISOString().slice(0,16);return t.start&&t.start>now?'未开始':'已结束'},
  async uploadHome(file,field,draft='homeDraft'){if(!file||this.homeUploading)return;this.homeUploading=field;try{const r=await fetch('/api/admin/uploads?kind=home',{method:'POST',credentials:'same-origin',headers:{'X-CSRF-Token':this.user.csrf,'X-Filename':encodeURIComponent(file.name)},body:file});const d=await r.json();if(!r.ok)throw Error(d.error||'上传失败');this[draft][field]=d.path;if(draft==='homeDraft')this.homeDirty=true;this.notify('图片已上传：'+file.name)}catch(e){this.notify(e.message)}finally{this.homeUploading=''}},
  async saveHome(){const d=this.homeDraft;const r=await this.managedAction({kind:'home',action:'home_save',background:d.background,contact_qr:d.contact_qr,contact_label:d.contact_label,wifi_ssid:d.wifi_ssid,wifi_password:d.wifi_password,wifi_security:d.wifi_security,wifi_hidden:!!d.wifi_hidden,wifi_label:d.wifi_label,wifi_show_password:!!d.wifi_show_password,show_qr:!!d.show_qr,_revision:this.data.revision},'电视首页设置已保存',false);if(r){this.homeDirty=false;this.homeDraft={...r.home}}},
  editGuide(item=null){this.guideDraft=item?{...copy(item),_revision:this.data.revision}:{id:'guide-'+newUuid().slice(0,12),name:'',body:'',image:'',sort:100,enabled:true,_revision:this.data.revision};this.modal='guide-edit'},
  async saveGuide(){const {_revision,...item}=this.guideDraft;await this.managedAction({kind:'guide',action:'save',item:{...item,sort:Number(item.sort)},_revision},'指南卡片已保存')},
  editTopic(item=null){this.topicSearch='';this.topicDraft=item?{...copy(item),_revision:this.data.revision}:{id:'topic-'+newUuid().slice(0,12),name:'',description:'',item_ids:[],start:'',end:'',sort:100,enabled:true,_revision:this.data.revision};this.modal='topic-edit'},
  topicAdd(id){this.topicDraft.item_ids=[...this.topicDraft.item_ids,id]},
  topicRemove(id){this.topicDraft.item_ids=this.topicDraft.item_ids.filter(x=>x!==id)},
  topicMove(index,delta){const list=[...this.topicDraft.item_ids],to=index+delta;if(to<0||to>=list.length)return;[list[index],list[to]]=[list[to],list[index]];this.topicDraft.item_ids=list},
  async saveTopic(){const {_revision,live,...item}=this.topicDraft;await this.managedAction({kind:'topic',action:'save',item:{...item,sort:Number(item.sort)},_revision},'专题已保存')}
};Object.defineProperties(app,Object.getOwnPropertyDescriptors(homeMenuUi()));return app}

/* PD07: independent draft/revision so editing the background never overwrites menu changes. */
function homeMenuUi(){return {
  menuDraft:[],menuDirty:false,menuRevision:0,
  menuBuiltins:[{id:'movie',name:'电影'},{id:'series',name:'电视剧'},{id:'apps',name:'已装应用'},{id:'more',name:'更多'},{id:'recent',name:'继续观看'},{id:'channels',name:'频道'},{id:'guide',name:'酒店指南'}],
  menuIcons:{movie:'电影',series:'电视',apps:'应用',more:'更多',music:'音乐',collection:'合集',channels:'频道',guide:'指南'},
  get menuShown(){return this.menuDraft.filter(c=>c.enabled)},
  get menuTrash(){return (this.homeData.menu||[]).filter(c=>c._archived)},
  get menuCategories(){return (this.data.categories||[]).filter(c=>!c._archived)},
  get menuApps(){const byPackage=new Map();for(const device of this.homeData.app_inventory||[])for(const app of device.apps||[]){const found=byPackage.get(app.package);if(found)found.rooms.push(device.room);else byPackage.set(app.package,{...app,rooms:[device.room]})}return [...byPackage.values()].sort((a,b)=>a.label.localeCompare(b.label))},
  menuLoaded(result){this.menuDraft=copy((result.menu||[]).filter(c=>!c._archived));this.menuRevision=result.revision??this.data.revision;this.menuDirty=false},
  menuAdd(){if(this.menuDraft.length>=20)return this.notify('最多保留 20 张菜单卡片，其余可移入回收站');this.menuDraft.push({id:'home-'+newUuid().slice(0,12),name:'新菜单',type:'builtin',target:'more',icon:'collection',enabled:this.menuShown.length<5});this.menuDirty=true},
  menuType(card){card.target=card.type==='builtin'?'more':'';card.icon=card.type==='app'?'apps':'collection';this.menuDirty=true},
  menuMove(index,delta){const to=index+delta;if(to<0||to>=this.menuDraft.length)return;const cards=[...this.menuDraft];[cards[index],cards[to]]=[cards[to],cards[index]];this.menuDraft=cards;this.menuDirty=true},
  menuToggle(card){if(!card.enabled&&this.menuShown.length>=5)return this.notify('首页最多显示 5 张，请先隐藏其他卡片');if(card.enabled&&this.menuShown.length<=1)return this.notify('至少保留一张显示的卡片');card.enabled=!card.enabled;this.menuDirty=true},
  menuRemove(card){if(card.enabled&&this.menuShown.length<=1)return this.notify('至少保留一张显示的卡片');this.pendingConfirm={message:`将「${card.name}」移入回收站？点击“保存菜单”后生效；不删除媒体，也不卸载应用。`,run:()=>{this.menuDraft=this.menuDraft.filter(c=>c.id!==card.id);this.menuDirty=true}}},
  menuAppSelect(card,value){if(!value)return;card.target=value;const app=this.menuApps.find(a=>a.package===value);if(app)card.name=app.label.slice(0,12);this.menuDirty=true},
  async saveMenu(){if(this.menuShown.length<1||this.menuShown.length>5)return this.notify('首页需显示 1–5 张卡片');const cards=this.menuDraft.map(({id,name,type,target,icon,enabled})=>({id,name,type,target,icon,enabled:!!enabled}));const result=await this.managedAction({kind:'home',action:'home_menu_save',cards,_revision:this.menuRevision},'首页菜单已保存',false);if(result)this.menuLoaded(result)},
  resetMenu(){const revision=this.menuRevision;this.pendingConfirm={message:'恢复电影、电视剧、已装应用、更多四张默认卡片？未保存修改将放弃，自定义入口移入回收站；媒体和应用保留。',run:async()=>{const result=await this.managedAction({kind:'home',action:'home_menu_reset',_revision:revision},'默认菜单已恢复',false);if(result)this.menuLoaded(result)}}},
  async restoreMenuCard(card){if(this.menuDirty)return this.notify('请先保存或取消菜单修改，再恢复卡片');const result=await this.managedAction({kind:'home_card',action:'restore',ids:[card.id],_revision:this.menuRevision},'卡片已恢复为隐藏状态',false);if(result)this.menuLoaded(this.homeData)}
}}
