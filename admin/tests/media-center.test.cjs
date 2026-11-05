const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
function fixture(){
  const opened=[];const ctx=vm.createContext({URL,URLSearchParams,location:{href:'http://localhost:18791/',hostname:'localhost',protocol:'http:',search:''},document:{addEventListener(){},visibilityState:'visible'},history:{replaceState(){}},window:{scrollTo(){},open:(...a)=>opened.push(a)},setTimeout:()=>0,clearTimeout(){},setInterval:()=>0,clearInterval(){},confirm:()=>true,fetch:async()=>({ok:true,text:async()=>''}),console});
  for(const f of ['app.js','remote-access.js','media-center.js','management.js'])vm.runInContext(fs.readFileSync(path.join(__dirname,'..',f),'utf8'),ctx);
  const app=vm.runInContext('cinemaAdmin()',ctx);app.opened=opened;app.calls=[];
  app.api=async(p,body)=>{app.calls.push([p,body]);if(p==='/state')return {...app.data,media_automation:true};if(p.startsWith('/media-automation'))return body?{message:'完成',status:{issues:[],policy:{hevc:body.hevc||'allow'}}}:{checked_at:1,issues:[{level:'error',title:'Radarr 连不上下载器',detail:'',action:'repair_downloaders'}],policy:{hevc:'allow'},services:[]};if(p.startsWith('/reports'))return {rows:[]};return {}};
  app.loadOperations=async()=>{};app.notify=()=>{};app.user={role:'admin',csrf:'x'};return app;
}
test('downloads sits right after pending and is renamed for media automation',async()=>{
  const app=fixture();const ids=app.nav.map(n=>n.id);assert.equal(ids[ids.indexOf('pending')+1],'downloads');
  app.view='downloads';await app.reload(false);assert.equal(app.nav.find(n=>n.id==='downloads').name,'加片与下载');
});
test('subtitle counts distinguish unknown from zero and native actions preserve their scope',async()=>{
  const app=fixture();assert.equal(app.mcSubtitleWanted,'字幕状态尚未读取');
  app.mediaCenter={subtitles:{wanted_movies:0,wanted_episodes:null}};
  assert.equal(app.mcSubtitleWanted,'待补字幕：电影 0 项 · 剧集 未读取');
  await app.mediaCenterAction('subtitle_policy');assert.equal(app.calls.at(-1)[1].action,'subtitle_policy');
  await app.mediaCenterAction('search_subtitles');assert.equal(app.calls.at(-1)[1].action,'search_subtitles');
  app.user={role:'operator',csrf:'x'};const before=app.calls.length;await app.mediaCenterAction('search_subtitles');assert.equal(app.calls.length,before);
});
test('issue buttons route to the right owner action or page',async()=>{
  const app=fixture();app.data.media_automation=true;
  assert.equal(app.mcActionLabel('repair_downloaders'),'修复下载器连接');assert.equal(app.mcActionLabel('open:radarr'),'打开 Radarr ↗');assert.equal(app.mcActionLabel(null),'');
  await app.mcIssueAction({action:'repair_downloaders'});assert.deepEqual(JSON.parse(JSON.stringify(app.calls.at(-1))),['/media-automation',{action:'repair_downloaders'}]);
  await app.mcIssueAction({action:'open:radarr'});assert.equal(app.opened.at(-1)[0],'http://localhost:7878/');
  let tab='';app.openOperations=async t=>{tab=t};await app.mcIssueAction({action:'view:compat'});assert.equal(tab,'compat');
});
test('native download workflow links preserve configured mount paths and missing remote entries',()=>{
  const app=fixture();app.accessMode='local';app.accessConfig={local_host:'192.168.4.185',urls:{}};
  assert.equal(app.mcNativeUrl('qbittorrent'),'http://192.168.4.185:8098/');
  assert.equal(app.mcNativeUrl('jackett'),'http://192.168.4.185:9117/UI/Dashboard');
  assert.equal(app.mcNativeUrl('seerr','requests'),'http://192.168.4.185:5055/requests');
  assert.equal(app.mcNativeUrl('radarr','activity/queue'),'http://192.168.4.185:7878/activity/queue');
  app.accessMode='remote';app.accessConfig.urls={seerr:'https://example.test/apps/seerr',radarr:'https://example.test/apps/radarr/'};
  assert.equal(app.mcNativeUrl('seerr','requests'),'https://example.test/apps/seerr/requests');
  assert.equal(app.mcNativeUrl('radarr','activity/queue'),'https://example.test/apps/radarr/activity/queue');
  assert.equal(app.mcNativeUrl('jackett'),'#remote-access');
});
test('status loads are throttled and only administrators force a refresh',async()=>{
  const app=fixture();app.data.media_automation=true;
  await app.loadMediaCenter();await app.loadMediaCenter();assert.equal(app.calls.filter(c=>c[0].startsWith('/media-automation')).length,1);
  assert.equal(app.mediaCenterAttention,1);
  await app.loadMediaCenter(true);assert.equal(app.calls.at(-1)[0],'/media-automation?refresh=1');
  app.user={role:'operator',csrf:'x'};await app.loadMediaCenter(true);assert.equal(app.calls.at(-1)[0],'/media-automation');
  app.data.media_automation=false;const before=app.calls.length;await app.loadMediaCenter(true);assert.equal(app.calls.length,before);
});
test('subtitle form sends only filled accounts and clears secrets after saving',async()=>{
  const app=fixture();app.data.media_automation=true;app.mcSubtitles.assrt_token='token-only';
  await app.mcSaveSubtitles();assert.deepEqual(JSON.parse(JSON.stringify(app.calls.at(-1)[1])),{action:'set_subtitles',assrt_token:'token-only'});
  assert.equal(app.mcSubtitles.assrt_token,'');assert.equal(app.mcSubtitlesDirty,false);
  app.user={role:'viewer',csrf:'x'};const before=app.calls.length;await app.mediaCenterAction('retest_indexers');assert.equal(app.calls.length,before);
});
test('paste-a-link form sends the magnet, refuses empty input and shows readable states',async()=>{
  const app=fixture();app.data.media_automation=true;const notes=[];app.notify=m=>notes.push(m);
  app.api=async(p,body)=>{app.calls.push([p,body]);return {message:'已开始下载',tasks:[{id:'add-1',stage:'matched',download:{progress:.42,size:4e9,speed:2e6,eta:600},match:{title:'流浪地球2',year:2023}}]}};
  await app.mcAddSubmit();assert.equal(app.calls.length,0);assert.match(notes.at(-1),/磁力链接/);
  app.mcAdd.link='  magnet:?xt=urn:btih:'+'a'.repeat(40)+'  ';app.mcAdd.kind='movie';await app.mcAddSubmit();
  assert.deepEqual(JSON.parse(JSON.stringify(app.calls.at(-1))),['/media-add',{action:'add',kind:'movie',link:'magnet:?xt=urn:btih:'+'a'.repeat(40)}]);
  assert.equal(app.mcAdd.link,'');assert.equal(app.mcAdd.kind,'movie');
  const t=app.mcAddTasks[0];assert.equal(app.mcStage(t).label,'已认出 · 下载中');assert.match(app.mcDownloadText(t),/^已下载 42%/);assert.equal(app.mcCanPick(t),true);
  assert.equal(app.mcStage({stage:'needs_match'}).cls,'warn');assert.equal(app.mcStage({stage:'imported'}).label,'已归档 · 同步中');assert.equal(app.mcStage({stage:'imported',error:'x'}).cls,'warn');assert.equal(app.mcStage({stage:'listed'}).label,'已进入待上架');assert.equal(app.mcKeepsFiles({stage:'downloaded'}),true);assert.equal(app.mcKeepsFiles({stage:'failed'}),false);assert.match(app.mcDownloadText({stage:'downloaded',download:{complete:true}}),/不入库/);assert.equal(app.mcCanPick({stage:'matched',download:{complete:true}}),false);
  assert.equal(app.mcDownloadText({stage:'matched',download:{state:'stalled',progress:.1}}),'暂时没有可连接的做种者，继续等待 · 已下载 10%');
  app.user={role:'operator',csrf:'x'};const before=app.calls.length;app.mcAdd.link='magnet:?xt=urn:btih:'+'b'.repeat(40);await app.mcAddSubmit();assert.equal(app.calls.length,before);
});
test('title picker reuses candidates, then confirms a download or renames a library item',async()=>{
  const app=fixture();app.data.media_automation=true;
  app.api=async(p,body)=>{if(p==='/state'||p.startsWith('/reports'))return p==='/state'?{...app.data,media_automation:true}:{rows:[]};app.calls.push([p,body]);if(p.startsWith('/media-add/search'))return {results:[{media_type:'movie',tmdb_id:2,title:'疯狂外星人',year:2015}]};return {message:'好',tasks:[]}};
  app.mcOpenPicker('match',{id:'add-1',name:'Home.mkv',kind:'auto',query:'Home',candidates:[{media_type:'movie',tmdb_id:1,title:'Home',year:2009}]});
  assert.equal(app.modal,'title-picker');assert.equal(app.calls.length,0);assert.equal(app.mcPick.query,'Home');assert.equal(app.mcPick.results.length,1);
  await app.mcSearch();assert.equal(app.calls.at(-1)[0],'/media-add/search?q=Home&kind=auto');
  await app.mcChoose(app.mcPick.results[0]);assert.deepEqual(JSON.parse(JSON.stringify(app.calls.at(-1))),['/media-add',{action:'match',id:'add-1',media_type:'movie',tmdb_id:2}]);assert.equal(app.modal,null);
  app.mcOpenPicker('rename',{id:'jf-x',title:'流浪 地球',kind:'series'});assert.equal(app.mcPick.kind,'series');
  assert.equal(app.calls.at(-1)[0],'/media-add/search?q=%E6%B5%81%E6%B5%AA%20%E5%9C%B0%E7%90%83&kind=series');
  await new Promise(r=>setImmediate(r));await app.mcChoose({media_type:'tv',tmdb_id:9});
  assert.deepEqual(JSON.parse(JSON.stringify(app.calls.find(c=>c[0]==='/media-rename'))),['/media-rename',{media_id:'jf-x',media_type:'tv',tmdb_id:9}]);
  assert.equal(app.mcPoster('/p1.jpg'),'/api/admin/media-add/poster?p=%2Fp1.jpg');assert.equal(app.mcPoster(''),'');
});
