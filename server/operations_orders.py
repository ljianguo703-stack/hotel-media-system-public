"""A01 advertising packages and insertion orders.

Registration and progress tracking only: quotes and payments are typed in by staff,
never derived from playback, and no payment gateway or accounting is involved.
"""
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import json
import re
import secrets
from zoneinfo import ZoneInfo
from advertising_contracts import package_capabilities, package_snapshot, contract, targets as delivery_targets


def core():
    import management
    return management


PAYMENT_STATES = ('未收款', '部分收款', '已收款', '已退款')
MONEY_FIELDS = {'package': ('list_price',), 'order': ('quoted_price', 'received_amount', 'payment_status', 'payment_note')}
MONEY_DEFAULTS = {'payment_status': '未收款', 'payment_note': ''}
TIER_PLACEMENTS = {1: ('home', 'preroll', 'midroll', 'pause', 'postroll', 'channel'), 2: ('all', 'home', 'playback'), 3: ('all', 'home', 'playback')}
MAX_MINOR = 100000000000000  # 1e12 in major units


def money_minor(value, label):
    """Parse a decimal amount (text or int, never float) into integer minor units (x100)."""
    m = core()
    if value is None or value == '': return 0
    if isinstance(value, (bool, float)) or not isinstance(value, (str, int)): raise m.Problem(label + '请以文本填写金额')
    try: amount = Decimal(str(value).strip().replace(',', ''))
    except InvalidOperation: raise m.Problem(label + '格式无效')
    if not amount.is_finite() or amount < 0 or amount != amount.quantize(Decimal('0.01')): raise m.Problem(label + '须为不小于 0、最多两位小数的金额')
    minor = int(amount * 100)
    if minor > MAX_MINOR: raise m.Problem(label + '超出范围')
    return minor


def money_text(minor):
    minor = int(minor or 0)
    return '%d.%02d' % (minor // 100, minor % 100)


def iso_day(value, label):
    m = core()
    text = m.text(value, 10, True)
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', text): raise m.Problem(label + '格式应为 YYYY-MM-DD')
    try: return date.fromisoformat(text)
    except ValueError: raise m.Problem(label + '不是有效日期')


class OrderOperations:
    @staticmethod
    def ad_event_reportable(event):
        return not (event.get('type') == 'ad_cancelled' and event.get('reason') == 'not_presented' and event.get('presented') is False)

    def validate_order_window(self, db, item):
        """Archived orders still own their historical campaign/date window."""
        for other in self.objects(db, 'order'):
            if other['id'] == item['id'] or other['end'] < item['start'] or other['start'] > item['end']: continue
            shared = set(item.get('campaign_ids', [])) & set(other.get('campaign_ids', []))
            if shared: raise core().Problem('广告任务 %s 的周期与投放单 %s 重叠（含回收站），请使用不重叠的续投周期' % ('、'.join(sorted(shared)), other.get('code', other['id'])), 409)

    def order_windows(self, db):
        windows = {}
        for order in self.objects(db, 'order'):
            for cid in order.get('campaign_ids', []): windows.setdefault(cid, []).append((order['id'], order['start'], order['end']))
        return windows

    @staticmethod
    def event_order(windows, cid, day):
        matches = [oid for oid, lo, hi in windows.get(cid, []) if lo <= day <= hi]
        # Old conflicting data is visible as a warning, never silently counted twice or assigned arbitrarily.
        return matches[0] if len(matches) == 1 else None

    # ---------- currency ----------
    @staticmethod
    def billing(db):
        row = db.execute("SELECT value FROM kv WHERE key='billing'").fetchone()
        return json.loads(row[0]) if row else {'currency': ''}

    def priced_records(self, db):
        rows = []
        for kind in ('package', 'order'):
            for item in self.objects(db, kind):
                if any(item.get(f) for f in ('list_price', 'quoted_price', 'received_amount')): rows.append(item.get('code') or item.get('name') or item['id'])
        return rows

    def save_currency(self, body, actor, revision):
        m = core()
        code = m.text(body.get('currency', ''), 3).upper()
        if code and not re.fullmatch(r'[A-Z]{3}', code): raise m.Problem('币种应为三位字母代码，例如 CNY、USD')
        with self.db() as db:
            self.check_revision(db, revision)
            old = self.billing(db).get('currency', '')
            if code == old: return {'currency': code}
            priced = self.priced_records(db)
            if old and priced: raise m.Problem('已有 %d 条带金额的套餐或投放单（如 %s），不能更改币种' % (len(priced), '、'.join(priced[:3])), 409)
            self.put_kv(db, 'billing', {'currency': code})
            self.changed(db, actor, '设置广告币种', code or '未设置')
        return {'currency': code}

    def billing_today(self, db):
        zone = ZoneInfo(self.kv(db, 'settings')['timezone'])
        return datetime.now(zone).date(), zone

    # ---------- shared validation ----------
    def guard_money(self, kind, item, old, user, currency):
        m = core()
        changed = [f for f in MONEY_FIELDS[kind] if item.get(f) != old.get(f, MONEY_DEFAULTS.get(f, 0))]
        if changed and user['role'] != 'admin': raise m.Problem('报价、标价与收款只允许管理员修改', 403)
        if not currency and any(item.get(f) for f in ('list_price', 'quoted_price', 'received_amount')): raise m.Problem('请先设置广告币种，再填写金额')
        return changed

    @staticmethod
    def money_audit(item, old, changed, currency):
        labels = {'list_price': '标价', 'quoted_price': '报价', 'received_amount': '已登记收款', 'payment_status': '收款状态', 'payment_note': '收款备注'}
        parts = []
        for f in changed:
            if f in ('list_price', 'quoted_price', 'received_amount'): parts.append('%s %s→%s %s' % (labels[f], money_text(old.get(f, 0)), money_text(item[f]), currency))
            elif f == 'payment_status': parts.append('%s %s→%s' % (labels[f], old.get(f, '未收款'), item[f]))
            else: parts.append(labels[f] + '已修改')
        return '；'.join(parts)

    def save_package(self, body, user, revision):
        m = core()
        ident = m.text(body.get('id'), 100, True)
        if not re.fullmatch(r'[A-Za-z0-9_-]+', ident): raise m.Problem('标识格式无效')
        tier = body.get('tier')
        if type(tier) is not int or tier not in TIER_PLACEMENTS: raise m.Problem('请选择广告档位')
        placements = body.get('placements') or []
        if not isinstance(placements, list) or not placements or len(set(placements)) != len(placements) or not set(placements) <= set(TIER_PLACEMENTS[tier]): raise m.Problem('请选择该档位允许的展示位置')
        with self.db() as db:
            self.check_revision(db, revision)
            row = db.execute("SELECT body FROM managed_objects WHERE kind='package' AND id=?", (ident,)).fetchone()
            old = json.loads(row['body']) if row else {}
            if old.get('_archived'): raise m.Problem('套餐已在回收站，请先恢复')
            currency = self.billing(db).get('currency', '')
            item = {**old, 'id': ident, 'name': m.text(body.get('name'), 160, True), 'tier': tier, 'placements': placements,
                    'default_days': m.number(body.get('default_days', 30), 1, 366), 'target': m.number(body.get('target', 0), 0, 1000000000),
                    'list_price': money_minor(body.get('list_price'), '标价'), 'notes': m.text(body.get('notes', ''), 2000), 'enabled': body.get('enabled', True) is True}
            item.update(package_capabilities({**body, 'tier': tier, 'placements': placements}, old))
            item['targets'] = delivery_targets(body.get('targets'), tier, item['target'], old.get('targets'))
            if any(p['id'] != ident and p['name'] == item['name'] for p in self.objects(db, 'package')): raise m.Problem('套餐名称已存在（含回收站）', 409)
            changed = self.guard_money('package', item, old, user, currency)
            now = m.now_ms()
            if not old: item['_created_at'] = now
            item['_updated_at'] = now
            # Freeze legacy orders before a template changes; never reinterpret old contracts through a new tier/name.
            if old:
                for order in self.objects(db, 'order'):
                    if order.get('package_id') == ident and not order.get('package_snapshot'):
                        order['package_snapshot'] = package_snapshot(old); self.put_object(db, 'order', order)
            self.put_object(db, 'package', item)
            note = self.money_audit(item, old, changed, currency)
            self.changed(db, user['name'], '保存广告套餐', item['name'] + ('（' + note + '）' if note else ''))
        return item

    def save_order(self, body, user, revision):
        m = core()
        ident = m.text(body.get('id'), 100, True)
        if not re.fullmatch(r'[A-Za-z0-9_-]+', ident): raise m.Problem('标识格式无效')
        start, end = iso_day(body.get('start'), '开始日期'), iso_day(body.get('end'), '结束日期')
        if end < start: raise m.Problem('结束日期不能早于开始日期')
        if (end - start).days > 365: raise m.Problem('投放周期不能超过 366 天')
        status = body.get('payment_status', '未收款')
        if status not in PAYMENT_STATES: raise m.Problem('收款状态无效')
        campaign_ids = body.get('campaign_ids') or []
        if not isinstance(campaign_ids, list) or len(campaign_ids) > 100 or len(set(campaign_ids)) != len(campaign_ids) or any(not isinstance(c, str) or not c for c in campaign_ids):
            raise m.Problem('关联广告任务列表无效（最多 100 个）')
        with self.db() as db:
            self.check_revision(db, revision)
            row = db.execute("SELECT body FROM managed_objects WHERE kind='order' AND id=?", (ident,)).fetchone()
            old = json.loads(row['body']) if row else {}
            if old.get('_archived'): raise m.Problem('投放单已在回收站，请先恢复')
            currency = self.billing(db).get('currency', '')
            advertiser = next((a for a in self.objects(db, 'advertiser') if a['id'] == body.get('advertiser_id')), None)
            if not advertiser or advertiser.get('_archived'): raise m.Problem('请选择有效的广告主')
            if not advertiser.get('enabled') and advertiser['id'] != old.get('advertiser_id'): raise m.Problem('广告主已停用，不能用于新的投放单')
            package = next((p for p in self.objects(db, 'package') if p['id'] == body.get('package_id')), None)
            if not package or package.get('_archived'): raise m.Problem('请选择有效的广告套餐')
            if not package.get('enabled') and package['id'] != old.get('package_id'): raise m.Problem('套餐已停用，不能用于新的投放单')
            reapply = body.get('reapply_template', False)
            if type(reapply) is not bool: raise m.Problem('套用模板选项无效')
            snapshot = old.get('package_snapshot') if package['id'] == old.get('package_id') and not reapply else None
            snapshot = snapshot or package_snapshot(package)
            group = m.text(body.get('group', '全部客房'), 160, True)
            if group != '全部客房' and not any(g['name'] == group and not g.get('_archived') for g in self.objects(db, 'group')): raise m.Problem('设备组不存在')
            self.validate_order_window(db, {'id': ident, 'start': start.isoformat(), 'end': end.isoformat(), 'campaign_ids': campaign_ids})
            for cid in campaign_ids:
                crow = db.execute('SELECT body FROM campaigns WHERE id=?', (cid,)).fetchone()
                if not crow:
                    if cid in old.get('campaign_ids', []): continue  # keep a dangling link visible rather than blocking unrelated edits
                    raise m.Problem('广告任务不存在：' + cid)
                campaign = json.loads(crow['body'])
                if campaign.get('status') == '已归档' and cid not in old.get('campaign_ids', []): raise m.Problem('广告任务已归档：' + campaign['name'])
                if campaign['tier'] not in snapshot.get('tiers', [snapshot['tier']]): raise m.Problem('广告任务「%s」是%d档，与套餐展示能力不符' % (campaign['name'], campaign['tier']))
                if campaign['merchant'] != advertiser['name'] and cid not in old.get('campaign_ids', []): raise m.Problem('只能关联本广告主的广告任务')
                allowed = snapshot.get('placements_by_tier', {}).get(str(campaign['tier']), snapshot.get('placements', []) if campaign['tier']==snapshot['tier'] else ['all'])
                if cid not in old.get('campaign_ids', []) and 'all' not in allowed and not set(campaign['placements']) <= set(allowed): raise m.Problem('广告位置超出套餐范围，请先调整任务或选择合适套餐')
            item = {**old, 'id': ident, 'advertiser_id': advertiser['id'], 'package_id': package['id'], 'campaign_ids': campaign_ids, 'group': group,
                    'start': start.isoformat(), 'end': end.isoformat(), 'target': m.number(body.get('target', old.get('target', package.get('target', 0))), 0, 1000000000),
                    'quoted_price': money_minor(body.get('quoted_price'), '报价'), 'received_amount': money_minor(body.get('received_amount'), '已登记收款'),
                    'payment_status': status, 'payment_note': m.text(body.get('payment_note', ''), 500), 'remind_days': m.number(body.get('remind_days', 7), 0, 365),
                    'notes': m.text(body.get('notes', ''), 2000), 'enabled': body.get('enabled', True) is True}
            item['package_snapshot'] = snapshot
            item['targets'] = delivery_targets(body.get('targets'), snapshot['tier'], item['target'], old.get('targets', package.get('targets')))
            quoted, received = item['quoted_price'], item['received_amount']
            if status == '未收款' and received: raise m.Problem('“未收款”时已登记收款应为 0')
            if status == '部分收款' and not 0 < received < quoted: raise m.Problem('“部分收款”时已登记收款应大于 0 且小于报价')
            if status == '已收款' and (not quoted or received != quoted): raise m.Problem('“已收款”时已登记收款应等于报价')
            if status == '已退款' and received: raise m.Problem('“已退款”时已登记收款（净额）应为 0')
            changed = self.guard_money('order', item, old, user, currency)
            now = m.now_ms()
            if not old:
                today, _ = self.billing_today(db)
                item['code'] = 'PO-' + today.strftime('%Y%m%d') + '-' + secrets.token_hex(2).upper()
                item['_created_at'] = now
            item['_updated_at'] = now
            self.put_object(db, 'order', item)
            note = self.money_audit(item, old, changed, currency)
            self.changed(db, user['name'], '保存投放单', item['code'] + ('（' + note + '）' if note else ''))
        return item

    # ---------- progress and listing ----------
    def order_progress(self, db, orders, zone):
        """Count real terminal events of linked campaigns inside each order window (event time, receipt fallback)."""
        windows = self.order_windows(db)
        empty = lambda: {'starts': 0, 'completes': 0, 'failed': 0, 'visible_ms': 0}
        stats = {o['id']: {**empty(), 'by_campaign': {}, 'by_tier': {t: {**empty(), 'devices': set()} for t in (1,2,3)}} for o in orders}
        campaign_tiers = {r['id']: json.loads(r['body']).get('tier', 1) for r in db.execute('SELECT id,body FROM campaigns')}
        if not orders or not windows: return stats
        first = min(date.fromisoformat(o['start']) for o in orders)
        begin = int(datetime(first.year, first.month, first.day, tzinfo=zone).timestamp() * 1000) - 7 * 86400000
        for row in db.execute('SELECT device,received,body FROM events WHERE received>=? ORDER BY received', (begin,)):
            event = json.loads(row['body']); typ = event.get('type', '')
            if typ not in ('ad_started', 'ad_completed', 'ad_failed', 'text_started', 'text_ended') or event.get('exclude_from_ad_reports'): continue
            cid = event.get('_campaign_id') or str(event.get('creative_id', '')).split('~', 1)[0]
            if cid not in windows: continue
            stamp = event.get('at_ms')
            if type(stamp) is not int or stamp <= 0 or stamp > row['received'] + 300000: stamp = row['received']
            oid = self.event_order(windows, cid, datetime.fromtimestamp(stamp / 1000, zone).date().isoformat())
            if oid not in stats: continue
            tier = event.get('_tier', campaign_tiers.get(cid, 1 if typ.startswith('ad_') else 2))
            if tier not in (1,2,3) or (typ.startswith('ad_') and tier!=1) or (typ.startswith('text_') and tier==1): continue
            s = stats[oid]; c = s['by_campaign'].setdefault(cid, empty())
            bucket = s['by_tier'][tier]
            if typ in ('ad_started','text_started') or (typ=='text_ended' and type(event.get('visible_ms')) is int and event['visible_ms']>0): bucket['devices'].add(row['device'])
            key = 'starts' if typ in ('ad_started', 'text_started') else 'completes' if typ == 'ad_completed' else 'failed' if typ == 'ad_failed' else None
            if key:
                for target in (s,c,bucket): target[key] += 1
            elif type(event.get('visible_ms')) is int:
                v = max(0, min(event['visible_ms'], 86400000))
                for target in (s,c,bucket): target['visible_ms'] += v
        return stats

    def describe_orders(self, db, orders):
        today, zone = self.billing_today(db)
        advertisers = {a['id']: a for a in self.objects(db, 'advertiser')}
        packages = {p['id']: p for p in self.objects(db, 'package')}
        campaigns = {r['id']: json.loads(r['body']) for r in db.execute('SELECT id,body FROM campaigns')}
        stats = self.order_progress(db, orders, zone)
        windows = self.order_windows(db)
        rows = []
        for o in orders:
            package = contract(o, packages); tier = package.get('tier', 0)
            start, end = date.fromisoformat(o['start']), date.fromisoformat(o['end'])
            state = '已归档' if o.get('_archived') else '已停用' if not o.get('enabled') else '未开始' if today < start else '已到期' if today > end else '执行中'
            s = stats.get(o['id'], {'starts': 0, 'completes': 0, 'failed': 0, 'visible_ms': 0, 'by_campaign': {}})
            metrics = s.get('by_tier', {}).get(tier, s)
            actual = metrics['starts'] if tier == 1 else metrics['visible_ms'] // 60000
            target = o.get('target', 0)
            progress_by_tier = []
            for t in package.get('tiers', [tier]):
                bucket = s.get('by_tier', {}).get(t, {'starts':0,'completes':0,'failed':0,'visible_ms':0,'devices':set()})
                value = bucket['starts'] if t==1 else bucket['visible_ms']//60000
                goal = o.get('targets', {}).get(str(t), target if t==tier else 0)
                progress_by_tier.append({'tier':t,'actual':value,'target':goal,'unit':'次' if t==1 else '分钟','percent':min(100,value*100//goal) if goal else None,
                                         'starts':bucket['starts'],'completes':bucket['completes'],'failed':bucket['failed'],'devices':len(bucket['devices'])})
            warnings, linked = [], []
            for cid in o.get('campaign_ids', []):
                if any(oid != o['id'] and lo <= o['end'] and hi >= o['start'] for oid, lo, hi in windows.get(cid, [])):
                    warnings.append('任务 %s 有重叠投放单；重叠日期事件暂不归属，修正周期后重新生成报告' % cid)
                c = campaigns.get(cid)
                if not c:
                    warnings.append('关联任务已不存在：' + cid); linked.append({'id': cid, 'exists': False, 'name': cid}); continue
                if o['group'] != '全部客房' and c.get('group') != o['group']: warnings.append('任务「%s」客房组为 %s，与投放单范围 %s 不一致' % (c['name'], c.get('group'), o['group']))
                if c.get('status') != '投放中' and state == '执行中': warnings.append('任务「%s」当前状态为 %s' % (c['name'], c.get('status')))
                linked.append({'id': cid, 'exists': True, 'name': c['name'], 'tier': c['tier'], 'group': c.get('group'), 'status': c.get('status'),
                               **s['by_campaign'].get(cid, {'starts': 0, 'completes': 0, 'failed': 0, 'visible_ms': 0})})
            if not o.get('campaign_ids') and state in ('未开始', '执行中'): warnings.append('尚未关联广告任务，进度不会增长')
            rows.append({**o, 'state': state, 'days_left': (end - today).days, 'expiring': state == '执行中' and (end - today).days <= o.get('remind_days', 7),
                         'advertiser_name': advertisers.get(o['advertiser_id'], {}).get('name', '已清空的广告主'), 'package_name': package.get('name', '已清空的套餐'), 'tier': tier,
                         'tiers':package.get('tiers',[tier]),'progress_by_tier':progress_by_tier,
                         'progress': {'actual': actual, 'target': target, 'unit': '次' if tier == 1 else '分钟', 'percent': min(100, actual * 100 // target) if target else None,
                                      'starts': metrics['starts'], 'completes': metrics['completes'], 'failed': metrics['failed'], 'visible_minutes': metrics['visible_ms'] // 60000},
                         'campaigns': linked, 'warnings': warnings,
                         'quoted_text': money_text(o.get('quoted_price', 0)), 'received_text': money_text(o.get('received_amount', 0))})
        return rows

    def order_rows(self, args):
        from operations_system import page_number
        limit = page_number(args, 'limit', 30, 1, 200); offset = page_number(args, 'offset', 0, 0, 1000000)
        arg = lambda key: args.get(key, [''])[0]
        query = arg('q').casefold()
        with self.db() as db:
            currency = self.billing(db).get('currency', '')
            archived = arg('archived') == '1'
            orders = sorted([o for o in self.objects(db, 'order') if bool(o.get('_archived')) == archived], key=lambda o: o.get('_created_at', 0), reverse=True)
            rows = self.describe_orders(db, orders)
        rows = [r for r in rows if (not arg('state') or r['state'] == arg('state')) and (not arg('advertiser') or r['advertiser_id'] == arg('advertiser'))
                and (not arg('payment') or r['payment_status'] == arg('payment')) and (arg('expiring') != '1' or r['expiring'])
                and (not query or query in ' '.join([r.get('code', ''), r['advertiser_name'], r['package_name'], r.get('notes', '')]).casefold())]
        summary = {'count': len(rows), 'expiring': sum(r['expiring'] for r in rows),
                   'quoted_total_text': money_text(sum(r.get('quoted_price', 0) for r in rows)), 'received_total_text': money_text(sum(r.get('received_amount', 0) for r in rows))}
        return {'rows': rows[offset:offset + limit], 'total': len(rows), 'summary': summary, 'currency': currency}

    def expiring_orders(self, db):
        return sum(r['expiring'] for r in self.describe_orders(db, [o for o in self.objects(db, 'order') if not o.get('_archived')]))

    # ---------- A03 merchant report ----------
    def merchant_report(self, args):
        """Printable advertiser report built only from events the TVs actually sent; no money, no estimates."""
        m = core()
        arg = lambda key: args.get(key, [''])[0]
        with self.db() as db:
            today, zone = self.billing_today(db)
            every = self.objects(db, 'order')
            if arg('order'):
                order = next((o for o in every if o['id'] == arg('order')), None)
                if not order: raise m.Problem('投放单不存在', 404)
                scope, advertiser_id = [order], order['advertiser_id']
                first, last = date.fromisoformat(order['start']), min(date.fromisoformat(order['end']), max(today, date.fromisoformat(order['start'])))
            else:
                advertiser_id = arg('advertiser')
                if not advertiser_id: raise m.Problem('请选择广告主或投放单')
                scope = [o for o in every if o['advertiser_id'] == advertiser_id]
                first, last = today - timedelta(days=6), today
            advertiser = next((a for a in self.objects(db, 'advertiser') if a['id'] == advertiser_id), None)
            if not advertiser: raise m.Problem('广告主不存在', 404)
            start = iso_day(arg('from'), '开始日期') if arg('from') else first
            end = iso_day(arg('until'), '结束日期') if arg('until') else last
            if end < start: raise m.Problem('结束日期不能早于开始日期')
            if (end - start).days > 365: raise m.Problem('报告范围不能超过 366 天')
            scope = [o for o in scope if o['start'] <= end.isoformat() and o['end'] >= start.isoformat()]
            described = {r['id']: r for r in self.describe_orders(db, scope)}
            names = {r['id']: r for r in self.objects(db, 'campaign_history')}
            names.update({r['id']: json.loads(r['body']) for r in db.execute('SELECT id,body FROM campaigns')})
            windows = self.order_windows(db)
            blank = lambda: {'starts': 0, 'completes': 0, 'failed': 0, 'interrupted': 0, 'visible_ms': 0,'fullscreen_starts':0,'fullscreen_completes':0,'text_starts':0}
            per_order = {o['id']: {**blank(), 'rooms': set()} for o in scope}
            per_campaign = {(o['id'], cid): {**blank(), 'rooms': set()} for o in scope for cid in o.get('campaign_ids', [])}
            days = {(start + timedelta(days=i)).isoformat(): blank() for i in range((end - start).days + 1)}
            failures = Counter()
            begin = int(datetime(start.year, start.month, start.day, tzinfo=zone).timestamp() * 1000) - 7 * 86400000
            kinds = {'ad_started': 'starts', 'text_started': 'starts', 'ad_completed': 'completes', 'ad_failed': 'failed', 'ad_interrupted': 'interrupted', 'ad_cancelled': 'interrupted'}
            for row in db.execute('SELECT device,received,body FROM events WHERE received>=? ORDER BY received', (begin,)) if windows else []:
                event = json.loads(row['body']); typ = event.get('type', '')
                if (typ not in kinds and typ != 'text_ended') or event.get('exclude_from_ad_reports') or not self.ad_event_reportable(event): continue
                cid = event.get('_campaign_id') or str(event.get('creative_id', '')).split('~', 1)[0]
                if cid not in windows: continue
                tier = event.get('_tier', names.get(cid, {}).get('tier', 1 if typ.startswith('ad_') else 2))
                if tier not in (1,2,3) or (typ.startswith('ad_') and tier != 1) or (typ.startswith('text_') and tier == 1): continue
                stamp = event.get('at_ms')
                if type(stamp) is not int or stamp <= 0 or stamp > row['received'] + 300000: stamp = row['received']
                day = datetime.fromtimestamp(stamp / 1000, zone).date().isoformat()
                if not start.isoformat() <= day <= end.isoformat(): continue
                oid = self.event_order(windows, cid, day)
                if oid not in per_order: continue
                room = str(event.get('_room') or row['device'])
                targets = (per_order[oid], per_campaign[oid, cid], days[day])
                if typ == 'text_ended':
                    if type(event.get('visible_ms')) is not int: continue
                    value = max(0, min(event['visible_ms'], 86400000))
                    for t in targets: t['visible_ms'] += value
                else:
                    for t in targets: t[kinds[typ]] += 1
                    typed = {'ad_started':'fullscreen_starts','ad_completed':'fullscreen_completes','text_started':'text_starts'}.get(typ)
                    if typed:
                        for t in targets: t[typed] += 1
                    if typ == 'ad_failed': failures[m.text(event.get('reason') or '未注明原因', 120)] += 1
                if typ in ('ad_started','text_started') or (typ=='text_ended' and type(event.get('visible_ms')) is int and event['visible_ms']>0):
                    per_order[oid]['rooms'].add(room); per_campaign[oid, cid]['rooms'].add(room)
            settings = self.kv(db, 'settings')
        def finish(stats):
            out = {k: v for k, v in stats.items() if k != 'rooms'}
            out['rooms'] = len(stats['rooms']); out['visible_minutes'] = stats['visible_ms'] // 60000
            return out
        orders = []
        for o in scope:
            d = described[o['id']]
            campaigns = [{'id': cid, 'name': names.get(cid, {}).get('name', cid), 'exists': cid in names, **finish(per_campaign[o['id'], cid])} for cid in o.get('campaign_ids', [])]
            orders.append({'id': o['id'], 'code': o.get('code', o['id']), 'package_name': d['package_name'], 'tier': d['tier'], 'group': o['group'],
                           'start': o['start'], 'end': o['end'], 'state': d['state'], 'target_progress': d['progress'], 'progress_by_tier':d['progress_by_tier'],'warnings': d['warnings'],
                           'range': finish(per_order[o['id']]), 'campaigns': campaigns})
        totals = blank(); rooms = set()
        for o in scope:
            for k in totals: totals[k] += per_order[o['id']][k]
            rooms |= per_order[o['id']]['rooms']
        totals['rooms'] = len(rooms); totals['visible_minutes'] = totals['visible_ms'] // 60000
        return {'hotel': settings.get('hotel', ''), 'timezone': settings['timezone'], 'advertiser': {'id': advertiser['id'], 'name': advertiser['name'], 'contact': advertiser.get('contact', '')},
                'from': start.isoformat(), 'until': end.isoformat(), 'generated_at': m.now_ms(), 'orders': orders, 'totals': totals,
                'timeline': [{'date': k, **{f: v for f, v in val.items() if f != 'visible_ms'}, 'visible_minutes': val['visible_ms'] // 60000} for k, val in days.items()],
                'failures': [{'reason': r, 'count': c} for r, c in failures.most_common(10)],
                'empty': not any(totals[k] for k in ('starts', 'completes', 'failed', 'interrupted', 'visible_ms')),
                'basis': '按当前保留的投放单归属，统计投放周期与报告日期交集内电视实际上报的事件；彻底清空投放单会移除其历史归属。技术验证事件不计，未呈现的取消不计中断。日期按系统默认时区，优先使用事件发生时间。一档为全屏图片/视频，二、三档为顶部滚动文字（按可见分钟计）。文字轮播按客户端汇总上报，可能延后约5分钟；仅统计已收到的时长，覆盖仅计呈现记录。本报告不含金额。'}
