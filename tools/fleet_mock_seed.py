"""IN15 loopback fixture only: synthetic rooms and simulated worker results, never ADB."""
import hashlib
import json
from fleet_store import encoded, now
from software_agent import Agent


def seed(store):
    from fleet_store import FleetRepository
    FleetRepository.executor_ready = lambda self, idle=False: True  # Isolated fake worker only; never connects TVs.
    with store.db() as db:
        db.execute('DELETE FROM devices')
        for n in range(150):
            identity=hashlib.sha256(('fleet-fixture-'+str(n)).encode()).hexdigest()
            ident='tv-'+hashlib.sha256(('hotel-tv-backend-device:'+identity).encode()).hexdigest()[:16]
            room=str((n//50+1)*100+n%50+1)
            d={'id':ident,'room':room,'group':str(n//50+1)+'楼','enabled':True}
            db.execute('INSERT INTO devices VALUES(?,?,?,?,?,?)',(ident,encoded(d),'fleet-fixture-token-'+str(n),0 if n==149 else now(),'192.168.20.'+str(10+n),''))
            endpoint={'id':ident,'address':'192.168.20.'+str(10+n),'port':5555,'identity_sha256':identity,'enrollment':'pending' if n==148 else 'verified','reboot_verified':True}
            db.execute('INSERT INTO software_targets VALUES(?,?)',(ident,encoded(endpoint)))
            db.execute('INSERT INTO fleet_status VALUES(?,?)',(ident,encoded({'protocol':2,'home_idle':n!=147,'idle_ms':65000,'maintenance_job':'','at':now(),'sdk':28,'keep_adb':True,'overlay':True,'screen_awake':True,'version':'0.1.11','version_code':12})))
        item={'id':'pkg-'+'b'*32,'kind':'apk','platform':'tv','software_id':'com.example.fixtureplayer','name':'模拟应用','version':'2.0','sha256':'b'*64,'min_sdk':21,'filename':'fixture.apk','bytes':4096,'signers':['1'*64]}
        db.execute('INSERT INTO software_packages VALUES(?,?)',(item['id'],encoded(item)))
        for (ident,) in db.execute('SELECT id FROM devices').fetchall():
            db.execute('INSERT INTO software_inventory VALUES(?,?,?)',(ident,item['software_id'],encoded({'sha256':'a'*64,'version':'1.0','installed':True})))


def tick(store, directory):
    agent=Agent(store.data,directory)
    with store.db() as db:
        db.execute("UPDATE devices SET last_seen=? WHERE json_extract(body,'$.room')!='350'",(now(),))
        for ident,raw in db.execute('SELECT id,body FROM fleet_status'):
            d=json.loads(raw);d['at']=now();db.execute('UPDATE fleet_status SET body=? WHERE id=?',(encoded(d),ident))
        jobs=[json.loads(r[0]) for r in db.execute("SELECT body FROM software_jobs WHERE status='queued' AND json_extract(body,'$.batch_id') IS NOT NULL ORDER BY rowid")]
    from fleet_store import FleetRepository
    repo=FleetRepository(store.data)
    with store.db() as db: ids=[r[0] for r in db.execute('SELECT id FROM devices')]
    for ident in ids: repo.reconcile(ident)
    with store.db() as db:
        jobs=[json.loads(r[0]) for r in db.execute("SELECT body FROM software_jobs WHERE status='queued' AND json_extract(body,'$.batch_id') IS NOT NULL ORDER BY rowid")]
    idle=[j for j in jobs if j.get('delivery')=='idle']
    manual=[j for j in jobs if j.get('delivery')!='idle'][:2]
    # Offline/busy rooms wait. One failed installation must not cancel other idle publications.
    # Manual uninstalls retain the original canary/stop-on-failure policy.
    for index,j in enumerate(idle+manual):
        with store.db() as db:
            fresh=agent.repo.read(db,'software_jobs',j['id'])
            if fresh['status']!='queued': continue
        j.update(status='completed' if (j.get('delivery')=='idle' and index!=1 or index==0) else 'failed',finished_at=now(),message='模拟核验成功' if index==0 else '模拟维护连接失败，后续目标停止')
        with store.db() as db:agent.repo.write_job(db,j)
        agent.fleet_finished(j)
