/* Disposable loopback data and synthetic edge-marked artwork only. */
const {chromium}=require('playwright-core'),fs=require('fs'),path=require('path'),assert=require('node:assert/strict');
(async()=>{
const root=path.resolve(__dirname,'..'),info=JSON.parse(fs.readFileSync(path.join(root,'artifacts/management/AD08/dev.json')));assert.equal(new URL(info.url).hostname,'127.0.0.1');
const out=path.join(root,'artifacts/management/AD15');fs.mkdirSync(out,{recursive:true});
const browser=await chromium.launch(require("./browser_options.cjs")),page=await browser.newPage({viewport:{width:1440,height:900}}),errors=[];
page.on('pageerror',e=>errors.push(e.message));
const settle=async()=>{await page.waitForFunction(()=>{const a=Alpine.$data(document.body);return a.user&&!a.busy&&!a.opsLoading&&!a.uploading});await page.evaluate(()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r))))};
const card=()=>page.locator('.ad-card-editor .sponsor-card-preview');
const metrics=()=>card().evaluate(el=>{const a=Alpine.$data(el.closest('.sponsor-preview-frame')),t=el.querySelector('.sponsor-text-track'),q=el.querySelector('.sponsor-qr');return {distance:a.distance,seconds:a.readingSeconds,duration:Alpine.$data(document.body).edit.duration,fit:getComputedStyle(el.querySelector('.sponsor-main')).objectFit,animation:t.getAnimations().length,qr:{x:q.getBoundingClientRect().x,y:q.getBoundingClientRect().y},text:t.innerText}});
const seek=async end=>{await card().evaluate((el,end)=>{const animation=el.querySelector('.sponsor-text-track').getAnimations()[0];if(animation){animation.pause();animation.currentTime=end?Number(animation.effect.getTiming().duration)-1000:0}},end);await page.evaluate(()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r))))};
try{
await page.goto(info.url+'/?view=ads');await page.locator('#username').fill('admin');await page.locator('#password').fill('ad08-local-test');await page.getByRole('button',{name:'登录后台',exact:true}).click();await page.locator('#ad-workflow-title').waitFor();await settle();
await page.getByRole('button',{name:'制作广告',exact:true}).click();await page.locator('#ad-name').fill('完整展示与滚动');await page.locator('#card-title').fill('远山餐厅 · 中文 English မြန်မာ');
await page.locator('#card-body').fill(('欢迎来到远山餐厅，美食与旅途相伴。English မြန်မာ\n').repeat(30).slice(0,499));await page.locator('#card-contact').fill(('电话 12345678 · 前台预约 · https://example.com/contact\n').repeat(10).slice(0,280)+'联系方式末尾 END');await page.locator('#card-link').fill('https://example.com/front');
await page.locator('#card-image').setInputFiles(path.join(info.data,'media/portrait.png'));await settle();await page.getByRole('button',{name:'保存草稿',exact:true}).click();await settle();
await page.getByRole('button',{name:'编辑广告 完整展示与滚动',exact:true}).click();await settle();await card().scrollIntoViewIfNeeded();await page.waitForFunction(()=>{const el=document.querySelector('.ad-card-editor .sponsor-preview-frame');return el&&Alpine.$data(el).distance>0});
let m=await metrics();assert.equal(m.fit,'contain');assert(m.animation>0);assert(m.text.includes('联系方式末尾 END'));assert.equal(m.duration,15);assert(m.seconds>15);console.log('PASS actual upload/save/reopen; full copy retained, portrait fit, real slow animation; existing duration unchanged');
await page.getByRole('button',{name:'采用建议时长',exact:true}).click();assert.equal((await metrics()).duration,m.seconds);console.log('PASS longer duration requires explicit operator action');
for(const [width,height]of[[390,844],[1280,720],[1440,900]]){
  await page.setViewportSize({width,height});await card().scrollIntoViewIfNeeded();await settle();await seek(false);const before=await metrics();
  await page.screenshot({animations:'allow',path:path.join(out,`copy-start-${width}x${height}.png`)});await seek(true);const after=await metrics();assert.deepEqual(after.qr,before.qr);
  const end=await card().evaluate(el=>{const t=el.querySelector('.sponsor-text-track'),w=el.querySelector('.sponsor-text-window');return {bottom:t.getBoundingClientRect().bottom,windowBottom:w.getBoundingClientRect().bottom,transform:getComputedStyle(t).transform,clamp:getComputedStyle(t.querySelector('b')).webkitLineClamp}});
  assert(Math.abs(end.bottom-end.windowBottom)<2);assert.notEqual(end.transform,'none');assert.equal(end.clamp,'none');
  await page.screenshot({animations:'allow',path:path.join(out,`copy-end-${width}x${height}.png`)});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);assert.equal(await page.locator('.real-drawer:visible .drawer-body').evaluate(e=>e.scrollWidth>e.clientWidth+1),false);
}
console.log('PASS actual end-of-cycle transform exposes final contact line; QR stays fixed; six screenshots and no horizontal overflow');
// Every source-edge marker must appear in the rendered image, not merely in its uploaded file.
await page.setViewportSize({width:1440,height:900});
for(const name of ['portrait.png','landscape.png','long.png']){
  await page.locator('#card-image').setInputFiles(path.join(info.data,'media',name));await settle();await card().scrollIntoViewIfNeeded();
  const img=card().locator('.sponsor-main');await img.evaluate(e=>e.decode());const png=await img.screenshot();
  const result=await page.evaluate(async({data,nw,nh})=>{const image=new Image();image.src='data:image/png;base64,'+data;await image.decode();const canvas=document.createElement('canvas');canvas.width=image.width;canvas.height=image.height;const c=canvas.getContext('2d');c.drawImage(image,0,0);const s=Math.min(image.width/nw,image.height/nh),w=nw*s,h=nh*s,x=(image.width-w)/2,y=(image.height-h)/2;return [[x+w/2,y+6*s],[x+6*s,y+h/2],[x+w-6*s,y+h/2],[x+w/2,y+h-6*s]].map(([px,py])=>Array.from(c.getImageData(Math.floor(px),Math.floor(py),1,1).data).slice(0,3))},{data:png.toString('base64'),nw:name==='landscape.png'?960:360,nh:name==='landscape.png'?540:name==='long.png'?1200:640});
  const expected=[[255,80,0],[220,40,40],[50,200,80],[40,90,230]];assert(result.every((rgb,i)=>rgb.every((v,j)=>Math.abs(v-expected[i][j])<35)),JSON.stringify({name,result}));
}
console.log('PASS rendered pixel markers prove all four edges preserved for landscape, portrait and long artwork');
for(const [color,expected]of[['#ffffff','rgb(11, 17, 24)'],['#000000','rgb(248, 250, 252)'],['#ffd34f','rgb(11, 17, 24)']]){await page.locator('#card-accent').fill(color);await settle();assert.equal(await card().locator('strong').evaluate(e=>getComputedStyle(e).color),expected)}
console.log('PASS light, dark and yellow card backgrounds automatically select contrasting text');
await page.goto(info.url+'/?view=operations&tab=home');await page.locator('.home-preview.bg').waitFor();await settle();
await page.locator('label.upload-label').filter({hasText:'上传背景图'}).locator('input[type=file]').setInputFiles(path.join(info.data,'media/portrait.png'));await page.waitForFunction(()=>!Alpine.$data(document.body).homeUploading);await settle();
assert.equal(await page.locator('.home-preview.bg').evaluate(e=>getComputedStyle(e).backgroundSize),'contain');assert(await page.getByText('建议使用清晰横图；原图完整显示、允许留边。时间和店名为透明底白字细黑描边，二维码有独立对比底板；海报里的文字保留原色。',{exact:true}).isVisible());
await page.getByRole('button',{name:'保存首页设置',exact:true}).click();await settle();const home=await page.evaluate(()=>Alpine.$data(document.body).homeDraft);assert(home.background.startsWith('/media/'));
for(const [width,height]of[[390,844],[1280,720],[1440,900]]){await page.setViewportSize({width,height});await page.locator('.home-preview.bg').scrollIntoViewIfNeeded();await settle();await page.screenshot({animations:'disabled',path:path.join(out,`home-editor-${width}x${height}.png`)});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false)}
console.log('PASS homepage background uploaded/saved through real UI; complete-image preview and current guidance; three homepage editor sizes');
assert.deepEqual(errors,[]);console.log('AD15 REAL HTTP VISUAL E2E PASS');
}catch(e){await page.screenshot({animations:'disabled',path:path.join(out,'failure.png'),fullPage:true});console.error(e.stack);console.error('PAGE ERRORS',errors);process.exitCode=1}
finally{await browser.close()}
})();
