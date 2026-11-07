const fs=require('fs'),path=require('path'),assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright-core');
const BASE='http://127.0.0.1:18947',OUT=process.env.TI_EVIDENCE;
(async()=>{
 fs.mkdirSync(OUT,{recursive:true});
 const browser=await chromium.launch(require('../../tools/browser_options.cjs'));
 const page=await browser.newPage({viewport:{width:1440,height:900}}),errors=[],steps=[];let installs=0;
 page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(r.url()===BASE+'/api/install')installs++});
 await page.goto(BASE+'/#ti-test-fixture-session');await page.waitForFunction(()=>document.getElementById('doctorStatus').textContent.includes('ADB可运行'));
 assert.match(await page.textContent('#installReadiness'),/检测设备/);
 await page.fill('#target','192.168.4.62');await page.click('#probe');await page.waitForFunction(()=>document.getElementById('detection').textContent.includes('首次安装'));
 assert(await page.locator('#installOne').isDisabled());assert.match(await page.textContent('#installReadiness'),/Ubuntu 已有 PIN/);
 assert.equal(await page.locator('#installOne').evaluate(e=>getComputedStyle(e).cursor),'not-allowed');steps.push('首次安装缺PIN时直接显示复用已有PIN的方法，禁用按钮不显示等待光标');
 await page.check('#configureServer');await page.fill('#backendServer','127.0.0.1:18948');await page.fill('#backendUser','admin');await page.fill('#backendPass','fixture-pass');await page.click('#backendLogin');
 await page.waitForFunction(()=>document.getElementById('backendStatus').textContent.includes('已登录后台'));await page.check('#managedFleet');
 assert.match(await page.textContent('#installReadiness'),/员工 PIN/);assert.match(await page.textContent('#installReadiness'),/填写房间名/);
 for(const [w,h] of [[390,844],[1280,720],[1440,900]]){await page.setViewportSize({width:w,height:h});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth-document.documentElement.clientWidth),0);await page.locator('#installOne').scrollIntoViewIfNeeded();await page.screenshot({path:path.join(OUT,w+'-missing-pin-room.png'),fullPage:false})}
 await page.fill('#room','301');assert(!/填写房间名/.test(await page.textContent('#installReadiness')));assert(await page.locator('#installOne').isDisabled());steps.push('同时列出缺PIN及缺房间名，补填房间名后立即更新，不用重新检测');
 await page.locator('summary').filter({hasText:'这家酒店已经有6位员工 PIN'}).click();await page.fill('#existingPin','123456');page.once('dialog',d=>d.accept());await page.click('#reusePin');
 await page.waitForFunction(()=>document.getElementById('installReadiness').textContent.includes('准备完成'));assert(await page.locator('#installOne').isEnabled());
 await page.fill('#room','');assert(await page.locator('#installOne').isDisabled());assert.match(await page.textContent('#installReadiness'),/填写房间名/);
 await page.uncheck('#managedFleet');assert(await page.locator('#installOne').isEnabled());steps.push('保存已有PIN后启用按钮；纳管缺房间名时禁用，取消纳管后房间名仍可留空');
 await page.fill('#target','192.168.4.63');assert(await page.locator('#installOne').isDisabled());assert.match(await page.textContent('#installReadiness'),/检测设备/);steps.push('修改IP后要求重新检测，不沿用上一台的已就绪状态');
 assert.equal(installs,0);assert.deepEqual(errors,[]);
 fs.writeFileSync(path.join(OUT,'readiness-e2e.json'),JSON.stringify({steps,errors,install_requests:installs,sizes:[390,1280,1440]},null,2));
 await browser.close();console.log(JSON.stringify({passed:steps.length,install_requests:installs,errors}));
})().catch(e=>{console.error(e);process.exit(1)});
