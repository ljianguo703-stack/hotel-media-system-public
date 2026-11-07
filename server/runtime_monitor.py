"""Unprivileged Linux metrics and authenticated TV HTTP-body accounting.

No packet capture, firewall rules, new listener or cloud dependency. Device
counters describe this cinema HTTP service, not other apps on the television.
"""
from collections import deque
from datetime import datetime, timedelta
import ipaddress
import json
from pathlib import Path
import platform
import socket
import sqlite3
import struct
import threading
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from contextlib import contextmanager


def linux_snapshot(proc=Path('/proc'), sysnet=Path('/sys/class/net')):
    result = {'available': False, 'hostname': socket.gethostname(), 'system': platform.system(),
              'cpu_ticks': None, 'cpu_percent': None, 'memory': None, 'uptime_seconds': None,
              'boot_id': None, 'interfaces': []}
    try:
        result['boot_id'] = (proc/'sys/kernel/random/boot_id').read_text().strip()
        ticks = [int(v) for v in (proc/'stat').read_text().splitlines()[0].split()[1:9]]
        result['cpu_ticks'] = [sum(ticks), ticks[3]+ticks[4]]
        memory = {line.split(':')[0]: int(line.split()[1])*1024 for line in (proc/'meminfo').read_text().splitlines() if len(line.split()) >= 2 and line.split()[1].isdigit()}
        total, available = memory.get('MemTotal', 0), memory.get('MemAvailable')
        if total and available is not None:
            result['memory'] = {'total': total, 'used': total-available, 'percent': round(100*(total-available)/total, 1)}
        result['uptime_seconds'] = int(float((proc/'uptime').read_text().split()[0]))
        result['available'] = True
    except (OSError, ValueError, IndexError):
        return result
    default = ''
    try:
        for line in (proc/'net/route').read_text().splitlines()[1:]:
            fields = line.split()
            if len(fields)>3 and fields[1]=='00000000' and int(fields[3],16)&2:
                default=fields[0]; break
    except (OSError, ValueError):
        pass
    try: interfaces = sorted(sysnet.iterdir())
    except OSError: interfaces = []
    for directory in interfaces:
        # Docker bridges/veths repeat physical traffic. Only actual NICs are listed.
        if not (directory/'device').exists() and directory.name!=default: continue
        try:
            if (directory/'operstate').read_text().strip() != 'up': continue
            row={'id':directory.name, 'rx_bytes':int((directory/'statistics/rx_bytes').read_text()),
                 'tx_bytes':int((directory/'statistics/tx_bytes').read_text()), 'ip':'',
                 'default_route':directory.name==default, 'speed_mbps':None, 'duplex':None}
            try:
                speed=int((directory/'speed').read_text());row['speed_mbps']=speed if speed>0 else None
                row['duplex']=(directory/'duplex').read_text().strip()
            except (OSError, ValueError): pass
            try:
                import fcntl
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                    raw=fcntl.ioctl(sock.fileno(),0x8915,struct.pack('256s',directory.name.encode()[:15]))
                    row['ip']=socket.inet_ntoa(raw[20:24])
            except (ImportError, OSError): pass
            result['interfaces'].append(row)
        except (OSError, ValueError): continue
    result['interfaces'].sort(key=lambda r:(not r['default_route'],r['id']))
    return result


class RuntimeMonitor:
    def __init__(self, data, timezone='UTC', reader=linux_snapshot, clock=time.time, monotonic=time.monotonic):
        self.path=Path(data)/'runtime-metrics.sqlite3'
        self.reader, self.clock, self.monotonic=reader, clock, monotonic
        self.lock=threading.RLock();self.flush_lock=threading.Lock();self.stop_event=threading.Event();self.thread=None
        self.zone=self.valid_zone(timezone);self.last_raw=None;self.last_mono=None;self.sampled_at=None
        self.system={};self.interfaces=[];self.rates={};self.old_device={};self.history={};self.error=''
        self.sessions={};self.totals={};self.starts={};self.generation=0;self.flushed=-1
        try:
            with self.db() as db:
                db.executescript('CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,since REAL,last REAL,ip TEXT);'
                    'CREATE TABLE IF NOT EXISTS totals(day TEXT,kind TEXT,id TEXT,rx INTEGER,tx INTEGER,started REAL,PRIMARY KEY(day,kind,id));')
                self.sessions={r[0]:{'since':r[1],'last':r[2],'ip':r[3]} for r in db.execute('SELECT id,since,last,ip FROM sessions')}
                for day,kind,ident,rx,tx,started in db.execute('SELECT day,kind,id,rx,tx,started FROM totals'):
                    self.totals[(day,kind,ident)]=[rx,tx];self.starts[(day,kind,ident)]=started
        except (OSError, sqlite3.Error):self.error='统计记录暂不可用'
        self.sample()

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=3)
        try:
            db.execute('PRAGMA journal_mode=WAL')
            with db:yield db
        finally:db.close()

    @staticmethod
    def valid_zone(value):
        try: return ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, TypeError): return ZoneInfo('UTC')

    def day(self): return datetime.fromtimestamp(self.clock(),self.zone).date().isoformat()+'@'+str(self.zone)

    def start(self):
        if self.thread: return
        self.thread=threading.Thread(target=self.run,name='hotel-runtime-metrics',daemon=True);self.thread.start()

    def run(self):
        while not self.stop_event.wait(2):
            try:self.sample();self.flush()
            except (OSError, sqlite3.Error, ValueError):self.error='状态采集暂不可用'

    def close(self):
        self.stop_event.set()
        if self.thread and self.thread is not threading.current_thread(): self.thread.join(timeout=5)
        self.flush()

    def touch(self, ident, ip):
        now=self.clock()
        with self.lock:
            before=self.sessions.get(ident)
            since=before['since'] if before and 0<=now-before['last']<120 and before['ip']==ip else now
            self.sessions[ident]={'since':since,'last':now,'ip':ip};self.generation+=1

    def add(self, ident, rx=0, tx=0):
        with self.lock:self._add('device',ident,rx,tx)

    def _add(self, kind, ident, rx, tx):
        key=(self.day(),kind,ident)
        self.totals.setdefault(key,[0,0]);self.starts.setdefault(key,self.clock())
        self.totals[key][0]+=max(0,int(rx));self.totals[key][1]+=max(0,int(tx));self.generation+=1

    def sample(self):
        raw=self.reader();mono=self.monotonic();now=self.clock()
        with self.lock:
            elapsed=mono-self.last_mono if self.last_mono is not None else None
            same=bool(self.last_raw and raw.get('boot_id') and raw.get('boot_id')==self.last_raw.get('boot_id'))
            if same and raw.get('cpu_ticks') and self.last_raw.get('cpu_ticks'):
                total=raw['cpu_ticks'][0]-self.last_raw['cpu_ticks'][0];idle=raw['cpu_ticks'][1]-self.last_raw['cpu_ticks'][1]
                if total>0 and 0<=idle<=total:raw['cpu_percent']=round(100*(total-idle)/total,1)
            previous={r['id']:r for r in (self.last_raw or {}).get('interfaces',[])}
            self.interfaces=[]
            for row in raw.get('interfaces',[]):
                old=previous.get(row['id']);rx=tx=None
                if same and elapsed and elapsed>0 and old:
                    dr=row['rx_bytes']-old['rx_bytes'];dt=row['tx_bytes']-old['tx_bytes']
                    if dr>=0 and dt>=0:
                        rx,tx=dr/elapsed,dt/elapsed;self._add('nic',row['id'],dr,dt)
                key=(self.day(),'nic',row['id']);values=self.totals.get(key,[0,0])
                history=self.history.setdefault(row['id'],deque(maxlen=450))
                if rx is not None:history.append({'at':int(now*1000),'rx_bps':rx,'tx_bps':tx})
                self.interfaces.append({**row,'rx_bps':rx,'tx_bps':tx,'today_rx':values[0],'today_tx':values[1],
                                        'collected_since':self.starts.get(key),'history':list(history)})
            current={key[2]:values[:] for key,values in self.totals.items() if key[:2]==(self.day(),'device')}
            for ident,values in current.items():
                old=self.old_device.get(ident);self.rates[ident]=[max(0,values[n]-old[n])/elapsed if old is not None and elapsed and elapsed>0 else None for n in (0,1)]
            self.old_device=current;self.last_raw=raw;self.last_mono=mono;self.system=raw;self.sampled_at=int(now*1000)

    def flush(self):
        with self.flush_lock:
            with self.lock:
                if self.flushed==self.generation:return
                cutoff=(datetime.fromtimestamp(self.clock(),self.zone).date()-timedelta(days=7)).isoformat()
                for key in [k for k in self.totals if k[0][:10]<cutoff]:self.totals.pop(key,None);self.starts.pop(key,None)
                generation=self.generation;sessions={k:v.copy() for k,v in self.sessions.items()}
                totals=[(*key,*value,self.starts[key]) for key,value in self.totals.items()]
            try:
                with self.db() as db:
                    db.executemany('INSERT OR REPLACE INTO sessions VALUES(?,?,?,?)',[(k,v['since'],v['last'],v['ip']) for k,v in sessions.items()])
                    db.executemany('INSERT OR REPLACE INTO totals VALUES(?,?,?,?,?,?)',totals)
                    cutoff=(datetime.fromtimestamp(self.clock(),self.zone).date()-timedelta(days=7)).isoformat()
                    db.execute('DELETE FROM totals WHERE substr(day,1,10)<?',(cutoff,))
                self.flushed=generation;self.error=''
            except (OSError, sqlite3.Error):self.error='统计记录暂未落盘'

    def snapshot(self, devices, timezone=None, local_ip=None):
        now=self.clock()
        with self.lock:
            if timezone:
                zone=self.valid_zone(timezone)
                if str(zone)!=str(self.zone):
                    self.zone=zone;self.last_raw=None;self.last_mono=None;self.old_device={};self.rates={}
            rows=[]
            for d in devices:
                if not d.get('online') or d.get('enabled') is False or d.get('_archived'):continue
                ident=d['id'];session=self.sessions.get(ident);key=(self.day(),'device',ident);values=self.totals.get(key)
                valid=bool(session and 0<=now-session['last']<120 and session['ip']==d.get('ip'))
                rate=self.rates.get(ident,[None,None])
                rows.append({k:d.get(k,'') for k in ('id','room','group','model','ip')}|{
                    'online':True,'online_since':int(session['since']*1000) if valid else None,
                    'online_seconds':max(0,int(now-session['since'])) if valid else None,
                    # Server sends -> TV receives; the labels are from the TV's perspective.
                    'rx_bps':rate[1],'tx_bps':rate[0],'today_rx':values[1] if values else None,'today_tx':values[0] if values else None,
                    'collected_since':self.starts.get(key)})
            system={k:self.system.get(k) for k in ('available','hostname','system','cpu_percent','memory','uptime_seconds')}
            try:system['ip']=str(ipaddress.ip_address(local_ip)) if local_ip else None
            except ValueError:system['ip']=None
            return {'sampled_at':self.sampled_at,'timezone':str(self.zone),'server':system,'devices':rows,
                    'interfaces':self.interfaces,'scope':'authenticated_tv_http_body','error':self.error,
                    'traffic_note':'设备为影院业务有效数据量，不含其他应用、HTTP头或网络协议开销；今日累计仅含已采集时段。'}


class MeteredStream:
    def __init__(self, stream, owner, outgoing):self.stream=stream;self.owner=owner;self.outgoing=outgoing
    def __getattr__(self,name):return getattr(self.stream,name)
    def write(self, data):
        result=self.stream.write(data)
        if self.owner._traffic_device and self.owner._traffic_body:
            self.owner.server.runtime_monitor.add(self.owner._traffic_device,tx=len(data) if result is None else result)
        return result
    def read(self,*args):
        data=self.stream.read(*args)
        if self.owner._traffic_device:self.owner.server.runtime_monitor.add(self.owner._traffic_device,rx=len(data))
        return data
