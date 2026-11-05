"""Single-hotel fleet workflows on the existing SQLite/software queue."""
import hashlib
import json
import re
import secrets
import ipaddress
from pathlib import Path
from software_packages import SoftwareError, PACKAGE
from software_store import Repository, encoded, now

PROTECTED = {'com.xjg.hoteltv', 'android', 'com.android.settings', 'com.android.systemui',
             'com.android.packageinstaller', 'com.google.android.packageinstaller',
             'com.android.permissioncontroller', 'com.google.android.permissioncontroller'}
IDLE_WAITING = ('waiting_online', 'waiting_idle', 'waiting_retry')


def initialize(db):
    for sql in (
        'CREATE TABLE IF NOT EXISTS fleet_plans(id TEXT PRIMARY KEY,body TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS fleet_status(id TEXT PRIMARY KEY,body TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS fleet_messages(id TEXT PRIMARY KEY,body TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS fleet_receipts(message TEXT NOT NULL,device TEXT NOT NULL,body TEXT NOT NULL,PRIMARY KEY(message,device))',
        "CREATE INDEX IF NOT EXISTS software_jobs_fleet_batch ON software_jobs(json_extract(body,'$.batch_id'))",
    ): db.execute(sql)


def targets(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 500 or any(not isinstance(x, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', x) for x in value) or len(set(value)) != len(value):
        raise SoftwareError('请选择1–500台不同的已登记电视')
    return value


def bounded_text(value, limit, label):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit or any(ord(x) < 32 and x not in '\n\t' for x in value):
        raise SoftwareError(label + '为空、过长或含无效字符')
    return value.strip()


def identifier(value, prefix):
    if not isinstance(value, str) or not re.fullmatch(re.escape(prefix)+r'[a-z0-9-]{12,64}', value):
        raise SoftwareError('任务或软件标识无效')
    return value


class FleetRepository(Repository):
    def executor_ready(self, idle=False):
        root = Path('/opt/hotel-tv-installer/current')
        ready = all((root / name).is_file() for name in ('software_agent.py', 'software_store.py', 'software_packages.py', 'fleet_agent.py', 'fleet_store.py'))
        return ready and (not idle or all('IDLE_INSTALL_PROTOCOL = 2' in (root/name).read_text() for name in ('software_agent.py', 'fleet_agent.py')))

    def devices(self, db):
        result = []
        status = {r[0]: json.loads(r[1]) for r in db.execute('SELECT id,body FROM fleet_status')}
        endpoints = {r[0]: json.loads(r[1]) for r in db.execute('SELECT id,body FROM software_targets')}
        for row in db.execute('SELECT id,body,last_seen,ip FROM devices ORDER BY id'):
            d = json.loads(row['body'])
            if d.get('_archived'): continue
            d.update(last_seen=row['last_seen'], ip=row['ip'], online=bool(row['last_seen'] and now()-row['last_seen'] < 120000),
                     management=status.get(row['id'], {}), maintenance=endpoints.get(row['id'], {}))
            result.append(d)
        return result

    def state(self):
        with self.db() as db:
            devices = self.devices(db)
            online = {d['id']: d['online'] for d in devices}
            plans = [json.loads(r[0]) for r in db.execute("SELECT body FROM fleet_plans WHERE json_extract(body,'$.status')!='preview' ORDER BY rowid DESC LIMIT 20")]
            messages = [json.loads(r[0]) for r in db.execute('SELECT body FROM fleet_messages ORDER BY rowid DESC LIMIT 100')]
            for p in plans:
                p['rows']=[{k:r[k] for k in ('id','room','group','ready','reasons')} for r in p['rows']]
                p['jobs']=[]
                for (raw,) in db.execute("SELECT body FROM software_jobs WHERE json_extract(body,'$.batch_id')=? ORDER BY rowid",(p['id'],)):
                    j=json.loads(raw)
                    if j.get('delivery') == 'idle' and j['status'] in IDLE_WAITING and not online.get(j['target']):
                        j.update(status='waiting_online', message='等待上线')
                    p['jobs'].append({**{k:j.get(k) for k in ('id','target','room','status','message','version','attempts','next_attempt_at','actual_version','actual_version_code')},'steps':j.get('steps',[])[-5:]})
                if p.get('delivery')=='idle' and p['status']!='cancelled':
                    statuses={j['status'] for j in p['jobs']}
                    if statuses=={'completed'}:p['status']='completed'
                    elif statuses and statuses <= {'completed','cancelled','superseded'}:p['status']='superseded'
                    elif statuses & {'queued','running'}:p['status']='running'
                    elif statuses and statuses <= {'completed','cancelled','superseded','failed','waiting_authorization'}:p['status']='needs_attention'
                    else:p['status']='published'
            for m in messages:
                receipts = {r[0]: json.loads(r[1]) for r in db.execute('SELECT device,body FROM fleet_receipts WHERE message=?', (m['id'],))}
                m['receipts'] = [{'id': t['id'], 'room': t['room'], **receipts.get(t['id'], {'status': 'expired' if now() >= m['expires_at'] else 'pending'})} for t in m['targets']]
            return {'revision': self.revision(db), 'executor_ready': self.executor_ready(), 'devices': devices, 'batches': plans, 'messages': messages, 'server_time_ms': now()}

    def report(self, device, body):
        if type(body.get('protocol')) is not int or body.get('protocol') not in (1, 2): raise SoftwareError('电视管理协议无效')
        value = {'protocol': body['protocol'], 'at': now()}
        for key in ('overlay', 'screen_awake', 'keep_adb'):
            if type(body.get(key)) is not bool: raise SoftwareError('电视管理状态无效')
            value[key] = body[key]
        for key, limit in (('version', 80), ('model', 150)):
            value[key] = bounded_text(body.get(key), limit, key)
        for key, high in (('version_code', 1000000000), ('sdk', 100)):
            if type(body.get(key)) is not int or not 1 <= body[key] <= high: raise SoftwareError('电视版本无效')
            value[key] = body[key]
        if value['protocol'] == 2:
            if type(body.get('home_idle')) is not bool or type(body.get('idle_ms')) is not int or not 0 <= body['idle_ms'] <= 31*86400000:
                raise SoftwareError('空闲回报无效')
            lease = body.get('maintenance_job', '')
            if not isinstance(lease, str) or lease and not re.fullmatch(r'sw-[a-f0-9]{24}', lease): raise SoftwareError('维护状态无效')
            value.update(home_idle=body['home_idle'], idle_ms=body['idle_ms'], maintenance_job=lease)
        with self.db() as db: db.execute('INSERT OR REPLACE INTO fleet_status VALUES(?,?)', (device['id'], encoded(value)))
        self.reconcile(device['id'])
        return {'ok': True, 'server_time_ms': now()}

    def enroll(self, body, actor, revision):
        if not self.executor_ready(): raise SoftwareError('请先通过软件与更新安装本次Ubuntu执行器DEB，再逐台纳管', 409)
        ident = body.get('id'); targets([ident])
        identity = body.get('identity_sha256', '')
        if not isinstance(identity,str) or not re.fullmatch(r'[a-f0-9]{64}', identity) or ident != 'tv-'+hashlib.sha256(('hotel-tv-backend-device:'+identity).encode()).hexdigest()[:16]:
            raise SoftwareError('设备标识与首次安装身份不一致')
        if body.get('confirm') is not True or body.get('online_confirmed') is not True or body.get('reboot_verified') is not True:
            raise SoftwareError('请先逐台完成首装、真实上线与重启验证，再确认后台管理授权')
        endpoint = self.validated_target(body, actor)
        with self.db() as db:
            self.check(db, revision)
            row = db.execute('SELECT body,last_seen FROM devices WHERE id=?', (ident,)).fetchone()
            device = json.loads(row['body']) if row else {}
            if not row or not row['last_seen'] or now()-row['last_seen'] >= 120000 or not device.get('room', '').strip():
                raise SoftwareError('后台尚未看到此房号电视真实上线，请先完成首装连接')
            if device.get('_archived') or not device.get('enabled', True):
                raise SoftwareError('请选择已启用的电视设备')
            if db.execute("SELECT 1 FROM software_jobs WHERE status IN ('queued','running') AND json_extract(body,'$.target')=?", (ident,)).fetchone():
                raise SoftwareError('此电视已有维护任务，请等待结束')
            endpoint.update(identity_sha256=identity, enrollment='pending', reboot_verified=True)
            db.execute('INSERT OR REPLACE INTO software_targets VALUES(?,?)', (ident, encoded(endpoint)))
            job = {'id': 'sw-'+secrets.token_hex(12), 'kind': 'fleet_probe', 'software_id': 'hotel-fleet',
                   'name': '首次后台纳管核验', 'version': '1', 'target': ident, 'endpoint': endpoint,
                   'actor': actor, 'status': 'queued', 'created_at': now(), 'steps': [], 'message': '等待服务器核验维护授权与设备身份'}
            self.write_job(db, job); self.bump(db)
        self.notify([job])
        return {'target': endpoint, 'job': job}

    def probe(self, ident, actor, revision):
        if not self.executor_ready(): raise SoftwareError('Ubuntu执行器尚未升级，不能核验纳管', 409)
        targets([ident])
        with self.db() as db:
            self.check(db, revision)
            endpoint = self.read(db, 'software_targets', ident)
            if not endpoint.get('identity_sha256') or not endpoint.get('reboot_verified'):
                raise SoftwareError('请先使用员工一键工具逐台完成身份与重启纳管')
            row = db.execute('SELECT body,ip,last_seen FROM devices WHERE id=?', (ident,)).fetchone()
            d = json.loads(row['body']) if row else {}
            if not d.get('enabled') or d.get('_archived') or now()-row['last_seen'] >= 120000:
                raise SoftwareError('电视离线或停用，暂不重新核验')
            try: ip = ipaddress.ip_address(row['ip'])
            except ValueError: raise SoftwareError('电视当前地址无效')
            if ip.version != 4 or not ip.is_private or ip.is_loopback or ip.is_unspecified or ip.is_multicast:
                raise SoftwareError('仅从此电视最新鉴权回报的酒店局域网地址重新核验')
            if db.execute("SELECT 1 FROM software_jobs WHERE status IN ('queued','running') AND json_extract(body,'$.target')=?", (ident,)).fetchone():
                raise SoftwareError('此电视已有维护任务，请等待结束')
            endpoint.update(address=str(ip), enrollment='pending')
            db.execute('UPDATE software_targets SET body=? WHERE id=?', (encoded(endpoint), ident))
            job = {'id':'sw-'+secrets.token_hex(12),'kind':'fleet_probe','software_id':'hotel-fleet','name':'后台维护重新核验','version':'1',
                   'target':ident,'endpoint':endpoint,'actor':actor,'status':'queued','created_at':now(),'steps':[],'message':'等待服务器按原物理身份核验当前维护连接'}
            self.write_job(db, job);self.bump(db)
        self.notify([job]);return {'job':job}

    def preflight(self, body, actor, revision):
        ids = targets(body.get('targets'))
        action = body.get('action')
        if action not in ('install', 'uninstall'): raise SoftwareError('批量操作仅支持安装/升级或卸载受管应用')
        delivery = body.get('delivery', 'manual')
        if delivery not in ('manual', 'idle') or delivery == 'idle' and action != 'install': raise SoftwareError('发布模式无效')
        with self.db() as db:
            self.check(db, revision)
            item = self.read(db, 'software_packages', identifier(body.get('package_id'), 'pkg-'))
            if item['kind'] != 'apk': raise SoftwareError('批量电视操作请选择APK')
            if action == 'uninstall' and item['software_id'] in PROTECTED: raise SoftwareError('影院和系统管理组件禁止批量卸载')
            devices = {d['id']: d for d in self.devices(db)}
            rows = []
            executor_ready = self.executor_ready(idle=delivery == 'idle')
            for ident in ids:
                d = devices.get(ident, {}); ep = d.get('maintenance', {}); ms = d.get('management', {})
                reasons = [] if executor_ready else ['Ubuntu执行器尚未升级，请先安装本次执行器DEB']
                if not d or d.get('enabled') is False: reasons.append('设备未登记或已停用')
                if delivery == 'manual' and not d.get('online'): reasons.append('电视离线，未纳入本次执行')
                if ep.get('enrollment') != 'verified': reasons.append('首次服务器维护授权尚未核验')
                if not ep.get('reboot_verified'): reasons.append('首次重启验证未完成')
                if delivery == 'manual' and (not ms.get('keep_adb') or now()-ms.get('at', 0) >= 120000): reasons.append('电视未回报有效的持续维护状态')
                if delivery == 'idle' and (ms.get('protocol') != 2 or not ms.get('keep_adb')): reasons.append('请先升级到支持空闲发布的客户端并完成纳管')
                if item.get('min_sdk', 1) > ms.get('sdk', 0): reasons.append('APK不兼容此Android版本')
                if delivery == 'manual' and db.execute("SELECT 1 FROM software_jobs WHERE status IN ('queued','running') AND json_extract(body,'$.target')=?", (ident,)).fetchone(): reasons.append('已有维护任务在执行')
                inv = db.execute('SELECT body FROM software_inventory WHERE target=? AND software=?', (ident, item['software_id'])).fetchone()
                installed = json.loads(inv[0]) if inv else {}
                if action == 'uninstall' and (not installed or installed.get('installed') is False): reasons.append('未核验为后台受管的已安装应用')
                if action == 'install' and installed.get('sha256') == item['sha256'] and installed.get('installed') is not False: reasons.append('该版本已核验安装，无需重复')
                rows.append({'id': ident, 'room': d.get('room', ident), 'group': d.get('group', ''), 'ready': not reasons,
                             'wait_reason': ('发布后等待上线' if not d.get('online') else '发布后等待空闲') if delivery == 'idle' else '',
                             'reasons': reasons, 'endpoint': ep, 'device_fingerprint': self.device_fingerprint(db, ident)})
            plan = {'id': 'batch-'+secrets.token_hex(12), 'actor': actor, 'action': action, 'package_id': item['id'],
                    'software_id': item['software_id'], 'name': item['name'], 'version': item['version'], 'sha256': item['sha256'],
                    'created_at': now(), 'expires_at': now()+5*60000, 'status': 'preview', 'rows': rows,
                    'delivery': delivery,
                    'execution_policy': ('保存发布目标；离线等待上线，首页空闲60秒后安装；播放/其他应用/待机时等待，各房独立核验' if delivery == 'idle' else '串行执行；第一台成功后继续，任一失败停止后续；离线和未纳管目标不执行')}
            db.execute('INSERT INTO fleet_plans VALUES(?,?)', (plan['id'], encoded(plan))); self.bump(db)
            return plan

    @staticmethod
    def device_fingerprint(db, ident):
        row = db.execute('SELECT body,token FROM devices WHERE id=?', (ident,)).fetchone()
        return hashlib.sha256(encoded(list(row) if row else []).encode()).hexdigest()

    def execute(self, ident, actor, revision, confirmed):
        identifier(ident, 'batch-')
        if confirmed is not True: raise SoftwareError('请先核对版本、操作和目标房间后确认')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            plan = self.read(db, 'fleet_plans', ident)
            if plan['actor'] != actor: raise SoftwareError('请由预检的管理员执行此批次', 403)
            if plan['status'] != 'preview':
                return {'batch': plan, 'jobs': [json.loads(r[0]) for r in db.execute("SELECT body FROM software_jobs WHERE json_extract(body,'$.batch_id')=?", (ident,))]}
            if type(revision) is not int or revision != self.revision(db): raise SoftwareError('批次信息已变化，请刷新后重试', 409)
            if now() >= plan['expires_at']: raise SoftwareError('预检已过期，请重新核对目标', 409)
            idle = plan.get('delivery') == 'idle'
            if not self.executor_ready(idle=idle): raise SoftwareError('Ubuntu执行器尚未升级，停止执行', 409)
            ready = [r for r in plan['rows'] if r['ready']]
            if not ready: raise SoftwareError('没有满足条件的电视，请先完成逐台纳管')
            jobs = []
            for row in ready:
                d = db.execute('SELECT body FROM devices WHERE id=?', (row['id'],)).fetchone()
                ep = self.read(db, 'software_targets', row['id'])
                if not d or self.device_fingerprint(db, row['id']) != row['device_fingerprint'] or ep != row['endpoint']:
                    raise SoftwareError('房间/凭证/维护连接在预检后变化，请重新预检', 409)
                if not idle and db.execute("SELECT 1 FROM software_jobs WHERE status IN ('queued','running') AND json_extract(body,'$.target')=?", (row['id'],)).fetchone(): raise SoftwareError('目标出现其他维护任务，请重新预检', 409)
                if idle:
                    for (raw,) in db.execute("SELECT body FROM software_jobs WHERE status NOT IN ('completed','cancelled','failed','superseded') AND json_extract(body,'$.target')=? AND json_extract(body,'$.software_id')=? AND json_extract(body,'$.delivery')='idle'", (row['id'], plan['software_id'])).fetchall():
                        old=json.loads(raw)
                        if old['status']=='running': old['superseded_by']=ident
                        else: old.update(status='superseded', message='已由新的发布目标替代', finished_at=now())
                        self.write_job(db,old)
                job = {'id': 'sw-'+secrets.token_hex(12), 'batch_id': ident, 'kind': 'apk' if plan['action'] == 'install' else 'fleet_remove',
                       'package_id': plan['package_id'], 'software_id': plan['software_id'], 'name': plan['name'], 'version': plan['version'],
                       'target': row['id'], 'room': row['room'], 'endpoint': ep, 'device_fingerprint': row['device_fingerprint'],
                       'actor': actor, 'status': 'queued', 'created_at': now(), 'deadline': now()+24*3600000, 'steps': [], 'message': '等待批次按顺序执行'}
                if idle:
                    token=db.execute('SELECT token FROM devices WHERE id=?',(row['id'],)).fetchone()[0]
                    job.update(delivery='idle', status='waiting_online', message='等待上线后空闲安装', deadline=0,
                               lease_nonce=secrets.token_hex(16), token_sha256=token[7:] if token.startswith('sha256:') else hashlib.sha256(token.encode()).hexdigest(), attempts=0)
                self.write_job(db, job); jobs.append(job)
            plan.update(status='published' if idle else 'queued', job_count=len(jobs)); db.execute('UPDATE fleet_plans SET body=? WHERE id=?', (encoded(plan), ident)); self.bump(db)
        self.notify(jobs)
        if idle:
            for job in jobs:self.reconcile(job['target'])
        return {'batch': plan, 'jobs': jobs}

    @staticmethod
    def same_authorization(left, right):
        return all(left.get(k) == right.get(k) for k in ('id','port','identity_sha256','enrollment','reboot_verified'))

    def reconcile(self, device_id):
        """An authenticated device heartbeat schedules work; no root operation runs in HTTP."""
        if not self.executor_ready(idle=True):return []
        queued=[]
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            devices={d['id']:d for d in self.devices(db)};d=devices.get(device_id,{})
            ms=d.get('management',{});endpoint=d.get('maintenance',{})
            rows=db.execute("SELECT body FROM software_jobs WHERE status IN ('waiting_online','waiting_idle','waiting_retry') AND json_extract(body,'$.delivery')='idle' AND json_extract(body,'$.target')=? ORDER BY rowid",(device_id,)).fetchall()
            busy=bool(db.execute("SELECT 1 FROM software_jobs WHERE status IN ('queued','running') AND json_extract(body,'$.target')=?",(device_id,)).fetchone())
            changed=False
            for (raw,) in rows:
                j=json.loads(raw);plan=self.read(db,'fleet_plans',j['batch_id']) if j.get('batch_id') else {'status':'published'};status,message='waiting_idle','等待影院首页空闲（播放、暂停、广告及其他应用均等待）'
                if plan['status']=='cancelled':status,message='cancelled','发布已撤回，未执行'
                elif endpoint.get('enrollment')=='pending':status,message='waiting_idle','正在重新核验维护连接，保留发布目标'
                elif not d or not d.get('enabled',True) or self.device_fingerprint(db,device_id)!=j['device_fingerprint'] or not self.same_authorization(endpoint,j['endpoint']):status,message='failed','房号、凭证或首次维护授权变化，请重新核验后发布'
                elif not d.get('online') or now()-ms.get('at',0)>=45000:status,message='waiting_online','等待上线'
                elif j.get('next_attempt_at',0)>now():status,message='waiting_retry','连接中断，等待下次核验'
                elif busy:status,message='waiting_idle','等待本房间上一项维护结束'
                elif ms.get('protocol')!=2 or not ms.get('keep_adb'):status,message='waiting_idle','等待新版空闲回报与有效维护状态'
                elif not ms.get('screen_awake'):status,message='waiting_idle','电视待机，等待客人开机；不唤醒电视'
                elif ms.get('home_idle') and ms.get('idle_ms',0)>=60000 and ms.get('maintenance_job','') in ('',j['id']):
                    try:
                        ip=ipaddress.ip_address(d.get('ip',''))
                        if ip.version!=4 or not ip.is_private or ip.is_loopback or ip.is_multicast or ip.is_unspecified:raise ValueError()
                    except ValueError:status,message='failed','鉴权回报地址不是可维护的酒店局域网地址'
                    else:
                        status,message='queued','电视已上线且空闲，等待安装前再次核验'
                        j['endpoint']={**endpoint,'address':str(ip)};busy=True
                if status!=j['status'] or message!=j.get('message') or status=='queued':
                    j.update(status=status,message=message);self.write_job(db,j);changed=True
                if status=='queued':queued.append(j)
            if changed:self.bump(db)
        self.notify(queued)
        return queued

    def cancel_batch(self, ident, actor, revision):
        identifier(ident, 'batch-')
        with self.db() as db:
            # Cancelling this immutable batch ID never expands its scope. A different room's
            # progress must not prevent the administrator from withdrawing pending work.
            db.execute('BEGIN IMMEDIATE'); plan = self.read(db, 'fleet_plans', ident)
            plan.update(status='cancelled', stopped_by=actor)
            db.execute('UPDATE fleet_plans SET body=? WHERE id=?', (encoded(plan), ident))
            for (raw,) in db.execute("SELECT body FROM software_jobs WHERE status IN ('queued','waiting_online','waiting_idle','waiting_retry','waiting_authorization')"):
                j = json.loads(raw)
                if j.get('batch_id') == ident:
                    j.update(status='cancelled', finished_at=now(), message='批次已停止；未执行，运行中的单台任务等待核验结束'); self.write_job(db, j)
            self.bump(db)
        return {'ok': True}

    def publish(self, body, actor, revision):
        ids = targets(body.get('targets'))
        title = bounded_text(body.get('title'), 100, '通知标题'); content = bounded_text(body.get('body'), 1000, '通知正文')
        seconds, minutes = body.get('display_seconds', 30), body.get('valid_minutes', 60)
        if type(seconds) is not int or not 5 <= seconds <= 300 or type(minutes) is not int or not 1 <= minutes <= 1440 or body.get('confirm') is not True:
            raise SoftwareError('请确认目标，显示时长5–300秒，有效期1–1440分钟')
        request = body.get('request_id', '')
        if not isinstance(request,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', request): raise SoftwareError('通知请求编号无效')
        fingerprint = hashlib.sha256(encoded([sorted(ids), title, content, seconds, minutes, actor]).encode()).hexdigest()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for (raw,) in db.execute('SELECT body FROM fleet_messages'):
                old = json.loads(raw)
                if old.get('request_id') == request:
                    if old['fingerprint'] != fingerprint: raise SoftwareError('请求编号已用于不同通知', 409)
                    return old
            if type(revision) is not int or revision != self.revision(db): raise SoftwareError('目标信息已变化，请刷新预览', 409)
            ds = {d['id']: d for d in self.devices(db)}
            if any(i not in ds or ds[i].get('enabled') is False for i in ids): raise SoftwareError('目标不存在、已归档或停用')
            if db.execute("SELECT count(*) FROM fleet_messages WHERE json_extract(body,'$.expires_at')>? AND json_extract(body,'$.cancelled')=0", (now(),)).fetchone()[0] >= 100: raise SoftwareError('有效通知过多，请先撤回或等待过期')
            m = {'id': 'notice-'+secrets.token_hex(12), 'title': title, 'body': content, 'display_seconds': seconds,
                 'created_at': now(), 'expires_at': now()+minutes*60000, 'actor': actor, 'cancelled': False,
                 'targets': [{'id': i, 'room': ds[i]['room']} for i in ids], 'request_id': request, 'fingerprint': fingerprint}
            db.execute('INSERT INTO fleet_messages VALUES(?,?)', (m['id'], encoded(m))); self.bump(db)
            return m

    def withdraw(self, ident, revision):
        identifier(ident, 'notice-')
        with self.db() as db:
            self.check(db, revision); m = self.read(db, 'fleet_messages', ident); m['cancelled'] = True
            db.execute('UPDATE fleet_messages SET body=? WHERE id=?', (encoded(m), ident)); self.bump(db)
        return {'ok': True}

    def pending_messages(self, device):
        with self.db() as db:
            rows = []
            for (raw,) in db.execute('SELECT body FROM fleet_messages ORDER BY rowid'):
                m = json.loads(raw)
                if not any(t['id'] == device['id'] for t in m['targets']): continue
                if m['cancelled'] or now() >= m['expires_at']: continue
                row = db.execute('SELECT body FROM fleet_receipts WHERE message=? AND device=?', (m['id'], device['id'])).fetchone()
                if row and json.loads(row[0])['status'] in ('dismissed', 'auto_closed'): continue
                rows.append({k: m[k] for k in ('id', 'title', 'body', 'display_seconds', 'expires_at')})
            return {'messages': rows, 'server_time_ms': now()}

    def receipt(self, device, body):
        identifier(body.get('id'), 'notice-')
        state = body.get('status')
        rank = {'fetched': 1, 'permission_missing': 1, 'displayed': 2, 'dismissed': 3, 'auto_closed': 3}
        if not isinstance(state,str) or state not in rank: raise SoftwareError('通知回执无效')
        with self.db() as db:
            m = self.read(db, 'fleet_messages', body.get('id'))
            if not any(t['id'] == device['id'] for t in m['targets']): raise SoftwareError('通知不属于此设备', 403)
            row = db.execute('SELECT body FROM fleet_receipts WHERE message=? AND device=?', (m['id'], device['id'])).fetchone()
            old = json.loads(row[0]) if row else {}
            if old.get('status') in ('dismissed', 'auto_closed') or rank[state] < rank.get(old.get('status'), 0): return {'ok': True}
            value = {**old, 'status': state, 'at': now()}
            if state == 'displayed': value.setdefault('displayed_at', now())
            if state in ('dismissed', 'auto_closed'): value.setdefault('closed_at', now())
            db.execute('INSERT OR REPLACE INTO fleet_receipts VALUES(?,?,?)', (m['id'], device['id'], encoded(value)))
        return {'ok': True}
