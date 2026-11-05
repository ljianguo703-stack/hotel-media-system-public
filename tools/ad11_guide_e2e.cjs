/* AD11: real loopback UI navigation. Never reads site settings or contacts a television. */
const {chromium}=require('playwright-core'),fs=require('fs'),path=require('path'),assert=require('node:assert/strict');
(async()=>{
const root=path.resolve(__dirname,'..'),info=JSON.parse(fs.readFileSync(path.join(root,'artifacts/management/AD08/dev.json')));
assert.equal(new URL(info.url).hostname,'127.0.0.1');
const output=path.join(root,'artifacts/management/AD11');fs.mkdirSync(output,{recursive:true});
const browser=await chromium.launch(require("./browser_options.cjs")),page=await browser.newPage({viewport:{width:1440,height:900}}),errors=[];
page.on('pageerror',e=>errors.push(e.message));
const state=()=>page.evaluate(()=>{const a=Alpine.$data(document.body);return {view:a.view,opsTab:a.opsTab,modal:a.modal,status:a.adStatus,report:a.reportDraft}});
const settle=async()=>{await page.waitForFunction(()=>{const a=Alpine.$data(document.body);return a.user&&!a.busy&&!a.opsLoading});await page.evaluate(()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r))))};
const ads=async()=>{await page.locator('.nav-item').filter({hasText:'广告投放'}).click();await page.locator('#ad-workflow-title').waitFor();await settle()};
try{
await page.goto(info.url+'/?view=ads');await page.locator('#username').fill('admin');await page.locator('#password').fill('ad08-local-test');await page.getByRole('button',{name:'登录后台',exact:true}).click();await page.locator('#ad-workflow-title').waitFor();await settle();
assert.equal(await page.locator('.ad-workflow-steps>li').count(),6);
for(const [label,tab] of [['管理商户','advertisers'],['选择或保存套餐','packages'],['登记投放单','orders'],['播放规则','ad_playback'],['广告总控','control'],['查看素材库','assets']]){
 await page.locator('.ad-workflow').getByRole('button',{name:label,exact:true}).click();await settle();const a=await state();assert.equal(a.view,'operations');assert.equal(a.opsTab,tab);await ads();console.log('PASS guide link:',label);
}
await page.getByRole('button',{name:'制作广告',exact:true}).click();await page.getByRole('dialog',{name:'编辑广告',exact:true}).waitFor();assert.equal((await state()).modal,'campaign');await page.getByRole('button',{name:'关闭广告编辑',exact:true}).click();console.log('PASS guide opens real material editor');
await page.getByRole('button',{name:'查看待发布广告',exact:true}).click();await settle();assert.equal((await state()).status,'草稿');assert.equal(await page.locator('[aria-label="广告状态"]').inputValue(),'草稿');console.log('PASS draft review filters list without publishing');
await page.getByRole('button',{name:'生成商户报告',exact:true}).click();await page.locator('#rep-adv').waitFor();assert.equal((await state()).modal,'report-choose');await page.locator('.dialog-wrap:visible .dialog-close').click();console.log('PASS guide opens merchant report selection');
await page.locator('.ad-workflow-help summary').click();await page.getByText('图片、联系方式和二维码：',{exact:true}).waitFor();await page.locator('.ad-workflow-help summary').click();
for(const [width,height] of [[390,844],[1280,720],[1440,900]]){
 await page.setViewportSize({width,height});await page.evaluate(()=>window.scrollTo(0,0));await settle();await page.waitForFunction(()=>innerWidth>700||document.querySelector('.sidebar').getBoundingClientRect().right<=1);
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
 await page.screenshot({animations:'disabled',path:path.join(output,`guide-${width}x${height}.png`),fullPage:true});
}
assert.deepEqual(errors,[]);console.log('PASS six steps / all real destinations / three sizes / zero page errors or overflow');
}catch(e){await page.screenshot({animations:'disabled',path:path.join(output,'failure.png'),fullPage:true});console.error(e);process.exitCode=1}
finally{await browser.close()}
})();
