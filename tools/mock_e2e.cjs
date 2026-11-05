/* Run against tools/mock_dev.py only. Exercises real HTTP persistence through the browser UI. */
const {chromium}=require('playwright-core');
const fs=require('fs'),path=require('path'),assert=require('assert');
(async()=>{
const out=path.resolve(__dirname,'../artifacts/management/AD08'),info=JSON.parse(fs.readFileSync(path.join(out,'dev.json')));
assert(new URL(info.url).hostname==='127.0.0.1');
const browser=await chromium.launch(require("./browser_options.cjs")),context=await browser.newContext({viewport:{width:1440,height:900}}),page=await context.newPage();
const errors=[];page.on('pageerror',e=>errors.push(e.message));
const state=()=>page.evaluate(()=>Alpine.$data(document.body).data),ui=async(code,arg)=>page.evaluate(({code,arg})=>Function('app','arg',code)(Alpine.$data(document.body),arg),{code,arg});
const settle=async()=>{await page.waitForFunction(()=>{const a=Alpine.$data(document.body);return a.user&&!a.busy&&!a.opsLoading&&a.operationsMarkup&&a.playbackMarkup});await page.locator('.managed-loading:visible').waitFor({state:'hidden'});await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))))};
try{
await page.goto(info.url+'/?view=ads');await page.locator('#username').fill('admin');await page.locator('#password').fill('ad08-local-test');await page.getByRole('button',{name:'登录后台',exact:true}).click();await settle();
await page.getByRole('button',{name:'播放规则',exact:true}).first().click();await page.locator('#rule-mid').fill('55');await page.locator('#rule-delay').fill('180');await page.locator('#rule-text').fill('10');
await page.getByRole('button',{name:'保存规则',exact:true}).click();await settle();
assert.equal(await ui('return app.playbackDraft.midroll_percent'),55);await page.reload();await settle();assert.equal(await page.locator('#rule-mid').inputValue(),'55');
console.log('PASS playback rules saved and reloaded');
await ui("return app.openOperations('packages')");await page.getByRole('button',{name:'新增套餐',exact:true}).click();await page.locator('#pkg-name').fill('全屏与文字组合');await page.locator('#pkg-tier').selectOption('1');
await page.locator('form').filter({has:page.locator('#pkg-name')}).getByText('二档 · 全天文字',{exact:true}).click();await page.locator('form').filter({has:page.locator('#pkg-name')}).getByText('三档 · 分时文字',{exact:true}).click();
for(const name of ['片前','中插','暂停','片尾'])await page.locator('fieldset').filter({has:page.getByText('主档展示位置',{exact:true})}).getByText(name,{exact:true}).click();
await page.locator('#pkg-target').fill('100');await page.locator('#pkg-seconds').fill('15');await page.getByRole('button',{name:'保存套餐',exact:true}).click();await settle();
let pkg=(await state()).ad_packages.find(p=>p.name==='全屏与文字组合');assert.deepEqual(pkg.tiers,[1,2,3]);assert.equal(pkg.default_ad_seconds,15);console.log('PASS combination package');
await ui("return app.navigate('ads')");await page.getByRole('button',{name:'新建广告',exact:true}).click();await page.locator('#ad-name').fill('青禾餐厅图文投放');await page.locator('#ad-merchant').selectOption({label:'青禾餐厅'});await page.locator('#ad-format').selectOption('图文');
await page.locator('#card-title').fill('青禾餐厅');await page.locator('#card-body').fill('时令美味，每日新鲜。');await page.locator('#card-contact').fill('一层大堂 · 联系电话 123456');
await page.locator('#card-link').fill('https://example.com/contact');
await page.locator('#card-image').setInputFiles(path.join(info.data,'media/sponsor.png'));
await page.waitForFunction(()=>!Alpine.$data(document.body).uploading);
await page.locator('#ad-duration').fill('12');await page.getByRole('button',{name:'保存草稿',exact:true}).click();await settle();
let ad=(await state()).campaigns.find(c=>c.name==='青禾餐厅图文投放');assert(ad);assert(ad.creative_card.qr.startsWith('/media/'));assert.equal(ad.creative_card.title,'青禾餐厅');assert.equal(ad.duration,12);console.log('PASS actual image upload / card / local QR saved');
await page.getByRole('button',{name:'编辑广告 青禾餐厅图文投放',exact:true}).click();assert.equal(await page.locator('#card-contact').inputValue(),'一层大堂 · 联系电话 123456');
for(const [w,h]of[[390,844],[1280,720],[1440,900]]){await page.setViewportSize({width:w,height:h});await page.locator('.ad-card-editor').scrollIntoViewIfNeeded();await ui("app.toast=''");await page.screenshot({animations:'disabled',path:path.join(out,`card-${w}x${h}.png`)});const bad=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1);assert(!bad,`card overflow ${w}`)}
await ui('app.close();return app.openOperations("orders")');await page.getByRole('button',{name:'新建投放单',exact:true}).click();await page.locator('#ord-adv').selectOption('sponsor-0');await page.locator('#ord-pkg').selectOption(pkg.id);
await page.locator('form').filter({has:page.locator('#ord-adv')}).getByText('青禾餐厅全屏 · 投放中 · 全部客房',{exact:true}).click();await page.getByRole('button',{name:'保存投放单',exact:true}).click();await settle();
let rows=await ui('return app.opsData.rows');assert.equal(rows.length,1);assert.deepEqual(rows[0].tiers,[1,2,3]);const order=rows[0];
await ui('app.editPackage(arg)',pkg);await page.locator('#pkg-name').fill('改名后的新模板');await page.getByRole('button',{name:'保存套餐',exact:true}).click();await settle();await ui('return app.openOperations("orders")');await settle();rows=await ui('return app.opsData.rows');assert.equal(rows[0].package_name,'全屏与文字组合');console.log('PASS order contract snapshot survives template edit');
await ui('app.editOrder(arg)',rows[0]);assert.equal(await ui('return app.orderContract.name'),'全屏与文字组合');await page.getByRole('button',{name:'重新套用当前模板',exact:true}).click();assert.equal(await ui('return app.orderContract.name'),'改名后的新模板');await page.getByRole('button',{name:'保存投放单',exact:true}).click();await settle();assert.equal((await ui('return app.opsData.rows'))[0].package_name,'改名后的新模板');console.log('PASS explicit reapply');
await ui('return app.openOperations("ad_playback")');await settle();
for(const [w,h]of[[390,844],[1280,720],[1440,900]]){await page.setViewportSize({width:w,height:h});await ui("app.toast=''");await page.screenshot({animations:'disabled',path:path.join(out,`rules-${w}x${h}.png`),fullPage:true});assert(!await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),`rules overflow ${w}`)}
await ui('app.editPackage(arg)',(await state()).ad_packages.find(p=>p.id===pkg.id));
for(const [w,h]of[[390,844],[1280,720],[1440,900]]){await page.setViewportSize({width:w,height:h});await ui("app.toast=''");await page.screenshot({animations:'disabled',path:path.join(out,`package-${w}x${h}.png`)});assert(!await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),`package overflow ${w}`)}
assert.deepEqual(errors,[]);console.log('PASS 9 screenshots / no horizontal overflow / no page errors');
await ui('app.close()');
const events=[{id:'browser-start',type:'ad_started',creative_id:'sponsor-0~home',at_ms:Date.now()},{id:'browser-end',type:'ad_completed',creative_id:'sponsor-0~home',at_ms:Date.now()}];
const delivered=await context.request.post(info.tv_url+'/api/v1/events',{headers:{'X-TV-Token':'ad08-test-only'},data:{events}});assert(delivered.ok());
await page.goto(info.url+'/merchant-report.html?order='+order.id);await page.getByRole('heading',{name:'青禾餐厅 · 广告投放报告',exact:true}).waitFor();assert.equal(await page.locator('.cards .card').first().locator('b').innerText(),'1');
for(const [w,h]of[[390,844],[1280,720],[1440,900]]){await page.setViewportSize({width:w,height:h});await page.screenshot({animations:'disabled',path:path.join(out,`report-${w}x${h}.png`),fullPage:true})}
console.log('PASS real TV events feed printable typed report / 3 sizes');
console.log('AD08 browser E2E complete');
}catch(e){await ui("app.toast=''");await page.screenshot({animations:'disabled',path:path.join(out,'failure.png'),fullPage:true});console.error(e.stack);console.error('PAGE ERRORS',errors);process.exitCode=1}finally{await browser.close()}
})();
