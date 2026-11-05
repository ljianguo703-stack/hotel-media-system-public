/* Use only the disposable tools/mock_dev.py fixture; no production connection. */
const {chromium}=require('playwright-core');
const fs=require('fs'),path=require('path'),assert=require('assert');
const root=path.resolve(__dirname,'..'),out=process.env.BG_EVIDENCE_DIR||path.join(root,'local/evidence/BG01');
const info=JSON.parse(fs.readFileSync(process.env.BG_MOCK_INFO||path.join(root,'artifacts/management/AD08/dev.json')));
(async()=>{
 assert.equal(new URL(info.url).hostname,'127.0.0.1');
 fs.mkdirSync(out,{recursive:true});
 const browser=await chromium.launch(require("./browser_options.cjs")),context=await browser.newContext({viewport:{width:1440,height:900}}),page=await context.newPage(),errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 const ui=(code,arg)=>page.evaluate(({code,arg})=>Function('app','arg',code)(Alpine.$data(document.body),arg),{code,arg});
 const settle=async()=>{await page.waitForFunction(()=>{const a=Alpine.$data(document.body);return a.user&&!a.busy&&!a.opsLoading&&a.homeDraft&&a.operationsMarkup});await page.locator('.managed-loading:visible').waitFor({state:'hidden'});};
 const tvHome=async()=>{const r=await context.request.get(info.tv_url+'/api/v1/catalog',{headers:{'X-TV-Token':'ad08-test-only'}});assert(r.ok());return (await r.json()).home;};
 try{
  await page.goto(info.url+'/?view=operations&tab=home');await page.locator('#username').fill('admin');await page.locator('#password').fill('ad08-local-test');await page.getByRole('button',{name:'登录后台',exact:true}).click();await settle();
  const images=await ui('return app.homeData.default_backgrounds');assert.equal(images.length,4);
  const initial=await tvHome();assert(images.some(i=>i.path===initial.background));const menu=await ui('return app.homeData.menu');
  for(const image of images){
   await page.getByRole('button',{name:'选择背景 '+image.name,exact:true}).click();
   assert.equal((await tvHome()).background,initial.background);
   assert.equal(await ui('return app.homeDirty'),true);
  }
  const selected=images[1];await page.getByRole('button',{name:'选择背景 '+selected.name,exact:true}).click();await page.getByRole('button',{name:'保存首页设置',exact:true}).click();await settle();
  assert.equal((await tvHome()).background,selected.path);await page.reload();await settle();
  assert.equal(await page.getByRole('button',{name:'选择背景 '+selected.name,exact:true}).getAttribute('aria-pressed'),'true');assert.deepEqual(await ui('return app.homeData.menu'),menu);
  assert.deepEqual({...await tvHome(),background:initial.background},initial);
  for(const image of images){const response=await context.request.get(info.tv_url+image.path,{headers:{'X-TV-Token':'ad08-test-only'}});assert(response.ok());assert((await response.body()).subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10])));}
  console.log('PASS four real bundled images / draft requires save / TV shared background / reload / menu and QR preserved');
  const upload=page.locator('label').filter({hasText:'上传背景图'}).locator('input[type=file]');await upload.setInputFiles(path.join(root,'admin/home-backgrounds/04-gold.png'));await page.waitForFunction(()=>!Alpine.$data(document.body).homeUploading);await page.getByRole('button',{name:'保存首页设置',exact:true}).click();await settle();const custom=(await tvHome()).background;assert(custom&&!images.some(i=>i.path===custom));await page.reload();await settle();assert.equal(await ui('return app.homeDraft.background'),custom);
  await page.locator('section').filter({has:page.getByRole('heading',{name:'首页背景',exact:true})}).getByRole('button',{name:'移除',exact:true}).click();await page.getByRole('button',{name:'保存首页设置',exact:true}).click();await settle();await page.reload();await settle();assert.equal((await tvHome()).background,'');
  console.log('PASS existing custom image upload / save / reload / remove persists');
  await page.getByRole('button',{name:'选择背景 '+selected.name,exact:true}).click();await page.getByRole('button',{name:'保存首页设置',exact:true}).click();await settle();
  for(const [w,h]of[[390,844],[1280,720],[1440,900]]){await page.setViewportSize({width:w,height:h});await ui("app.toast=''");await page.locator('.home-form').evaluate(el=>window.scrollTo(0,el.getBoundingClientRect().top+scrollY-16));await page.screenshot({path:path.join(out,`home-backgrounds-${w}x${h}.png`),animations:'disabled',fullPage:true});await page.screenshot({path:path.join(out,`home-backgrounds-preview-${w}x${h}.png`),animations:'disabled'});assert(!await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),`horizontal overflow ${w}`);}
  assert.deepEqual(errors,[]);console.log('PASS three viewport screenshots / no overflow / no page errors');
 }catch(e){await page.screenshot({path:path.join(out,'backgrounds-failure.png'),fullPage:true});throw e;}finally{await browser.close();}
})().catch(e=>{console.error(e.stack);process.exitCode=1});
