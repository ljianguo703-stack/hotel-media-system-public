"""Task centre, synchronization controls and on-demand server health."""
from datetime import datetime
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import socket
import threading
import time
from urllib.parse import urlsplit
from physical_storage import snapshot as physical_storage_snapshot
from library_scan import LibraryScanOperations


def core():
    import management
    return management


def page_number(args,key,default,lower,upper):
    try:value=int(args.get(key,[str(default)])[0])
    except (ValueError,TypeError):raise core().Problem('分页或数量参数无效')
    return core().number(value,lower,upper)


class SystemOperations(LibraryScanOperations):
    def sync_control(self):
        with self.db() as db:
            row=db.execute("SELECT value FROM kv WHERE key='sync_control'").fetchone()
            return json.loads(row[0]) if row else {'paused':False,'interval_seconds':self.sync_interval_seconds}

    def save_sync_control(self, body, actor, revision):
        m=core();interval=m.number(body.get('interval_seconds'),30,3600)
        if type(body.get('paused')) is not bool:raise m.Problem('暂停选项无效')
        with self.db() as db:
            self.check_revision(db,revision);self.put_kv(db,'sync_control',{'paused':body['paused'],'interval_seconds':interval});self.changed(db,actor,'保存自动同步设置',str(interval)+' 秒，'+('已暂停' if body['paused'] else '已启用'))
        self.sync_wakeup.set();return {'ok':True}

    def save_sync_connection(self, body, actor, revision):
        m=core();url=m.text(body.get('url'),500,True)
        try:parsed=urlsplit(url);port=parsed.port
        except ValueError:raise m.Problem('Jellyfin 地址或端口格式无效')
        if parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or parsed.port is None or parsed.path not in ('','/') or parsed.query or parsed.fragment or parsed.username:raise m.Problem('Jellyfin 地址仅支持本机 http://127.0.0.1:端口')
        old=self.jellyfin_config() or {};token=m.text(body.get('token') or old.get('token'),1000,True)
        path=self.data/'nas-integration.json';temporary=self.data/('nas-integration-'+secrets.token_hex(8)+'.tmp')
        old_bytes=path.read_bytes() if path.exists() else None
        try:
            fd=os.open(str(temporary),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as out:json.dump({'url':url.rstrip('/'),'token':token},out)
            with self.db() as db:
                self.check_revision(db,revision);temporary.replace(path)
                try:self.changed(db,actor,'保存 Jellyfin 连接','本机端口 '+str(parsed.port))
                except Exception:
                    if old_bytes is None:path.unlink(missing_ok=True)
                    else:path.write_bytes(old_bytes)
                    raise
        finally:temporary.unlink(missing_ok=True)
        self.sync_wakeup.set();return {'ok':True}

    def test_sync_connection(self, actor):
        m=core()
        try:
            payload=self.jellyfin_request('/Items?Recursive=true&IncludeItemTypes=Movie&Limit=1')
            connected=isinstance(payload.get('Items'),list)
            if not connected:raise m.Problem('Jellyfin 返回格式不正确')
            result={'connected':True,'message':'Jellyfin 连接和认证正常'}
        except m.Problem as exc:result={'connected':False,'message':str(exc)}
        with self.db() as db:self.audit(db,actor,'测试 Jellyfin 连接','通过' if result['connected'] else '失败')
        return result

    def server_health(self, local_service_host=None):
        m=core();services=[]
        try:cfg=self.jellyfin_config()
        except m.Problem:cfg=None
        for name,port in [('Jellyfin',urlsplit(cfg['url']).port if cfg else 8096),('文件中心',8095),('Dockge',5001),('文件与硬盘管理',9090)]:
            reachable=False
            # Some NAS containers bind only this machine's LAN interface. The accepted socket's
            # local address is trusted; never use Host or forwarded headers as a probe target.
            for host in dict.fromkeys(['127.0.0.1']+([local_service_host] if local_service_host else [])):
                try:
                    with socket.create_connection((host,port),timeout=.35):reachable=True
                except OSError:continue
                break
            services.append({'name':name,'port':port,'reachable':reachable,'status':'TCP 可达' if reachable else '未连通'})
        disks=[]
        with self.db() as db:sources=[json.loads(r['body']) for r in db.execute('SELECT body FROM sources')]
        for source in sources:
            try:
                usage=shutil.disk_usage(source['path']);disks.append({'name':source['name'],'path':source['path'],'total':usage.total,'free':usage.free,'used':usage.used,'readable':os.access(source['path'],os.R_OK|os.X_OK)})
            except OSError:disks.append({'name':source['name'],'path':source['path'],'readable':False})
        return {'system':platform.system(),'hostname':socket.gethostname(),'load':list(os.getloadavg()) if hasattr(os,'getloadavg') else [],'services':services,'disks':disks,'physical_storage':physical_storage_snapshot(),'checked_at':m.now_ms()}

    def task_rows(self,args):
        m=core();limit=page_number(args,'limit',50,1,200);offset=page_number(args,'offset',0,0,1000000);status=args.get('status',[''])[0];query=args.get('q',[''])[0].casefold()
        with self.db() as db:rows=[json.loads(r['body']) for r in db.execute('SELECT body FROM jobs ORDER BY rowid DESC')]
        if status:rows=[r for r in rows if r['status']==status]
        if query:rows=[r for r in rows if query in json.dumps(r,ensure_ascii=False).casefold()]
        return {'rows':rows[offset:offset+limit],'total':len(rows),'offset':offset,'limit':limit}

    def audit_rows(self,args):
        m=core();limit=page_number(args,'limit',50,1,200);offset=page_number(args,'offset',0,0,1000000);query=args.get('q',[''])[0]
        where=' WHERE actor LIKE ? OR action LIKE ? OR object LIKE ?' if query else '';params=['%'+query+'%']*3 if query else []
        with self.db() as db:
            total=db.execute('SELECT count(*) FROM audit'+where,params).fetchone()[0]
            rows=[dict(r) for r in db.execute('SELECT id,at,actor,action,object FROM audit'+where+' ORDER BY id DESC LIMIT ? OFFSET ?',params+[limit,offset])]
        return {'rows':rows,'total':total,'offset':offset,'limit':limit}

    def wait_task(self, ident, timeout=3600):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            with self.db() as db:row=db.execute('SELECT body FROM jobs WHERE id=?',(ident,)).fetchone()
            if not row:raise core().Problem('子任务已不存在')
            job=json.loads(row['body'])
            if job['status'] in ('failed','completed'):return job
            time.sleep(.1)
        raise core().Problem('子任务执行超时，请在任务中心检查')

    def start_library_update(self, actor, source_ids=None, sync_jellyfin=True):
        m=core()
        if not self.workflow_lock.acquire(blocking=False):raise m.Problem('已有一键片库更新在执行',409)
        job={'id':'library-update-'+secrets.token_hex(8),'kind':'library-update','status':'queued','started':m.now_ms(),'steps':[],'added':0,'updated':0,'missing':0,'source_ids':source_ids,'sync_jellyfin':sync_jellyfin}
        try:
            with self.db() as db:db.execute('INSERT INTO jobs VALUES(?,?)',(job['id'],m.encoded(job)))
        except Exception:
            self.workflow_lock.release();raise
        def run():
            try:
                job['status']='running';self.job(job)
                with self.db() as db:sources=[json.loads(r['body']) for r in db.execute('SELECT body FROM sources')]
                steps=[('目录扫描：'+s['name'],lambda ident=s['id']:self.start_scan(ident,actor)) for s in sources if s.get('enabled',True) and not s.get('_archived') and (source_ids is None or s['id'] in source_ids)]
                try:
                    if sync_jellyfin and self.jellyfin_config():steps.insert(0,('Jellyfin 同步',lambda:self.start_jellyfin_sync(actor)))
                except m.Problem as exc:job['steps'].append({'name':'Jellyfin 配置','status':'failed','error':str(exc)})
                for name,start in steps:
                    try:
                        end=time.monotonic()+3
                        while True:
                            try:child=start();break
                            except m.Problem as exc:
                                if exc.status!=409 or time.monotonic()>=end:raise
                                time.sleep(.1)
                        child=self.wait_task(child['id']);job['steps'].append({'name':name,'id':child['id'],'status':child['status'],'error':child.get('error','')})
                        for key in ('added','updated','missing'):job[key]+=child.get(key,0)
                    except m.Problem as exc:job['steps'].append({'name':name,'status':'failed','error':str(exc)})
                    self.job(job)
                job['status']='failed' if any(s['status']=='failed' for s in job['steps']) else 'completed'
                if job['status']=='failed':job['error']='部分步骤失败，请查看步骤明细；其他成功步骤已保留'
            except Exception as exc:job.update(status='failed',error='片库更新中断（'+type(exc).__name__+'），可在任务中心重试')
            finally:
                job['finished']=m.now_ms()
                try:self.job(job)
                finally:self.workflow_lock.release()
        threading.Thread(target=run,name=job['id'],daemon=True).start();return job

    def retry_task(self, ident, actor):
        m=core()
        with self.db() as db:
            row=db.execute('SELECT body FROM jobs WHERE id=?',(ident,)).fetchone()
            if not row:raise m.Problem('任务不存在',404)
            old=json.loads(row['body'])
        if old['status']!='failed':raise m.Problem('只有失败任务可以重试')
        if old.get('kind')=='library-update':job=self.start_library_update(actor,old.get('source_ids'),old.get('sync_jellyfin',True))
        elif ident.startswith('scan-') and old.get('source_id'):job=self.start_scan(old['source_id'],actor)
        elif ident.startswith('jellyfin-'):job=self.start_jellyfin_sync(actor)
        else:raise m.Problem('此任务不能自动重试，请使用原始操作入口')
        with self.db() as db:self.audit(db,actor,'重试后台任务',ident+' → '+job['id'])
        return job

    def release_preview(self, ident):
        m=core()
        with self.db() as db:
            row=db.execute('SELECT id,at,actor,action,body FROM releases WHERE id=?',(ident,)).fetchone()
            if not row:raise m.Problem('历史配置不存在',404)
            snapshot=json.loads(row['body']);result={'id':ident,'at':row['at'],'actor':row['actor'],'action':row['action'],'revision':self.kv(db,'revision')}
            for key,table in [('media','media'),('campaigns','campaigns')]:
                old={r['id']:dict(r) for r in db.execute('SELECT * FROM '+table)};new={r['id']:r for r in snapshot[key]}
                def canonical(row):return {**row,'body':json.loads(row['body'])}
                added=[k for k in new if k not in old];removed=[k for k in old if k not in new];changed=[k for k in old.keys()&new.keys() if canonical(old[k])!=canonical(new[k])]
                def summary(ids,lookup):
                    return [{'id':k,'title':json.loads(lookup[k]['body']).get('title') or json.loads(lookup[k]['body']).get('name') or k} for k in ids[:20]]
                result[key]={'added_count':len(added),'removed_count':len(removed),'changed_count':len(changed),'added':summary(added,new),'removed':summary(removed,old),'changed':summary(changed,new)}
            settings=self.kv(db,'settings');result['settings_changes']=[{'key':k,'before':settings.get(k),'after':v} for k,v in snapshot['settings'].items() if settings.get(k)!=v]
        return result

    def backup_path(self, name):
        import re
        m=core()
        if not isinstance(name,str) or not re.fullmatch(r'hotel-[0-9]+-[a-f0-9]{6}\.sqlite3',name):raise m.Problem('备份名称无效')
        folder=(self.data/'backups').resolve();path=folder/name
        if not path.is_file() or path.is_symlink() or folder not in path.resolve().parents:raise m.Problem('备份不存在',404)
        return path

    def backup_record(self,db,name):
        path=self.backup_path(name);row=db.execute("SELECT body FROM managed_objects WHERE kind='backup' AND id=?",(name,)).fetchone()
        return json.loads(row['body']) if row else {'id':name,'name':name,'bytes':path.stat().st_size,'created_at':int(path.stat().st_mtime*1000)}

    def backup_rows(self):
        with self.db() as db:
            markers={b['id'] for b in self.objects(db,'backup_tombstone')};folder=self.data/'backups';rows=[]
            for file in folder.glob('hotel-*.sqlite3') if folder.is_dir() else []:
                if file.name in markers:continue
                try:rows.append(self.backup_record(db,file.name))
                except core().Problem:continue
        return {'rows':sorted(rows,key=lambda b:b.get('created_at',0),reverse=True)}

    def user_rows(self,db):
        meta={u['id']:u for u in self.objects(db,'user')}
        return [{**dict(r),'_archived':meta.get(r['name'],{}).get('_archived',False)} for r in db.execute('SELECT name,role,enabled FROM users')]

    def source_save(self, body, actor, revision):
        m=core();raw=body.get('item',{});ident=m.text(raw.get('id'),100,True)
        if type(raw.get('enabled',True)) is not bool:raise m.Problem('来源启用状态无效')
        with self.db() as db:
            self.check_revision(db,revision);old=self.record(db,'source',ident)
            if old.get('_archived'):raise m.Problem('来源在回收站，请先恢复')
            if 'path' in raw and raw['path']!=old['path']:raise m.Problem('来源路径不能直接修改，请接入新的目录')
            enabled=raw.get('enabled',True)
            if ident=='main' and not enabled:raise m.Problem('默认片库来源不能停用')
            count=db.execute('SELECT count(*) FROM media WHERE source=? AND published=1',(ident,)).fetchone()[0]
            if old.get('enabled',True) and not enabled and count and body.get('confirm') is not True:raise m.Problem('停用将从电视移除 '+str(count)+' 部已上架影片，请明确确认',409)
            item={**old,'name':m.text(raw.get('name'),100,True),'notes':m.text(raw.get('notes',''),2000),'enabled':enabled,'_updated_at':m.now_ms()}
            self.write_record(db,'source',item);self.changed(db,actor,'保存片库来源',item['name']+('：启用' if enabled else '：停用'))
        return item

    def media_files(self, ident):
        m=core()
        with self.db() as db:item=self.record(db,'media',ident)
        rows=[];files=item.get('episodes',[]) if item.get('kind')=='series' else [{'id':item['id'],'title':item.get('title',item['id']),'path':item.get('path','')}]
        for entry in files:
            try:self.require_media_path(entry['path']);readable=True;reason=''
            except m.Problem as exc:readable=False;reason=str(exc)
            rows.append({'id':entry['id'],'title':entry.get('title',entry['id']),'path':entry.get('path',''),'readable':readable,'reason':reason})
        return {'id':ident,'files':rows,'missing':sum(not r['readable'] for r in rows)}

    def audit_csv(self,args):
        import csv
        import io
        query=args.get('q',[''])[0];where=' WHERE actor LIKE ? OR action LIKE ? OR object LIKE ?' if query else '';params=['%'+query+'%']*3 if query else []
        with self.db() as db:rows=db.execute('SELECT at,actor,action,object FROM audit'+where+' ORDER BY id DESC LIMIT 10001',params).fetchall()
        out=io.StringIO();writer=csv.writer(out);writer.writerow(['时间（毫秒）','操作者','操作','对象'])
        for row in rows[:10000]:writer.writerow(["'"+str(v) if isinstance(v,str) and v.startswith(('=','+','-','@','\t','\r')) else v for v in row])
        return {'csv':out.getvalue(),'count':min(10000,len(rows)),'truncated':len(rows)>10000}
