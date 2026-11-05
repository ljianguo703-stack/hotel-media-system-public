const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
function create(crypto=require('node:crypto').webcrypto){
 const ctx=vm.createContext({URL,URLSearchParams,Intl,Blob,crypto,location:{href:'http://localhost:18893/?view=media',hostname:'localhost',protocol:'http:',origin:'http://localhost:18893'},history:{replaceState(){}},window:{scrollTo(){}},document:{addEventListener(){}},setTimeout:()=>0,clearTimeout(){}});
 for(const name of ['app.js','orders.js','home.js','compat.js','channels.js','downloads.js','downloads_ui.js','management.js'])vm.runInContext(fs.readFileSync(path.join(__dirname,'..',name),'utf8'),ctx);
 const app=vm.runInContext('cinemaAdmin()',ctx);app.view='media';app.user={name:'admin',role:'admin',csrf:'test'};return app;
}
function state(revision){return {revision,settings:{hotel:'test'},media:[],devices:[],campaigns:[],sources:[],users:[],advertisers:[],assets:[],device_groups:[]}}
function deferred(){let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve}}

test('late state responses cannot replace a newer server revision or leave refresh busy',async()=>{
 const app=create(),old=deferred(),fresh=deferred();let count=0;app.api=()=>++count===1?old.promise:fresh.promise;
 const first=app.reload(true),second=app.reload(false);fresh.resolve(state(2));await second;old.resolve(state(1));await first;
 assert.equal(app.data.revision,2);assert.equal(app.busy,false);
});
test('switching management tabs discards a late result instead of replacing the sync form',async()=>{
 const app=create(),slow=deferred();app.view='operations';app.opsTab='tasks';app.api=()=>app.opsTab==='tasks'?slow.promise:Promise.resolve({url:'http://127.0.0.1:8096',interval_seconds:60,paused:true});
 const pending=app.loadOperations();app.opsTab='sync';await app.loadOperations();slow.resolve({rows:[{id:'old-task'}],total:1});await pending;
 assert.equal(app.syncDraft.interval_seconds,60);assert.equal(app.syncDraft.paused,true);assert.equal('rows' in app.opsData,false);
});
test('confirmation sends the reviewed revision even after polling advances state',async()=>{
 const app=create();let sent;app.data=state(7);app.api=async(p,b)=>{if(b){sent=b;return {ok:true}}return state(8)};
 app.askManaged({kind:'system',action:'trash_clear',mode:'expired'},'review');app.data.revision=8;await app.confirmPending();
 assert.equal(sent.revision,7);assert.equal(sent.confirm,true);assert.equal(app.pendingConfirm,null);
});
test('one-time credentials are excluded from result history and cleared on close/logout',async()=>{
 const app=create();app.action=async()=>({ok:['room-1'],failed:[],credentials:[{id:'room-1',token:'ephemeral-test-token'}]});
 await app.managedAction({kind:'device',action:'devices_import'});assert.equal(app.modal,'credentials');assert.equal(app.credentials.length,1);assert.equal(JSON.stringify(app.opsOutcome).includes('ephemeral-test-token'),false);
 app.close();assert.equal(app.credentials.length,0);app.credentials=[{token:'second'}];app.syncDraft.token='connection-test';app.api=async()=>({});await app.logout();assert.equal(app.credentials.length,0);assert.equal(app.syncDraft.token,'');assert.equal(app.user,null);
});
test('a missing-file warning is shown after a successful media save',async()=>{
 const app=create();app.edit={id:'series-1'};app.action=async()=>({ok:true,warnings:['第 2 集：文件不可读']});await app.saveMedia();assert.equal(app.modal,'media-warnings');assert.equal(app.mediaWarnings.length,1);
});
test('device setup export uses the actual server API port',()=>{
 const app=create();app.data.endpoints={tv_port:18892};assert.equal(app.tvUrl,'http://localhost:18892');
});
test('an older report query cannot overwrite a newer filter result',async()=>{
 const app=create(),old=deferred(),fresh=deferred();let n=0;app.api=()=>++n===1?old.promise:fresh.promise;
 const first=app.updateReports(),second=app.updateReports();fresh.resolve({rows:[{id:'new'}]});await second;old.resolve({rows:[{id:'old'}]});await first;assert.equal(app.report.rows[0].id,'new');
});
test('orders tab sends its filters and keeps money as exact text',async()=>{
 const app=create();let asked='';app.view='operations';app.opsTab='orders';app.data={...state(3),billing:{currency:'CNY'},ad_packages:[{id:'p1',name:'月包',tier:3,target:60,default_days:30,enabled:true,placements:['all']}]};
 app.api=async p=>{asked=p;return {rows:[],total:0,summary:{expiring:0}}};
 app.orderFilters={state:'执行中',advertiser:'adv-1',payment:'',expiring:true,archived:false};await app.loadOperations();
 const q=new URLSearchParams(asked.split('?')[1]);assert.equal(q.get('section'),'orders');assert.equal(q.get('state'),'执行中');assert.equal(q.get('advertiser'),'adv-1');assert.equal(q.get('expiring'),'1');assert.equal(q.has('payment'),false);
 assert.equal(app.minorText(30),'0.30');assert.equal(app.minorText(120005),'1200.05');assert.equal(app.moneyLabel('1.00'),'1.00 CNY');
 app.editOrder();assert.equal(app.orderDraft.package_id,'p1');assert.equal(app.orderDraft.target,60);assert.equal(app.orderDraft.quoted_price,'');
 let sent;app.managedAction=async payload=>{sent=payload;return {}};app.orderDraft.quoted_price='0.30';await app.saveOrder();
 assert.equal(sent.item.quoted_price,'0.30');assert.equal('code' in sent.item,false);assert.equal(sent.kind,'order');
});
test('packages are read from state without an extra request and placements follow the tier',async()=>{
 const app=create();let calls=0;app.view='operations';app.opsTab='packages';app.api=async()=>{calls++;return {}};await app.loadOperations();assert.equal(calls,0);
 app.data={...state(1),billing:{currency:''},ad_packages:[]};app.editPackage();app.packageTier('1');assert.equal(JSON.stringify(app.packageDraft.placements),'["home"]');assert.equal(app.packageDraft.list_price,'');
});
test('channel insert editing clones rules and submits numeric offsets without losing the reviewed revision',async()=>{
 const app=create();app.data={...state(4),media:[{id:'film',title:'节目'}]};
 const row={id:'c',number:1,name:'频道',mode:'loop',items:['film'],slots:[],breaks:{enabled:false,seconds:30,every:1},enabled:true,inserts:[{id:'r',media_id:'film',position:'at',at_seconds:120,kind:'ad',seconds:30,campaign_id:''}]};
 app.editChannel(row);app.channelDraft.inserts[0].at_seconds='60';assert.equal(row.inserts[0].at_seconds,120);
 let sent;app.managedAction=async p=>{sent=p};await app.saveChannel();assert.equal(sent._revision,4);assert.equal(sent.item.inserts[0].at_seconds,60);
 app.channelRemove(app.channelDraft.items,0);assert.equal(app.channelDraft.inserts.length,0);
});

// Real hotel deployment uses http://LAN-IP, where randomUUID is deliberately unavailable.
test('all create dialogs and channel inserts work on plain LAN HTTP without randomUUID',()=>{
 const crypto={getRandomValues:require('node:crypto').webcrypto.getRandomValues.bind(require('node:crypto').webcrypto)};
 const app=create(crypto);app.data={...state(6),categories:[],billing:{currency:''},ad_packages:[],media:[{id:'film',title:'test',kind:'movie',published:true,available:true}]};app.opsData={channels:[]};
 const ids=[];
 app.editCategory();ids.push(app.categoryEdit.id);
 app.editEntity('advertiser');ids.push(app.edit.id);
 app.editEntity('group');ids.push(app.edit.id);
 app.editPackage();ids.push(app.packageDraft.id);
 app.editOrder();ids.push(app.orderDraft.id);
 app.editGuide();ids.push(app.guideDraft.id);
 app.editTopic();ids.push(app.topicDraft.id);
 app.editChannel();ids.push(app.channelDraft.id);app.channelDraft.items=['film'];app.addChannelInsert();ids.push(app.channelDraft.inserts[0].id);
 assert.equal(ids.length,9);assert.equal(new Set(ids).size,9);for(const id of ids)assert.match(id,/^[a-z]+-[a-f0-9-]+$/);
 assert.equal(app.modal,'channel-edit');assert.equal(app.channelDraft.inserts[0].media_id,'film');
});
test('smart download page sends one sentence, shows real progress and never resends stored secrets',async()=>{
 const app=create();const sent=[];app.data={...state(1)};app.user={role:'admin',csrf:'x',name:'admin'};
 app.action=async(path,body)=>{sent.push(body);return body.action==='download_settings'?{settings:body.settings,secrets_present:{}}:{id:'dl-1'}};app.api=async()=>({rows:[],counts:{},labels:{},notices:[],ready:{engines:{}},disk:{}});
 app.dlText='帮我下载电影《片名》，要4K';await app.dlSend();
 assert.equal(sent[0].action,'download_create');assert.equal(sent[0].request,'帮我下载电影《片名》，要4K');assert.equal('manual' in sent[0],false);assert.equal(app.dlText,'帮我下载电影《片名》，要4K');
 assert.equal(app.dlPercent({progress:{downloaded:50,total:200}}),25);assert.equal(app.dlPercent({progress:{downloaded:50,total:null}}),null);assert.equal(app.dlBytes(null),'大小未知');
 app.dlSettings={sources:[{id:'s1'}],engines:{}};app.dlSecrets={qbittorrent:{password:''},aria2:{secret:'abc'},sources:{s1:''}};
 await app.dlSaveSettings();const body=sent[1];
 assert.equal(JSON.stringify(body.secrets),'{"aria2":{"secret":"abc"}}');
});

test('public-source shortcuts need no key, avoid duplicates and use the correct endpoint',()=>{
 const app=create();app.dlSettings={sources:[]};app.notify=()=>{};
 for(const kind of ['knaben','apibay','nyaa'])app.dlAddSource(kind);
 assert.equal(app.dlSettings.sources.length,3);assert.equal(app.dlSettings.sources[0].url,'https://api.knaben.org/v1');
 app.dlSettings.sources[0].enabled=false;app.dlAddSource('knaben');
 assert.equal(app.dlSettings.sources.length,3);assert.equal(app.dlSettings.sources[0].enabled,true);
 const s=app.dlSettings.sources[1];app.dlHealth['source:'+s.id]={status:'ok'};s.type='nyaa';app.dlSourceChanged(s);
 assert.equal(s.url,'https://nyaa.si');assert.equal(app.dlHealth['source:'+s.id],undefined);
});

// DL10 workflow tests: search stays editable, selection starts a separate download, locations preserve user choice.
test('simple download defaults to files only and keeps the search available after selection',async()=>{
 const app=create();const sent=[];app.data=state(10);app.dlDownloadDir='/mnt/archive';app.dlText='Ubuntu 24.04';
 app.api=async()=>({rows:[{id:'search-1',intent:'search',candidates:[{key:'one',protocol:'torrent'}]}],ready:{engines:{}},disk:{},default_dir:'/old'});
 app.action=async(p,b)=>{sent.push(b);return {id:sent.length===1?'search-1':'download-1'}};
 await app.dlSend();assert.equal(sent[0].flow_version,2);assert.equal(sent[0].organize,false);assert.equal(sent[0].publish,false);assert.equal(sent[0].auto,false);assert.equal(app.dlText,'Ubuntu 24.04');
 await app.dlDownload({key:'one'});assert.equal(sent[1].op,'download_candidate');assert.equal(sent[1].download_dir,'/mnt/archive');assert.equal(app.dlSearchId,'search-1');assert.equal(app.dlTab,'search');
});
test('a direct URL needs no film metadata and import is an explicit per-task choice',async()=>{
 const app=create();let sent;app.action=async(p,b)=>{sent=b;return {id:'file-1'}};app.api=async()=>({rows:[],ready:{engines:{}},disk:{}});
 app.dlText='https://example.org/archive.zip';await app.dlSend();assert.equal(sent.request,'');assert.equal(sent.manual.uri,'https://example.org/archive.zip');assert.equal(sent.manual.kind,'');assert.equal(sent.organize,false);assert.equal(app.dlTab,'tasks');
 app.dlText='magnet:?xt=urn:btih:'+('a'.repeat(40));app.dlOrganize=true;app.dlPublish=true;app.dlImportKind='series';app.dlImportTitle='Owner title';await app.dlSend();
 assert.equal(sent.organize,true);assert.equal(sent.publish,true);assert.equal(sent.overrides.kind,'series');assert.equal(sent.overrides.title,'Owner title');
});
test('folder browsing ignores late responses and selecting a folder does not save settings implicitly',async()=>{
 const app=create(),old=deferred(),fresh=deferred();let count=0,calls=0;app.api=()=>++count===1?old.promise:fresh.promise;app.action=async()=>{calls++};
 app.dlFolder.open=true;const a=app.dlBrowse('/mnt/old'),b=app.dlBrowse('/mnt/new');fresh.resolve({path:'/mnt/new',root:'disk2',folders:[],writable:true});await b;old.resolve({path:'/mnt/old',root:'disk1',folders:[],writable:true});await a;
 assert.equal(app.dlFolder.path,'/mnt/new');app.dlFolder.target='settings';app.dlSettings={save_dir:'/before'};app.dlChooseFolder();assert.equal(app.dlSettings.save_dir,'/mnt/new');assert.equal(calls,0);
});

// DL11: poll updates preserve stable result instances and pagination is local.
test('download poll keeps unchanged result objects and edited form values',async()=>{
 const app=create(),search={id:'s',intent:'search',created_by:'admin',request:'片名',state:'awaiting_choice',parsed:{title:'片名'},candidates:[{key:'a',title:'片名'}]},job={id:'j',intent:'download',state:'downloading',progress:{downloaded:1}};
 app.dl.rows=[search,job];app.dlSearchId='s';app.dlLoaded=true;app.dlText='正在编辑';app.dlQuality='2160p';app.dlDownloadDir='/E/下载';const root=app.dl;
 app.api=async()=>({rows:JSON.parse(JSON.stringify([search,{...job,progress:{downloaded:2}}])),disk:{free_gb:10}});await app.loadDownloads();
 assert.equal(app.dl,root);assert.equal(app.dlResult,search);assert.equal(app.dlResult.candidates,search.candidates);assert.equal(app.dl.rows[1].progress.downloaded,2);assert.equal(app.dlText,'正在编辑');assert.equal(app.dlQuality,'2160p');assert.equal(app.dlDownloadDir,'/E/下载');
});
test('search pagination retains every matching and similar candidate',()=>{
 const app=create();app.dlSearchId='s';app.dl.rows=[{id:'s',intent:'search',candidates:Array.from({length:279},(_,i)=>({key:String(i),match:i%2?'match':'similar'}))}];
 assert.equal(app.dlResultPages,14);assert.equal(app.dlPagedResults.length,20);app.dlSetPage(13);assert.equal(app.dlPagedResults.length,19);assert.equal(app.dlPagedResults[0].key,'260');app.dlSetPage(999);assert.equal(app.dlPage,13);
 app.dlMatch='similar';app.dlPage=0;assert.equal(app.dlResultRows.length,140);assert.equal(app.dlResultPages,7);
});

// DL12: submitted jobs must describe actual transfer phases, not just downloader acceptance.
test('download cards distinguish metadata, queue and zero-peer stalls from actual transfer',()=>{
 const app=create();app.dl.labels={paused:'已暂停',downloaded:'已下载'};
 const task={state:'downloading',engine:'qbittorrent',progress:{engine_state:'metadata',speed:0,seeds:0,peers:0,waiting_seconds:190}};
 assert.equal(app.dlTaskLabel(task),'等待元数据');assert.equal(app.dlTaskClass(task),'warn');assert.equal(app.dlPeerText(task),'已连接 0 个节点 · 0 个做种者 · 已等待 3 分钟');
 task.progress.engine_state='queued';assert.equal(app.dlTaskLabel(task),'下载器排队');task.progress.engine_state='stalled';assert.equal(app.dlTaskLabel(task),'等待可用节点');
 task.progress={engine_state:'downloading',speed:1024,seeds:3,peers:5};assert.equal(app.dlTaskLabel(task),'下载中');assert.equal(app.dlPeerText(task),'已连接 5 个节点 · 3 个做种者');
 task.state='paused';assert.equal(app.dlTaskLabel(task),'已暂停');task.engine='aria2';assert.equal(app.dlPeerText(task),'');
});
test('node discovery uses the existing task without creating a duplicate or moving files',async()=>{
 const app=create();let sent;app.action=async(p,b)=>{sent=b;return{id:'existing'}};app.api=async()=>({rows:[],disk:{}});
 await app.dlAct({id:'existing'},'reannounce');assert.equal(sent.action,'download_action');assert.equal(sent.op,'reannounce');assert.equal(sent.id,'existing');
 assert.equal(sent.manual,undefined);assert.equal(sent.download_dir,undefined);
});
