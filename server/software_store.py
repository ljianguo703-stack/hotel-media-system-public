"""Durable software queue, separate from business configuration and scan jobs."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from software_packages import SoftwareError, digest, inspect

ACTIVE = ('queued', 'running')
RETRYABLE = ('failed', 'waiting_authorization', 'interrupted', 'rolled_back')


def now():
    return int(time.time() * 1000)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def initialize(db):
    statements = '''
      CREATE TABLE IF NOT EXISTS software_packages(id TEXT PRIMARY KEY,body TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS software_jobs(id TEXT PRIMARY KEY,status TEXT NOT NULL,body TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS software_inventory(target TEXT NOT NULL,software TEXT NOT NULL,body TEXT NOT NULL,PRIMARY KEY(target,software));
      CREATE TABLE IF NOT EXISTS software_targets(id TEXT PRIMARY KEY,body TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS software_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
      INSERT OR IGNORE INTO software_meta VALUES('revision','0');
    '''
    for statement in statements.split(';'):
        if statement.strip():
            db.execute(statement)
    from fleet_store import initialize as initialize_fleet
    initialize_fleet(db)


class Repository:
    def __init__(self, data):
        self.data = Path(data).resolve()
        self.path = self.data / 'events.sqlite3'
        self.packages = self.data / 'software' / 'packages'

    @contextmanager
    def db(self):
        # The privileged agent must create SQLite WAL/SHM files as the web service owner.
        # The installer is a single-threaded systemd oneshot process.
        uid, gid = os.geteuid(), os.getegid()
        owner = self.path.stat()
        switched = uid == 0 and owner.st_uid != 0
        if switched:
            os.setegid(owner.st_gid)
            os.seteuid(owner.st_uid)
        db = None
        try:
            db = sqlite3.connect(str(self.path), timeout=15)
            db.row_factory = sqlite3.Row
            with db:
                yield db
        finally:
            if db is not None:
                db.close()
            if switched:
                os.seteuid(uid)
                os.setegid(gid)

    @staticmethod
    def revision(db):
        return int(db.execute("SELECT value FROM software_meta WHERE key='revision'").fetchone()[0])

    @staticmethod
    def bump(db):
        db.execute("UPDATE software_meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")

    def check(self, db, expected):
        db.execute('BEGIN IMMEDIATE')
        if type(expected) is not int or expected != self.revision(db):
            raise SoftwareError('软件信息已变化，请刷新后重试', 409)

    @staticmethod
    def read(db, table, ident):
        row = db.execute('SELECT body FROM ' + table + ' WHERE id=?', (ident,)).fetchone()
        if not row:
            raise SoftwareError('软件包或任务不存在', 404)
        return json.loads(row[0])

    @staticmethod
    def write_job(db, job):
        job['updated_at'] = now()
        db.execute('INSERT OR REPLACE INTO software_jobs VALUES(?,?,?)', (job['id'], job['status'], encoded(job)))

    def state(self):
        with self.db() as db:
            revision = self.revision(db)
            packages = [json.loads(r[0]) for r in db.execute('SELECT body FROM software_packages ORDER BY rowid DESC')]
            jobs = [json.loads(r[0]) for r in db.execute('SELECT body FROM software_jobs ORDER BY rowid DESC LIMIT 200')]
            inventory = [json.loads(r[0]) for r in db.execute('SELECT body FROM software_inventory')]
            targets = [json.loads(r[0]) for r in db.execute('SELECT body FROM software_targets')]
            row = db.execute("SELECT value FROM software_meta WHERE key='agent'").fetchone()
            agent = json.loads(row[0]) if row else {}
        agent['online'] = now() - agent.get('at', 0) < 30000
        agent['configured'] = Path('/etc/systemd/system/hotel-tv-installer.path').is_file()
        return {'revision': revision, 'packages': packages, 'jobs': jobs, 'inventory': inventory, 'targets': targets, 'agent': agent}

    def register(self, path, filename, actor):
        metadata = inspect(path, filename)
        sha = digest(path)
        ident = 'pkg-' + sha[:32]
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT body FROM software_packages WHERE id=?', (ident,)).fetchone()
            if previous:
                return json.loads(previous[0])
            for row in db.execute('SELECT body FROM software_packages'):
                item = json.loads(row[0])
                if (item['kind'], item['software_id'], item['version']) == (metadata['kind'], metadata['software_id'], metadata['version']):
                    raise SoftwareError('同一软件版本已存在不同安装包，请提高版本号', 409)
            self.packages.mkdir(parents=True, exist_ok=True)
            final = self.packages / (ident + Path(filename).suffix.lower())
            path.replace(final)
            item = {**metadata, 'id': ident, 'filename': filename[:200], 'sha256': sha, 'bytes': final.stat().st_size,
                    'uploaded_at': now(), 'actor': actor, 'notes': ''}
            db.execute('INSERT INTO software_packages VALUES(?,?)', (ident, encoded(item)))
            self.bump(db)
            return item

    def package_path(self, item):
        if not re.fullmatch(r'pkg-[0-9a-f]{32}', item['id']) or Path(item['filename']).suffix.lower() not in ('.apk', '.deb', '.zip'):
            raise SoftwareError('软件包记录无效')
        path = self.packages / (item['id'] + Path(item['filename']).suffix.lower())
        if path.is_symlink() or not path.is_file() or digest(path) != item['sha256']:
            raise SoftwareError('安装包缺失或 SHA256 已变化，已停止执行', 409)
        return path

    def queue(self, package_id, targets, actor, revision, request_id):
        if not isinstance(targets, list) or not 1 <= len(targets) <= 500 or any(not isinstance(t, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', t) for t in targets) or len(targets) != len(set(targets)):
            raise SoftwareError('请选择 1–500 个不同安装目标')
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', request_id):
            raise SoftwareError('安装请求编号无效')
        fingerprint = hashlib.sha256(encoded([package_id, sorted(targets), actor]).encode()).hexdigest()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = [json.loads(r[0]) for r in db.execute('SELECT body FROM software_jobs')]
            old = [j for j in previous if j.get('request_id') == request_id]
            if old:
                if any(j.get('fingerprint') != fingerprint for j in old):
                    raise SoftwareError('安装请求编号已用于其他操作', 409)
                return {'jobs': old}
            if type(revision) is not int or revision != self.revision(db):
                raise SoftwareError('软件信息已变化，请刷新后重试', 409)
            item = self.read(db, 'software_packages', package_id)
            self.package_path(item)
            if item['platform']=='tv' and len(targets)>1:
                raise SoftwareError('多台电视请使用批量运维，先完成逐台纳管并预检目标房间')
            if item['platform'] == 'server' and targets != ['server'] or item['platform'] == 'tv' and 'server' in targets:
                raise SoftwareError('软件包与目标平台不匹配')
            jobs = []
            for target in targets:
                if target != 'server':
                    row = db.execute('SELECT body FROM devices WHERE id=?', (target,)).fetchone()
                    device = json.loads(row[0]) if row else {}
                    if not device or not device.get('enabled', True) or device.get('_archived'):
                        raise SoftwareError('电视不存在或已停用：' + target)
                    # Enrollment is mandatory: do not guess/open ADB from the last-seen IP.
                    endpoint = self.read(db, 'software_targets', target)
                    if endpoint.get('enrollment') != 'verified' or not endpoint.get('identity_sha256'):
                        raise SoftwareError('请先在批量运维中完成本台身份纳管；不能按手填IP安装')
                    status = db.execute('SELECT body FROM fleet_status WHERE id=?', (target,)).fetchone()
                    managed = json.loads(status[0]) if status else {}
                    if managed.get('protocol') != 2 or not managed.get('keep_adb'):
                        raise SoftwareError('请用员工工具升级至支持空闲维护的客户端，再从后台发布')
                if any(j['target'] == target and j['status'] in ACTIVE for j in previous):
                    raise SoftwareError('目标已有待执行或执行中的安装任务：' + target, 409)
                row = db.execute('SELECT body FROM software_inventory WHERE target=? AND software=?', (target, item['software_id'])).fetchone()
                if row and json.loads(row[0]).get('sha256') == item['sha256'] and json.loads(row[0]).get('installed') is not False:
                    raise SoftwareError('目标已核验安装该版本：' + target, 409)
                job = {'id': 'sw-' + secrets.token_hex(12), 'package_id': item['id'], 'name': item['name'], 'software_id': item['software_id'],
                       'version': item['version'], 'kind': item['kind'], 'target': target, 'actor': actor, 'status': 'queued',
                       'created_at': now(), 'request_id': request_id, 'fingerprint': fingerprint, 'message': '等待本机安装任务启动', 'steps': []}
                if target != 'server':
                    job['endpoint'] = endpoint
                    from fleet_store import FleetRepository
                    token = db.execute('SELECT token FROM devices WHERE id=?', (target,)).fetchone()[0]
                    job.update(delivery='idle', device_fingerprint=FleetRepository.device_fingerprint(db,target),
                               lease_nonce=secrets.token_hex(16),token_sha256=token[7:] if token.startswith('sha256:') else hashlib.sha256(token.encode()).hexdigest(),attempts=0)
                self.write_job(db, job)
                jobs.append(job)
            self.bump(db)
            return {'jobs': jobs}

    def control(self, ident, action, actor, revision, request_id=''):
        if not isinstance(ident, str) or not re.fullmatch(r'sw-[0-9a-f]{24}', ident):
            raise SoftwareError('软件任务编号无效')
        if action == 'retry':
            with self.db() as db:
                old = self.read(db, 'software_jobs', ident)
            if old['status'] not in RETRYABLE:
                raise SoftwareError('只有失败、待授权或中断任务可以手动重试')
            if old['kind'] == 'fleet_probe':
                from fleet_store import FleetRepository
                return FleetRepository(self.data).probe(old['target'], actor, revision)
            if old.get('batch_id'):
                raise SoftwareError('此任务属于批次，请在批量运维中重新预检未完成目标')
            return self.queue(old['package_id'], [old['target']], actor, revision, request_id)
        if action != 'cancel':
            raise SoftwareError('软件任务操作无效')
        with self.db() as db:
            self.check(db, revision)
            job = self.read(db, 'software_jobs', ident)
            if job['status'] not in ('queued', 'waiting_authorization', 'waiting_online', 'waiting_idle', 'waiting_retry'):
                raise SoftwareError('执行中的安装不可直接取消；请等待结果', 409)
            job.update(status='cancelled', message='管理员已取消', finished_at=now())
            self.write_job(db, job)
            self.bump(db)
        return {'job': job}

    def notify(self, jobs):
        directory = self.data / 'software' / 'queue'
        directory.mkdir(parents=True, exist_ok=True)
        if directory.is_symlink():
            raise SoftwareError('任务已保存，但安装触发目录无效，请管理员检查', 409)
        for job in jobs:
            if job.get('status') != 'queued':
                continue
            path = directory / job['id']
            try:
                fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o640)
                os.close(fd)
            except FileExistsError:
                if path.is_symlink():
                    raise SoftwareError('安装触发文件无效，请管理员检查', 409)

    @staticmethod
    def validated_target(body, actor):
        import ipaddress
        ident, address, port = body.get('id'), body.get('address'), body.get('port', 5555)
        if not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', ident):
            raise SoftwareError('电视编号无效')
        try:
            ip = ipaddress.ip_address(address)
            allowed = any(ip in ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
        except (ValueError, TypeError):
            allowed = False
        if not allowed or type(port) is not int or not 1024 <= port <= 65535 or body.get('confirm') is not True:
            raise SoftwareError('请填写酒店局域网 IPv4 与维护端口，并确认已在电视上授权 ADB')
        return {'id': ident, 'address': str(ip), 'port': port, 'actor': actor, 'updated_at': now()}

    def save_target(self, body, actor, revision):
        item = self.validated_target(body, actor)
        ident = item['id']
        with self.db() as db:
            self.check(db, revision)
            row = db.execute('SELECT body FROM devices WHERE id=?', (ident,)).fetchone()
            device = json.loads(row[0]) if row else {}
            if not device or device.get('_archived') or not device.get('enabled', True):
                raise SoftwareError('请选择已启用的电视设备')
            previous = db.execute('SELECT body FROM software_targets WHERE id=?', (ident,)).fetchone()
            if previous and json.loads(previous[0]).get('identity_sha256'):
                raise SoftwareError('已纳管电视请使用重新核验更新连接；不能覆盖物理身份')
            db.execute('INSERT OR REPLACE INTO software_targets VALUES(?,?)', (ident, encoded(item)))
            self.bump(db)
        return item
