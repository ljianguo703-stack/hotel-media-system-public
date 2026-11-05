"""Fleet hooks for the existing privileged installer; fixed operations only."""
import hashlib
import json
import re
import time
import base64
from pathlib import Path
from fleet_store import FleetRepository, PROTECTED
from software_packages import SoftwareError, DeferredInstall, WaitAuthorization, tool
from software_store import encoded, now

IDLE_INSTALL_PROTOCOL = 2


class FleetAgent:
    def fleet_guard(self, job):
        if not job.get('batch_id') and job.get('delivery')!='idle': return
        with self.repo.db() as db:
            latest=db.execute('SELECT body FROM software_jobs WHERE id=?',(job['id'],)).fetchone()
            if latest and json.loads(latest[0]).get('superseded_by'): raise SoftwareError('此安装目标已被新发布替代')
            plan = self.repo.read(db, 'fleet_plans', job['batch_id']) if job.get('batch_id') else {'status':'published'}
            if plan['status'] in ('cancelled', 'paused') or job.get('delivery') != 'idle' and now() >= job.get('deadline', 0):
                raise SoftwareError('批次已停止或任务过期，不再执行')
            if FleetRepository.device_fingerprint(db, job['target']) != job.get('device_fingerprint'):
                raise SoftwareError('房号、启用状态或设备凭证已变化，停止本批次')
            endpoint = self.repo.read(db, 'software_targets', job['target'])
            same = self.repo_endpoint_equal(endpoint, job)
            if not same or endpoint.get('enrollment') != 'verified':
                raise SoftwareError('维护授权或连接已变化，请重新预检')

    @staticmethod
    def repo_endpoint_equal(endpoint, job):
        return FleetRepository.same_authorization(endpoint, job['endpoint']) if job.get('delivery') == 'idle' else endpoint == job['endpoint']

    def install_idle_apk(self, job, item, artifact):
        """Reuse authorized ADB, with a device-side idle lease and exact installed-byte checks."""
        adb=[tool('adb'), '-P', '5039'];serial=job['endpoint']['address']+':'+str(job['endpoint']['port'])
        acquired=False
        unknown_outcome=False
        receiver='com.xjg.hoteltv/.hotel.HotelMaintenanceReceiver'
        def broadcast(operation):
            return self.run(adb+['-s',serial,'shell','am','broadcast','-a','com.xjg.hoteltv.MAINTENANCE',
                '-n',receiver,'--es','operation',operation,'--es','job',job['id'],'--es','nonce',job['lease_nonce'],
                '--es','token_sha256',job['token_sha256']],timeout=20)
        def installed_matches():
            dump=self.run(adb+['-s',serial,'shell','dumpsys','package',item['software_id']],timeout=20,allowed=(0,1))
            codes=re.findall(r'\bversionCode=(\d+)',dump)
            if codes and int(codes[0])>item['version_code']:raise SoftwareError('目标已有更高版本，发布不降级、不卸载清数据')
            if not codes or int(codes[0])!=item['version_code']:return False
            # Xiaomi retains version metadata after a keep-data uninstall, while pm path
            # correctly returns exit 1 and no APK. This is an absent package, not a failed install.
            paths=self.run(adb+['-s',serial,'shell','pm','path',item['software_id']],timeout=20,allowed=(0,1)).splitlines()
            if len(paths)!=1 or not paths[0].startswith('package:/'):return False
            path=paths[0][8:]
            if '\n' in path or '..' in Path(path).parts or not path.endswith('.apk'):raise SoftwareError('电视回报安装路径无效')
            current=Path(artifact).parent/'installed-check.apk'
            self.run(adb+['-s',serial,'pull',path,str(current)],timeout=60)
            matched=current.is_file() and hashlib.sha256(current.read_bytes()).hexdigest()==item['sha256']
            if matched:
                names=re.findall(r'\bversionName=([^\r\n]+)',dump)
                job.update(actual_version=names[0].strip() if names else item['version'],actual_version_code=int(codes[0]))
            return matched
        try:
            self.run(adb+['connect',serial],timeout=20,allowed=(0,1))
            state=self.run(adb+['devices'],timeout=15)
            if re.search(re.escape(serial)+r'\s+unauthorized\b',state):raise WaitAuthorization('本房维护授权已失效，需员工核验；未发安装命令')
            if not re.search(re.escape(serial)+r'\s+device\b',state):raise DeferredInstall('waiting_online','等待上线后核验实际版本')
            self.fleet_identity(job,adb,serial)
            sdk=self.run(adb+['-s',serial,'shell','getprop','ro.build.version.sdk']).strip()
            if not sdk.isdigit() or int(sdk)<item['min_sdk']:raise SoftwareError('电视Android版本不满足此APK')
            if installed_matches():
                # A previous timed-out attempt may still own this exact job's
                # lease. Verified bytes resolve that uncertainty without reinstall.
                try:broadcast('release')
                except SoftwareError:job.setdefault('warnings',[]).append('实际安装已核验，维护锁释放回执未到，将在本机超时解除')
                self.step(job,'实装版本及APK字节已与发布目标相同，未重复安装');return
            result=broadcast('prepare')
            if 'HOTEL_BINDING_CHANGED' in result:raise SoftwareError('电视接入凭证已变化，未安装；请核验房间归属')
            if 'HOTEL_READY:'+job['lease_nonce'] not in result:raise DeferredInstall('waiting_idle','等待影院首页空闲；客人播放、操作或使用其他应用时不安装')
            acquired=True
            self.fleet_guard(job)
            self.step(job,'电视已再次确认空闲并暂锁播放入口，保留数据安装')
            unknown_outcome=True
            result=self.run(adb+['-s',serial,'install','-r',str(artifact)],timeout=300)
            if not re.search(r'^Success\s*$',result,re.M):raise SoftwareError('电视未确认安装成功')
            if not installed_matches():raise SoftwareError('安装后实际版本或APK字节与发布目标不符，未记成功')
            unknown_outcome=False
            self.step(job,'实际版本及APK SHA256核验通过')
        except DeferredInstall:
            raise
        except SoftwareError as exc:
            if 'INSTALL_FAILED' in str(exc) or '电视未确认安装成功' in str(exc):unknown_outcome=False
            # A disconnect during install has an unknown outcome. Reconnect and check bytes
            # on the next attempt rather than replaying an install blindly.
            try:state=self.run(adb+['devices'],timeout=10)
            except SoftwareError:state=''
            if re.search(re.escape(serial)+r'\s+unauthorized\b',state):
                raise WaitAuthorization('本房维护授权失效，需员工核验；未自动重新授权')
            if not re.search(re.escape(serial)+r'\s+(device|unauthorized)\b',state):
                raise DeferredInstall('waiting_online','连接中断；上线后先核对实际版本，再决定是否补装')
            if isinstance(exc,WaitAuthorization):raise DeferredInstall('waiting_retry','连接已恢复，稍后重新核验')
            if unknown_outcome and '超时' in str(exc):
                raise DeferredInstall('waiting_retry','安装结果待核验；保留本机维护锁至到期，下次先核对实际版本')
            raise
        finally:
            if acquired and not unknown_outcome:
                try:broadcast('release')
                except SoftwareError:job.setdefault('warnings',[]).append('空闲维护锁释放回执未到，将在本机超时自动解除')
            try:self.run(adb+['disconnect',serial],timeout=10)
            except SoftwareError:pass

    def fleet_identity(self, job, adb, serial):
        identity = self.run(adb+['-s', serial, 'shell', 'settings', 'get', 'secure', 'android_id'], sensitive=True).strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]{4,128}', identity) or identity == 'null': raise SoftwareError('无法核验电视物理身份')
        hashed = hashlib.sha256(identity.encode()).hexdigest()
        expected = job['endpoint'].get('identity_sha256')
        derived = 'tv-'+hashlib.sha256(('hotel-tv-backend-device:'+hashed).encode()).hexdigest()[:16]
        if not expected or hashed != expected or derived != job['target']:
            raise SoftwareError('地址对应的不是已登记电视，未执行任何应用变更')

    def fleet_connect(self, job):
        serial = job['endpoint']['address']+':'+str(job['endpoint']['port'])
        adb = [tool('adb'), '-P', '5039']
        result = self.run(adb+['connect', serial], timeout=20)
        state = self.run(adb+['devices'])
        if job['kind']=='fleet_probe' and re.search(re.escape(serial)+r'\s+unauthorized\b', state):
            fingerprint=''
            try:
                public=Path.home().joinpath('.android/adbkey.pub').read_text().split()[0]
                raw=hashlib.md5(base64.b64decode(public)).hexdigest().upper()
                fingerprint='；服务器ADB密钥指纹 '+':'.join(raw[i:i+2] for i in range(0,len(raw),2))
            except (OSError,ValueError,IndexError): pass
            self.step(job,'首次纳管：请员工在电视上核对并授权本店Ubuntu服务器调试密钥'+fingerprint)
            for _ in range(30):
                time.sleep(2)
                state=self.run(adb+['devices'])
                if not re.search(re.escape(serial)+r'\s+unauthorized\b', state): break
        if re.search(re.escape(serial)+r'\s+unauthorized\b', state):
            raise WaitAuthorization('首次服务器授权尚未完成，请由首装员工确认电视上的服务器调试密钥')
        if not re.search(re.escape(serial)+r'\s+device\b', state): raise SoftwareError('电视维护连接不可用，未执行变更')
        try:self.fleet_identity(job, adb, serial)
        except SoftwareError:
            try:self.run(adb+['disconnect', serial],timeout=10)
            except SoftwareError:pass
            raise
        return adb, serial

    def fleet_task(self, job):
        self.fleet_guard(job)
        adb, serial = self.fleet_connect(job)
        try:
            if job['kind'] == 'fleet_probe':
                with self.repo.db() as db:
                    row = db.execute('SELECT body FROM fleet_status WHERE id=?', (job['target'],)).fetchone()
                    status = json.loads(row[0]) if row else {}
                    if not status.get('keep_adb') or now()-status.get('at', 0) >= 120000:
                        raise SoftwareError('电视未报告有效的持续维护状态；请先完成最新版首装/重启上线')
                    endpoint = self.repo.read(db, 'software_targets', job['target'])
                    if endpoint.get('identity_sha256') != job['endpoint'].get('identity_sha256'):
                        raise SoftwareError('纳管身份已变化，拒绝覆盖')
                    endpoint.update(enrollment='verified', verified_at=now())
                    db.execute('UPDATE software_targets SET body=? WHERE id=?', (encoded(endpoint), job['target']))
                    self.repo.bump(db)
                self.step(job, '服务器ADB已授权、物理身份一致、电视已上线并报告持续维护状态')
            elif job['kind'] == 'fleet_remove':
                package = job['software_id']
                if package in PROTECTED or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+', package):
                    raise SoftwareError('禁止卸载影院或系统管理组件')
                third_party = self.run(adb+['-s', serial, 'shell', 'pm', 'list', 'packages', '-3', '--user', '0', package])
                if 'package:'+package not in third_party.splitlines(): raise SoftwareError('目标不是当前用户已安装的第三方应用，未卸载')
                home = self.run(adb+['-s', serial, 'shell', 'cmd', 'package', 'resolve-activity', '--brief', '--user', '0', '-a', 'android.intent.action.MAIN', '-c', 'android.intent.category.HOME'])
                components = re.findall(r'^([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+)/[A-Za-z0-9_.$]+$', home.strip(), re.M)
                if not components or package in components: raise SoftwareError('目标是HOME或无法核定HOME，禁止卸载')
                self.step(job, '已核验为受管第三方应用，保留数据卸载；影院/HOME/系统组件受保护')
                # Run the device's PackageManager explicitly. Host adb uninstall -k differs
                # across platform-tools/device combinations; scope removal to the checked user.
                code, result = self.run(adb+['-s', serial, 'shell', 'pm', 'uninstall', '-k', '--user', '0', package],
                                        timeout=120, allowed=(0, 1), with_code=True)
                if code or not re.search(r'^Success\s*$', result, re.M):
                    detail = re.search(r'Failure\s*\[[^\]\r\n]{1,180}\]|DELETE_FAILED_[A-Z0-9_]+|Unknown option[^\r\n]{0,100}', result)
                    raise SoftwareError('电视未确认保留数据卸载成功，未标记完成'+('：'+detail[0] if detail else '；请检查执行器日志'))
                remaining = self.run(adb+['-s', serial, 'shell', 'pm', 'list', 'packages', '--user', '0', package])
                if 'package:'+package in remaining.splitlines(): raise SoftwareError('卸载后仍检测到应用，请人工核对')
                with self.repo.db() as db:
                    row = db.execute('SELECT body FROM software_inventory WHERE target=? AND software=?', (job['target'], package)).fetchone()
                    inv = json.loads(row[0]) if row else {}
                    inv.update(installed=False, verified_at=now(), job_id=job['id'])
                    db.execute('INSERT OR REPLACE INTO software_inventory VALUES(?,?,?)', (job['target'], package, encoded(inv)))
                self.step(job, '已核验应用从当前用户移除，应用数据保留以供同签名重装恢复')
            self.save(job, status='completed', message='维护完成，实际状态已核验', finished_at=now())
        finally:
            try: self.run(adb+['disconnect', serial], timeout=10)
            except SoftwareError: pass

    def fleet_finished(self, job):
        if not job.get('batch_id'): return
        with self.repo.db() as db:
            plan = self.repo.read(db, 'fleet_plans', job['batch_id'])
            rows = [json.loads(r[0]) for r in db.execute("SELECT body FROM software_jobs WHERE json_extract(body,'$.batch_id')=?", (job['batch_id'],))]
            if job.get('delivery') == 'idle':
                if plan['status']!='cancelled':
                    statuses={j['status'] for j in rows}
                    if statuses=={'completed'}:plan.update(status='completed',finished_at=now())
                    elif statuses <= {'completed','cancelled','superseded'}:plan['status']='superseded'
                    elif statuses & {'queued','running'}:plan['status']='running'
                    elif statuses <= {'completed','cancelled','superseded','failed','waiting_authorization'}:plan['status']='needs_attention'
                    else:plan['status']='published'
                db.execute('UPDATE fleet_plans SET body=? WHERE id=?',(encoded(plan),plan['id']));self.repo.bump(db)
                return
            if job['status'] != 'completed':
                plan.update(status='paused', reason='已有单台未成功，后续目标停止；请核对失败项后重新预检')
                for j in rows:
                    if j['status'] == 'queued':
                        j.update(status='cancelled', message=plan['reason'], finished_at=now()); self.repo.write_job(db, j)
            elif all(j['status'] == 'completed' for j in rows): plan.update(status='completed', finished_at=now())
            elif plan['status'] not in ('cancelled', 'paused'): plan['status'] = 'running'
            db.execute('UPDATE fleet_plans SET body=? WHERE id=?', (encoded(plan), plan['id'])); self.repo.bump(db)
