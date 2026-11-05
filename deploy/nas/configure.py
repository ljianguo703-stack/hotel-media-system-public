#!/usr/bin/env python3
"""Configure additive NAS services on an existing Ubuntu hotel server. Run as root.
Initial service password is read from stdin and never put into the deployment archive.
No block device, partition, filesystem, or existing media is modified.
"""
import argparse
import ipaddress
import grp
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import time

ROOT=Path('/srv/nas')
STACKS=Path('/opt/stacks')
MEDIA=Path('/srv/hotel-tv/media')
IMAGES=json.loads((Path(__file__).parent.parent/'images.lock.json').read_text())

def call(args, **kwargs): return subprocess.run(args,check=True,text=True,**kwargs)
def output(args): return subprocess.check_output(args,text=True).strip()
def write(path, value, mode=0o640, owner=None):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():
        backup=Path('/var/backups/hotel-nas') / time.strftime('%Y%m%d-%H%M%S') / str(path).lstrip('/')
        backup.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,backup)
    path.write_text(value);path.chmod(mode)
    if owner: os.chown(path,*owner)
def directory(path, uid, gid, mode=0o750):
    path.mkdir(parents=True,exist_ok=True);os.chown(path,uid,gid);path.chmod(mode)
def image(tag):
    obj=json.loads(output(['docker','image','inspect',tag]))[0]
    return obj['RepoDigests'][0]
def stack(name, service):
    path=STACKS/name/'compose.yaml'
    if path.exists(): raise RuntimeError('Existing stack: '+name)
    write(path,json.dumps({'services':service},ensure_ascii=False,indent=2)+'\n')

if __name__=='__main__':
    assert os.geteuid()==0
    parser=argparse.ArgumentParser();parser.add_argument('--host',required=True);parser.add_argument('--operator',required=True);parser.add_argument('--timezone',default='UTC');args=parser.parse_args()
    LAN=str(ipaddress.IPv4Address(args.host))
    interfaces=json.loads(output(['ip','-j','-4','address']))
    prefix=next(a['prefixlen'] for i in interfaces for a in i.get('addr_info',[]) if a.get('local')==LAN)
    subnet=str(ipaddress.ip_network(LAN+'/'+str(prefix),strict=False))
    incoming=sys.stdin.readline().strip()
    passwords=json.loads(incoming) if incoming.startswith('{') else {}
    password=passwords.get('smb',incoming)
    if len(password)<8: raise RuntimeError('Initial password must contain at least 8 characters')
    uid=pwd.getpwnam(args.operator).pw_uid;gid=grp.getgrnam('hotel-tv').gr_gid
    ROOT.mkdir(parents=True,exist_ok=True);STACKS.mkdir(parents=True,exist_ok=True)
    for name in ['jellyfin/config','jellyfin/cache','filebrowser/data','dockge']:
        directory(ROOT/'appdata'/name,uid,gid)
    directory(ROOT/'shared',uid,gid,0o2775)
    # These directories were already used by the hotel TV, and remain its canonical media paths.
    for p in [MEDIA, MEDIA/'电影', MEDIA/'电视剧', ROOT/'shared']:
        call(['setfacl','-m','g:hotel-tv:rwx,d:u::rwx,d:g::rwx,d:m::rwx,d:o::r-x',str(p)])
    logging={'driver':'json-file','options':{'max-size':'10m','max-file':'3'}}
    common={'restart':'unless-stopped','logging':logging}
    stack('jellyfin',{'jellyfin':{**common,'image':image(IMAGES['jellyfin']),'container_name':'nas-jellyfin',
        'user':f'{uid}:{gid}','ports':['127.0.0.1:8096:8096'],'volumes':[
            f'{ROOT}/appdata/jellyfin/config:/config',f'{ROOT}/appdata/jellyfin/cache:/cache',f'{MEDIA}:{MEDIA}:ro'],
        'environment':{'TZ':args.timezone,'JELLYFIN_PublishedServerUrl':f'http://{LAN}:8096'},
        'security_opt':['no-new-privileges:true'],'mem_limit':'8g','pids_limit':1024}})
    fbconfig={'server':{'cacheDir':'/home/filebrowser/data/cache','sources':[
        {'path':'/media','name':'酒店媒体','config':{'defaultEnabled':True}},
        {'path':'/shared','name':'共享文件','config':{'defaultEnabled':True}}]},
        'auth':{'methods':{'password':{'enabled':True,'minLength':8,'signup':False}}}}
    write(ROOT/'appdata/filebrowser/data/config.yaml',json.dumps(fbconfig,ensure_ascii=False,indent=2)+'\n',owner=(uid,gid))
    write(STACKS/'filebrowser'/'.env','FILEBROWSER_ADMIN_PASSWORD='+passwords.get('filebrowser',password)+'\n',0o600)
    stack('filebrowser',{'filebrowser':{**common,'image':image(IMAGES['filebrowser']),
        'container_name':'nas-filebrowser','user':f'{uid}:{gid}','ports':['127.0.0.1:8095:80'],
        'volumes':[f'{ROOT}/appdata/filebrowser/data:/home/filebrowser/data',f'{MEDIA}:/media',f'{ROOT}/shared:/shared'],
        'environment':{'FILEBROWSER_ADMIN_PASSWORD':'${FILEBROWSER_ADMIN_PASSWORD}','TZ':args.timezone},
        'security_opt':['no-new-privileges:true'],'mem_limit':'1g','pids_limit':256}})
    stack('dockge',{'dockge':{**common,'image':image(IMAGES['dockge']),'container_name':'nas-dockge',
        'ports':['127.0.0.1:5001:5001'],'volumes':['/var/run/docker.sock:/var/run/docker.sock',
            f'{ROOT}/appdata/dockge:/app/data',f'{STACKS}:{STACKS}'],
        'environment':{'DOCKGE_STACKS_DIR':str(STACKS),'TZ':args.timezone},'mem_limit':'1g','pids_limit':512}})
    # A separate Samba account does not change the Ubuntu SSH/login password.
    try: pwd.getpwnam('nasadmin')
    except KeyError: call(['useradd','--system','--no-create-home','--shell','/usr/sbin/nologin','--gid','hotel-tv','nasadmin'])
    call(['smbpasswd','-s','-a','nasadmin'],input=password+'\n'+password+'\n',stdout=subprocess.DEVNULL)
    smb=Path('/etc/samba/smb.conf');existing=smb.read_text()
    include='include = /etc/samba/hotel-nas.conf'
    if include not in existing: write(smb,existing+'\n# Hotel NAS shares; original configuration preserved\n'+include+'\n',0o644)
    shares=f'''[HotelMedia]
    path = /srv/hotel-tv/media
    comment = Hotel movie and TV library
    browseable = yes
    read only = no
    guest ok = no
    valid users = nasadmin
    force user = {args.operator}
    force group = hotel-tv
    create mask = 0664
    force create mode = 0664
    directory mask = 2775
    force directory mode = 2775
    hosts allow = 127.0.0.1 {subnet}
    hosts deny = ALL

[NASFiles]
    path = /srv/nas/shared
    comment = Shared files
    browseable = yes
    read only = no
    guest ok = no
    valid users = nasadmin
    force user = {args.operator}
    force group = hotel-tv
    create mask = 0664
    force create mode = 0664
    directory mask = 2775
    force directory mode = 2775
    hosts allow = 127.0.0.1 {subnet}
    hosts deny = ALL
'''
    write('/etc/samba/hotel-nas.conf',shares,0o644)
    call(['testparm','-s'],stdout=subprocess.DEVNULL)
    call(['systemctl','enable','--now','smbd'])
    call(['systemctl','restart','smbd'])
    for name in ['jellyfin','filebrowser','dockge']:
        call(['docker','compose','-f',str(STACKS/name/'compose.yaml'),'up','-d'])
    lock={name:json.loads(output(['docker','image','inspect',tag]))[0]['RepoDigests'] for name,tag in [
        ('jellyfin',IMAGES['jellyfin']),('dockge',IMAGES['dockge']),('filebrowser',IMAGES['filebrowser'])]}
    write(ROOT/'images.lock.json',json.dumps(lock,indent=2)+'\n',0o644)
    print('NAS services created. Media remains at',MEDIA)
