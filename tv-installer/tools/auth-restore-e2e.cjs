const fs=require('fs'),path=require('path'),assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright-core');
const BASE='http://127.0.0.1:18947',OUT=process.env.TI_EVIDENCE;
const VERSION=fs.readFileSync(path.resolve(__dirname,'../config.go'),'utf8').match(/const version = "([^"]+)"/)[1];
(async()=>{
 fs.mkdirSync(OUT,{recursive:true});const browser=await chromium.launch(require('../../tools/browser_options.cjs'));
 const page=await browser.newPage({viewport:{width:1440,height:900}}),errors=[],mutations=[],steps=[];
 page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(['/api/install','/api/restore'].some(p=>r.url()===BASE+p))mutations.push(r.url())});
 try{
 await page.goto(BASE+'/#ti-test-fixture-session');await page.waitForFunction(v=>document.getElementById('version').textContent.includes(v),VERSION);
 await page.fill('#target','192.168.4.96');await page.click('#probe');await page.waitForFunction(()=>document.getElementById('connectionStatus').textContent.includes('Connection refused'));
 assert.match(await page.textContent('#connectionStatus'),/尚未连通/);assert(!(await page.textContent('#connectionStatus')).includes('电视正在等待'));assert(await page.locator('#installOne').isDisabled());
 for(const [w,h]of[[390,844],[1280,720],[1440,900]]){await page.setViewportSize({width:w,height:h});await page.locator('#connectionStatus').scrollIntoViewIfNeeded();assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);await page.screenshot({path:path.join(OUT,`connection-${w}x${h}.png`)})}
 steps.push('zero-exit connection refusal stays visible with its real reason; install remains disabled');
 await page.fill('#target','192.168.4.95');const reconnect=page.waitForRequest(r=>r.url()===BASE+'/api/reconnect');await page.click('#reconnect');assert.equal((await reconnect).postDataJSON().target,'192.168.4.95');await page.waitForFunction(()=>document.getElementById('connectionStatus').textContent.includes('独立授权'));assert(await page.locator('#reconnect').isEnabled());steps.push('explicit reconnect targets only entered TV and reports pending consent');
 await page.fill('#target','192.168.4.62');await page.click('#reconnect');await page.waitForFunction(()=>document.getElementById('connectionStatus').textContent.includes('已取得电视调试连接'));await page.click('#probe');await page.waitForFunction(()=>document.getElementById('connectionStatus').textContent.includes('设备信息读取成功'));steps.push('authorized reconnect proceeds to a fresh successful detection');
 await page.click('[data-page="restore"]');assert(await page.locator('#fromTV').isVisible());assert.equal(await page.locator('#fromTV').evaluate(e=>!!e.closest('details:not([open])')),false);assert.match(await page.textContent('#tvRestorePoint'),/不是清空/);
 await page.fill('#restoreTarget','192.168.4.97');const before=await page.evaluate(async()=>(await api('state')).job);await page.click('#fromTV');await page.waitForFunction(()=>document.getElementById('restoreStatus').textContent.includes('尚未执行还原'));await page.waitForFunction(()=>document.getElementById('backupList').textContent.includes('从电视读取'));
 const after=await page.evaluate(async()=>(await api('state')).job);assert.deepEqual(after,before);assert.equal(mutations.length,0);steps.push('new-computer empty backup list imports the TV-side point over the real fixture HTTP API without restoring');
 for(const [w,h]of[[390,844],[1280,720],[1440,900]]){await page.setViewportSize({width:w,height:h});await page.locator('#tvRestorePoint').scrollIntoViewIfNeeded();assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);await page.screenshot({path:path.join(OUT,`restore-${w}x${h}.png`)})}
 assert.deepEqual(errors,[]);fs.writeFileSync(path.join(OUT,'result.json'),JSON.stringify({passed:true,fixture:'Go HTTP app with simulated TV and original point prepared by another simulated computer; no real TV',steps,install_or_restore_requests:mutations.length,page_errors:errors},null,2));console.log('PASS auth states, targeted reconnect, visible TV restore import, six screenshots, zero install/restore requests');
 }catch(e){await page.screenshot({path:path.join(OUT,'failure.png'),fullPage:true});throw e}finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
