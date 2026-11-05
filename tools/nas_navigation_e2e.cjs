// Disposable real HTTP backend; native application links are inspected without opening them.
const {chromium}=require('playwright-core');
const fs=require('fs'),path=require('path'),assert=require('assert');
const root=path.resolve(__dirname,'..'),out=path.join(root,'local/evidence/IN02/nas');
const info=JSON.parse(fs.readFileSync(path.join(root,'artifacts/management/AD08/dev.json')));
(async()=>{
 assert.equal(new URL(info.url).hostname,'127.0.0.1');fs.mkdirSync(out,{recursive:true});
 const browser=await chromium.launch(require('./browser_options.cjs')),page=await browser.newPage(),errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 const ui=fn=>page.evaluate(fn);
 try{
  await page.goto(info.url+'/?view=nas&nas=shared');await page.locator('#username').fill('admin');await page.locator('#password').fill('ad08-local-test');await page.getByRole('button',{name:'登录后台',exact:true}).click();
  await page.getByRole('heading',{name:'电脑共享',exact:true}).waitFor();
  assert((await page.locator('#nas-share-address').inputValue()).startsWith('smb://127.0.0.1/'));
  for(const id of ['jellyfin','files','cockpit','dockge']){
   const a=page.locator('#nas-link-'+id);assert.equal(await a.getAttribute('target'),'_blank');assert.equal(new URL(await a.getAttribute('href'),info.url).hostname,'127.0.0.1');
  }
  await page.locator('#nas-link-npc').click();await page.waitForFunction(()=>Alpine.$data(document.body).npcLoaded);
  await ui(()=>{const a=Alpine.$data(document.body);a.npcDraft.server='127.0.0.1:18024';a.npcDirty=true;});
  await page.locator('#nas-link-sync').click();await page.locator('#nas-link-npc').click();
  assert.equal(await ui(()=>Alpine.$data(document.body).npcDraft.server),'127.0.0.1:18024');
  for(const id of ['sync','shared','storage','install','access']){
   await page.locator('#nas-link-'+id).click();await page.locator('[data-nas-pane="'+id+'"]').waitFor({state:'visible'});
   assert.equal(new URL(page.url()).searchParams.get('nas'),id);
  }
  await page.goBack();await page.waitForFunction(()=>Alpine.$data(document.body).nasTab==='install');
  await page.locator('#nas-link-shared').click();
  for(const [width,height] of [[390,844],[1280,720],[1440,900]]){
   await page.setViewportSize({width,height});await page.waitForTimeout(300);
   assert(!await ui(()=>document.documentElement.scrollWidth>innerWidth+1));
   await page.screenshot({path:path.join(out,`nas-${width}x${height}.png`),animations:'disabled',fullPage:true});
  }
  assert.equal(await page.locator('.nas-services').count(),0);
  await ui(()=>Alpine.$data(document.body).navigate('overview'));
  await page.locator('.nas-workspace').waitFor({state:'hidden'});
  assert.equal(await page.getByRole('heading',{name:'电脑共享',exact:true}).isVisible(),false);
  assert.deepEqual(errors,[]);console.log('PASS NAS deep links, native application destinations, draft preservation, browser history and three sizes');
 }catch(e){await page.screenshot({path:path.join(out,'failure.png'),fullPage:true});throw e;}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
