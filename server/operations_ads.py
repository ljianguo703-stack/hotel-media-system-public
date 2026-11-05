"""Advertising inventory, partial bulk actions and schedule inspection."""
from datetime import datetime,timedelta
import hashlib
import json
from pathlib import Path
import secrets
from urllib.parse import quote,unquote
from zoneinfo import ZoneInfo
from ad_playback import PlaybackAdvertising


def core():
    import management
    return management


class AdvertisingOperations(PlaybackAdvertising):
    def campaign_validate_publication(self, db, item):
        m=core()
        if self.archived('campaign',item): raise m.Problem('广告已在回收站，请先恢复')
        if item['tier']==1 and item['format']=='图文':
            self.sponsor_card(item.get('creative_card'),item['merchant'],published=True)
        elif item['tier']==1:
            suffixes={'.jpg','.jpeg','.png','.webp'} if item['format']=='图片' else {'.mp4','.m4v','.webm','.mov','.mkv'}
            self.require_media_path(item['path'],suffixes)
        merchant=next((a for a in self.objects(db,'advertiser') if a['name']==item['merchant']),None)
        if merchant and (not merchant.get('enabled',True) or merchant.get('_archived')): raise m.Problem('广告主已停用，请先启用或变更广告主')
        if item['group']!='全部客房':
            group=next((g for g in self.objects(db,'group') if g['name']==item['group']),None)
            names={json.loads(r['body']).get('group') for r in db.execute('SELECT body FROM devices')}
            if group and (group.get('_archived') or not group.get('enabled',True)): raise m.Problem('目标设备组已停用')
            if not group and item['group'] not in names: raise m.Problem('目标设备组不存在')
        self.creatives(item)

    def campaigns_batch(self, body, actor, revision):
        m=core();ids=body.get('ids');action=body.get('operation')
        if not isinstance(ids,list) or not 1<=len(ids)<=500 or any(not isinstance(i,str) for i in ids) or len(set(ids))!=len(ids): raise m.Problem('每次请选择 1–500 条广告')
        if action not in ('publish','pause','archive','copy','set_group','set_merchant'): raise m.Problem('广告批量操作无效')
        result={'ok':[],'failed':[],'created':[]};seen=set()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE');self.check_revision(db,revision)
            for ident in ids:
                item={};success=[];created=[]
                db.execute('SAVEPOINT advertising_group')
                try:
                    item=self.record(db,'campaign',ident);key=item.get('_bundle_id') or ident
                    if key in seen:
                        db.execute('RELEASE advertising_group');continue
                    seen.add(key)
                    members=self.campaign_bundle_members(db,item,include_inactive=action in ('copy','archive','set_group','set_merchant'))
                    new_group='cmb-'+secrets.token_hex(12) if action=='copy' and item.get('_bundle_id') else None
                    for member in members:
                        row=dict(member)
                        if action=='copy':
                            row.update(id=(new_group+'-t'+str(row['tier'])) if new_group else 'ad-'+secrets.token_hex(12),name=row['name'][:190]+'（副本）',status='草稿',_archived=False,_created_at=m.now_ms(),_updated_at=m.now_ms())
                            if new_group: row.update(_bundle_id=new_group,_bundle_name=row.get('_bundle_name',row['name'])[:170]+'（副本）')
                            row.pop('_archived_at',None);db.execute('INSERT INTO campaigns VALUES(?,?)',(row['id'],m.encoded(row)));created.append(row['id'])
                        else:
                            if self.archived('campaign',row): raise m.Problem('组合中有广告已在回收站，请先恢复')
                            if action=='publish':self.campaign_validate_publication(db,row);row['status']='投放中'
                            elif action=='pause':row['status']='已暂停'
                            elif action=='archive':row.update(status='已归档',_archived=True,_archived_at=m.now_ms())
                            else:
                                value=m.text(body.get('value'),200,True);kind='group' if action=='set_group' else 'advertiser'
                                if value!='全部客房' or kind!='group':
                                    target=next((o for o in self.objects(db,kind) if o['name']==value and o.get('enabled',True) and not o.get('_archived')),None)
                                    if not target: raise m.Problem('目标'+('设备组' if kind=='group' else '广告主')+'不存在或已停用')
                                row['group' if action=='set_group' else 'merchant']=value
                            row['_updated_at']=m.now_ms();self.write_record(db,'campaign',row)
                        success.append(member['id'])
                    db.execute('RELEASE advertising_group');result['ok'].extend(success);result['created'].extend(created)
                except m.Problem as exc:
                    db.execute('ROLLBACK TO advertising_group');db.execute('RELEASE advertising_group')
                    result['failed'].append({'id':ident,'title':item.get('_bundle_name') or item.get('name',ident),'reason':str(exc)})
            if result['ok']:self.changed(db,actor,'批量广告'+{'publish':'发布','pause':'暂停','archive':'归档','copy':'复制','set_group':'改组','set_merchant':'改广告主'}[action]+' '+str(len(result['ok']))+' 条（失败 '+str(len(result['failed']))+'）',','.join(result['ok']))
        return result

    def register_asset(self, route, name, size, digest, actor):
        m=core();ident='asset-'+hashlib.sha256(route.encode()).hexdigest()[:20]
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE');before=self.kv(db,'revision')
            item={'id':ident,'name':name,'path':route,'bytes':size,'sha256':digest,'enabled':True,'notes':'',
                  'format':'图片' if Path(route).suffix.lower() in ('.jpg','.jpeg','.png','.webp') else '视频','_created_at':m.now_ms(),'_updated_at':m.now_ms()}
            self.put_object(db,'asset',item);self.changed(db,actor,'上传广告素材',name)
            return {'asset_id':ident,'previous_revision':before,'revision':self.kv(db,'revision')}

    def discover_assets(self, actor, revision):
        m=core();found=[];folder=self.media/'广告'
        if folder.is_dir():
            for file in folder.rglob('*'):
                if len(found)>=3000:raise m.Problem('单次发现超过 3000 个素材，请分批整理目录')
                if not file.is_file() or file.suffix.lower() not in ('.jpg','.jpeg','.png','.webp','.mp4','.webm','.m4v','.mov'):continue
                route='/media/'+quote(file.relative_to(self.media).as_posix(),safe='/')
                try:self.require_media_path(route)
                except m.Problem:continue
                found.append((file,route))
        added=0
        with self.db() as db:
            self.check_revision(db,revision);paths={unquote(a['path']) for a in self.objects(db,'asset')}|{unquote(a['path']) for a in self.objects(db,'asset_tombstone')}
            for file,route in found:
                if unquote(route) in paths:continue
                item={'id':'asset-'+hashlib.sha256(route.encode()).hexdigest()[:20],'name':file.name,'path':route,'bytes':file.stat().st_size,'enabled':True,'notes':'',
                      'format':'图片' if file.suffix.lower() in ('.jpg','.jpeg','.png','.webp') else '视频','_created_at':m.now_ms(),'_updated_at':m.now_ms()}
                self.put_object(db,'asset',item);added+=1
            if added:self.changed(db,actor,'发现本地广告素材',str(added)+' 项')
        return {'added':added,'scanned':len(found)}

    def schedule_preview(self, args):
        m=core()
        with self.db() as db:
            settings=self.kv(db,'settings');campaigns=[json.loads(r['body']) for r in db.execute('SELECT body FROM campaigns')]
        try:
            stamp=args.get('at',[''])[0]
            when=datetime.fromisoformat(stamp) if stamp else datetime.fromtimestamp(m.now_ms()/1000,ZoneInfo(settings['timezone']))
            if when.tzinfo is None:when=when.replace(tzinfo=ZoneInfo(settings['timezone']))
        except (ValueError,TypeError):raise m.Problem('请选择有效的预览日期与时间')
        group=args.get('group',['全部客房'])[0];rows=[];excluded=[];buckets={}
        for c in campaigns:
            reasons=[];local=when.astimezone(ZoneInfo(c['timezone']));clock=local.strftime('%H:%M');wrap=c['start']>c['end'] and c['tier']!=2
            day=local-timedelta(days=1) if wrap and clock<c['end'] else local
            if c['status']!='投放中' or self.archived('campaign',c):reasons.append('状态为'+c['status'])
            if c['group'] not in ('全部客房',group):reasons.append('不属于该设备组')
            creatives=self.creatives(c);ms=int(when.timestamp()*1000)
            if not creatives or not creatives[0]['valid_from_ms']<=ms<creatives[0]['valid_until_ms']:reasons.append('不在有效日期范围')
            if day.isoweekday() not in c['days']:reasons.append('不在投放星期')
            if c['tier']!=2 and c['start']!=c['end']:
                active=(clock>=c['start'] or clock<c['end']) if wrap else c['start']<=clock<c['end']
                if not active:reasons.append('不在每日投放时段')
            row={'id':c['id'],'name':c['name'],'tier':c['tier'],'priority':c['priority'],'placements':c['placements'],'timezone':c['timezone'],'local_time':local.isoformat(timespec='minutes'),'group':c['group']}
            if reasons:excluded.append({**row,'reasons':reasons});continue
            rows.append(row)
            for placement in c['placements']:
                keys=[placement] if c['tier']==1 else ['text-home','text-playback'] if placement=='all' else ['text-'+placement]
                for key in keys:
                    bucket=buckets.setdefault(key,[])
                    if not any(r['id']==row['id'] for r in bucket):bucket.append(row)
        conflicts=[]
        for key,items in buckets.items():
            ordered=sorted(items,key=lambda r:(-r['priority'],r['id']))
            if len(ordered)>1:conflicts.append({'placement':key,'candidate_ids':[c['id'] for c in ordered],'preferred':ordered[0]['name']})
        return {'at':when.isoformat(timespec='minutes'),'group':group,'eligible':rows,'excluded':excluded,'conflicts':conflicts,
                'basis':'根据日期、时段、星期、设备组与优先级预览；会话次数及最短展示间隔仍由电视按实际观看执行。'}
