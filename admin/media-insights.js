/* AD13: existing TV events, explicit selection and a server-bound cleanup preview. */
function mediaInsightsUi(){return {
  cleanupMarkup:'',mediaStats:null,mediaStatsMap:{},mediaStatsLoading:false,mediaStatsError:'',mediaStatsSerial:0,mediaColdOnly:false,
  cleanupPlan:null,cleanupConfirmed:false,cleanupResult:null,
  mediaInsight(m){return this.mediaStatsMap[m.id]},
  mediaPlays(m){const r=this.mediaInsight(m);return r?r.plays_30:'—'},
  async loadMediaInsights(force=false){
    if(!this.user||this.view!=='media'||this.mediaStatsLoading)return;
    if(!force&&this.mediaStats&&this.mediaStats.revision===this.data.revision)return;
    const serial=++this.mediaStatsSerial,csrf=this.user.csrf;this.mediaStatsLoading=true;this.mediaStatsError='';
    try{const result=await this.api('/media-insights');if(serial!==this.mediaStatsSerial||!this.user||this.user.csrf!==csrf)return;this.mediaStats=result;this.mediaStatsMap=Object.fromEntries(result.rows.map(r=>[r.id,r]));}
    catch(e){this.mediaStatsError=e.message}finally{if(serial===this.mediaStatsSerial)this.mediaStatsLoading=false}
  },
  async showColdMedia(){await this.loadMediaInsights(true);if(this.mediaStatsError)return;this.mediaColdOnly=true;this.mediaSort='cold';this.mediaStatus='active';this.mediaView='list';this.selected=[];this.mediaPage=0},
  leaveColdMedia(){this.mediaColdOnly=false;this.selected=[];this.mediaPage=0},
  async previewMediaCleanup(){
    if(this.busy||!this.administrator||!this.selected.length)return;
    if(this.selected.length>50){this.notify('冷门清理每次最多选择50部作品');return}
    this.busy=true;this.cleanupResult=null;this.cleanupConfirmed=false;
    try{this.cleanupPlan=await this.api('/media-cleanup/preview',{ids:[...this.selected],revision:this.data.revision});this.modal='media-cleanup'}
    catch(e){this.notify(e.message);this.error=e.message}finally{this.busy=false}
  },
  async executeMediaCleanup(){
    if(this.busy||!this.administrator||!this.cleanupConfirmed||!this.cleanupPlan?.rows.length)return;
    this.busy=true;
    try{this.cleanupResult=await this.api('/media-cleanup/execute',{plan_id:this.cleanupPlan.id,confirm:true,revision:this.cleanupPlan.revision});this.cleanupConfirmed=false;this.selected=[];await this.reload(false);await this.loadMediaInsights(true);this.notify(this.cleanupResult.status==='completed'?'所选原视频已清理':'部分文件未清理，请查看结果')}
    catch(e){this.notify(e.message);this.error=e.message}finally{this.busy=false}
  },
  cleanupStatus(value){return {running:'处理中或曾中断，请核对文件',partial:'部分完成，请查看失败项',completed:'已完成'}[value]||value},
}}
