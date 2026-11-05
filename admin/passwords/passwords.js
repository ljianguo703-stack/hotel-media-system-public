/* Cockpit owns Ubuntu authentication, HTTPS, origin validation and root authorization. */
'use strict';
(() => {
  const byId=id=>document.getElementById(id);
  let state=null,busy=false;
  const fields=new Map();
  function note(text,error=false){byId('notice').textContent=text;byId('notice').classList.toggle('error',error)}
  function setBusy(value){busy=value;byId('save').disabled=busy||!state;byId('refresh').disabled=busy;if(busy)byId('save').textContent='正在处理…';else count();for(const f of fields.values())for(const input of [f.password,f.confirm])input.disabled=busy||!f.installed}
  function call(body){
    if(!window.cockpit)return Promise.reject(Error('请从后台进入 Ubuntu 验证页面，或在 Cockpit 打开“酒店密码管理”'));
    // stdin avoids command-line/history exposure. No Ubuntu password reaches this page.
    return new Promise((resolve,reject)=>{
      cockpit.spawn(['/usr/bin/python3','/usr/local/lib/hotel-passwords/manage.py'],{superuser:'require',err:'message'})
        .input(JSON.stringify(body))
        .done(raw=>{try{const result=JSON.parse(raw);result.error?reject(Error(result.error)):resolve(result)}catch{reject(Error('服务器结果无法读取，请检查本次修改状态'))}})
        .fail(()=>reject(Error('请先在页面顶部的“受限访问 / Limited access”中开启管理权限，再点击“验证并加载”；连接中断时请重新登录 Ubuntu')));
    });
  }
  function element(tag,cls,text){const el=document.createElement(tag);if(cls)el.className=cls;if(text!==undefined)el.textContent=text;return el}
  function render(data){
    fields.clear();byId('services').replaceChildren();
    for(const s of data.services){
      const row=element('section','service-row'+(s.installed?'':' unavailable'));
      const info=element('div','service-info');info.append(element('h3','service-title',s.name));
      if(s.id==='admin'){
        const select=element('select','service-account');select.id='admin-account';select.setAttribute('aria-label','后台账号');
        for(const user of data.users.filter(u=>u.enabled)){const option=element('option','',user.name+' · '+({admin:'管理员',operator:'运营',viewer:'只读'}[user.role]||user.role));option.value=user.name;select.append(option)}
        if([...select.options].some(o=>o.value==='admin'))select.value='admin';info.append(select);
      }else info.append(element('p','service-meta',!s.installed?'尚未安装':s.id==='tv_pin'?'全店维护 · 6 位数字':s.account?'账号 '+s.account:'后台连接密钥'));
      row.append(info);const pair={installed:s.installed};
      for(const [key,label]of[['password',s.id==='tv_pin'?'新 PIN':'新密码'],['confirm','再次输入']]){
        const wrap=element('div','field'),id='pw-'+s.id+'-'+key,l=element('label','field-label',label);l.htmlFor=id;
        const input=element('input');input.id=id;input.type='password';input.autocomplete='new-password';input.maxLength=s.id==='tv_pin'?6:128;input.placeholder=s.installed?(s.id==='tv_pin'?'留空保持原 PIN':'留空保持原密码'):'未安装';input.disabled=!s.installed;
        if(s.id==='tv_pin'){input.inputMode='numeric';input.pattern='[0-9]{6}'}else input.minLength=12;
        input.addEventListener('input',()=>{input.setCustomValidity('');count()});wrap.append(l,input);
        if(key==='password'&&s.id==='tv_pin')wrap.append(element('p','field-note','新版电视刷新后同步，离线期间保留原 PIN。'));
        pair[key]=input;row.append(wrap);
      }
      pair.error=element('p','row-error');pair.error.hidden=true;pair.row=row;row.append(pair.error);
      fields.set(s.id,pair);byId('services').append(row);
    }count();
  }
  function count(){const n=[...fields.values()].filter(f=>f.password.value||f.confirm.value).length;byId('count').textContent=n?'已填写 '+n+' 项':'留空的项目不修改';if(!busy)byId('save').textContent=n?'统一保存 '+n+' 项':'统一保存'}
  async function load(){if(busy)return;if(state&&[...fields.values()].some(f=>f.password.value||f.confirm.value)){note('请先保存或清空已填写的密码，再重新加载。',true);return}setBusy(true);note('正在验证 Ubuntu 管理权限…');try{const next=await call({action:'status'});state=next;render(next);note('已授权，可直接重置各应用密码。');byId('identity').textContent='Ubuntu 已授权';byId('authority-title').textContent='Ubuntu 管理权限已验证';byId('authority').classList.add('authorized');byId('refresh').textContent='重新验证'}catch(e){note(e.message,true)}finally{setBusy(false)}}
  async function save(event){
    event.preventDefault();if(busy||!state)return;const passwords={};
    for(const [id,f]of fields){
      const p=f.password.value,c=f.confirm.value;if(!p&&!c)continue;
      if(!f.installed)continue;
      if(p!==c){f.confirm.setCustomValidity('两次输入不一致');f.confirm.reportValidity();return}
      if(id==='tv_pin'?!/^[0-9]{6}$/.test(p):p.length<12||p.length>128){f.password.setCustomValidity(id==='tv_pin'?'请输入 6 位数字':'密码需为 12–128 个字符');f.password.reportValidity();return}
      if(p!==p.trim()||/[\x00-\x1f\x7f]/.test(p)){f.password.setCustomValidity('密码首尾不能有空格，也不能含控制字符');f.password.reportValidity();return}
      passwords[id]=p;
    }
    if(!Object.keys(passwords).length){note('请至少填写一项新密码。',true);return}
    setBusy(true);note('正在逐项修改，请保持页面打开…');byId('results').hidden=true;
    try{
      const result=await call({action:'save',passwords,admin_username:byId('admin-account').value});
      byId('result-list').replaceChildren();const failed=[];
      for(const item of result.results){
        const li=element('li',item.ok?'ok':'failed');li.append(element('strong','',(item.ok?'✓ ':'! ')+item.name),document.createTextNode(item.message));byId('result-list').append(li);
        const f=fields.get(item.id);f.error.hidden=item.ok;f.row.classList.toggle('has-error',!item.ok);f.error.textContent=item.ok?'':item.message+'；已保留输入，可直接重试。';
        if(item.ok){f.password.value='';f.confirm.value=''}else failed.push(item);
      }
      byId('results').hidden=false;note(result.ok?'已完成本次保存。':failed.map(i=>i.name).join('、')+'未完成，成功项已清空，可重试保留的项目。',!result.ok);count();
      const target=failed.length?fields.get(failed[0].id).row:byId('results');target.scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth',block:'nearest'});
    }catch(e){note(e.message+'；请核对结果后重试，已输入内容暂时保留。',true)}finally{for(const key of Object.keys(passwords))delete passwords[key];setBusy(false)}
  }
  byId('password-form').addEventListener('submit',save);byId('refresh').addEventListener('click',load);
  window.addEventListener('pagehide',()=>{for(const f of fields.values()){f.password.value='';f.confirm.value=''}});
  load();
})();
