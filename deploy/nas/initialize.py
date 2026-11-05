#!/usr/bin/env python3
"""Initialize new NAS applications with their supported local APIs; no credentials logged."""
import argparse,grp,json,os,pwd
from pathlib import Path
import subprocess,sys,time
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import build_opener,ProxyHandler,Request

opener=build_opener(ProxyHandler({}))
base='http://127.0.0.1:8096';token=''
def api(route,body=None):
    headers={'Authorization':'MediaBrowser Client="Hotel NAS Setup", Device="Ubuntu", DeviceId="hotel-nas-setup", Version="1.0"'}
    if token:headers['Authorization'] += ', Token="'+token+'"'
    if body is not None:headers['Content-Type']='application/json'
    for attempt in range(12):
        try:
            with opener.open(Request(base+route,data=json.dumps(body).encode() if body is not None else None,headers=headers),timeout=45) as r:
                raw=r.read();return json.loads(raw) if raw else None
        except HTTPError as exc:
            if exc.code not in (502,503,504) or attempt==11: raise
        except (OSError, URLError):
            if attempt==11: raise
        time.sleep(1)

def main():
    global token
    parser=argparse.ArgumentParser();parser.add_argument('--host',required=True);parser.add_argument('--operator',required=True);args=parser.parse_args()
    incoming=sys.stdin.readline().strip()
    passwords=json.loads(incoming) if incoming.startswith('{') else {}
    password=passwords.get('jellyfin',incoming)
    assert len(password)>=8
    # FileBrowser v2 moved HTTP settings out of server.
    p=Path('/srv/nas/appdata/filebrowser/data/config.yaml')
    if p.read_text().lstrip().startswith('{'):
        config=json.loads(p.read_text());config['server'].pop('port',None);p.write_text(json.dumps(config,ensure_ascii=False,indent=2))
    os.chown(p,pwd.getpwnam(args.operator).pw_uid,grp.getgrnam('hotel-tv').gr_gid);p.chmod(0o640)
    # Hotel adapter uses loopback; management browser uses the LAN address.
    p=Path('/opt/stacks/jellyfin/compose.yaml');config=json.loads(p.read_text());config['services']['jellyfin']['ports']=['127.0.0.1:8096:8096'];p.write_text(json.dumps(config,indent=2));p.chmod(0o640)
    for name in ['jellyfin','filebrowser']:
        subprocess.run(['docker','compose','-f',f'/opt/stacks/{name}/compose.yaml','up','-d'],check=True)
    # Already-running services do not need a restart for API initialization.
    for _ in range(60):
        try:info=api('/System/Info/Public');break
        except Exception:time.sleep(1)
    else:raise RuntimeError('Jellyfin startup timed out')
    if not info.get('StartupWizardCompleted'):
        api('/Startup/Configuration',{'ServerName':'酒店媒体中心','UICulture':'zh-CN','MetadataCountryCode':'CN','PreferredMetadataLanguage':'zh'})
        api('/Startup/User')
        api('/Startup/User',{'Name':'admin','Password':password})
        api('/Startup/RemoteAccess',{'EnableRemoteAccess':False,'EnableAutomaticPortMapping':False})
        api('/Startup/Complete',{})
    login=api('/Users/AuthenticateByName',{'Username':'admin','Pw':password});token=login['AccessToken']
    folders=api('/Library/VirtualFolders')
    for name,kind,path in [('电影','movies','/srv/hotel-tv/media/电影'),('电视剧','tvshows','/srv/hotel-tv/media/电视剧')]:
        if any(v['Name']==name for v in folders):continue
        opts={'EnableRealtimeMonitor':True,'PreferredMetadataLanguage':'zh','MetadataCountryCode':'CN','SaveLocalMetadata':False,'PathInfos':[{'Path':path}],
              'EnableChapterImageExtraction':False,'ExtractChapterImagesDuringLibraryScan':False,'EnableTrickplayImageExtraction':False,
              'ExtractTrickplayImagesDuringLibraryScan':False}
        api('/Library/VirtualFolders?'+urlencode({'name':name,'collectionType':kind,'refreshLibrary':'false'}),{'LibraryOptions':opts})
    keys=api('/Auth/Keys')['Items'];key=next((k for k in keys if k.get('AppName')=='HotelTV Local Sync'),None)
    if not key:
        api('/Auth/Keys?'+urlencode({'app':'HotelTV Local Sync'}),{})
        key=next(k for k in api('/Auth/Keys')['Items'] if k['AppName']=='HotelTV Local Sync')
    p=Path('/srv/hotel-tv/data/nas-integration.json');p.write_text(json.dumps({'url':base,'token':key['AccessToken']}));p.chmod(0o600)
    user=__import__('pwd').getpwnam('hotel-tv');os.chown(p,user.pw_uid,user.pw_gid)
    api('/Library/Refresh',{})
    # The setup operation is executed inside Dockge using its public Socket.IO setup API.
    js='''const {io}=require('socket.io-client');let raw='';process.stdin.on('data',d=>raw+=d);process.stdin.on('end',()=>{const password=raw.trim();const s=io('http://127.0.0.1:5001',{transports:['websocket']});const timer=setTimeout(()=>process.exit(2),20000);const finish=r=>{console.log('Dockge setup/login:',r.ok?'confirmed':'failed');clearTimeout(timer);s.close();process.exit(r.ok?0:1)};s.on('connect',()=>s.emit('setup','admin',password,r=>{if(r.ok)finish(r);else s.emit('login',{username:'admin',password},finish)}))});'''
    r=subprocess.run(['docker','exec','-i','nas-dockge','node','-e',js],input=passwords.get('dockge',password)+'\n',text=True,capture_output=True)
    print(r.stdout.strip())
    if r.returncode:raise RuntimeError('Dockge setup failed: '+r.stderr[:200])
    print('Jellyfin initialized:',info.get('Version'),'libraries:',','.join(v['Name'] for v in api('/Library/VirtualFolders')))
    print('Hotel adapter key saved privately on Ubuntu; no key was sent to the TV or browser.')


if __name__ == '__main__': main()
