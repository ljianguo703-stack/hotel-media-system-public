"""Management lifecycle and operator workflows. Original media files are never removed."""
import hashlib
import json
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import unquote

LABELS = {'home_card':'首页菜单卡片','media':'影片','campaign':'广告任务','device':'设备','category':'分类','source':'来源',
          'advertiser':'广告主','group':'设备组','asset':'素材','backup':'备份','user':'后台账号','package':'广告套餐','order':'投放单','guide':'指南卡片','topic':'专题','channel':'频道'}
TABLES = {'media':'media','campaign':'campaigns','device':'devices','category':'categories','source':'sources'}

def core():
    import management
    return management


from media_cleanup import MediaCleanupOperations
from operations_ads import AdvertisingOperations
from operations_devices import DeviceOperations
from operations_system import SystemOperations
from operations_reports import ReportingOperations
from operations_orders import OrderOperations
from operations_control import ControlOperations
from operations_home import HomeOperations
from operations_compat import CompatOperations
from operations_channels import ChannelOperations
from operations_downloads import DownloadOperations


class OperationsMixin(MediaCleanupOperations, AdvertisingOperations, DeviceOperations, SystemOperations, ReportingOperations, OrderOperations, ControlOperations, HomeOperations, CompatOperations, ChannelOperations, DownloadOperations):
    def initialize_operations(self, db):
        db.execute('CREATE INDEX IF NOT EXISTS events_received_idx ON events(received)')
        db.execute('CREATE TABLE IF NOT EXISTS managed_objects(kind TEXT NOT NULL,id TEXT NOT NULL,body TEXT NOT NULL,PRIMARY KEY(kind,id))')
        from software_store import initialize as initialize_software
        initialize_software(db)
        self.initialize_home_menu(db)
        self.initialize_guest_requests(db)
        db.execute("INSERT OR IGNORE INTO kv VALUES('management_policy',?)", (json.dumps({'trash_days':30,'cleanup_enabled':True}),))
        # Existing names remain the public contract; register them without changing the business revision.
        for table, kind, key in [('devices','group','group'),('campaigns','group','group'),('campaigns','advertiser','merchant')]:
            for row in db.execute('SELECT body FROM '+table).fetchall():
                name=json.loads(row['body']).get(key,'')
                if name and name!='全部客房': self.ensure_named(db,kind,name)

    @staticmethod
    def objects(db, kind):
        return [json.loads(r['body']) for r in db.execute('SELECT body FROM managed_objects WHERE kind=? ORDER BY rowid',(kind,))]

    def ensure_named(self, db, kind, name):
        old=next((i for i in self.objects(db,kind) if i['name']==name),None)
        if old: return old
        item={'id':kind+'-'+secrets.token_hex(8),'name':name,'enabled':True,'notes':''}
        self.put_object(db,kind,item);return item

    @staticmethod
    def put_object(db, kind, item):
        db.execute('INSERT OR REPLACE INTO managed_objects VALUES(?,?,?)',(kind,item['id'],core().encoded(item)))

    def record(self, db, kind, ident):
        if kind=='backup':return self.backup_record(db,ident)
        if kind=='user':
            record=next((u for u in self.user_rows(db) if u['name']==ident),None)
            if not record:raise core().Problem('账号不存在',404)
            extra=next((u for u in self.objects(db,'user') if u['id']==ident),{})
            return {**record,**{k:v for k,v in extra.items() if k.startswith('_')},'id':ident,'name':ident}
        if kind in TABLES:
            row=db.execute('SELECT body FROM '+TABLES[kind]+' WHERE id=?',(ident,)).fetchone()
        else: row=db.execute('SELECT body FROM managed_objects WHERE kind=? AND id=?',(kind,ident)).fetchone()
        if not row: raise core().Problem(LABELS.get(kind,kind)+'不存在',404)
        return json.loads(row['body'])

    def write_record(self, db, kind, item):
        if kind=='user':
            db.execute('UPDATE users SET enabled=? WHERE name=?',(int(item['enabled']),item['id']))
            db.execute('DELETE FROM sessions WHERE name=?',(item['id'],))
        if kind in TABLES: db.execute('UPDATE '+TABLES[kind]+' SET body=? WHERE id=?',(core().encoded(item),item['id']))
        else: self.put_object(db,kind,item)

    def references(self, db, kind, item):
        refs=[]
        if kind in ('category','source'):
            for row in db.execute('SELECT source,body FROM media'):
                media=json.loads(row['body'])
                if (kind=='category' and media.get('category')==item['name']) or (kind=='source' and row['source']==item['id']): refs.append(media.get('title',media['id']))
        elif kind in ('advertiser','group','asset'):
            for table in (('devices','campaigns') if kind=='group' else ('campaigns',)):
                for row in db.execute('SELECT body FROM '+table):
                    body=json.loads(row['body'])
                    card_paths = [(body.get('creative_card') or {}).get(k, '') for k in ('image','logo','qr')]
                    match=(body.get('group')==item['name'] if kind=='group' else body.get('merchant')==item['name'] if kind=='advertiser' else unquote(item.get('path','')) in [unquote(p) for p in [body.get('path',''),*card_paths] if p])
                    if match: refs.append(body.get('name') or body.get('room') or body['id'])
        if kind in ('advertiser','package'):
            key='advertiser_id' if kind=='advertiser' else 'package_id'
            refs+=['投放单 '+o.get('code',o['id']) for o in self.objects(db,'order') if o.get(key)==item['id']]
        if kind in ('asset', 'campaign'):
            key = 'asset_id' if kind == 'asset' else 'campaign_id'
            refs += ['频道 '+c['name'] for c in self.objects(db,'channel') if any(r.get(key)==item['id'] for r in c.get('inserts',[]))]
        return refs

    def managed_state(self, db):
        result={}
        for kind,key in [('advertiser','advertisers'),('group','device_groups'),('asset','assets')]:
            rows=self.objects(db,kind)
            for item in rows: item['references']=len(self.references(db,kind,item))
            result[key]=rows
        packages=self.objects(db,'package')
        for item in packages: item['references']=len(self.references(db,'package',item))
        result['ad_packages']=packages;result['billing']=self.billing(db)
        result['management_policy']=self.kv(db,'management_policy')
        result['trash_count']=len(self.trash_rows(db))
        maintenance=db.execute("SELECT value FROM kv WHERE key='maintenance_status'").fetchone()
        result['maintenance']=json.loads(maintenance[0]) if maintenance else {}
        return result

    @staticmethod
    def archived(kind, item):
        return bool(item.get('_archived')) or (kind=='campaign' and item.get('status')=='已归档')

    def trash_rows(self, db):
        rows=[];days=self.kv(db,'management_policy')['trash_days']
        for kind in LABELS:
            items=[json.loads(r['body']) for r in db.execute('SELECT body FROM '+TABLES[kind])] if kind in TABLES else self.objects(db,kind)
            for item in items:
                if not self.archived(kind,item): continue
                at=item.get('_archived_at',0)
                rows.append({'id':item['id'],'kind':kind,'type':LABELS[kind],'name':item.get('title') or item.get('name') or item.get('room') or item['id'],
                             'archived_at':at,'expires_at':at+days*86400000 if at else 0,'references':len(self.references(db,kind,item))})
        return sorted(rows,key=lambda i:i['archived_at'],reverse=True)

    def save_entity(self, kind, body, actor, revision):
        m=core()
        if kind not in ('advertiser','group','asset'): raise m.Problem('此对象请使用专用编辑入口')
        ident=m.text(body.get('id'),100,True)
        if not re.fullmatch(r'[A-Za-z0-9_-]+',ident): raise m.Problem('标识格式无效')
        with self.db() as db:
            self.check_revision(db,revision)
            row=db.execute('SELECT body FROM managed_objects WHERE kind=? AND id=?',(kind,ident)).fetchone()
            old=json.loads(row['body']) if row else {}
            if old.get('_archived'): raise m.Problem('对象已在回收站，请先恢复')
            item={**old,'id':ident,'name':m.text(body.get('name'),160,True),'notes':m.text(body.get('notes',''),2000),'enabled':body.get('enabled',True) is True}
            if kind=='group' and item['name']=='全部客房': raise m.Problem('“全部客房”为系统范围，不可作为自定义设备组')
            if any(c['id']!=ident and c['name']==item['name'] for c in self.objects(db,kind)): raise m.Problem('名称已存在（含回收站），请改名或恢复原记录',409)
            if kind=='advertiser':
                item.update(contact=m.text(body.get('contact',''),120),phone=m.text(body.get('phone',''),100),industry=m.text(body.get('industry',old.get('industry','')),80),house=body.get('house',False) is True)
            if kind=='asset':
                item['path']=self.require_media_path(body.get('path',old.get('path','')))
                if Path(item['path']).suffix.lower() not in ('.jpg','.jpeg','.png','.webp','.mp4','.webm','.m4v','.mov'): raise m.Problem('请选择图片或广告视频')
                if old and old.get('path')!=item['path'] and self.references(db,kind,old): raise m.Problem('素材仍被广告引用，不能替换绑定文件')
                item['format']='图片' if Path(item['path']).suffix.lower() in ('.jpg','.jpeg','.png','.webp') else '视频'
            if not old: item['_created_at']=m.now_ms()
            item['_updated_at']=m.now_ms()
            if old and old['name']!=item['name'] and kind in ('group','advertiser'):
                key='group' if kind=='group' else 'merchant'
                for table in (('devices','campaigns') if kind=='group' else ('campaigns',)):
                    for row in db.execute('SELECT id,body FROM '+table).fetchall():
                        record=json.loads(row['body'])
                        if record.get(key)==old['name']:
                            record[key]=item['name'];db.execute('UPDATE '+table+' SET body=? WHERE id=?',(m.encoded(record),row['id']))
            self.put_object(db,kind,item);self.changed(db,actor,'保存'+LABELS[kind],item['name']);return item

    def lifecycle(self, kind, ids, action, actor, revision, value=None):
        m=core()
        if kind not in LABELS or action not in ('archive','restore','enable','disable','purge'): raise m.Problem('管理操作无效')
        if not isinstance(ids,list) or not 1<=len(ids)<=500 or any(not isinstance(i,str) or not i or len(i)>200 for i in ids) or len(set(ids))!=len(ids): raise m.Problem('每次请选择 1–500 个不同对象')
        if kind=='campaign': return self.campaign_bundle_lifecycle(ids,action,actor,revision)
        result={'ok':[],'failed':[]}
        with self.db() as db:
            self.check_revision(db,revision)
            for ident in ids:
                item={}
                try:
                    item=self.record(db,kind,ident)
                    if kind=='home_card': self.check_home_card_lifecycle(db,item,action)
                    if kind=='order' and action in ('restore','enable'): self.validate_order_window(db,item)
                    if kind=='user' and action in ('disable','archive','purge'):
                        if ident==actor:raise m.Problem('不能停用、归档或清空当前登录账号')
                        if item['role']=='admin' and item['enabled'] and db.execute("SELECT count(*) FROM users WHERE role='admin' AND enabled=1").fetchone()[0]<=1:raise m.Problem('必须保留至少一个启用的管理员')
                    if kind=='source' and ident=='main': raise m.Problem('默认片库来源不能删除或停用')
                    if kind=='source' and action=='archive':
                        count=db.execute('SELECT count(*) FROM media WHERE source=? AND published=1',(ident,)).fetchone()[0]
                        if count and value is not True:raise m.Problem('归档将从电视移除 '+str(count)+' 部影片，请明确确认')
                    if action=='purge' or (action=='archive' and kind!='source'):
                        refs=self.references(db,kind,item)
                        if refs: raise m.Problem('仍有 '+str(len(refs))+' 个引用：'+'、'.join(refs[:5])+'；请先迁移引用')
                    if action=='purge':
                        if not self.archived(kind,item): raise m.Problem('只允许清空回收站中的记录')
                        self.purge_record(db,kind,item)
                    else:
                        if action in ('archive','restore'):
                            item['_archived']=action=='archive'
                            if action=='archive': item['_archived_at']=m.now_ms()
                            if kind=='campaign': item['status']='已归档' if action=='archive' else '草稿'
                            elif kind=='media': db.execute('UPDATE media SET published=0 WHERE id=?',(ident,))
                            else: item['enabled']=False
                        else:
                            if self.archived(kind,item): raise m.Problem('请先从回收站恢复')
                            if kind in ('media','campaign'): raise m.Problem('请使用上架或投放入口')
                            item['enabled']=action=='enable'
                        item['_updated_at']=m.now_ms();self.write_record(db,kind,item)
                    result['ok'].append(ident)
                except m.Problem as exc: result['failed'].append({'id':ident,'title':item.get('title') or item.get('name') or item.get('room') or ident,'reason':str(exc)})
            if result['ok']: self.changed(db,actor,{'archive':'移入回收站','restore':'恢复','purge':'清空','enable':'启用','disable':'停用'}[action]+LABELS[kind]+' '+str(len(result['ok']))+' 项（失败 '+str(len(result['failed']))+'）',','.join(result['ok']))
        return result

    def purge_record(self, db, kind, item):
        # A lightweight marker blocks rescans from re-importing deliberately cleared media.
        if kind=='media':
            source=db.execute('SELECT source FROM media WHERE id=?',(item['id'],)).fetchone()
            marker={'id':item['id'],'name':item.get('title',item['id']),'source_id':source['source'],'keys':self.media_keys(item),'purged_at':core().now_ms()}
            self.put_object(db,'media_tombstone',marker)
        if kind=='campaign':
            self.put_object(db,'campaign_history',{'id':item['id'],'name':item['name'],'tier':item['tier'],'merchant':item.get('merchant','')})
        if kind=='asset': self.put_object(db,'asset_tombstone',{'id':item['id'],'path':item['path']})
        if kind=='backup':self.put_object(db,'backup_tombstone',{'id':item['id']})
        if kind=='user':
            db.execute('DELETE FROM sessions WHERE name=?',(item['id'],));db.execute('DELETE FROM users WHERE name=?',(item['id'],))
        if kind in TABLES: db.execute('DELETE FROM '+TABLES[kind]+' WHERE id=?',(item['id'],))
        else: db.execute('DELETE FROM managed_objects WHERE kind=? AND id=?',(kind,item['id']))

    @staticmethod
    def media_keys(item):
        paths=[item['path']] if item.get('path') else [e['path'] for e in item.get('episodes',[])]
        return [hashlib.sha256(('path:'+p).encode()).hexdigest() for p in paths]+([hashlib.sha256(('jf:'+item['jellyfin_id']).encode()).hexdigest()] if item.get('jellyfin_id') else [])

    @staticmethod
    def source_enabled(db, ident):
        row=db.execute('SELECT body FROM sources WHERE id=?',(ident,)).fetchone()
        if not row:return False
        source=json.loads(row['body']);return source.get('enabled',True) and not source.get('_archived')

    @staticmethod
    def remember_episodes(item, previous=None):
        if item.get('kind')!='series':return
        old=previous or {};memory={**old.get('_episode_memory',{}),**item.get('_episode_memory',{})}
        for episode in old.get('episodes',[])+item.get('episodes',[]):
            memory[episode['path']]={k:episode[k] for k in ('id','title','season','number') if k in episode}
        item['_episode_memory']=memory

    def media_blocked(self, db, item):
        keys=set(self.media_keys(item))
        return any(keys.intersection(c['keys']) for c in self.objects(db,'media_tombstone'))

    def save_policy(self, body, actor, revision):
        m=core();days=m.number(body.get('trash_days'),1,365)
        if type(body.get('cleanup_enabled')) is not bool: raise m.Problem('请选择是否自动清理')
        with self.db() as db:
            self.check_revision(db,revision);self.put_kv(db,'management_policy',{'trash_days':days,'cleanup_enabled':body['cleanup_enabled']});self.changed(db,actor,'保存回收站策略',str(days)+' 天')
        return {'ok':True}

    def clear_trash(self, body, actor, revision):
        m=core();mode=body.get('mode','expired')
        if mode not in ('expired','all') or body.get('confirm') is not True:raise m.Problem('请选择清空范围并明确确认')
        result={'ok':[],'failed':[]}
        with self.db() as db:
            self.check_revision(db,revision);rows=self.trash_rows(db)
            if mode=='expired':rows=[r for r in rows if r['expires_at'] and r['expires_at']<=m.now_ms()]
            order={k:i for i,k in enumerate(LABELS)};rows.sort(key=lambda r:order[r['kind']])
            if len(rows)>2000:raise m.Problem('回收站超过 2000 项，请分类型分批处理')
            for row in rows:
                ident=row['kind']+':'+row['id']
                try:
                    item=self.record(db,row['kind'],row['id'])
                    if row['kind']=='source' and row['id']=='main':raise m.Problem('不能清空默认片库来源')
                    if row['kind']=='user' and row['id']==actor:raise m.Problem('不能清空当前登录账号')
                    refs=self.references(db,row['kind'],item)
                    if refs:raise m.Problem('仍有 '+str(len(refs))+' 个引用，请先迁移')
                    self.purge_record(db,row['kind'],item);result['ok'].append(ident)
                except m.Problem as exc:result['failed'].append({'id':ident,'title':row['name'],'reason':str(exc)})
            if result['ok']:self.changed(db,actor,'一键清空回收站 '+str(len(result['ok']))+' 项（失败 '+str(len(result['failed']))+'）',mode)
        return result

    def cleanup_expired(self):
        with self.db() as db:
            policy=self.kv(db,'management_policy')
            if not policy['cleanup_enabled']: return {'ok':[],'failed':[]}
            due=[r for r in self.trash_rows(db) if r['expires_at'] and r['expires_at']<=core().now_ms()]
        result={'ok':[],'failed':[]}
        # Use normal permission-independent lifecycle validation, including live reference checks.
        for kind in LABELS:
            ids=[r['id'] for r in due if r['kind']==kind][:500]
            if ids:
                with self.db() as db: revision=self.kv(db,'revision')
                try:
                    out=self.lifecycle(kind,ids,'purge','system:回收站到期清理',revision)
                    result['ok']+=out['ok'];result['failed']+=out['failed']
                except core().Problem as exc: result['failed'].append({'id':kind,'title':LABELS[kind],'reason':str(exc)})
        return result

    def operations_get(self, section, args, user, local_service_host=None):
        m=core()
        if section=='guest_requests':
            if user['role']!='admin':raise m.Problem('观众留言需要管理员权限',403)
            return self.guest_admin_state(args)
        if section=='overview':return self.management_overview()
        if section=='library_scan':return self.library_scan_plan(user)
        if section=='media_files':return self.media_files(m.text(args.get('id',[''])[0],200,True))
        if section in ('backups','release_preview') and user['role']!='admin':raise m.Problem('备份与配置回退需要管理员权限',403)
        if section=='backups':return self.backup_rows()
        if section=='release_preview':
            from operations_system import page_number
            return self.release_preview(page_number(args,'id',0,1,1000000000))
        if section=='tasks':return self.task_rows(args)
        if section=='audit_csv':return self.audit_csv(args)
        if section=='audit':return self.audit_rows(args)
        if section=='health':return self.server_health(local_service_host)
        if section=='sync':
            try:config=self.jellyfin_config() or {}
            except m.Problem:config={}
            return {**self.sync_control(),'configured':bool(config),'url':config.get('url','http://127.0.0.1:8096')}
        if section=='device':
            if user['role']!='admin':raise m.Problem('设备详情需要管理员权限',403)
            return self.device_detail(m.text(args.get('id',[''])[0],100,True))
        if section=='devices_csv':
            if user['role']!='admin':raise m.Problem('导出设备需要管理员权限',403)
            return {'csv':self.devices_csv()}
        if section=='schedule': return self.schedule_preview(args)
        if section=='orders': return self.order_rows(args)
        if section=='merchant_report': return self.merchant_report(args)
        if section=='ad_control': return self.ad_control_status()
        if section=='ad_playback': return self.playback_admin_state()
        if section=='home': return self.home_admin_state()
        if section=='downloads': return self.download_rows(args)
        if section=='download_locations': return self.download_locations(args)
        if section=='download_settings':
            if user['role']!='admin':raise m.Problem('下载连接设置需要管理员权限',403)
            return self.download_settings_view()
        if section=='compat': return self.compat_rows(args)
        if section=='channels': return self.channel_admin_state()
        if section=='xmltv': return self.channel_xmltv()
        with self.db() as db:
            if section=='trash':
                rows=self.trash_rows(db)
                if user['role']!='admin': rows=[r for r in rows if r['kind'] in ('media','campaign','advertiser','asset','package','order','guide','topic','channel','home_card')]
                return {'rows':rows,'policy':self.kv(db,'management_policy'),'ignored_media':self.objects(db,'media_tombstone') if user['role']=='admin' else []}
            if section=='entities': return self.managed_state(db)
        raise m.Problem('管理页面不存在',404)

    def operations_post(self, body, user):
        m=core();kind=body.get('kind');action=body.get('action');revision=body.get('revision');actor=user['name']
        if user['role']=='viewer': raise m.Problem('只读账号不能修改',403)
        if user['role']!='admin' and (kind not in ('media','campaign','advertiser','asset','package','order','ad_control','guide','topic','home','home_card','compat','channel','download') or action in ('purge','policy','currency_save','download_settings','download_test','download_mkdir','allow_reimport','devices_import','devices_batch','sync_control','sync_connection','sync_test','retry_task','backup_create','trash_clear')): raise m.Problem('此管理操作需要管理员权限',403)
        expected={'devices_import':'device','devices_batch':'device','campaign_batch':'campaign','discover_assets':'asset','library_update':'media','source_save':'source'}
        expected.update({a:'download' for a in ('download_create','download_action','download_settings','download_test','download_mkdir')})
        if action in expected and kind!=expected[action]:raise m.Problem('操作与管理对象不匹配')
        if action in ('guest_settings','guest_status'):
            if kind!='guest_request' or user['role']!='admin':raise m.Problem('观众留言需要管理员权限',403)
            return self.guest_settings_save(body,user,revision) if action=='guest_settings' else self.guest_status_save(body,user)
        if action in ('save','source_save') and not isinstance(body.get('item'),dict):raise m.Problem('资料表单必须为对象')
        if action=='trash_clear':return self.clear_trash(body,actor,revision)
        if action=='currency_save':return self.save_currency(body,actor,revision)
        if action=='ad_control':return self.save_ad_control(body,user,revision)
        if action=='ad_playback':
            if kind!='ad_control':raise m.Problem('操作与管理对象不匹配')
            return self.save_playback_rules(body,user,revision)
        if action=='home_save':return self.save_home(body,user,revision)
        if action in ('home_menu_save','home_menu_reset'):
            if kind!='home': raise m.Problem('操作与管理对象不匹配')
            return self.save_home_menu(body,user,revision) if action=='home_menu_save' else self.reset_home_menu(user,revision)
        if action=='download_settings':return self.save_download_settings(body,user,revision)
        if action=='download_test':return self.test_download_service(m.text(body.get('target'),60,True),user)
        if action=='download_create':return self.create_download(body,user,revision)
        if action=='download_action':return self.download_action(body,user,revision)
        if action=='download_mkdir':return self.make_download_folder(body,user,revision)
        if action=='compat_probe':return self.compat_probe(body.get('ids'),actor,revision)
        if action=='compat_enqueue':return self.compat_enqueue(body.get('ids'),actor,revision)
        if action=='compat_control':return self.compat_control(m.text(body.get('id'),100,True),body.get('op'),actor,revision)
        if action=='save' and kind=='guide':return self.save_guide(body['item'],user,revision)
        if action=='save' and kind=='topic':return self.save_topic(body['item'],user,revision)
        if action=='save' and kind=='channel':return self.save_channel(body['item'],user,revision)
        if action=='save' and kind=='package':return self.save_package(body['item'],user,revision)
        if action=='save' and kind=='order':return self.save_order(body['item'],user,revision)
        if action=='source_save':return self.source_save(body,actor,revision)
        if action=='backup_create':
            with self.db() as db:self.check_revision(db,revision)
            path=self.backup(actor);return {'id':path.name,'bytes':path.stat().st_size}
        if action=='sync_control':return self.save_sync_control(body,actor,revision)
        if action=='sync_connection':return self.save_sync_connection(body,actor,revision)
        if action=='sync_test':return self.test_sync_connection(actor)
        if action=='library_scan':
            if kind!='media':raise m.Problem('操作与管理对象不匹配')
            return self.start_mounted_library_scan(body.get('paths'),user)
        if action=='library_update':return self.start_library_update(actor)
        if action=='retry_task':return self.retry_task(m.text(body.get('id'),100,True),actor)
        if action=='devices_import':return self.devices_import(body,actor,revision)
        if action=='devices_batch':return self.devices_batch(body,actor,revision)
        if action=='campaign_batch': return self.campaigns_batch(body,actor,revision)
        if action=='discover_assets': return self.discover_assets(actor,revision)
        if action=='save' and kind=='advertiser' and user['role']!='admin':
            with self.db() as db:old=next((a for a in self.objects(db,'advertiser') if a['id']==body['item'].get('id')),{})
            if bool(old.get('house'))!=(body['item'].get('house',False) is True):raise m.Problem('“酒店自有（停播不停）”标记只允许管理员修改',403)
        if action=='save': return self.save_entity(kind,body.get('item',{}),actor,revision)
        if action=='policy': return self.save_policy(body,actor,revision)
        if action=='allow_reimport':
            with self.db() as db:
                self.check_revision(db,revision);marker=self.record(db,'media_tombstone',m.text(body.get('id'),200,True))
                db.execute("DELETE FROM managed_objects WHERE kind='media_tombstone' AND id=?",(marker['id'],));self.changed(db,actor,'允许影片重新入库',marker['name'])
            return {'ok':True}
        if action=='purge' and body.get('confirm') is not True: raise m.Problem('清空后不能恢复编辑信息，请明确确认')
        return self.lifecycle(kind,body.get('ids'),action,actor,revision,value=body.get('confirm'))

    def run_management_scheduler(self):
        def run():
            while True:
                time.sleep(60)
                try:
                    with self.db() as db:row=db.execute("SELECT value FROM kv WHERE key='maintenance_status'").fetchone()
                    previous=json.loads(row[0]) if row else {}
                    if core().now_ms()-previous.get('at',0)<3600000:continue
                    result=self.cleanup_expired()
                    status={'at':core().now_ms(),'cleared':len(result['ok']),'blocked':len(result['failed']),'error':''}
                except Exception as exc:status={'at':core().now_ms(),'cleared':0,'blocked':0,'error':type(exc).__name__}
                try:
                    with self.db() as db:self.put_kv(db,'maintenance_status',status)
                except Exception:pass  # An unwritable database cannot be used to report its own error.
        threading.Thread(target=run,name='management-maintenance',daemon=True).start()
