"""AD12: atomic administration groups, retaining the existing per-tier TV/event IDs."""
import json
import re
import secrets


def core():
    import management
    return management


class CampaignBundleOperations:
    def campaign_bundle_members(self, db, item, include_inactive=False):
        bid = item.get('_bundle_id')
        if not bid: return [item]
        rows = [json.loads(r['body']) for r in db.execute('SELECT body FROM campaigns')]
        return [r for r in rows if r.get('_bundle_id') == bid and (include_inactive or r.get('_bundle_active', True))]

    def save_campaign_bundle(self, ident, body, actor, revision):
        m = core()
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', ident): raise m.Problem('组合广告标识无效')
        items = body.get('items')
        if not isinstance(items, list) or not 1 <= len(items) <= 3 or any(not isinstance(i, dict) for i in items): raise m.Problem('请选择一至三个广告档位')
        tiers = [i.get('tier') for i in items]
        if any(type(t) is not int or t not in (1, 2, 3) for t in tiers) or len(set(tiers)) != len(tiers): raise m.Problem('组合内的档位无效或重复')
        name = m.text(body.get('name'), 180, True, '广告名称')
        status = body.get('status')
        if status not in ('草稿', '投放中', '已暂停'): raise m.Problem('请选择组合广告的保存状态')
        # One owner and scope per group; time-of-day/text/material remain specific to each tier.
        shared = ('merchant', 'group', 'timezone', 'from', 'until', 'days')
        first = items[0]
        if any(any(i.get(k) != first.get(k) for k in shared) for i in items): raise m.Problem('组合广告的商户、客房组和投放日期须一致')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE'); self.check_revision(db, revision)
            rows = [json.loads(r['body']) for r in db.execute('SELECT body FROM campaigns')]
            existing = [r for r in rows if r.get('_bundle_id') == ident]
            if any(self.archived('campaign', r) for r in existing): raise m.Problem('组合中有回收站广告，请先恢复后编辑')
            by_tier = {r['tier']: r for r in existing}
            legacy_id = body.get('legacy_id')
            if legacy_id:
                legacy = next((r for r in rows if r['id'] == legacy_id), None)
                if not legacy or self.archived('campaign', legacy): raise m.Problem('原广告不存在或已归档')
                if legacy.get('_bundle_id') and legacy['_bundle_id'] != ident: raise m.Problem('不能接管其他组合的广告')
                if legacy['tier'] not in by_tier: by_tier[legacy['tier']] = legacy
            saved = []
            for item in items:
                tier = item['tier']; cid = by_tier.get(tier, {}).get('id') or ident + '-t' + str(tier)
                collision = next((r for r in rows if r['id'] == cid), None)
                if collision and collision.get('_bundle_id') != ident and cid != legacy_id: raise m.Problem('广告标识已被使用')
                value = {**item, 'name': name if len(items) == 1 else name + ' · ' + {1:'一档全屏',2:'二档全天文字',3:'三档分时文字'}[tier], 'status': status}
                try: result = self._upsert_campaign(db, cid, value, actor, notify=False)
                except m.Problem as exc: raise m.Problem({1:'一档全屏',2:'二档全天文字',3:'三档分时文字'}[tier]+'：'+str(exc), exc.status) from exc
                result.update(_bundle_id=ident, _bundle_name=name, _bundle_active=True)
                db.execute('UPDATE campaigns SET body=? WHERE id=?', (m.encoded(result), cid)); saved.append(cid)
            # Unchecking a tier stops it, while keeping its original ID/material/report history.
            for old in by_tier.values():
                if old['tier'] not in tiers:
                    old.update(_bundle_id=ident, _bundle_name=name, _bundle_active=False, status='已暂停', _updated_at=m.now_ms())
                    db.execute('UPDATE campaigns SET body=? WHERE id=?', (m.encoded(old), old['id']))
            self.changed(db, actor, '保存组合广告：' + status, name)
        return {'id': ident, 'campaign_ids': saved, 'tiers': tiers}

    def campaign_bundle_lifecycle(self, ids, action, actor, revision):
        m=core();result={'ok':[],'failed':[]};seen=set()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE');self.check_revision(db,revision)
            for ident in ids:
                item={};db.execute('SAVEPOINT campaign_lifecycle')
                try:
                    item=self.record(db,'campaign',ident);key=item.get('_bundle_id') or ident
                    if key in seen:db.execute('RELEASE campaign_lifecycle');continue
                    seen.add(key);members=self.campaign_bundle_members(db,item,include_inactive=True)
                    for row in members:
                        if action in ('purge','archive'):
                            refs=self.references(db,'campaign',row)
                            if refs:raise m.Problem('组合中有广告仍被引用，请先迁移引用')
                        if action=='purge':
                            if not self.archived('campaign',row):raise m.Problem('只允许清空回收站中的广告')
                            self.purge_record(db,'campaign',row)
                        elif action in ('archive','restore'):
                            row['_archived']=action=='archive'
                            if action=='archive':row['_archived_at']=m.now_ms()
                            row['status']='已归档' if action=='archive' else ('草稿' if row.get('_bundle_active',True) else '已暂停')
                            row['_updated_at']=m.now_ms();self.write_record(db,'campaign',row)
                        else:raise m.Problem('请使用广告投放入口整组发布或暂停')
                    db.execute('RELEASE campaign_lifecycle');result['ok'].extend(r['id'] for r in members)
                except m.Problem as exc:
                    db.execute('ROLLBACK TO campaign_lifecycle');db.execute('RELEASE campaign_lifecycle')
                    result['failed'].append({'id':ident,'title':item.get('_bundle_name') or item.get('name',ident),'reason':str(exc)})
            if result['ok']:self.changed(db,actor,'组合广告'+{'archive':'移入回收站','restore':'恢复','purge':'清空'}.get(action,action),','.join(result['ok']))
        return result
