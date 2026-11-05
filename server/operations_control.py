"""A04 unified advertising control: global full-screen limits, a global text window and a one-click
commercial stop. TVs report the control version they applied; offline TVs are never shown as stopped."""
import json
import re


def core():
    import management
    return management


DEFAULT_CONTROL = {'version': 1, 'stopped': False, 'stopped_at': 0, 'stopped_by': '', 'max_fullscreen_per_hour': 0,
                   'max_fullscreen_seconds_per_hour': 0, 'min_fullscreen_gap_seconds': 0, 'text_start': '', 'text_end': '', 'updated_at': 0}
LIMIT_FIELDS = ('max_fullscreen_per_hour', 'max_fullscreen_seconds_per_hour', 'min_fullscreen_gap_seconds', 'text_start', 'text_end')


class ControlOperations:
    @staticmethod
    def ad_control(db):
        row = db.execute("SELECT value FROM kv WHERE key='ad_control'").fetchone()
        return {**DEFAULT_CONTROL, **(json.loads(row[0]) if row else {})}

    def house_merchants(self, db):
        return {a['name'] for a in self.objects(db, 'advertiser') if a.get('house') and not a.get('_archived')}

    def control_manifest(self, db, device, ads_by_campaign, record):
        """Apply the stop switch to the TV manifest and record which control version this TV fetched."""
        control = self.ad_control(db)
        house = self.house_merchants(db) if control['stopped'] else set()
        ads = [ad for merchant, items in ads_by_campaign if not control['stopped'] or merchant in house for ad in items]
        if record:
            row = db.execute("SELECT body FROM managed_objects WHERE kind='ad_control_delivery' AND id=?", (device['id'],)).fetchone()
            seen = json.loads(row['body']) if row else {}
            if seen.get('version') != control['version']:
                self.put_object(db, 'ad_control_delivery', {'id': device['id'], 'version': control['version'], 'at': core().now_ms()})
        public = {k: control[k] for k in ('version', 'stopped', *LIMIT_FIELDS)}
        public['timezone'] = self.kv(db, 'settings')['timezone']
        return ads, public

    def save_ad_control(self, body, user, revision):
        m = core()
        with self.db() as db:
            self.check_revision(db, revision)
            old = self.ad_control(db); item = dict(old)
            if 'stopped' in body:
                if not isinstance(body['stopped'], bool): raise m.Problem('停播开关无效')
                item['stopped'] = body['stopped']
                if body['stopped'] and not old['stopped']: item.update(stopped_at=m.now_ms(), stopped_by=user['name'])
            if any(f in body for f in LIMIT_FIELDS):
                if user['role'] != 'admin': raise m.Problem('统一上限与文字时段只允许管理员修改', 403)
                item['max_fullscreen_per_hour'] = m.number(body.get('max_fullscreen_per_hour', old['max_fullscreen_per_hour']), 0, 1000)
                item['max_fullscreen_seconds_per_hour'] = m.number(body.get('max_fullscreen_seconds_per_hour', old['max_fullscreen_seconds_per_hour']), 0, 3600)
                item['min_fullscreen_gap_seconds'] = m.number(body.get('min_fullscreen_gap_seconds', old['min_fullscreen_gap_seconds']), 0, 86400)
                start, end = str(body.get('text_start', old['text_start'])).strip(), str(body.get('text_end', old['text_end'])).strip()
                if bool(start) != bool(end): raise m.Problem('文字时段需同时填写开始和结束，或都留空表示全天')
                for value in (start, end):
                    if value and (not re.fullmatch(r'\d{2}:\d{2}', value) or int(value[:2]) > 23 or int(value[3:]) > 59): raise m.Problem('时间格式应为 HH:MM')
                if start and start == end: raise m.Problem('文字时段开始和结束不能相同；全天显示请都留空')
                item.update(text_start=start, text_end=end)
            if item == old: return self.ad_control_status(db)
            item['version'] = old['version'] + 1; item['updated_at'] = m.now_ms()
            self.put_kv(db, 'ad_control', item)
            what = ('一键停止商业广告' if item['stopped'] and not old['stopped'] else '恢复商业广告' if old['stopped'] and not item['stopped'] else '修改统一广告上限')
            self.changed(db, user['name'], what, '版本 %d' % item['version'])
            return self.ad_control_status(db)

    def ad_control_status(self, db=None):
        if db is None:
            with self.db() as conn: return self.ad_control_status(conn)
        m = core()
        control = self.ad_control(db); now = m.now_ms()
        delivered = {d['id']: d for d in self.objects(db, 'ad_control_delivery')}
        applied = {}
        since = control['updated_at'] - 86400000 if control['updated_at'] else 0
        for row in db.execute("SELECT device,received,body FROM events WHERE received>=? ORDER BY received", (since,)):
            event = json.loads(row['body'])
            if event.get('type') == 'ad_control_applied' and type(event.get('version')) is int:
                prev = applied.get(row['device'])
                if not prev or event['version'] >= prev['version']: applied[row['device']] = {'version': event['version'], 'at': row['received']}
        rows, counts = [], {'已执行': 0, '已拉取未回执': 0, '待确认': 0}
        for r in db.execute('SELECT id,body,last_seen FROM devices'):
            body = json.loads(r['body'])
            if body.get('enabled') is False: continue
            a, d = applied.get(r['id'], {}), delivered.get(r['id'], {})
            state = '已执行' if a.get('version', 0) >= control['version'] else '已拉取未回执' if d.get('version', 0) >= control['version'] else '待确认'
            counts[state] += 1
            rows.append({'id': r['id'], 'room': body.get('room', r['id']), 'group': body.get('group', '全部客房'), 'online': bool(r['last_seen'] and now - r['last_seen'] < 120000),
                         'last_seen': r['last_seen'], 'state': state, 'applied_version': a.get('version', 0), 'applied_at': a.get('at', 0),
                         'delivered_version': d.get('version', 0), 'delivered_at': d.get('at', 0)})
        rows.sort(key=lambda x: ({'待确认': 0, '已拉取未回执': 1, '已执行': 2}[x['state']], x['room']))
        return {'control': control, 'devices': rows, 'counts': counts, 'house_merchants': sorted(self.house_merchants(db)),
                'basis': '“已执行”= 电视回报已应用当前版本；“已拉取未回执”= 电视已取到当前清单但没有回执（旧版客户端或尚未处理）；“待确认”= 离线或尚未刷新，不能视为已停播。正在播放的一条广告会播完后才生效。'}
