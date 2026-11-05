/* Only a disposable loopback fixture: no business campaigns or television access. */
const {chromium}=require('playwright-core'),fs=require('fs'),path=require('path'),assert=require('node:assert/strict');
(async()=>{
const root=path.resolve(__dirname,'..'),info=JSON.parse(fs.readFileSync(path.join(root,'artifacts/management/AD08/dev.json')));assert.equal(new URL(info.url).hostname,'127.0.0.1');
const out=path.join(root,'artifacts/management/AD14');fs.mkdirSync(out,{recursive:true});
const browser=await chromium.launch(require("./browser_options.cjs")),page=await browser.newPage({viewport:{width:1440,height:900}}),errors=[];let saves=0;
page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(r.method()==='POST'&&r.url().includes('/campaign-bundles/'))saves++});
const settle=async()=>{await page.waitForFunction(()=>{const a=Alpine.$data(document.body);return a.user&&!a.busy&&!a.opsLoading&&!a.uploading});await page.evaluate(()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r))))};
const tab=n=>page.getByRole('tab',{name:new RegExp(['','一档','二档','三档'][n])}).click();
const state=()=>page.evaluate(()=>{const a=Alpine.$data(document.body);return {active:a.edit.tier,modal:a.modal,issue:a.adValidation,pending:!!a.pendingConfirm,revision:a.data.revision,rows:a.data.campaigns.filter(c=>c._bundle_name==='AD14 组合修复')}});
try{
await page.goto(info.url+'/?view=ads');await page.locator('#username').fill('admin');await page.locator('#password').fill('ad08-local-test');await page.getByRole('button',{name:'登录后台',exact:true}).click();await page.locator('#ad-workflow-title').waitFor();await settle();
await page.getByRole('button',{name:'制作广告',exact:true}).click();await page.locator('#ad-name').fill('AD14 组合修复');await page.locator('#ad-merchant').selectOption({label:'青禾餐厅'});await page.locator('#card-image').setInputFiles(path.join(info.data,'media/sponsor.png'));await settle();
await page.getByRole('checkbox',{name:/二档.*档位/}).click();await page.locator('#ad-text').fill('全天优惠 · 已填内容需保留');
await page.getByRole('button',{name:'发布到电视',exact:true}).click();await settle();let s=await state();assert.equal(s.active,1);assert.equal(s.issue.message,'一档全屏：赞助商标题不能为空');assert.equal(s.pending,false);assert.equal(saves,0);assert.equal(await page.locator('#card-title').evaluate(e=>e===document.activeElement),true);assert.equal(await page.locator('#card-title').getAttribute('aria-invalid'),'true');assert(await page.locator('.ad-material-thumb').first().isVisible());
console.log('PASS empty hidden title selects full-screen tier and focuses exact field before any request; uploaded image retained');
for(const [width,height]of[[390,844],[1280,720],[1440,900]]){
  await page.setViewportSize({width,height});await page.locator('#ad-validation').getByRole('button',{name:'定位填写',exact:true}).click();await settle();
  assert.equal(await page.locator('#ad-validation').isVisible(),true);assert.equal(await page.locator('#card-title').isVisible(),true);
  assert.equal(await page.locator('.real-drawer:visible .drawer-body').evaluate(e=>e.scrollWidth>e.clientWidth+1),false);assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
  await page.screenshot({animations:'disabled',path:path.join(out,`field-error-${width}x${height}.png`)});
}
// The message stays in the form after the global five-second toast lifetime.
await page.waitForTimeout(5200);assert.equal(await page.locator('#ad-validation span').innerText(),'一档全屏：赞助商标题不能为空');
await page.locator('#card-title').fill('青禾餐厅');await tab(2);assert.equal(await page.locator('#ad-text').inputValue(),'全天优惠 · 已填内容需保留');await page.locator('#ad-text').fill('');await tab(1);await page.getByRole('button',{name:'保存草稿',exact:true}).click();await settle();s=await state();assert.equal(s.active,2);assert.equal(s.issue.message,'二档全天文字：滚动文字不能为空');assert.equal(saves,0);assert.equal(await page.locator('#ad-text').evaluate(e=>e===document.activeElement),true);
console.log('PASS empty ticker selects second tier; persistent error remains readable; no partial writes');
await page.locator('#ad-text').fill('全天优惠 · 已填内容需保留');await tab(1);assert.equal(await page.locator('#card-title').inputValue(),'青禾餐厅');await page.getByRole('button',{name:'发布到电视',exact:true}).click();await page.getByRole('button',{name:'确认执行',exact:true}).click();await settle();s=await state();assert.equal(s.modal,null);assert.equal(saves,1);assert.equal(s.rows.length,2);assert(s.rows.every(r=>r.status==='投放中'));assert.equal(s.rows.find(r=>r.tier===1).text,'');assert.equal(s.rows.find(r=>r.tier===2).text,'全天优惠 · 已填内容需保留');
console.log('PASS one confirmed publish persists both tiers; full-screen does not require scrolling text');
// Call the real API with invalid hidden material to exercise authoritative server validation and atomic rollback.
const result=await page.evaluate(async()=>{const a=Alpine.$data(document.body),rows=a.data.campaigns.filter(c=>c._bundle_name==='AD14 组合修复'),bad=JSON.parse(JSON.stringify(rows));bad.find(c=>c.tier===1).creative_card.contact='x'.repeat(301);const response=await fetch('/api/admin/campaign-bundles/'+rows[0]._bundle_id,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':a.user.csrf},body:JSON.stringify({name:'AD14 组合修复',items:bad,status:'投放中',revision:a.data.revision})});const body=await response.json();const after=await (await fetch('/api/admin/state')).json();return {status:response.status,body,revision:after.revision,rows:after.campaigns.filter(c=>c._bundle_name==='AD14 组合修复')}});
assert.equal(result.status,400);assert.match(result.body.error,/一档全屏：联系方式 \/ 地址最多300个字符/);assert.equal(result.revision,s.revision);assert.deepEqual(result.rows,s.rows);
console.log('PASS real API names overlong card field; both published tiers and revision stay unchanged');
assert.deepEqual(errors,[]);console.log('PASS three screenshots / no horizontal overflow / zero page errors');console.log('AD14 REAL HTTP VALIDATION E2E PASS');
}catch(e){await page.screenshot({animations:'disabled',path:path.join(out,'failure.png'),fullPage:true});console.error(e);console.error('PAGE ERRORS',errors);console.error('UI STATE',await state());process.exitCode=1}
finally{await browser.close()}
})();
