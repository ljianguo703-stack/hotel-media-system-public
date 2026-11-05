/* A01 advertising packages and insertion orders. Amounts are typed text; the server stores exact minor units. */
function ordersUi(){return {
  orderFilters:{state:'',advertiser:'',payment:'',expiring:false,archived:false},orderDraft:null,packageDraft:null,orderView:null,currencyDraft:'',
  paymentStates:['未收款','部分收款','已收款','已退款'],orderStates:['未开始','执行中','已到期','已停用'],
  tierPlacements:{1:[['home','首页'],['preroll','片前'],['midroll','中插'],['pause','暂停'],['postroll','片尾'],['channel','频道节目间']],2:[['all','全部画面'],['home','首页'],['playback','播放中']],3:[['all','全部画面'],['home','首页'],['playback','播放中']]},
  get billingCurrency(){return this.data.billing?.currency||''},
  get packageRows(){const q=this.opsQuery.trim().toLowerCase();return (this.data.ad_packages||[]).filter(p=>!p._archived&&(!this.opsStatus||(this.opsStatus==='enabled')===!!p.enabled)&&(!q||[p.name,p.notes].join(' ').toLowerCase().includes(q)))},
  get usablePackages(){return (this.data.ad_packages||[]).filter(p=>!p._archived&&(p.enabled||p.id===this.orderDraft?.package_id))},
  get usableAdvertisers(){return (this.data.advertisers||[]).filter(a=>!a._archived&&(a.enabled||a.id===this.orderDraft?.advertiser_id))},
  get orderPackage(){return (this.data.ad_packages||[]).find(p=>p.id===this.orderDraft?.package_id)||null},
  get orderCampaignChoices(){
    const p=this.orderContract||this.orderPackage,a=(this.data.advertisers||[]).find(a=>a.id===this.orderDraft?.advertiser_id),rows=(this.data.campaigns||[]).filter(c=>(p?.tiers||[p?.tier]).includes(c.tier)&&(c.merchant===a?.name||this.orderDraft.campaign_ids.includes(c.id))&&(c.status!=='已归档'||this.orderDraft.campaign_ids.includes(c.id))&&(c._bundle_active!==false||this.orderDraft.campaign_ids.includes(c.id))),groups=new Map();
    for(const c of rows){const key=c._bundle_id||c.id;if(!groups.has(key))groups.set(key,[]);groups.get(key).push(c)}
    return [...groups.values()].map(items=>({...items[0],name:items[0]._bundle_name?items[0]._bundle_name+'（'+items.map(c=>this.tierName(c.tier)).join(' + ')+'）':items[0].name,_bundle_choice_ids:items.map(c=>c.id)}));
  },
  orderQueryParams(){const f=this.orderFilters,p={};if(f.state)p.state=f.state;if(f.advertiser)p.advertiser=f.advertiser;if(f.payment)p.payment=f.payment;if(f.expiring)p.expiring='1';if(f.archived)p.archived='1';return p},
  moneyLabel(text){return this.billingCurrency?`${text} ${this.billingCurrency}`:text},
  minorText(value){const v=Number(value||0);return Math.floor(v/100)+'.'+String(v%100).padStart(2,'0')},
  tierName(tier){return {1:'一档 · 全屏',2:'二档 · 全天文字',3:'三档 · 分时文字'}[tier]||'未知档位'},
  placementName(tier,id){return (this.tierPlacements[tier]||[]).find(p=>p[0]===id)?.[1]||id},
  progressText(p){return p.target?`${p.actual} / ${p.target} ${p.unit}`:`${p.actual} ${p.unit}（未设目标）`},
  dayText(days){return days<0?`已过 ${-days} 天`:days===0?'今天到期':`剩 ${days} 天`},
  editCurrency(){this.currencyDraft=this.billingCurrency;this.modal='currency'},
  async saveCurrency(){await this.managedAction({kind:'order',action:'currency_save',currency:this.currencyDraft.trim().toUpperCase(),_revision:this.data.revision},'广告币种已保存')},
  editPackage(item=null){const p=item?copy(item):{id:'pkg-'+newUuid().slice(0,12),name:'',tier:3,placements:['all'],default_days:30,target:0,list_price:0,notes:'',enabled:true};
    this.packageDraft={...(typeof this.normalizePackage==='function'?this.normalizePackage(p):p),list_price:p.list_price?this.minorText(p.list_price):'',_revision:this.data.revision};this.modal='package-edit'},
  packageTier(tier){this.packageDraft.tier=Number(tier);this.packageDraft.placements=[this.tierPlacements[this.packageDraft.tier][0][0]];this.packageDraft.tiers=[this.packageDraft.tier];this.packageDraft.placements_by_tier={}},
  async savePackage(){const {_revision,references,...item}=this.packageDraft;await this.managedAction({kind:'package',action:'save',item:{...item,tier:Number(item.tier),default_days:Number(item.default_days),target:Number(item.target),default_ad_seconds:Number(item.default_ad_seconds||15),targets:Object.fromEntries(Object.entries(item.targets||{}).map(([k,v])=>[k,Number(v)]))},_revision},'套餐已保存')},
  editOrder(row=null){const today=new Date(),iso=d=>d.toISOString().slice(0,10),pkg=(this.data.ad_packages||[]).find(p=>p.enabled&&!p._archived);
    const base=row?copy(row):{id:'ord-'+newUuid().slice(0,12),advertiser_id:'',package_id:pkg?.id||'',campaign_ids:[],group:'全部客房',start:iso(today),end:iso(new Date(today.getTime()+((pkg?.default_days||30)-1)*86400000)),target:pkg?.target||0,quoted_price:0,received_amount:0,payment_status:'未收款',payment_note:'',remind_days:7,notes:'',enabled:true};
    const keep=['id','advertiser_id','package_id','campaign_ids','group','start','end','target','payment_status','payment_note','remind_days','notes','enabled','code','package_snapshot','targets'];
    this.orderDraft={targets:copy(base.targets||pkg?.targets||{'1':0,'2':0,'3':0}),reapply_template:false,...Object.fromEntries(keep.filter(k=>k in base).map(k=>[k,base[k]])),quoted_price:base.quoted_price?this.minorText(base.quoted_price):'',received_amount:base.received_amount?this.minorText(base.received_amount):'',_revision:this.data.revision};this.modal='order-edit'},
  orderPackageChanged(){const p=this.orderPackage;if(!p)return;this.orderDraft.package_snapshot=null;this.orderDraft.reapply_template=true;this.orderDraft.target=p.target;this.orderDraft.targets=copy(p.targets||{'1':0,'2':0,'3':0});this.orderDraft.campaign_ids=this.orderDraft.campaign_ids.filter(id=>this.orderCampaignChoices.some(c=>(c._bundle_choice_ids||[c.id]).includes(id)));
    if(this.orderDraft.start){const s=new Date(this.orderDraft.start+'T00:00:00Z');this.orderDraft.end=new Date(s.getTime()+(p.default_days-1)*86400000).toISOString().slice(0,10)}},
  toggleOrderCampaign(id){const list=this.orderDraft.campaign_ids,choice=this.orderCampaignChoices.find(c=>c.id===id),ids=choice?._bundle_choice_ids||[id];this.orderDraft.campaign_ids=ids.every(x=>list.includes(x))?list.filter(x=>!ids.includes(x)):[...new Set([...list,...ids])]},
  async saveOrder(){const {_revision,code,...item}=this.orderDraft;await this.managedAction({kind:'order',action:'save',item:{...item,target:Number(item.target),remind_days:Number(item.remind_days),targets:Object.fromEntries(Object.entries(item.targets||{}).map(([k,v])=>[k,Number(v)]))},_revision},'投放单已保存')},
  reportDraft:{advertiser:'',from:'',until:''},controlDraft:null,controlFilter:'',
  get controlRows(){return (this.opsData.devices||[]).filter(d=>!this.controlFilter||d.state===this.controlFilter)},
  controlLoaded(result){if(!this.controlDraft||!this.controlDirty)this.controlDraft={...result.control};},
  controlDirty:false,
  async saveControlLimits(){const c=this.controlDraft;await this.managedAction({kind:'ad_control',action:'ad_control',max_fullscreen_per_hour:Number(c.max_fullscreen_per_hour),max_fullscreen_seconds_per_hour:Number(c.max_fullscreen_seconds_per_hour),min_fullscreen_gap_seconds:Number(c.min_fullscreen_gap_seconds),text_start:c.text_start||'',text_end:c.text_end||'',_revision:this.data.revision},'统一上限已保存');this.controlDirty=false},
  askStop(stop){this.pendingConfirm={message:stop?'确认一键停止全部商业广告？标记为“酒店自有”的广告继续播放；离线电视要等重新联网拉取后才会停。':'确认恢复商业广告投放？',run:()=>this.managedAction({kind:'ad_control',action:'ad_control',stopped:stop,_revision:this.data.revision},stop?'已下发停播，请查看各电视执行状态':'已恢复商业广告')}},
  openMerchantReport(query){window.open('merchant-report.html?'+new URLSearchParams(query),'_blank','noopener')},
  chooseReport(){const end=new Date(),start=new Date(end.getTime()-6*86400000),iso=d=>d.toISOString().slice(0,10);this.reportDraft={advertiser:this.orderFilters.advertiser||'',from:iso(start),until:iso(end)};this.modal='report-choose'},
  submitReport(){const d=this.reportDraft;if(!d.advertiser)return this.notify('请选择广告主');this.openMerchantReport({advertiser:d.advertiser,from:d.from,until:d.until});this.modal=null},
  openOrder(row){this.orderView=copy(row);this.modal='order-detail'},
  async setOrderFilter(){this.opsPage=0;try{await this.loadOperations()}catch(e){this.notify(e.message)}}
}}
