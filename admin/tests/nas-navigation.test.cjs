const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
function setup(href='http://192.168.4.185:18791/?view=nas',role='admin'){
  const location=new URL(href),events={},calls=[],historyStack=[href];
  function update(url,push){location.href=String(url);calls.push(push?'push':'replace');if(push)historyStack.push(location.href)}
  const context=vm.createContext({URL,URLSearchParams,location,history:{replaceState:(a,b,url)=>update(url,false),pushState:(a,b,url)=>update(url,true)},window:{scrollTo(){},addEventListener:(name,fn)=>events[name]=fn}});
  vm.runInContext(`function cinemaAdmin(){return {view:'nas',user:${role?JSON.stringify({role}):'null'},get administrator(){return this.user?.role==='admin'},npcDraft:{server:'edited'},accessDraft:{urls:{files:'https://pending.test/'}},async init(){},async login(){this.user={role:'admin'}},$watch(){},navigate(view){this.view=view;const url=new URL(location.href);url.searchParams.set('view',view);history.replaceState({},'',url)},notify(message){this.toast=message},serviceUrl(id){return this.urls?.[id]||''},requireService(event){event.preventDefault();this.fallback=true},async reload(){},async loadNpc(){},async loadAccess(){}}}`,context);
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../nas-navigation.js'),'utf8'),context);
  return {app:vm.runInContext('cinemaAdmin()',context),location,events,calls,historyStack};
}
test('fresh NAS defaults to sync; unknown and privileged links are safely resolved',async()=>{
  for(const [url,role,expected] of [['?view=nas','admin','sync'],['?view=nas&nas=shared','admin','shared'],['?view=nas&nas=nope','admin','sync'],['?view=nas&nas=npc','viewer','sync']]){
    const {app}=setup('http://localhost/'+url,role);await app.init();assert.equal(app.nasTab,expected);
  }
});
test('legacy npc and public-entry hashes select their own panel even with an old query',async()=>{
  for(const [hash,panel] of [['#npc','npc'],['#remote-access','access']]){const {app}=setup('http://localhost/?view=nas&nas=sync'+hash);await app.init();assert.equal(app.nasTab,panel)}
});
test('switch, reload and Back restore selection while leaving both forms untouched',async()=>{
  const {app,location,events,calls}=setup();await app.init();app.openNas('npc');assert.equal(app.nasTab,'npc');assert.equal(location.searchParams.get('nas'),'npc');
  app.openNas('access');assert.equal(app.npcDraft.server,'edited');assert.equal(app.accessDraft.urls.files,'https://pending.test/');
  location.search='?view=nas&nas=npc';events.popstate();assert.equal(app.nasTab,'npc');app.readNasLocation();assert.equal(app.nasTab,'npc');assert.equal(calls.filter(x=>x==='push').length,2);
});
test('external apps retain reviewed addresses; missing public entry opens the configuration panel',async()=>{
  const {app,location}=setup('http://public.test/?view=downloads');app.view='downloads';await app.init();let prevented=0;const e={preventDefault(){prevented++}};
  app.urls={files:'https://public.test:25807/'};const files=app.nasItems.find(x=>x.id==='files');assert.equal(app.nasHref(files),'https://public.test:25807/');app.followNas(e,files);assert.equal(prevented,0);
  app.followNas(e,app.nasItems.find(x=>x.id==='cockpit'));assert.equal(prevented,1);assert.equal(app.view,'nas');assert.equal(app.nasTab,'access');assert.equal(location.searchParams.get('nas'),'access');
});
test('hash changes and successful login preserve old direct links',async()=>{
  const {app,location,events}=setup('http://localhost/?view=nas#npc',null);await app.init();assert.equal(app.nasTab,'sync');await app.login();assert.equal(app.nasTab,'npc');location.hash='#remote-access';events.hashchange();assert.equal(app.nasTab,'access');
});
test('viewers cannot select administrator modules; modifier-click on internal links stays native',async()=>{
  const {app}=setup(undefined,'viewer');await app.init();assert.equal(app.nasItems.some(x=>['npc','access','cockpit','dockge','install'].includes(x.id)),false);app.openNas('npc');assert.equal(app.nasTab,'sync');let prevented=0;app.followNas({metaKey:true,preventDefault(){prevented++}},{id:'shared'});assert.equal(prevented,0);assert.equal(app.nasTab,'sync');
});
