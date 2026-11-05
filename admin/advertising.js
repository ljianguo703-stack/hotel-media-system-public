/* AD08: LAN-only playback policy and fixed sponsor-card authoring. */
function sponsorCardDefaults(){return {title:'',body:'',contact:'',image:'',logo:'',qr:'',qr_link:'',qr_label:'扫码了解详情',accent:'#214B53'}}
function sponsorInk(hex){
  const luminance=value=>{const parts=[1,3,5].map(i=>{const c=parseInt(value.slice(i,i+2),16)/255;return c<=.04045?c/12.92:((c+.055)/1.055)**2.4});return parts[0]*.2126+parts[1]*.7152+parts[2]*.0722};
  const l=luminance(/^#[0-9a-f]{6}$/i.test(hex)?hex:'#214B53');return (luminance('#f8fafc')+.05)/(l+.05)>=(l+.05)/(luminance('#0b1118')+.05)?'#f8fafc':'#0b1118';
}
function sponsorCopyTiming(distance,width){const travel=Math.max(0,distance)*960/Math.max(1,width)/24;return {travel,seconds:Math.ceil(travel+4)}}
function sponsorPreview(){return {
  distance:0,readingSeconds:0,
  init(){this.$nextTick(()=>{this.$el._adResize=new ResizeObserver(()=>this.measure());this.$el._adResize.observe(this.$refs.window);this.$el._adResize.observe(this.$refs.track);this.measure(true)})},
  measure(restart=false){
    const {card,track,window:viewport}=this.$refs;if(!card||!track||!viewport||!card.clientWidth)return;
    const distance=Math.max(0,track.scrollHeight-viewport.clientHeight),key=distance+':'+card.clientWidth;
    if(!restart&&key===this.$el._adMetrics)return;this.$el._adMetrics=key;
    this.$el._adMotion?.cancel();track.style.transform='';this.distance=distance;
    const timing=sponsorCopyTiming(distance,card.clientWidth);this.readingSeconds=distance?timing.seconds:0;
    if(distance){const total=timing.travel+4;this.$el._adMotion=track.animate([{transform:'translateY(0)',offset:0},{transform:'translateY(0)',offset:2/total},{transform:`translateY(-${distance}px)`,offset:(total-2)/total},{transform:`translateY(-${distance}px)`,offset:1}],{duration:total*1000,iterations:Infinity,easing:'linear'})}
  },
  destroy(){this.$el._adResize?.disconnect();this.$el._adMotion?.cancel()}
}}
function advertisingUi(){return {
  campaignPackage:'',
  applyCampaignPackage(){
    const p=(this.data.ad_packages||[]).find(p=>p.id===this.campaignPackage),b=this.campaignBundle;if(!p||!b)return;
    b.drafts[b.active]=copy(this.edit);const shared=Object.fromEntries(['name','merchant','group','timezone','from','until','days','_revision'].map(k=>[k,copy(this.edit[k])]));b.tiers=[...(p.tiers||[p.tier])].sort();
    for(const t of b.tiers){if(!b.drafts[t])b.drafts[t]={...copy(this.edit),id:b.id+'-t'+t,tier:t,format:t===1?'图文':'文字',creative_card:sponsorCardDefaults(),text:'',path:'',start:'00:00',end:'00:00'};b.drafts[t].placements=[...(t===p.tier?p.placements:(p.placements_by_tier?.[String(t)]||['all']))];if(t===1)b.drafts[t].duration=p.default_ad_seconds||15}
    b.active=p.tier;this.edit={...copy(b.drafts[p.tier]),...shared};this.notify('已套用所选档位、位置和时长；每档素材仍由你填写');
  },
  campaignBundle:null,adValidation:null,
  get adBundleTiers(){return this.campaignBundle?.tiers||[this.edit.tier]},
  beginCampaignBundle(ad=null){
    this.adValidation=null;
    const bid=ad?._bundle_id||'cmb-'+newUuid().replace(/-/g,'').slice(0,24),rows=ad?._bundle_id?(this.data.campaigns||[]).filter(c=>c._bundle_id===bid&&!c._archived):ad?[ad]:[];
    const drafts=Object.fromEntries(rows.map(c=>[c.tier,copy(c)])),tiers=rows.length?rows.filter(c=>c._bundle_active!==false).map(c=>c.tier).sort():[1];
    this.campaignBundle={id:bid,legacy_id:ad&&!ad._bundle_id?ad.id:null,drafts,tiers:tiers.length?tiers:[ad?.tier||1],active:this.edit.tier};
    this.edit.name=ad?._bundle_name||ad?.name||'';this.campaignPackage='';
  },
  campaignBundleSwitch(tier){
    const b=this.campaignBundle;if(!b||this.uploading||!b.tiers.includes(tier))return;
    b.drafts[b.active]=copy(this.edit);
    const shared=Object.fromEntries(['name','merchant','group','timezone','from','until','days','_revision'].map(k=>[k,copy(this.edit[k])])),saved=b.drafts[tier];
    const fresh={...copy(this.edit),id:b.id+'-t'+tier,tier,format:tier===1?'图文':'文字',creative_card:sponsorCardDefaults(),text:'',path:'',placements:tier===1?['preroll']:['all'],start:'00:00',end:'00:00'};
    this.edit={...(saved||fresh),...shared,tier};b.active=tier;
  },
  campaignBundleToggle(tier){
    if(!this.writable||this.uploading)return;const b=this.campaignBundle;
    if(b.tiers.includes(tier)){
      if(b.tiers.length===1)return this.notify('至少保留一个投放档位');
      if(b.active===tier)this.campaignBundleSwitch(b.tiers.find(t=>t!==tier));
      b.tiers=b.tiers.filter(t=>t!==tier);
      if(this.adValidation?.tier===tier)this.adValidation=null;
    }else{b.tiers=[...b.tiers,tier].sort();this.campaignBundleSwitch(tier)}
  },
  campaignIssue(payload){
    const check=(value,label,limit,required,tier,field)=>{
      if(typeof value!=='string')return {tier,field,message:label+'格式无效'};
      if(required&&!value.trim())return {tier,field,message:label+'不能为空'};
      const count=[...value].length;
      if(count>limit)return {tier,field,message:label+'最多'+limit+'个字符（当前'+count+'）'};
      return null;
    };
    let issue=check(payload.name,'广告名称',180,true,this.campaignBundle.active,'ad-name');if(issue)return issue;
    for(const item of payload.items){
      const t=item.tier,prefix={1:'一档全屏',2:'二档全天文字',3:'三档分时文字'}[t]+'：';
      const checks=[[item.merchant,'广告主',200,true,'ad-merchant']];
      if(t===1&&item.format==='图文'){
        const card={...sponsorCardDefaults(),...(item.creative_card||{})};
        for(const [key,label,limit,required,field] of [['title','赞助商标题',80,true,'card-title'],['body','宣传文字',500,false,'card-body'],['contact','联系方式 / 地址',300,false,'card-contact'],['qr_link','二维码链接',1200,false,'card-link'],['qr_label','二维码说明',40,false,'card-qrlabel']])checks.push([card[key],label,limit,required,field]);
      }else if(t!==1)checks.push([item.text,'滚动文字',2000,true,'ad-text']);
      else if(payload.status==='投放中')checks.push([item.path,'广告素材',2000,true,'ad-library']);
      for(const [value,label,limit,required,field] of checks){issue=check(value,prefix+label,limit,required,t,field);if(issue)return issue}
    }
    return null;
  },
  focusCampaignIssue(){
    const issue=this.adValidation;if(!issue?.field||!this.campaignBundle?.tiers.includes(issue.tier))return;
    this.campaignBundleSwitch(issue.tier);
    this.$nextTick(()=>{const el=document.getElementById(issue.field);if(!el)return;el.setAttribute('aria-invalid','true');el.setAttribute('aria-describedby','ad-validation');el.dataset.adInvalid='true';el.scrollIntoView({block:'center'});el.focus({preventScroll:true})});
  },
  async saveCampaign(status){
    const b=this.campaignBundle;if(!b||this.busy||this.uploading||!this.writable)return;
    this.adValidation=null;this.error='';
    document.querySelectorAll('[data-ad-invalid]').forEach(el=>{el.removeAttribute('aria-invalid');el.removeAttribute('aria-describedby');delete el.dataset.adInvalid});
    b.drafts[b.active]=copy(this.edit);
    const shared=Object.fromEntries(['merchant','group','timezone','from','until','days'].map(k=>[k,copy(this.edit[k])])),payload={name:this.edit.name,status,legacy_id:b.legacy_id,items:b.tiers.map(t=>({...copy(b.drafts[t]),...shared,tier:t})),_revision:this.edit._revision};
    const issue=this.campaignIssue(payload);
    if(issue){this.toast='';clearTimeout(this.toastTimer);this.adValidation=issue;this.focusCampaignIssue();return}
    const submit=async()=>{const r=await this.action('/campaign-bundles/'+b.id,payload,status==='投放中'?'组合广告已整组发布':'组合广告已保存');if(!r&&this.error&&this.modal==='campaign')this.adValidation={message:this.error};return r};
    if(status==='投放中')this.pendingConfirm={message:'确认发布所选的 '+b.tiers.length+' 个档位？全部校验通过后才会整组发布。',run:submit};else await submit();
  },
  get filteredAds(){
    const groups=new Map();for(const ad of this.data.campaigns||[]){if(ad._archived||ad.status==='已归档'||ad._bundle_active===false)continue;const key=ad._bundle_id||ad.id;if(!groups.has(key))groups.set(key,[]);groups.get(key).push(ad)}
    return [...groups.values()].map(items=>{items.sort((a,b)=>a.tier-b.tier);return {...items[0],name:items[0]._bundle_name||items[0].name,_bundle_items:items}}).filter(a=>(this.tier==='全部'||a._bundle_items.some(c=>c.tier===Number(this.tier)))&&(!this.adStatus||a._bundle_items.some(c=>c.status===this.adStatus))&&(!this.adGroup||a.group===this.adGroup)&&(!this.adMerchant||a.merchant===this.adMerchant)&&`${a.name} ${a.merchant}`.toLowerCase().includes(this.search.toLowerCase()));
  },
  bundleTierLabel(ad){return (ad._bundle_items||[ad]).map(c=>this.tierNames[c.tier]).join(' + ')},
  bundlePlacementLabel(ad){return (ad._bundle_items||[ad]).map(c=>((ad._bundle_items||[]).length>1?this.tierNames[c.tier]+'：':'')+this.placementLabel(c)).join('；')},
  bundleScheduleLabel(ad){return (ad._bundle_items||[ad]).map(c=>this.tierNames[c.tier]+' '+this.schedule(c)).join('；')},
  bundleStatus(ad){const states=[...new Set((ad._bundle_items||[ad]).map(c=>c.status))];return states.length===1?states[0]:'部分档位状态不同'},
  playbackDraft:null,playbackRevision:0,playbackDirty:false,playbackMarkup:'',cardMarkup:'',cardPreviewMarkup:'',
  get sponsorChoices(){return (this.data.advertisers||[]).filter(a=>a.enabled&&!a._archived)},
  playbackLoaded(result){if(!this.playbackDirty){this.playbackDraft={pause_max_minutes:0,...copy(result.rules)};this.playbackRevision=result.revision}},
  markPlayback(){this.playbackDirty=true},
  async savePlayback(){const rules=copy(this.playbackDraft);for(const k of ['midroll_percent','midroll_delay_seconds','text_item_seconds','pause_max_minutes'])rules[k]=Number(rules[k]);const r=await this.managedAction({kind:'ad_control',action:'ad_playback',rules,_revision:this.playbackRevision},'播放规则已保存',false);if(r){this.playbackDirty=false;this.playbackLoaded(r)}},
  toggleSponsor(id){this.playbackDirty=true;const ids=this.playbackDraft.sponsor_ids;this.playbackDraft.sponsor_ids=ids.includes(id)?ids.filter(x=>x!==id):[...ids,id]},
  moveSponsor(id,d){const ids=[...this.playbackDraft.sponsor_ids],i=ids.indexOf(id),j=i+d;if(i<0||j<0||j>=ids.length)return;[ids[i],ids[j]]=[ids[j],ids[i]];this.playbackDraft.sponsor_ids=ids;this.playbackDirty=true},
  rotationFor(slot){const r=this.playbackDraft;if(!r)return[];const merchants=new Map(this.sponsorChoices.map(a=>[a.name,a.id]));return (this.data.campaigns||[]).filter(c=>c.tier===1&&c.status==='投放中'&&c.rotation_enabled!==false&&c.placements.includes(slot)&&merchants.has(c.merchant)&&(!r.sponsor_ids.length||r.sponsor_ids.includes(merchants.get(c.merchant))))
    .sort((a,b)=>{const at=c=>r.sponsor_ids.length?r.sponsor_ids.indexOf(merchants.get(c.merchant)):0;return at(a)-at(b)||(a.rotation_order??100)-(b.rotation_order??100)||b.priority-a.priority||a.id.localeCompare(b.id)})
    .filter((c,i,all)=>all.findIndex(x=>x.merchant===c.merchant)===i)},
  rotationSeconds(slot){return this.rotationFor(slot).reduce((n,c)=>n+c.duration,0)},
  adFormatChanged(){if(this.edit.format==='图文'){this.edit.creative_card={...sponsorCardDefaults(),...(this.edit.creative_card||{})};if(!this.edit.creative_card.title)this.edit.creative_card.title=this.edit.merchant}},
  async uploadCard(file,field){if(!file||this.uploading)return;this.uploading=true;const ident=this.edit.id;try{const res=await fetch('/api/admin/uploads?kind=ads',{method:'POST',credentials:'same-origin',headers:{'X-CSRF-Token':this.user.csrf,'X-Filename':encodeURIComponent(file.name)},body:file});const r=await res.json();if(!res.ok)throw Error(r.error||'上传失败');if(this.modal!=='campaign'||this.edit.id!==ident)return;if(r.previous_revision===this.edit._revision)this.edit._revision=r.revision;this.edit.creative_card[field]=r.path;if(field==='qr')this.edit.creative_card.qr_link='';this.notify('素材已上传，可保存广告')}catch(e){this.notify(e.message)}finally{this.uploading=false}},
  get editingCard(){return this.edit.creative_card||sponsorCardDefaults()},
  get shownSponsorCard(){return this.modal==='campaign'?this.editingCard:this.previewCard},
  get previewCard(){return this.preview?.creative_card||sponsorCardDefaults()},
  duplicatePackage(p){this.editPackage({...copy(p),id:'pkg-'+newUuid().slice(0,12),name:p.name+' 副本',references:0,list_price:this.administrator?p.list_price:0})},
  normalizePackage(p){return {...p,tiers:p.tiers||[Number(p.tier)],placements_by_tier:p.placements_by_tier||{},targets:p.targets||{'1':0,'2':0,'3':0},default_ad_seconds:p.default_ad_seconds||15}},
  togglePackageTier(t){const p=this.packageDraft;if(t===p.tier)return;p.tiers=p.tiers.includes(t)?p.tiers.filter(x=>x!==t):[...p.tiers,t].sort();p.placements_by_tier[String(t)]=p.placements_by_tier[String(t)]||['all']},
  get orderContract(){const d=this.orderDraft;return d?.package_snapshot&&!d.reapply_template?d.package_snapshot:this.orderPackage},
  reapplyPackage(){this.orderDraft.reapply_template=true;this.orderPackageChanged()},
}}
