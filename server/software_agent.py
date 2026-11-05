#!/usr/bin/env python3
"""Opt-in local installer. Run as a separate service, never in the HTTP process."""
import argparse
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import time
import zipfile
from software_packages import SoftwareError, DeferredInstall, WaitAuthorization, digest, inspect, tool
from software_store import Repository, encoded, now
from fleet_agent import FleetAgent

IDLE_INSTALL_PROTOCOL = 2


class RolledBack(SoftwareError):
    pass


class Runner:
    def __init__(self, log=None):
        self.log = Path(log) if log else None

    def __call__(self, args, timeout=120, allowed=(0,), with_code=False, sensitive=False):
        try:
            process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                output, _ = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate()
                raise SoftwareError('安装命令超时，需检查目标状态后手动重试')
        except OSError:
            raise SoftwareError('执行工具不可用：' + Path(args[0]).name)
        text = output.decode('utf-8', 'replace')[-32000:]
        if self.log:
            if self.log.exists() and self.log.stat().st_size > 5 * 1024 ** 2:
                self.log.replace(self.log.with_suffix('.previous.log'))
            with open(self.log, 'a', encoding='utf-8') as log:
                os.chmod(self.log, 0o600)
                log.write('\n' + Path(args[0]).name + ' exit=' + str(process.returncode) + '\n' + ('[受保护的设备身份读取]' if sensitive else text) + '\n')
        if process.returncode not in allowed:
            reason = re.search(r'INSTALL_FAILED_[A-Z0-9_]+|unauthorized|device offline|no devices|Permission denied', text)
            if reason and reason[0] in ('unauthorized', 'device offline', 'no devices'):
                raise WaitAuthorization('电视 ADB 未授权或离线，请在维护模式确认后手动重试')
            raise SoftwareError('安装工具失败（退出码 ' + str(process.returncode) + '）' + ('：' + reason[0] if reason else '；请在本机执行器日志检查依赖、权限及兼容性'))
        return (process.returncode, text) if with_code else text


class Agent(FleetAgent):
    def __init__(self, data, state, releases='/opt/hotel-tv/releases', current='/opt/hotel-tv/current', service='hotel-tv', admin_port=18791, runner=None):
        self.repo = Repository(data)
        self.state = Path(state)
        self.releases, self.current = Path(releases), Path(current)
        self.service, self.admin_port = service, admin_port
        self.run = runner or Runner(self.state / 'installer.log')
        self.deployment_lock = self.state / 'deployment.lock'
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state, 0o700)

    def heartbeat(self):
        with self.repo.db() as db:
            db.execute("INSERT OR REPLACE INTO software_meta VALUES('agent',?)", (encoded({'at': now(), 'system': platform.system(), 'version': 1}),))

    def save(self, job, **changes):
        job.update(changes)
        with self.repo.db() as db:
            latest=db.execute('SELECT body FROM software_jobs WHERE id=?',(job['id'],)).fetchone()
            if latest and json.loads(latest[0]).get('superseded_by'):
                job['superseded_by']=json.loads(latest[0])['superseded_by']
                if job['status'] in ('queued','waiting_idle','waiting_online','waiting_retry','failed'):
                    job.update(status='superseded',message='已由新发布替代，不再重试旧版本',finished_at=now())
            self.repo.write_job(db, job)
            self.repo.bump(db)

    def step(self, job, message):
        job['steps'].append({'at': now(), 'message': message})
        self.save(job, message=message)
        self.heartbeat()

    def recover(self):
        # Never replay a package manager operation after a crash.
        with self.repo.db() as db:
            rows = [json.loads(r[0]) for r in db.execute("SELECT body FROM software_jobs WHERE status='running'")]
        for job in rows:
            if job.get('delivery') == 'idle':
                self.save(job,status='waiting_online',message='执行器重启；等待上线，先核验实际安装结果',next_attempt_at=0)
            elif job.get('kind') == 'system' and job.get('previous_release'):
                try:
                    with self.deploy_guard():
                        current = self.current.resolve()
                        allowed = {Path(job['previous_release']).resolve(), Path(job.get('new_release', job['previous_release'])).resolve()}
                        if current not in allowed:
                            raise SoftwareError('当前生产版本已由其他发布变更，保留新版本，请人工核对中断任务')
                        if current == Path(job['previous_release']).resolve():
                            self.run(['systemctl', 'start', self.service])
                            self.wait_health()
                        else:
                            self.restore_system(job)
                    self.save(job, status='rolled_back', message='执行器中断，已恢复升级前程序和数据库；请检查后手动重试', finished_at=now())
                except Exception as exc:
                    self.save(job, status='interrupted', message=str(exc) if isinstance(exc, SoftwareError) else '升级中断且自动恢复未完成，请按备份说明恢复服务', finished_at=now())
            else:
                self.save(job, status='interrupted', message='执行器中断；未自动重装，请检查实际版本后手动重试', finished_at=now())
            self.fleet_finished(job)

    def once(self):
        self.heartbeat()
        with self.repo.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT body FROM software_jobs WHERE status='queued' ORDER BY rowid LIMIT 1").fetchone()
            if not row:
                return False
            job = json.loads(row[0])
            job.update(status='running', started_at=now(), message='校验安装包和目标')
            self.repo.write_job(db, job)
            self.repo.bump(db)
            item = None if job['kind'] in ('fleet_probe', 'fleet_remove') else self.repo.read(db, 'software_packages', job['package_id'])
        try:
            self.fleet_guard(job)
            if item is None:
                self.fleet_task(job)
            else:
                self.install_artifact(job, item)
        except DeferredInstall as exc:
            attempts=job.get('attempts',0)+1
            self.save(job,status=exc.wait_status,message=str(exc),attempts=attempts,next_attempt_at=now()+min(300000,15000*2**min(attempts-1,5)))
        except WaitAuthorization as exc:
            self.save(job, status='waiting_authorization', message=str(exc), finished_at=now())
        except RolledBack as exc:
            self.save(job, status='rolled_back', message=str(exc), finished_at=now())
        except Exception as exc:
            if not isinstance(exc, SoftwareError):
                import traceback
                failure_log = self.state / 'exception.log'
                with open(failure_log, 'w') as output:
                    os.chmod(failure_log, 0o600)
                    traceback.print_exc(file=output)
            message = str(exc) if isinstance(exc, SoftwareError) else '执行器异常（' + type(exc).__name__ + '），请检查服务日志和目标版本'
            self.save(job, status='failed', message=message, finished_at=now())
        finally:
            self.fleet_finished(job)
        return True

    def install_artifact(self, job, item):
        with tempfile.TemporaryDirectory(prefix='install-', dir=self.state) as temp:
            source = self.repo.package_path(item)
            artifact = Path(temp) / ('package' + Path(item['filename']).suffix.lower())
            fd = os.open(str(source), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(fd, 'rb') as incoming, open(artifact, 'xb') as outgoing:
                shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
            if digest(artifact) != item['sha256']:
                raise SoftwareError('安装前文件 SHA256 校验失败')
            metadata = inspect(artifact, item['filename'])
            if any(metadata.get(key) != item.get(key) for key in ('kind', 'software_id', 'version')):
                raise SoftwareError('软件元信息已变化，拒绝执行')
            self.step(job, '安装包 SHA256 和元信息复核通过')
            if item['kind'] == 'apk':
                self.install_apk(job, item, artifact)
            elif item['kind'] == 'deb':
                self.install_deb(job, item, artifact)
            elif item['kind'] == 'system':
                self.install_system(job, item, artifact)
            inventory = {k: item[k] for k in ('software_id', 'name', 'version', 'sha256', 'kind')}
            if item['kind']=='apk':inventory['installed']=True
            inventory.update(target=job['target'], verified_at=now(), job_id=job['id'])
            with self.repo.db() as db:
                db.execute('INSERT OR REPLACE INTO software_inventory VALUES(?,?,?)', (job['target'], item['software_id'], encoded(inventory)))
            self.save(job, status='completed', message='安装完成，目标版本已核验' + ('；' + '；'.join(job['warnings']) if job.get('warnings') else ''), finished_at=now())

    def clear_signals(self):
        directory = self.repo.data / 'software' / 'queue'
        if not directory.exists():
            return
        fd = os.open(str(directory), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for name in os.listdir(fd):
                if not re.fullmatch(r'sw-[0-9a-f]{24}', name):
                    continue
                with self.repo.db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    row = db.execute('SELECT status FROM software_jobs WHERE id=?', (name,)).fetchone()
                    if not row or row[0] != 'queued':
                        try:
                            os.unlink(name, dir_fd=fd)
                        except FileNotFoundError:
                            pass
        finally:
            os.close(fd)

    def install_apk(self, job, item, artifact):
        if job.get('delivery') == 'idle':
            return self.install_idle_apk(job,item,artifact)
        target = job['endpoint']
        serial = target['address'] + ':' + str(target['port'])
        adb = [tool('adb'), '-P', '5039']  # Dedicated local server; do not alter the operator's ADB session.
        try:
            result = self.run(adb + ['connect', serial], timeout=20)
            if 'connected to ' + serial not in result and 'already connected' not in result:
                raise WaitAuthorization('电视维护 ADB 未连通；本系统不会开启调试端口，请在电视端授权后手动重试')
            state = self.run(adb + ['-s', serial, 'get-state'], timeout=15).strip()
            if state != 'device':
                raise WaitAuthorization('电视尚未授权 ADB，请确认维护连接后重试')
            self.fleet_identity(job, adb, serial)
            sdk = self.run(adb + ['-s', serial, 'shell', 'getprop', 'ro.build.version.sdk']).strip()
            if not sdk.isdigit() or int(sdk) < item['min_sdk']:
                raise SoftwareError('电视 Android 版本不满足该 APK 的最低要求')
            self.step(job, '已连接维护 ADB，检查兼容性；保留应用数据覆盖安装')
            existing = self.run(adb + ['-s', serial, 'shell', 'dumpsys', 'package', item['software_id']])
            codes = re.findall(r'\bversionCode=(\d+)', existing)
            if codes and int(codes[0]) > item['version_code']:
                raise SoftwareError('目标已安装更高版本；禁止自动卸载、清数据或降级')
            result = self.run(adb + ['-s', serial, 'install', '-r', str(artifact)], timeout=600)
            if not re.search(r'^Success\s*$', result, re.M):
                raise SoftwareError('电视未报告安装成功，请检查签名、空间和安装权限')
            installed = self.run(adb + ['-s', serial, 'shell', 'dumpsys', 'package', item['software_id']])
            if not re.search(r'\bversionCode=' + str(item['version_code']) + r'\b', installed):
                raise SoftwareError('电视安装结束，但实际版本核验不一致')
        finally:
            try:
                self.run(adb + ['disconnect', serial], timeout=10)
            except SoftwareError:
                job.setdefault('warnings', []).append('维护 ADB 断开失败，请在员工维护中手动关闭连接')

    def install_deb(self, job, item, artifact):
        architecture = self.run(['dpkg', '--print-architecture']).strip()
        if item['architecture'] not in ('all', architecture):
            raise SoftwareError('安装包架构 ' + item['architecture'] + ' 与服务器 ' + architecture + ' 不匹配')
        query_code, query_text = self.run(['dpkg-query', '-W', '-f=${Version}', item['software_id']], allowed=(0, 1), with_code=True)
        current = query_text.strip() if query_code == 0 else ''
        if current:
            try:
                self.run(['dpkg', '--compare-versions', current, 'le', item['version']])
            except SoftwareError:
                raise SoftwareError('服务器已安装更高版本，禁止自动降级')
        self.step(job, '核验服务器架构，安装本地 DEB；依赖包按 APT 配置获取')
        self.run(['apt-get', '-y', '--no-remove', '-o', 'APT::Sandbox::User=root', '-o', 'Dpkg::Options::=--force-confold', 'install', str(artifact)], timeout=1800)
        result = self.run(['dpkg-query', '-W', '-f=${Status}\n${Version}', item['software_id']]).strip().splitlines()
        if result != ['install ok installed', item['version']]:
            raise SoftwareError('DEB 安装后版本或配置状态不符合预期，请检查依赖')

    def deploy_guard(self):
        from contextlib import contextmanager
        @contextmanager
        def hold():
            with open(self.deployment_lock, 'a') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise SoftwareError('已有共同版本发布正在执行，请稍后手动重试')
                yield
        return hold()

    def switch(self, target):
        temporary = self.current.with_name(self.current.name + '.software-next')
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(target)
        temporary.replace(self.current)

    def backup_database(self, path):
        # Create the backup as the DB owner, then move it into root-only storage.
        owner = self.repo.path.stat()
        directory = self.repo.data / 'software'
        directory.mkdir(exist_ok=True)
        temporary = directory / (path.name + '.temporary')
        fd = os.open(str(temporary), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        if os.geteuid() == 0:
            os.chown(temporary, owner.st_uid, owner.st_gid)
        try:
            with self.repo.db() as source:
                destination = sqlite3.connect(str(temporary))
                try:
                    source.backup(destination)
                    destination.commit()
                    destination.execute('PRAGMA journal_mode=DELETE').fetchone()
                finally:
                    destination.close()
            temporary.replace(path)
            if os.geteuid() == 0:
                os.chown(path, 0, 0)
            os.chmod(path, 0o600)
        finally:
            temporary.unlink(missing_ok=True)

    def restore_system(self, job):
        previous, backup = Path(job['previous_release']), Path(job['database_backup'])
        if previous.parent.resolve() != self.releases.resolve() or not previous.is_dir():
            raise SoftwareError('升级恢复信息不完整')
        self.run(['systemctl', 'stop', self.service])
        if not job.get('backup_ready'):
            self.switch(previous)
            self.run(['systemctl', 'start', self.service])
            self.wait_health()
            return
        if not backup.is_file():
            self.switch(previous)
            self.run(['systemctl','start',self.service])
            raise SoftwareError('升级前数据库备份不存在；已切回原程序并尝试恢复服务，请核对业务数据库')
        # Keep queue/audit history from the interrupted run while restoring all business tables.
        try:
            with self.repo.db() as db:
                history = {table: [tuple(r) for r in db.execute('SELECT * FROM ' + table)] for table in ('software_packages', 'software_jobs', 'software_inventory', 'software_targets', 'software_meta')}
        except sqlite3.Error:
            history = {}
        try:
            source = sqlite3.connect(backup.as_uri() + '?mode=ro', uri=True)
            try:
                source.execute('PRAGMA schema_version').fetchone()
                with self.repo.db() as destination: source.backup(destination)
            finally:
                source.close()
            try:
                with self.repo.db() as db:
                    for table, rows in history.items():
                        db.execute('DELETE FROM ' + table)
                        if rows:
                            db.executemany('INSERT INTO ' + table + ' VALUES(' + ','.join('?' for _ in rows[0]) + ')', rows)
            except sqlite3.Error:
                # New queue schema must never prevent the old business program restarting.
                job.setdefault('warnings', []).append('已恢复业务备份；新版维护历史与旧表不兼容，未回填')
        finally:
            self.switch(previous)
            self.run(['systemctl', 'start', self.service])
        self.wait_health()

    def wait_health(self):
        for _ in range(30):
            connection = http.client.HTTPConnection('127.0.0.1', self.admin_port, timeout=2)
            try:
                connection.request('GET', '/api/admin/session')
                response = connection.getresponse()
                data = json.loads(response.read())
                if response.status == 200 and 'user' in data:
                    return
            except (OSError, ValueError):
                pass
            finally:
                connection.close()
            time.sleep(1)
        raise SoftwareError('影院新版本启动后健康检查失败')

    def install_system(self, job, item, artifact):
        with self.deploy_guard():
            return self.install_system_locked(job, item, artifact)

    def install_system_locked(self, job, item, artifact):
        previous = self.current.resolve(strict=True)
        if not self.current.is_symlink() or previous.parent.resolve() != self.releases.resolve() or previous.name != item['base_release']:
            raise SoftwareError('当前正式版本与升级包 base_release 不一致，防止覆盖其他迭代，请重新打包')
        with zipfile.ZipFile(artifact) as archive:
            expanded = sum(entry.file_size for entry in archive.infolist())
        self.releases.mkdir(parents=True, exist_ok=True)
        reserve = 512 * 1024 ** 2
        database_size = self.repo.path.stat().st_size if self.repo.path.exists() else 0
        if shutil.disk_usage(self.releases).free < expanded + database_size * 2 + reserve or shutil.disk_usage(self.state).free < database_size * 2 + reserve:
            raise SoftwareError('升级可用空间不足；尚未停止服务，请腾出空间后重试')
        destination = self.releases / item['version']
        if destination.exists():
            marker = destination / '.software-package-sha256'
            if destination.is_symlink() or not marker.is_file() or marker.read_text() != item['sha256'] or self.current.resolve() == destination.resolve():
                raise SoftwareError('目标发布目录已存在且不是本包的失败暂存，请使用新的发布版本号')
            shutil.rmtree(destination)
        self.releases.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(artifact) as archive:
            manifest = json.loads(archive.read('hotel-software.json'))
            destination.mkdir(mode=0o755)
            try:
                for filename in manifest['files']:
                    path = destination / filename
                    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                    with archive.open(filename) as source, open(path, 'xb') as output:
                        shutil.copyfileobj(source, output, 1024 * 1024)
                    os.chmod(path, 0o644)
                # Check syntax without importing/executing the uploaded program as root.
                for path in (destination / 'server').rglob('*.py'):
                    compile(path.read_bytes(), str(path), 'exec')
                (destination / '.software-package-sha256').write_text(item['sha256'])
            except Exception:
                shutil.rmtree(destination)
                raise SoftwareError('影院程序预检失败，原服务未切换')
        backup = self.state / (job['id'] + '-before.sqlite3')
        self.step(job, '停止影院写入，备份数据库后切换程序；已有媒体文件保留')
        self.save(job, previous_release=str(previous), database_backup=str(backup), new_release=str(destination), backup_ready=False)
        self.run(['systemctl', 'stop', self.service])
        try:
            self.backup_database(backup)
            self.save(job, backup_ready=True)
            self.switch(destination)
            self.run(['systemctl', 'start', self.service])
            self.wait_health()
        except Exception:
            if job.get('database_backup'):
                self.restore_system(job)
                raise RolledBack('影院新版本启动失败，已恢复升级前程序和数据库')
            self.run(['systemctl', 'start', self.service])
            raise
        self.step(job, '影院新版本健康检查通过，升级前数据库与程序均已保留')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, default=Path('/srv/hotel-tv/data'))
    parser.add_argument('--state', type=Path, default=Path('/var/lib/hotel-tv-installer'))
    parser.add_argument('--releases', default='/opt/hotel-tv/releases')
    parser.add_argument('--current', default='/opt/hotel-tv/current')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--drain', action='store_true', help='process manually queued tasks and exit; activated by systemd.path')
    args = parser.parse_args()
    os.umask(0o027)
    if platform.system() != 'Linux' or os.geteuid() != 0:
        parser.error('安装执行器需在 Ubuntu 本机以 root 独立服务运行')
    os.environ['DEBIAN_FRONTEND'] = 'noninteractive'
    os.environ['ANDROID_USER_HOME'] = str(args.state / 'android')
    (args.state / 'android').mkdir(parents=True, exist_ok=True, mode=0o700)
    agent = Agent(args.data, args.state, args.releases, args.current)
    agent.deployment_lock = Path('/var/lock/hotel-tv-deploy.lock')
    with open(args.state / 'agent.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        agent.recover()
        while agent.once():
            if args.once:
                break
        agent.clear_signals()


if __name__ == '__main__':
    main()
