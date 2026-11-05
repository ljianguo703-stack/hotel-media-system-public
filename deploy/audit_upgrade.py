#!/usr/bin/env python3
"""IN17 native configuration migration. Default is a read-only plan; run on the hotel Ubuntu only."""
import argparse
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
PORTS={'dockge':('dockge',5001,5001),'filebrowser':('filebrowser',8095,80),'jellyfin':('jellyfin',8096,8096)}


def loopback_compose(value, service, host_port, container_port):
    value=json.loads(json.dumps(value))
    entry=value['services'][service]
    # Only the known HTTP mapping changes; media, identity, environment and all other settings survive.
    replacement='127.0.0.1:%d:%d'%(host_port,container_port)
    ports=[];found=False
    for port in entry.get('ports',[]):
        matching=(isinstance(port,dict) and str(port.get('published'))==str(host_port)) or (isinstance(port,str) and len(port.split(':'))>=2 and port.split(':')[-2]==str(host_port) and port.split(':')[-1].split('/')[0]==str(container_port))
        if matching:ports.append(replacement);found=True
        else:ports.append(port)
    if not found:raise RuntimeError('未找到预期HTTP映射；保留现有Compose，请人工核对 '+service)
    entry['ports']=ports
    return value


def module(name, path):
    spec=importlib.util.spec_from_file_location(name,path)
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result);return result


def update_native_rules(store,config,backup):
    from bootstrap import private_json, service_identity
    from media_quality import allow_library_qualities
    for app in ('radarr','sonarr'):
        with service_identity():
            profiles=store._native(config,app,'/api/v3/qualityprofile')
            media=store._native(config,app,'/api/v3/config/mediamanagement')
        profile=next((p for p in profiles if p['id']==config[app].get('profile')),None)
        # The backup directory is root-only; never attempt these writes as hotel-tv.
        if profile:private_json(backup/('before-'+app+'-quality.json'),profile)
        private_json(backup/('before-'+app+'-mediamanagement.json'),media)
        with service_identity():
            if profile:store._native(config,app,'/api/v3/qualityprofile/'+str(profile['id']),allow_library_qualities(profile),'PUT')
            media['minimumFreeSpaceWhenImporting']=5120
            store._native(config,app,'/api/v3/config/mediamanagement',media,'PUT')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--rotate-services',action='store_true',help='为五项原生服务生成独立密码；后台密码、PIN和设备凭据不轮换')
    args=parser.parse_args()
    if not args.apply:
        print(json.dumps({'mode':'plan','required':'Ubuntu + Docker>=28 + IN17程序/执行器',
            'operations':['备份受管配置和一致性业务数据库','Dockge/文件中心/Jellyfin只绑定回环并接入后台网关','启用全下载入口的数据盘保护','采用Ubuntu主机业务时区并同步现有投放','放宽自有质量规则到标清/720p/1080p（保留人工选择）'],
            'optional_independent_passwords':args.rotate_services,'preserved':['媒体文件','设备凭据','PIN','后台账号','现有回退点','其他Docker应用'],'legacy_media_cleanup':'只列入验收提醒；不删除旧文件'},ensure_ascii=False,indent=2))
        return
    if sys.platform!='linux' or os.geteuid()!=0:raise RuntimeError('仅在酒店Ubuntu本机用sudo执行；本机Mac不修改现场')
    import fcntl
    deployment_lock=open('/var/lock/hotel-tv-deploy.lock','a')
    try:fcntl.flock(deployment_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:raise RuntimeError('已有整机安装或程序升级正在进行；未修改配置，请待任务结束再迁移')
    import yaml
    sys.path.insert(0,str(ROOT/'deploy'))
    from bootstrap import private_json, service_identity
    version=subprocess.check_output(['docker','version','--format','{{.Server.Version}}'],text=True).strip()
    if not version.split('.')[0].isdigit() or int(version.split('.')[0])<28:raise RuntimeError('请先升级Docker Engine至28或更新版本；未修改现有引擎')
    state=json.loads(Path('/etc/hotel-tv/install-state.json').read_text())
    integration=Path('/srv/hotel-tv/data/media-automation.json')
    config=json.loads(integration.read_text())
    storage=Path(config['storage_root'])
    if not (storage/'.hotel-media-install.json').is_file():raise RuntimeError('数据盘挂载标记缺失；未启用可能指向系统盘的目录')
    zone=subprocess.check_output(['timedatectl','show','-p','Timezone','--value'],text=True).strip()
    from zoneinfo import ZoneInfo
    ZoneInfo(zone)
    backup=Path('/var/backups/hotel-tv')/('IN17-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+secrets.token_hex(3))
    backup.mkdir(parents=True,mode=0o700)
    paths=[Path('/opt/stacks')/name/'compose.yaml' for name in PORTS]
    paths += [Path('/srv/nas/appdata/download-tools/default.conf'),integration]
    for path in paths:
        target=backup/str(path).lstrip('/');target.parent.mkdir(parents=True,exist_ok=True,mode=0o700);shutil.copy2(path,target);target.chmod(0o600)
    temporary=Path('/srv/hotel-tv/data/backups')/('in17-'+secrets.token_hex(8)+'.sqlite3')
    with service_identity():
        temporary.parent.mkdir(exist_ok=True,mode=0o700)
        with sqlite3.connect('/srv/hotel-tv/data/events.sqlite3') as source, sqlite3.connect(temporary) as destination:
            source.backup(destination)
        temporary.chmod(0o600)
    shutil.move(temporary,backup/'before.sqlite3')
    changed=[]
    proxy=module('in17_proxy',ROOT/'deploy/nas/download-tools/install.py')
    try:
        for name,(service,host_port,container_port) in PORTS.items():
            path=Path('/opt/stacks')/name/'compose.yaml'
            value=loopback_compose(yaml.safe_load(path.read_text()),service,host_port,container_port)
            path.write_text(yaml.safe_dump(value,allow_unicode=True,sort_keys=False));changed.append(name)
            subprocess.run(['docker','compose','-f',str(path),'up','-d'],check=True,stdout=subprocess.DEVNULL)
        proxy.refresh_proxy(state['host'])
    except Exception:
        for name in changed:
            path=Path('/opt/stacks')/name/'compose.yaml';shutil.copy2(backup/str(path).lstrip('/'),path)
            subprocess.run(['docker','compose','-f',str(path),'up','-d'],check=False,stdout=subprocess.DEVNULL)
        raise
    config.update(storage_guard=True,require_storage_marker=True)
    with service_identity():
        private_json(integration,config)
        with sqlite3.connect('/srv/hotel-tv/data/events.sqlite3') as db:
            value=json.loads(db.execute("SELECT value FROM kv WHERE key='settings'").fetchone()[0]);value['timezone']=zone
            db.execute("UPDATE kv SET value=? WHERE key='settings'",(json.dumps(value),))
            for ident,raw in db.execute('SELECT id,body FROM campaigns').fetchall():
                campaign=json.loads(raw);campaign['timezone']=zone;db.execute('UPDATE campaigns SET body=? WHERE id=?',(json.dumps(campaign,ensure_ascii=False),ident))
            db.execute("UPDATE kv SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
    # Existing native APIs own quality settings; no replacement download engine is introduced.
    sys.path.insert(0,str(ROOT/'server'))
    from management import Store
    with service_identity():
        store=Store(Path('/srv/hotel-tv/data'),Path('/srv/hotel-tv/media'),Path('/etc/hotel-tv/devices.json'),[storage,Path('/mnt'),Path('/media')])
    update_native_rules(store,config,backup)
    if args.rotate_services:
        manager=module('in17_passwords',ROOT/'deploy/passwords/manage.py').Manager()
        credentials={key:secrets.token_urlsafe(24) for key in ('dockge','filebrowser','jellyfin','smb','qbittorrent')}
        private_json(backup/'new-service-credentials.json',credentials)
        result=manager.save({'passwords':credentials})
        private_json(backup/'password-results.json',result)
        if not all(row['ok'] for row in result['results']):raise RuntimeError('部分服务改密需重试；新凭据与逐项结果已私有保存，未重置后台/PIN')
    subprocess.run(['systemctl','restart','hotel-tv'],check=True)
    print(json.dumps({'ok':True,'backup':str(backup),'business_timezone':zone,'media_deleted':False,'native_passwords_rotated':args.rotate_services},ensure_ascii=False))


if __name__=='__main__':
    try:main()
    except Exception as exc:
        print(str(exc) if isinstance(exc,RuntimeError) else 'IN17配置迁移未完成：'+type(exc).__name__+'；保留备份和各应用数据',file=sys.stderr);sys.exit(1)
