import io
import gzip
import json
from pathlib import Path
import stat
import tarfile
import unittest
from unittest.mock import patch
import zipfile
from management import Store, Problem
from software_packages import SoftwareError, digest, inspect
from software_store import Repository, encoded, now
from software_agent import Agent, WaitAuthorization
import test_management as management_tests


def make_deb(version='1.0', payload=b'first release'):
    parts = [('debian-binary', b'2.0\n')]
    for name, files in [('control.tar.gz', {'control': ('Package: hotel-test-app\nVersion: '+version+'\nArchitecture: all\nDescription: installer test\nMaintainer: Test <test@example.test>\n').encode()}), ('data.tar.gz', {'usr/share/hotel-test-app/version.txt': payload})]:
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w') as archive:
            directories=set()
            for filename, body in files.items():
                for directory in reversed(Path(filename).parents):
                    if str(directory) not in ('.', '') and str(directory) not in directories:
                        entry=tarfile.TarInfo(str(directory));entry.type=tarfile.DIRTYPE;entry.mode=0o755;archive.addfile(entry);directories.add(str(directory))
                member = tarfile.TarInfo(filename); member.size = len(body);member.mode=0o644
                archive.addfile(member, io.BytesIO(body))
        parts.append((name, gzip.compress(output.getvalue(), mtime=0)))
    output = bytearray(b'!<arch>\n')
    for name, body in parts:
        output.extend((name+'/').ljust(16).encode()+b'0'.ljust(12)+b'0'.ljust(6)+b'0'.ljust(6)+b'100644'.ljust(8)+str(len(body)).encode().ljust(10)+b'`\n'+body)
        if len(body)%2:output.extend(b'\n')
    return bytes(output)


def bundle(manifest=None, files=None):
    output=io.BytesIO()
    manifest=manifest or {'format':1,'kind':'compose','id':'demo-app','name':'测试容器','version':'1.0','service':{'image':'example/demo@sha256:'+'1'*64,'ports':[{'host':18081,'container':8080}]}}
    with zipfile.ZipFile(output,'w') as archive:
        archive.writestr('hotel-software.json',json.dumps(manifest))
        for name,body in (files or {}).items():archive.writestr(name,body)
    return output.getvalue()


class SoftwareE2E(unittest.TestCase):
    setUp=management_tests.ManagementE2E.setUp
    tearDown=management_tests.ManagementE2E.tearDown
    request=management_tests.ManagementE2E.request
    state=management_tests.ManagementE2E.state

    def repo(self):return Repository(self.data)
    def upload(self,raw=None,name='test.deb',auth=True,headers=None):
        return self.request('/api/admin/software/upload',raw if raw is not None else make_deb(),auth=auth,headers={'X-Filename':name,**(headers or {})})
    def snapshot(self):return self.request('/api/admin/software',auth=True)[2]
    def mutate(self,path,**body):return self.request('/api/admin/software/'+path,{'revision':self.snapshot()['revision'],**body},auth=True)
    def queue(self,item,**body):return self.mutate('install',package_id=item['id'],targets=['server'],confirm=True,request_id='test-request-12345',**body)

    def test_upload_auth_csrf_and_admin_role(self):
        self.assertEqual(self.upload(auth=False)[0],401)
        self.assertEqual(self.upload(headers={'X-CSRF-Token':'wrong'})[0],403)
        with self.store.db() as db:db.execute("UPDATE users SET role='operator' WHERE name='admin'")
        self.assertEqual(self.upload()[0],403)
        self.assertEqual(self.request('/api/admin/software',auth=True)[0],403)
        self.assertEqual(self.request('/api/admin/software/install',{'confirm':True},auth=True)[0],403)

    def test_metadata_duplicate_and_immutable_version(self):
        code,_,result=self.upload();self.assertEqual(code,201);item=result['package']
        self.assertEqual((item['kind'],item['software_id'],item['version']),('deb','hotel-test-app','1.0'))
        self.assertEqual(self.upload()[2]['package']['id'],item['id'])
        self.assertEqual(self.upload(make_deb(payload=b'different'))[0],409)
        self.assertEqual(len(self.snapshot()['packages']),1)
        self.assertEqual(list((self.data/'software/incoming').iterdir()),[])

    def test_bad_archives_and_filenames(self):
        for body,name in [(b'junk','x.deb'),(b'junk','x.exe'),(bundle(files={'../escape':b'bad'}),'x.zip'),(bundle(), '../evil.zip')]:
            self.assertEqual(self.upload(body,name)[0],400,name)
        self.assertEqual(self.snapshot()['packages'],[])
        self.assertFalse((self.root/'escape').exists())

    def test_container_management_reuses_dockge_instead_of_custom_packages(self):
        base=json.loads(zipfile.ZipFile(io.BytesIO(bundle())).read('hotel-software.json'))
        for change in [{'privileged':True},{'ports':[{'host':18791,'container':80}]},{'user':'0:0'},{'image':'example/demo:latest'},{'data_path':'/run'}]:
            value={**base,'service':{**base['service'],**change}}
            self.assertEqual(self.upload(bundle(value),'x.zip')[0],400)
        self.assertEqual(self.upload(bundle(),'x.zip')[0],400)

    def test_zip_symlink_and_duplicate_rejected(self):
        for mode in ('link','duplicate'):
            output=io.BytesIO()
            with zipfile.ZipFile(output,'w') as archive:
                if mode=='link':
                    entry=zipfile.ZipInfo('foo');entry.create_system=3;entry.external_attr=(stat.S_IFLNK|0o777)<<16;archive.writestr(entry,'/etc')
                else:
                    archive.writestr('hotel-software.json','{}');archive.writestr('hotel-software.json','{}')
            self.assertEqual(self.upload(output.getvalue(),'x.zip')[0],400)

    def test_manual_confirm_revision_and_idempotency(self):
        item=self.upload()[2]['package'];rev=self.snapshot()['revision']
        self.assertEqual(self.mutate('install',package_id=item['id'],targets=['server'],request_id='test-123456')[0],400)
        self.assertEqual(self.queue(item,revision=rev-1)[0],409)
        code,_,result=self.queue(item);self.assertEqual(code,200)
        repeated=self.queue(item);self.assertEqual(repeated[0],200);self.assertEqual(repeated[2],result)
        self.assertEqual(len(self.snapshot()['jobs']),1)
        other=self.mutate('install',package_id=item['id'],targets=['room-101'],confirm=True,request_id='test-request-12345')
        self.assertEqual(other[0],409)
        conflict=self.mutate('install',package_id=item['id'],targets=['server'],confirm=True,request_id='different-request')
        self.assertEqual(conflict[0],409)

    def test_cancel_and_restart_persistence(self):
        item=self.upload()[2]['package'];job=self.queue(item)[2]['jobs'][0]
        Store(self.data,self.media,self.config)
        self.assertEqual(self.snapshot()['jobs'][0]['status'],'queued')
        self.assertEqual(self.mutate('job',id=job['id'],action='cancel')[0],200)
        self.assertEqual(self.snapshot()['jobs'][0]['status'],'cancelled')
        self.assertEqual(self.mutate('job',id=job['id'],action='retry',request_id='retry-cancelled')[0],400)

    def test_systemd_signals_are_committed_and_only_finished_signals_removed(self):
        item=self.upload()[2]['package'];job=self.queue(item)[2]['jobs'][0]
        marker=self.data/'software/queue'/job['id']
        self.assertTrue(marker.is_file())
        agent=Agent(self.data,self.root/'agent')
        agent.clear_signals()
        self.assertTrue(marker.exists())
        self.assertEqual(self.mutate('job',id=job['id'],action='cancel')[0],200)
        agent.clear_signals()
        self.assertFalse(marker.exists())
        self.assertFalse(agent.once())

    def test_tampering_blocks_install_and_agent(self):
        item=self.upload()[2]['package'];path=self.repo().package_path(item);path.write_bytes(b'tampered')
        self.assertEqual(self.queue(item)[0],409)
        self.assertEqual(self.snapshot()['jobs'],[])

    def test_television_enrollment_is_explicit_and_lan_only(self):
        for address in ('127.0.0.1','169.254.169.254','8.8.8.8','::1'):
            self.assertEqual(self.mutate('target',id='room-101',address=address,port=5555,confirm=True)[0],400)
        self.assertEqual(self.mutate('target',id='room-101',address='192.168.4.62',port=5555,confirm=False)[0],400)
        result=self.mutate('target',id='room-101',address='192.168.4.62',port=5555,confirm=True)
        self.assertEqual(result[0],200)
        self.assertEqual(self.snapshot()['targets'][0]['address'],'192.168.4.62')

    def test_agent_deb_success_records_verified_inventory(self):
        item=self.upload()[2]['package'];self.queue(item);calls=[]
        def runner(args,**kw):
            calls.append(args)
            if args[0]=='dpkg':return 'arm64\n'
            if args[0]=='dpkg-query':return (1, 'not installed') if kw.get('with_code') else 'install ok installed\n1.0'
            return ''
        agent=Agent(self.data,self.root/'agent',runner=runner)
        self.assertTrue(agent.once());snapshot=self.snapshot()
        self.assertEqual(snapshot['jobs'][0]['status'],'completed',snapshot['jobs'])
        self.assertEqual(snapshot['inventory'][0]['version'],'1.0')
        self.assertTrue(any(c[0]=='apt-get' and '--no-remove' in c for c in calls))
        self.assertEqual(self.queue(item)[0],200) # same request remains idempotent
        self.assertEqual(self.mutate('install',package_id=item['id'],targets=['server'],confirm=True,request_id='fresh-request-1')[0],409)

    def test_agent_failure_retry_and_crash_do_not_fake_success(self):
        item=self.upload()[2]['package'];self.queue(item)
        def runner(*args,**kw):raise SoftwareError('安装依赖缺失')
        agent=Agent(self.data,self.root/'agent',runner=runner);agent.once()
        snapshot=self.snapshot();job=snapshot['jobs'][0]
        self.assertEqual(job['status'],'failed');self.assertEqual(snapshot['inventory'],[])
        self.assertEqual(self.mutate('job',id=job['id'],action='retry',request_id='retry-new-job')[0],200)
        with self.repo().db() as db:
            new=self.repo().read(db,'software_jobs',self.snapshot()['jobs'][0]['id']);new['status']='running';self.repo().write_job(db,new)
        agent.recover()
        self.assertEqual(self.snapshot()['jobs'][0]['status'],'interrupted')

    def test_adb_waits_for_authorization_and_disconnects(self):
        calls=[]
        def runner(args,**kw):calls.append(args);return 'failed to connect'
        agent=Agent(self.data,self.root/'agent',runner=runner)
        with patch('software_agent.tool',return_value='/test/adb'):
            with self.assertRaises(WaitAuthorization):agent.install_apk({'endpoint':{'address':'192.168.4.62','port':5555}}, {'software_id':'com.example.app','version_code':2,'min_sdk':21},self.root/'x.apk')
        self.assertEqual(calls[-1][-2:],['disconnect','192.168.4.62:5555'])
        self.assertFalse(any('tcpip' in c or 'uninstall' in c for c in calls))

    def test_apk_metadata_requires_valid_signature(self):
        apk=self.root/'x.apk'
        with zipfile.ZipFile(apk,'w') as archive:archive.writestr('AndroidManifest.xml',b'binary')
        responses=["package: name='com.example.demo' versionCode='12' versionName='1.2'\nsdkVersion:'21'\napplication-label:'Demo'",'Signer #1 certificate SHA-256 digest: '+'a'*64]
        with patch('software_packages.tool',side_effect=lambda x:x),patch('software_packages.checked',side_effect=responses):
            item=inspect(apk,'x.apk');self.assertEqual(item['version_code'],12);self.assertEqual(item['signers'],['a'*64])
        with patch('software_packages.tool',side_effect=lambda x:x),patch('software_packages.checked',side_effect=[responses[0],'bad']):
            with self.assertRaises(SoftwareError):inspect(apk,'x.apk')

    def test_system_update_obeys_shared_deployment_lock(self):
        import fcntl
        agent=Agent(self.data,self.root/'agent')
        with open(agent.deployment_lock,'a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(SoftwareError):
                agent.install_system({}, {}, self.root/'not-read.zip')

    def test_recovery_never_overwrites_a_newer_shared_release(self):
        releases=self.root/'releases';old=releases/'old';other=releases/'newer'
        old.mkdir(parents=True);other.mkdir();current=self.root/'current';current.symlink_to(other)
        item=self.upload()[2]['package'];job=self.queue(item)[2]['jobs'][0]
        job.update(status='running',kind='system',previous_release=str(old),new_release=str(releases/'candidate'))
        with self.repo().db() as db:self.repo().write_job(db,job)
        calls=[];agent=Agent(self.data,self.root/'agent',releases,current,runner=lambda *a,**kw:calls.append(a))
        agent.recover()
        self.assertEqual(current.resolve(),other.resolve());self.assertEqual(calls,[])
        self.assertEqual(self.snapshot()['jobs'][0]['status'],'interrupted')

    def test_system_base_guard_and_rollback_restore_business_data(self):
        import hashlib
        files={'server/server.py':b'print("new")','admin/index.html':b'<h1>New</h1>'}
        manifest={'format':1,'kind':'system','id':'hotel-tv-server','name':'影院','version':'new-release','base_release':'old-release','files':{k:hashlib.sha256(v).hexdigest() for k,v in files.items()}}
        item=self.upload(bundle(manifest,files),'system.zip')[2]['package'];job=self.queue(item)[2]['jobs'][0]
        releases=self.root/'releases';previous=releases/'old-release';previous.mkdir(parents=True)
        current=self.root/'current';current.symlink_to(previous);calls=[]
        agent=Agent(self.data,self.root/'agent',releases,current,runner=lambda args,**kw:calls.append(args) or '')
        health_calls=[]
        def health():
            health_calls.append(1)
            if len(health_calls)==1:
                with self.store.db() as db:db.execute("UPDATE kv SET value='\"BROKEN\"' WHERE key='revision'")
                raise SoftwareError('健康检查失败')
        agent.wait_health=health
        self.assertTrue(agent.once());snapshot=self.snapshot()
        self.assertEqual(snapshot['jobs'][0]['status'],'rolled_back',(self.root/'agent/exception.log').read_text() if (self.root/'agent/exception.log').exists() else snapshot['jobs'])
        self.assertEqual(current.resolve(),previous.resolve());self.assertEqual(snapshot['inventory'],[])
        self.assertIsInstance(self.store.state()['revision'],int)
        self.assertTrue(Path(snapshot['jobs'][0]['database_backup']).exists())
