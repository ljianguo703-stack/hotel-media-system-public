"""Device group assignment, CSV onboarding and credential lifecycle."""
import csv
import io
import json
import re
import secrets


def core():
    import management
    return management


class DeviceOperations:
    def device_body(self, raw, previous=None):
        m=core();old=previous or {};ident=m.text(raw.get('id'),100,True)
        if not re.fullmatch(r'[A-Za-z0-9_-]+',ident):raise m.Problem('设备标识仅支持字母、数字、下划线和连字符')
        enabled=raw.get('enabled',True)
        if isinstance(enabled,str):
            if enabled.strip().lower() not in ('1','0','true','false','是','否'):raise m.Problem('enabled 请填 true/false 或 1/0')
            enabled=enabled.strip().lower() in ('1','true','是')
        if type(enabled) is not bool:raise m.Problem('设备启用状态无效')
        return {**{k:v for k,v in old.items() if k.startswith('_')},'id':ident,'room':m.text(raw.get('room'),80,True),
                'group':m.text(raw.get('group') or '全部客房',100,True),'enabled':enabled,'model':m.text(raw.get('model',''),150),'version':m.text(raw.get('version',''),80)}

    def devices_import(self, body, actor, revision):
        m=core();raw=m.text(body.get('csv'),800000,True).lstrip('\ufeff')
        try:
            reader=csv.DictReader(io.StringIO(raw));rows=list(reader)
            if not reader.fieldnames or not {'id','room'}<=set(reader.fieldnames):raise ValueError()
        except (ValueError,csv.Error):raise m.Problem('CSV 必须包含 id、room 列，请使用导入模板')
        if not 1<=len(rows)<=500:raise m.Problem('CSV 每次需包含 1–500 行设备')
        overwrite=body.get('overwrite',False)
        if type(overwrite) is not bool:raise m.Problem('更新选项无效')
        result={'ok':[],'failed':[],'credentials':[]};seen=set()
        with self.db() as db:
            self.check_revision(db,revision)
            for index,row in enumerate(rows,2):
                ident=row.get('id') or '第 '+str(index)+' 行'
                try:
                    if ident in seen:raise m.Problem('CSV 内设备标识重复')
                    seen.add(ident)
                    existing=db.execute('SELECT body FROM devices WHERE id=?',(ident,)).fetchone();previous=json.loads(existing['body']) if existing else {}
                    if existing and not overwrite:raise m.Problem('设备已存在；如需更新，请勾选“更新已有设备”')
                    if previous.get('_archived'):raise m.Problem('设备在回收站，请先恢复')
                    item=self.device_body(row,previous)
                    if item['group']!='全部客房':
                        group=self.ensure_named(db,'group',item['group'])
                        if group.get('_archived') or not group.get('enabled',True):raise m.Problem('设备组已停用或归档')
                    item['_updated_at']=m.now_ms()
                    if existing:db.execute('UPDATE devices SET body=? WHERE id=?',(m.encoded(item),item['id']))
                    else:
                        item['_created_at']=m.now_ms();token=secrets.token_urlsafe(32)
                        db.execute('INSERT INTO devices(id,body,token) VALUES(?,?,?)',(item['id'],m.encoded(item),m.device_token_hash(token)))
                        result['credentials'].append({'id':item['id'],'room':item['room'],'token':token})
                    result['ok'].append(item['id'])
                except m.Problem as exc:result['failed'].append({'id':ident,'title':'第 '+str(index)+' 行 / '+str(row.get('room','')),'reason':str(exc)})
            if result['ok']:self.changed(db,actor,'CSV 导入设备 '+str(len(result['ok']))+' 台（失败 '+str(len(result['failed']))+'）',','.join(result['ok']))
        return result

    def devices_batch(self, body, actor, revision):
        m=core();ids=body.get('ids');action=body.get('operation')
        if not isinstance(ids,list) or not 1<=len(ids)<=500 or any(not isinstance(i,str) for i in ids) or len(set(ids))!=len(ids):raise m.Problem('每次请选择 1–500 台设备')
        if action not in ('enable','disable','set_group','rotate','archive'):raise m.Problem('设备批量操作无效')
        if action=='rotate' and body.get('confirm') is not True:raise m.Problem('轮换后旧凭证立即失效，请明确确认')
        result={'ok':[],'failed':[],'credentials':[]}
        with self.db() as db:
            self.check_revision(db,revision)
            target=body.get('value')
            if action=='set_group' and target!='全部客房':
                group=next((g for g in self.objects(db,'group') if g['name']==target and not g.get('_archived') and g.get('enabled',True)),None)
                if not group:raise m.Problem('请选择已启用的设备组')
            for ident in ids:
                item={}
                try:
                    item=self.record(db,'device',ident)
                    if item.get('_archived'):raise m.Problem('设备在回收站，请先恢复')
                    if action=='set_group':item['group']=target
                    elif action=='rotate':
                        token=secrets.token_urlsafe(32);db.execute('UPDATE devices SET token=? WHERE id=?',(m.device_token_hash(token),ident));result['credentials'].append({'id':ident,'room':item['room'],'token':token})
                    elif action=='archive':item.update(_archived=True,_archived_at=m.now_ms(),enabled=False)
                    else:item['enabled']=action=='enable'
                    item['_updated_at']=m.now_ms();self.write_record(db,'device',item);result['ok'].append(ident)
                except m.Problem as exc:result['failed'].append({'id':ident,'title':item.get('room',ident),'reason':str(exc)})
            if result['ok']:self.changed(db,actor,'批量设备'+{'enable':'启用','disable':'停用','set_group':'分组','rotate':'凭证轮换','archive':'归档'}[action]+' '+str(len(result['ok']))+' 台（失败 '+str(len(result['failed']))+'）',','.join(result['ok']))
        return result

    def device_detail(self, ident):
        m=core()
        with self.db() as db:
            item=self.record(db,'device',ident)
            live=db.execute('SELECT last_seen,ip,last_event FROM devices WHERE id=?',(ident,)).fetchone()
            events=[{'received':r['received'],**{k:v for k,v in json.loads(r['body']).items() if not k.startswith('_')}} for r in db.execute('SELECT received,body FROM events WHERE device=? ORDER BY received DESC LIMIT 30',(ident,))]
            counts=db.execute('SELECT count(*) AS events FROM events WHERE device=?',(ident,)).fetchone()['events']
        return {'device':item,'connection':dict(live),'events':events,'event_count':counts,'online':bool(item['enabled'] and live['last_seen'] and m.now_ms()-live['last_seen']<120000)}

    def devices_csv(self):
        out=io.StringIO();writer=csv.writer(out);writer.writerow(['id','room','group','enabled','model','version'])
        with self.db() as db:
            for row in db.execute('SELECT body FROM devices'):
                d=json.loads(row['body'])
                if d.get('_archived'):continue
                values=[str(d.get(k,'')) for k in ('id','room','group','enabled','model','version')]
                writer.writerow(["'"+v if v.startswith(('=','+','-','@','\t','\r')) else v for v in values])
        return out.getvalue()
