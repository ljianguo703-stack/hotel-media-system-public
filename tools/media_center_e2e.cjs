// 1.1.0 browser acceptance for the media automation center against tools/media_center_mock.py.
const {chromium}=require('playwright-core');const fs=require('fs'),path=require('path'),assert=require('assert');
const out=path.join(__dirname,'..','local/evidence/V110');const info=JSON.parse(fs.readFileSync(path.join(out,'mock-info.json')));
(async()=>{const browser=await chromium.launch(require("./browser_options.cjs"));const result={errors:[],widths:{}};
  for(const [w,h] of [[390,844],[1280,720],[1440,900]]){
    const page=await browser.newPage({viewport:{width:w,height:h}});page.on('pageerror',e=>result.errors.push(String(e)));page.on('console',m=>{if(m.type()==='error'&&!m.location().url.endsWith('/favicon.ico'))result.errors.push(m.text())});
    await page.goto(info.url+'/?view=downloads');await page.fill('input[type=password]','v110-mock-test-only');await page.click('button[type=submit]');
    await page.waitForSelector('.mc-page h2:has-text("需要处理")');await page.waitForSelector('.mc-issue',{timeout:30000});
    await page.getByText('自动校时 仅低匹配',{exact:true}).waitFor();
    const workflow=page.getByRole('region',{name:'下载进度与搜索源'});
    assert(await workflow.isVisible());
    const links={};
    for(const [label,port,pathname] of [['下载进度与控制 ↗','8098','/'],['加片请求记录 ↗','5055','/requests'],['Jackett 资源搜索（需配置站点）↗','9117','/UI/Dashboard'],['Prowlarr 资源搜索 ↗','9696','/search'],['电影下载队列 ↗','7878','/activity/queue'],['剧集下载队列 ↗','8989','/activity/queue']]){
      const a=workflow.getByRole('link',{name:label,exact:true});assert(await a.isVisible());
      const href=await a.getAttribute('href'),url=new URL(href);assert.equal(url.port,port);assert.equal(url.pathname,pathname);
      assert.equal(await a.getAttribute('target'),'_blank');assert((await a.getAttribute('rel')).includes('noopener'));links[label]=href;
    }
    assert(await page.getByRole('link',{name:'查看下载进度 ↗',exact:true}).isVisible());
    const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth+1);
    await page.screenshot({path:path.join(out,`center-${w}.png`),fullPage:true});
    result.widths[w]={overflow,workflow_links:links,issues:await page.$$eval('.mc-issue h3',n=>n.map(x=>x.textContent.trim()))};
    await page.waitForSelector('.mc-add-item');result.widths[w].add_items=await page.$$eval('.mc-add-item .mc-add-main strong',n=>n.map(x=>x.textContent.trim()));
    await page.getByText('将按下列顺序连续编号，请核对内容：',{exact:true}).waitFor();
    assert(!await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),'file confirmation overflow '+w);
    await page.screenshot({path:path.join(out,`file-confirmation-${w}.png`),fullPage:true});
    if(w===390){
      await page.click('.mc-add-item >> text=选择片名');await page.waitForSelector('.mc-pick');
      result.widths[w].picker_overflow=await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth+1);
      await page.screenshot({path:path.join(out,'picker-390.png')});await page.click('.dialog-close');
    }
    if(w===1440){
      const movie=page.locator('.mc-add-item').filter({hasText:'多视频正片确认'});
      await movie.locator('select').selectOption('资源文件夹/Second.1080p.mkv');await movie.getByRole('button',{name:'确认文件并继续入库',exact:true}).click();
      await movie.getByRole('button',{name:'确认文件并继续入库',exact:true}).waitFor({state:'hidden'});
      const series=page.locator('.mc-add-item').filter({hasText:'分集顺序确认'});
      await series.getByLabel('季号',{exact:true}).fill('2');await series.getByLabel('起始集号',{exact:true}).fill('4');
      page.once('dialog',dialog=>dialog.accept());await series.getByRole('button',{name:'确认文件并继续入库',exact:true}).click();
      await series.getByRole('button',{name:'确认文件并继续入库',exact:true}).waitFor({state:'hidden'});result.file_and_episode_confirmation=true;
      await page.click('text=修复下载器连接');await page.waitForFunction(()=>![...document.querySelectorAll('.mc-issue h3')].some(n=>n.textContent.includes('连不上下载器')),null,{timeout:30000});
      result.after_repair=await page.$$eval('.mc-issue h3',n=>n.map(x=>x.textContent.trim()));
      await page.check('input[name=mc-hevc][value=prefer_h264]');await page.click('text=保存片源规则');await page.waitForSelector('text=片源规则已改为',{timeout:15000});
      await page.fill('#mc-assrt','v110-mock-assrt-token');await page.click('text=保存字幕源');await page.waitForSelector('text=射手网 已启用',{timeout:15000});
      result.assrt_field_cleared=(await page.inputValue('#mc-assrt'))==='';
      await page.getByRole('button',{name:'启用中文默认规则与自动校时',exact:true}).click();
      await page.getByText('中文默认规则 已配置',{exact:true}).waitFor();
      await page.getByText('自动校时 已启用',{exact:true}).waitFor();
      assert.equal(await page.getByText('自动校时 仅低匹配',{exact:true}).count(),0);
      await page.getByRole('button',{name:'搜索缺失字幕',exact:true}).click();
      await page.getByText('已交给 Bazarr 后台搜索；是否找到匹配字幕请查看待补数量和原生任务结果',{exact:true}).waitFor();
      result.native_subtitle_policy_and_search=true;
      await page.screenshot({path:path.join(out,'center-1440-after.png'),fullPage:true});
      await page.click('.mc-add-item >> text=选择片名');await page.waitForSelector('.mc-pick');result.candidates=await page.$$eval('.mc-pick strong',n=>n.map(x=>x.textContent.trim()));
      await page.screenshot({path:path.join(out,'picker-1440.png')});
      await page.click('.mc-pick >> nth=1');await page.waitForSelector('text=已确认',{timeout:15000});
      result.after_match=await page.$$eval('.mc-add-item .tag',n=>n.map(x=>x.textContent.trim()));
      await page.fill('#mc-link','magnet:?xt=urn:btih:'+'4'.repeat(40)+'&dn=Inception.2010.1080p');await page.click('text=开始下载');await page.waitForSelector('text=已开始下载',{timeout:15000});
      result.link_field_cleared=(await page.inputValue('#mc-link'))==='';result.task_count=await page.$$eval('.mc-add-item',n=>n.length);
      const mark=result.errors.length;await page.fill('#mc-link','ed2k://|file|a.mkv|1|ABC|/');await page.click('text=开始下载');await page.waitForSelector('text=ed2k 链接需要电驴下载器',{timeout:15000});result.ed2k_refused=true;
      const extra=result.errors.splice(mark);result.expected_refusals=extra.filter(e=>e.includes('status of 400')).length;result.errors.push(...extra.filter(e=>!e.includes('status of 400')));   // the refusal itself is an HTTP 400
      await page.evaluate(()=>document.getElementById('mc-add').scrollIntoView());await page.screenshot({path:path.join(out,'add-1440.png')});
      await page.click('nav >> text=待上架');await page.waitForSelector('.media-tile');
      await page.click('.media-tile >> text=改片名');await page.waitForSelector('.mc-pick');await page.click('.mc-pick >> text=流浪地球2');
      await page.waitForSelector('text=正在改为《流浪地球2》',{timeout:15000});await page.waitForSelector('.media-tile >> text=资料更新中',{timeout:15000});result.renamed=true;
      await page.screenshot({path:path.join(out,'pending-rename-1440.png')});
      await page.click('nav >> text=运营概览');await page.waitForSelector('.mc-overview');
      result.overview=await page.textContent('.mc-overview .panel-head');await page.screenshot({path:path.join(out,'overview-1440.png')});
      result.nav=await page.$$eval('nav .nav-item',n=>n.map(x=>x.textContent.trim()));
    }
    await page.close();}
  await browser.close();fs.writeFileSync(path.join(out,'center-e2e.json'),JSON.stringify(result,null,1));console.log(JSON.stringify(result,null,1));
  assert.deepEqual(result.errors,[]);assert(Object.values(result.widths).every(x=>!x.overflow&&!x.picker_overflow));
  assert(result.renamed&&result.ed2k_refused&&result.assrt_field_cleared&&result.link_field_cleared&&result.native_subtitle_policy_and_search);
})().catch(e=>{console.error(e);process.exit(1)});
