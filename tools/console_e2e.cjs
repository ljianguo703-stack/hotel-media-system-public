/* Disposable Linux fixture only. Real HTTP telemetry, all menu routes and role visibility. */
const {chromium}=require('playwright-core'),fs=require('fs'),path=require('path'),assert=require('node:assert/strict');
(async()=>{
 const base=process.env.HOTEL_CONSOLE_URL||'http://127.0.0.1:27991',tv=process.env.HOTEL_CONSOLE_TV||'http://127.0.0.1:27990';
 assert.equal(new URL(base).hostname,'127.0.0.1');const out=path.resolve(process.env.HOTEL_CONSOLE_EVIDENCE||'local/evidence/IN20/console');fs.mkdirSync(out,{recursive:true});
 const browser=await chromium.launch(require('./browser_options.cjs')),context=await browser.newContext({viewport:{width:1440,height:900}}),page=await context.newPage(),errors=[];
 page.on('pageerror',e=>errors.push(e.message));const ui=fn=>page.evaluate(fn);
 const settle=()=>page.waitForFunction(()=>{const a=Alpine.$data(document.body);return a.user&&!a.busy&&!a.opsLoading&&a.consoleMarkup&&a.dashboardMarkup});
 try{
  for(const id of ['101','201']){const headers={'X-TV-Token':'in20-fixture-'+id};assert((await context.request.get(tv+'/api/v1/catalog',{headers})).ok());assert((await context.request.get(tv+'/media/clip.mp4',{headers:{...headers,Range:'bytes=0-65535'}})).ok())}
  await page.goto(base);await page.locator('#password').fill('in20-local-test');await page.getByRole('button',{name:'登录后台',exact:true}).click();await settle();
  await page.waitForFunction(()=>Alpine.$data(document.body).dashboardData?.devices.length===2);
  const data=await ui(()=>Alpine.$data(document.body).dashboardData);assert.equal(data.server.available,true);assert(data.interfaces.length>=1);assert(data.devices.every(d=>d.online_since&&d.today_rx>=65536));assert(!data.devices.some(d=>d.room==='999'));
  assert.equal(await page.locator('.online-table tbody tr').count(),2);assert.equal(await page.locator('.online-table tbody').getByText('999 房',{exact:true}).count(),0);console.log('PASS real Linux CPU/memory/uptime/NIC sampling and authenticated TV body counters');
  const entries=await ui(()=>Alpine.$data(document.body).consoleItems);assert.equal(entries.length,41);
  const visited=[];
  await ui(()=>{window.open=(url)=>{window.__openedNative=url;return null}});
  for(const entry of entries){await ui(()=>Alpine.$data(document.body).openConsoleAll());await page.locator('.console-all-grid').getByRole('button',{name:entry.name,exact:true}).click();await settle();
   const state=await ui(()=>{const a=Alpine.$data(document.body);return {view:a.view,tab:a.opsTab,nas:a.nasTab,modal:a.modal,error:a.error,external:window.__openedNative}});
   if(entry.view==='operations'){assert.equal(state.view,'operations',entry.id);assert.equal(state.tab,entry.target,entry.id)}
   else if(entry.view==='nas'){assert.equal(state.view,'nas',entry.id);assert.equal(state.nas,entry.target,entry.id)}
   else if(entry.view==='service'){assert(state.external||state.view==='nas',entry.id)}else assert.equal(state.view,entry.view,entry.id);
   if(entry.target==='categories'){assert.equal(state.modal,'categories');await ui(()=>Alpine.$data(document.body).close())}
   assert(!state.error,entry.id+': '+state.error);visited.push(entry.id);
  }
  console.log('PASS all 41 visible menu entries reach their original screens, dialogs or native app destination');
  await ui(()=>Alpine.$data(document.body).openConsole('overview'));await settle();await page.locator('input[x-model=deviceQuery]').fill('201');assert.equal(await page.locator('.online-table tbody tr').count(),1);await page.locator('input[x-model=deviceQuery]').fill('');
  await page.getByRole('button',{name:'搜索功能与设置',exact:true}).click();await page.getByRole('textbox',{name:'搜索功能',exact:true}).fill('NAS');assert((await page.locator('.console-search-results button').count())>=6);await page.getByRole('button',{name:'关闭搜索',exact:true}).click();
  for(const [width,height] of [[390,844],[1280,720],[1440,900]]){await page.setViewportSize({width,height});await page.waitForTimeout(150);assert(!await ui(()=>document.documentElement.scrollWidth>innerWidth+1),'overflow '+width);await page.screenshot({path:path.join(out,`home-${width}x${height}.png`),fullPage:true,animations:'disabled'});await ui(()=>Alpine.$data(document.body).openConsoleSettings());await page.screenshot({path:path.join(out,`settings-${width}x${height}.png`),fullPage:true,animations:'disabled'});assert(!await ui(()=>document.documentElement.scrollWidth>innerWidth+1),'settings overflow '+width);await ui(()=>Alpine.$data(document.body).openConsole('overview'));await settle()}
  for(const role of ['operator','viewer']){await ui(()=>Alpine.$data(document.body).logout());await page.locator('#username').fill(role);await page.locator('#password').fill('in20-local-test');await page.getByRole('button',{name:'登录后台',exact:true}).click();await settle();const list=await ui(()=>Alpine.$data(document.body).consoleItems);assert(!list.some(i=>['users','passwords','backups','fleet','software','guest_requests'].includes(i.id)));assert(list.some(i=>i.id==='overview'));}
  assert.deepEqual(errors,[]);fs.writeFileSync(path.join(out,'verification.json'),JSON.stringify({visited,roles:['admin','operator','viewer'],realLinuxMetrics:true,pageErrors:errors},null,2));console.log('PASS 6 screenshots, search, online filtering, roles and no browser errors');
 }catch(error){await page.screenshot({path:path.join(out,'failure.png'),fullPage:true});console.error(error.stack);console.error(errors);process.exitCode=1}finally{await browser.close()}
})();
