/* TV02: virtual channels on a shared timeline with ad breaks and a generated guide. */
function channelsUi(){return {
  channelDraft:null,channelSearch:'',channelTarget:-1,channelView:null,
  modeLabels:{loop:'顺序循环',shuffle:'随机（顺序保存）',slots:'按时段编排'},
  get channelRows(){return (this.opsData.channels||[])},
  get channelProgrammeChoices(){const c=this.channelDraft;if(!c)return [];return [...new Set(c.mode==='slots'?c.slots.flatMap(s=>s.items):c.items)].map(id=>({id,title:this.mediaTitle(id)}))},
  get channelIdentChoices(){return (this.data.assets||[]).filter(a=>a.enabled&&!a._archived&&a.format==='视频')},
  get channelAdChoices(){return (this.data.campaigns||[]).filter(c=>c.tier===1&&c.status!=='已归档'&&(c.placements||[]).includes('channel'))},
  get channelCandidates(){const q=this.channelSearch.trim().toLowerCase();return (this.data.media||[]).filter(m=>!m._archived&&(!q||(m.title||'').toLowerCase().includes(q))).slice(0,30)},
  channelTime(ms){return new Date(ms).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false})},
  editChannel(row=null){const used=new Set(this.channelRows.map(c=>c.number));let n=1;while(used.has(n))n++;
    const c=row?copy(row):{id:'channel-'+newUuid().slice(0,12),number:n,name:'',logo:'',mode:'loop',items:[],slots:[],breaks:{enabled:true,seconds:30,every:1},enabled:true};
    this.channelDraft={id:c.id,number:c.number,name:c.name,logo:c.logo||'',mode:c.mode,items:[...c.items],slots:(c.slots||[]).map(s=>({...s,items:[...s.items]})),breaks:{...c.breaks},inserts:copy(c.inserts||[]),enabled:c.enabled,reshuffle:false,_revision:this.data.revision};
    this.channelTarget=-1;this.channelSearch='';this.modal='channel-edit'},
  channelList(){return this.channelTarget<0?this.channelDraft.items:this.channelDraft.slots[this.channelTarget].items},
  channelAdd(id){this.channelList().push(id)},
  channelRemove(list,i){list.splice(i,1);this.channelPruneInserts()},
  channelPruneInserts(){const ids=new Set(this.channelProgrammeChoices.map(m=>m.id));this.channelDraft.inserts=this.channelDraft.inserts.filter(r=>ids.has(r.media_id))},
  addChannelInsert(){if(!this.channelProgrammeChoices.length)return;this.channelDraft.inserts.push({id:'insert-'+newUuid().slice(0,12),media_id:this.channelProgrammeChoices[0].id,position:'at',at_seconds:60,kind:'ad',seconds:30,campaign_id:'',asset_id:''})},
  channelEntryLabel(e){return e.title+(e.kind==='program'&&e.offset_ms>0?' · 从第 '+Math.floor(e.offset_ms/1000)+' 秒续播':'')},
  channelMove(list,i,d){const j=i+d;if(j<0||j>=list.length)return;[list[i],list[j]]=[list[j],list[i]]},
  addSlot(){this.channelDraft.slots.push({start:'08:00',end:'12:00',items:[]});this.channelTarget=this.channelDraft.slots.length-1},
  async saveChannel(){const {_revision,...item}=this.channelDraft;item.number=Number(item.number);item.breaks={enabled:!!item.breaks.enabled,seconds:Number(item.breaks.seconds),every:Number(item.breaks.every)};if(item.mode!=='slots')item.slots=[];else item.items=[];
    item.inserts=item.inserts.map(r=>({...r,at_seconds:Number(r.at_seconds),seconds:Number(r.seconds)}));
    await this.managedAction({kind:'channel',action:'save',item,_revision},'频道已保存（正在计算节目时长时可能需要几秒）')},
  openChannel(row){this.channelView=copy(row);this.modal='channel-detail'},
  async exportXmltv(){try{const r=await this.api('/operations?section=xmltv');const blob=new Blob([r.xml],{type:'application/xml'}),a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='hotel-tv-guide.xml';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000)}catch(e){this.notify(e.message)}}
}}
