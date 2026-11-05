"""Event-time reporting with attribution frozen at server receipt."""
from datetime import datetime,timedelta
import hashlib
import json
from zoneinfo import ZoneInfo


def core():
    import management
    return management


class ReportingOperations:
    def attach_attribution(self,db,device,event):
        event['_group']=device.get('group','全部客房');event['_room']=device.get('room',device['id'])
        creative=str(event.get('creative_id',''));cid=creative.split('~',1)[0]
        row=db.execute('SELECT body FROM campaigns WHERE id=?',(cid,)).fetchone() if creative else None
        if row:
            campaign=json.loads(row['body']);placements=campaign['placements'];placement=creative.split('~',1)[1] if '~' in creative else placements[0] if len(placements)==1 else ''
            event.update(_campaign_id=cid,_campaign_name=campaign['name'],_merchant=campaign.get('merchant',''),_tier=campaign['tier'],_placement=placement if placement in placements else '')
        if event.get('content_id'):
            row=db.execute('SELECT body FROM media WHERE id=?',(str(event['content_id']),)).fetchone()
            if row:event['_content_title']=json.loads(row['body']).get('title',event['content_id'])
            else:
                for row in db.execute("SELECT id,body FROM media WHERE json_extract(body,'$.kind')='series'"):
                    item=json.loads(row['body'])
                    episode=next((e for e in item.get('episodes',[]) if e.get('id')==event['content_id']),None)
                    if episode:
                        event.update(_content_title=item.get('title',row['id']),_content_parent=row['id'],_episode_title=episode.get('title',''))
                        break

    def build_reports(self,days=7,filters=None):
        m=core();days=m.number(days,1,366);f=filters or {};now=m.now_ms()
        with self.db() as db:
            zone=ZoneInfo(self.kv(db,'settings')['timezone']);current={r['id']:json.loads(r['body']) for r in db.execute('SELECT id,body FROM campaigns')}
            history={r['id']:r for r in self.objects(db,'campaign_history')};campaigns={**history,**current}
            device_groups={r['id']:json.loads(r['body']).get('group','全部客房') for r in db.execute('SELECT id,body FROM devices')}
            episode_parents={}
            for media in db.execute("SELECT id,body FROM media WHERE json_extract(body,'$.kind')='series'"):
                series=json.loads(media['body'])
                for episode in series.get('episodes',[]):episode_parents[episode['id']]=(media['id'],series.get('title',media['id']))
            begin=now-days*86400000;end=now+300000
            try:
                if f.get('from'):begin=int(datetime.strptime(f['from'],'%Y-%m-%d').replace(tzinfo=zone).timestamp()*1000)
                if f.get('until'):end=int((datetime.strptime(f['until'],'%Y-%m-%d').replace(tzinfo=zone)+timedelta(days=1)).timestamp()*1000)
                if end<=begin or end-begin>367*86400000:raise ValueError()
            except (ValueError,TypeError):raise m.Problem('请选择先后正确且不超过 366 天的日期范围')
            dimension=f.get('dimension','campaign')
            if dimension not in ('campaign','merchant','group','placement','device'):raise m.Problem('报表维度无效')
            if f.get('tier') and str(f['tier']) not in ('1','2','3'):raise m.Problem('广告档位无效')
            grouped={};timeline={};sessions=set();content={};seen_content=set();pause_groups=set()
            for row in db.execute('SELECT device,received,body FROM events WHERE received>=? ORDER BY received',(begin-300000,)):
                event=json.loads(row['body']);typ=event.get('type','')
                if typ=='deployment_probe' or event.get('exclude_from_ad_reports'):continue
                stamp=event.get('at_ms')
                if type(stamp) is not int or stamp<=0 or stamp>row['received']+300000:stamp=row['received']
                if not begin<=stamp<end:continue
                cid=event.get('_campaign_id') or str(event.get('creative_id','')).split('~',1)[0];campaign=campaigns.get(cid,{})
                tier=event.get('_tier',campaign.get('tier',event.get('tier',1)));merchant=event.get('_merchant',campaign.get('merchant','未归属'));group=event.get('_group',device_groups.get(row['device'],'未归组'))
                placement=event.get('_placement') or str(event.get('creative_id','')).partition('~')[2] or '未记录'
                if f.get('group') and group!=f['group']:continue
                is_content=typ in ('content_started','content_completed','content_failed')
                if f.get('tier') and (is_content or tier!=int(f['tier'])):continue
                if f.get('merchant') and (is_content or merchant!=f['merchant']):continue
                if f.get('placement') and (is_content or placement!=f['placement']):continue
                when=datetime.fromtimestamp(stamp/1000,zone);day=when.strftime('%Y-%m-%d');bucket=timeline.setdefault(day,{'date':day,'day':when.strftime('%m-%d'),'ad_starts':0,'content_sessions':0})
                if is_content:
                    legacy_parent=episode_parents.get(str(event.get('content_id','')))
                    content_id=str(event.get('_content_parent') or (legacy_parent[0] if legacy_parent else event.get('content_id','')));key=(row['device'],event.get('client_session_id',''),str(event.get('content_id','')))
                    title=event.get('_content_title') if event.get('_content_parent') or not legacy_parent else legacy_parent[1]
                    summary=content.setdefault(content_id,{'id':content_id,'title':title or content_id or '未记录作品','sessions':0,'completes':0,'failed':0})
                    if typ=='content_started' and key not in sessions:sessions.add(key);summary['sessions']+=1;bucket['content_sessions']+=1
                    elif typ=='content_completed':summary['completes']+=1
                    elif typ=='content_failed':summary['failed']+=1
                    continue
                if typ not in ('ad_requested','ad_started','ad_completed','ad_failed','ad_cancelled','ad_interrupted','text_started','text_ended'):continue
                if not self.ad_event_reportable(event): continue
                name=event.get('_campaign_name',campaign.get('name',cid or '未记录广告'))
                key=(cid,name,merchant,tier) if dimension=='campaign' else (merchant,) if dimension=='merchant' else (group,) if dimension=='group' else (placement,) if dimension=='placement' else (row['device'],)
                if key not in grouped:
                    base_id=cid if dimension=='campaign' else str(key[0]);ident=base_id
                    if any(r['id']==ident for r in grouped.values()):ident+='|'+hashlib.sha256(repr(key).encode()).hexdigest()[:10]
                    grouped[key]={'id':ident,'campaign_id':cid,'name':name if dimension=='campaign' else str(key[0]),'merchant':merchant,'group':group,'placement':placement,'tier':tier,'requested':0,'starts':0,'completes':0,'failed':0,'interrupted':0,'visible_ms':0,'pause_sessions':0,'pause_cycles':0}
                g=grouped[key]
                if typ=='ad_started' and event.get('slot')=='pause':
                    g['pause_cycles']+=1
                    pause=(key,row['device'],event.get('group_play_id') or event['id'])
                    if pause not in pause_groups:pause_groups.add(pause);g['pause_sessions']+=1
                if g['tier']!=tier:g['tier']=0
                for field,value,label in (('merchant',merchant,'多个广告主'),('group',group,'多个设备组'),('placement',placement,'多个位置')):
                    if g[field]!=value:g[field]=label
                if typ=='ad_requested':g['requested']+=1
                elif typ in ('ad_started','text_started'):g['starts']+=1;bucket['ad_starts']+=1
                elif typ=='ad_completed':g['completes']+=1
                elif typ=='ad_failed':g['failed']+=1
                elif typ in ('ad_cancelled','ad_interrupted'):g['interrupted']+=1
                elif typ=='text_ended' and type(event.get('visible_ms')) is int:g['visible_ms']+=max(0,min(event['visible_ms'],86400000))
        return {'days':days,'from':datetime.fromtimestamp(begin/1000,zone).strftime('%Y-%m-%d'),'until':datetime.fromtimestamp((end-1)/1000,zone).strftime('%Y-%m-%d'),
                'dimension':dimension,'rows':list(grouped.values()),'timeline':sorted(timeline.values(),key=lambda r:r['date']),'content_sessions':len(sessions),'content':sorted(content.values(),key=lambda c:-c['sessions']),
                'basis':'优先按客户端事件发生时间，缺失或异常时间回退到接收时间。广告名称/广告主/设备组在服务器接收时冻结；旧事件无快照时按现存索引或清空时保留的摘要归属。文字时长仅计已收到的事件；暂停组与素材播放次数分开，循环次数不代表新增观众。客户端时间最多接受7天离线回填，不采纳客户端排除标记。'}

    def management_overview(self):
        m=core()
        with self.db() as db:
            media=[dict(r) for r in db.execute('SELECT body,published,available FROM media')];ads=[json.loads(r['body']) for r in db.execute('SELECT body FROM campaigns')]
            jobs=[json.loads(r['body']) for r in db.execute('SELECT body FROM jobs ORDER BY rowid DESC LIMIT 100')]
            missing=sum(not r['available'] and not json.loads(r['body']).get('_archived') for r in media)
            unpublished=sum(not r['published'] and not json.loads(r['body']).get('_archived') for r in media)
            archived=len(self.trash_rows(db));failed=sum(j['status']=='failed' for j in jobs);running=sum(j['status'] in ('running','queued') for j in jobs)
            drafts=sum(a['status']=='草稿' and not a.get('_archived') for a in ads)
            expiring=self.expiring_orders(db)
        return {'unpublished':unpublished,'missing':missing,'draft_ads':drafts,'failed_jobs':failed,'running_jobs':running,'trash':archived,'expiring_orders':expiring,'checked_at':m.now_ms()}
