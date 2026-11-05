/* TV01: compatibility pre-check and compatible copies. */
function compatUi(){return {
  compatVerdict:'',compatSelected:[],compatTimer:null,
  verdictLabels:{pending:'未预检',ok:'可直接播放',remux:'需换封装',audio:'需转音轨',video:'需转码视频',error:'无法读取'},
  jobLabels:{queued:'排队中',running:'转换中',paused:'已暂停',done:'已完成',failed:'失败',cancelled:'已取消'},
  get compatRows(){return (this.opsData.rows||[]).filter(r=>!this.compatVerdict||(r.verdict||'pending')===this.compatVerdict)},
  compatLoaded(result){clearTimeout(this.compatTimer);if(this.opsTab==='compat'&&(result.rows||[]).some(r=>['queued','running'].includes(r.job_status)))this.compatTimer=setTimeout(()=>{if(this.opsTab==='compat'&&!this.modal)this.loadOperations()},3000)},
  verdictClass(v){return {ok:'online',remux:'warn',audio:'warn',video:'offline',error:'offline'}[v]||'neutral'},
  async probeAll(){const ids=[...new Set(this.compatRows.filter(r=>!r.verdict).map(r=>r.media_id))];if(!ids.length)return this.notify('当前列表没有未预检的文件');await this.managedAction({kind:'compat',action:'compat_probe',ids:ids.slice(0,500),_revision:this.data.revision},'预检完成')},
  async probeRows(rows){const ids=[...new Set(rows.map(r=>r.media_id))];await this.managedAction({kind:'compat',action:'compat_probe',ids,_revision:this.data.revision},'预检完成')},
  async enqueueRows(ids){await this.managedAction({kind:'compat',action:'compat_enqueue',ids,_revision:this.data.revision},'已加入兼容版队列');this.compatSelected=[]},
  async compatOp(row,op){await this.managedAction({kind:'compat',action:'compat_control',id:row.id,op,_revision:this.data.revision},'已更新',false)},
  sizeText(n){return n?(n>1073741824?(n/1073741824).toFixed(2)+' GB':(n/1048576).toFixed(1)+' MB'):''}
}}
