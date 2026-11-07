"""MD01: TV home appearance (A07 background + contact QR + Wi-Fi QR), read-only hotel guide cards (A05)
and custom topics shown as home rows (A06). Guide cards are information only: no orders, bookings or payments."""
from datetime import datetime
import hashlib
import json
import re
import secrets
from zoneinfo import ZoneInfo

import qr_image
from home_menu import HomeMenuOperations
from home_backgrounds import available_backgrounds, prepare_backgrounds


def core():
    import management
    return management


IMAGES = {'.jpg', '.jpeg', '.png', '.webp'}
HOME_DEFAULT = {'background': '', 'contact_qr': '', 'contact_label': '扫码联系前台', 'wifi_ssid': '', 'wifi_password': '', 'wifi_security': 'WPA',
                'wifi_hidden': False, 'wifi_label': '扫码连接 Wi-Fi', 'wifi_show_password': False, 'wifi_qr': '', 'show_qr': True}


class HomeOperations(HomeMenuOperations):
    def initialize_home_backgrounds(self, db, fresh):
        images = prepare_backgrounds(self.media)
        if fresh and images:
            item = {**HOME_DEFAULT, 'background': secrets.choice(images)['path']}
            db.execute("INSERT OR IGNORE INTO kv(key,value) VALUES('home_screen',?)", (json.dumps(item, ensure_ascii=False),))

    @staticmethod
    def home_settings(db):
        row = db.execute("SELECT value FROM kv WHERE key='home_screen'").fetchone()
        return {**HOME_DEFAULT, **(json.loads(row[0]) if row else {})}

    def optional_image(self, value):
        value = str(value or '').strip()
        return self.require_media_path(value, IMAGES) if value else ''

    def save_home(self, body, user, revision):
        m = core()
        security = body.get('wifi_security', 'WPA')
        if security not in ('WPA', 'WEP', 'nopass'): raise m.Problem('Wi-Fi 加密方式无效')
        item = {'background': self.optional_image(body.get('background')), 'contact_qr': self.optional_image(body.get('contact_qr')),
                'contact_label': m.text(body.get('contact_label', ''), 40), 'wifi_ssid': str(body.get('wifi_ssid', '')).strip()[:32],
                'wifi_password': str(body.get('wifi_password', ''))[:63], 'wifi_security': security, 'wifi_hidden': body.get('wifi_hidden') is True,
                'wifi_label': m.text(body.get('wifi_label', ''), 40), 'wifi_show_password': body.get('wifi_show_password') is True, 'show_qr': body.get('show_qr', True) is True}
        if item['wifi_ssid']:
            if security == 'WPA' and not 8 <= len(item['wifi_password']) <= 63: raise m.Problem('WPA 密码应为 8–63 个字符')
            if security == 'WEP' and len(item['wifi_password']) not in (5, 13, 10, 26): raise m.Problem('WEP 密码长度应为 5/13 个字符或 10/26 位十六进制')
            if security == 'nopass': item['wifi_password'] = ''
            payload = qr_image.wifi_payload(item['wifi_ssid'], item['wifi_password'], security, item['wifi_hidden'])
            name = 'wifi-' + hashlib.sha256(payload.encode()).hexdigest()[:16] + '.png'
            folder = self.media / '_home'; folder.mkdir(parents=True, exist_ok=True)
            target = folder / name
            if not target.exists():
                temp = folder / ('.' + name + '.tmp'); temp.write_bytes(qr_image.qr_png(payload)); temp.replace(target)
            item['wifi_qr'] = '/media/_home/' + name
        else:
            item.update(wifi_password='', wifi_qr='')
        with self.db() as db:
            self.check_revision(db, revision)
            self.put_kv(db, 'home_screen', item)
            self.changed(db, user['name'], '保存电视首页设置', '背景' + ('已设置' if item['background'] else '未设置') + '，Wi-Fi ' + (item['wifi_ssid'] or '未设置'))
        return self.home_admin_state()

    def save_guide(self, body, user, revision):
        m = core()
        ident = m.text(body.get('id'), 100, True)
        if not re.fullmatch(r'[A-Za-z0-9_-]+', ident): raise m.Problem('标识格式无效')
        with self.db() as db:
            self.check_revision(db, revision)
            row = db.execute("SELECT body FROM managed_objects WHERE kind='guide' AND id=?", (ident,)).fetchone()
            old = json.loads(row['body']) if row else {}
            if old.get('_archived'): raise m.Problem('卡片已在回收站，请先恢复')
            item = {**old, 'id': ident, 'name': m.text(body.get('name'), 40, True), 'body': m.text(body.get('body', ''), 800, True),
                    'image': self.optional_image(body.get('image')), 'sort': m.number(body.get('sort', 100), 0, 100000), 'enabled': body.get('enabled', True) is True}
            now = m.now_ms()
            if not old: item['_created_at'] = now
            item['_updated_at'] = now
            self.put_object(db, 'guide', item); self.changed(db, user['name'], '保存酒店指南卡片', item['name'])
        return item

    def topic_time(self, value, zone, label):
        m = core()
        value = str(value or '').strip()
        if not value: return ''
        try: datetime.strptime(value, '%Y-%m-%dT%H:%M').replace(tzinfo=zone)
        except ValueError: raise m.Problem(label + '格式应为 YYYY-MM-DDTHH:MM')
        return value

    def save_topic(self, body, user, revision):
        m = core()
        ident = m.text(body.get('id'), 100, True)
        if not re.fullmatch(r'[A-Za-z0-9_-]+', ident): raise m.Problem('标识格式无效')
        ids = body.get('item_ids') or []
        if not isinstance(ids, list) or len(ids) > 200 or len(set(ids)) != len(ids) or any(not isinstance(i, str) for i in ids): raise m.Problem('专题内容列表无效（最多 200 部）')
        with self.db() as db:
            self.check_revision(db, revision)
            zone = ZoneInfo(self.kv(db, 'settings')['timezone'])
            known = {r['id'] for r in db.execute('SELECT id FROM media')}
            row = db.execute("SELECT body FROM managed_objects WHERE kind='topic' AND id=?", (ident,)).fetchone()
            old = json.loads(row['body']) if row else {}
            if old.get('_archived'): raise m.Problem('专题已在回收站，请先恢复')
            missing = [i for i in ids if i not in known and i not in old.get('item_ids', [])]
            if missing: raise m.Problem('影片不存在：' + '、'.join(missing[:5]))
            start, end = self.topic_time(body.get('start'), zone, '上线时间'), self.topic_time(body.get('end'), zone, '下线时间')
            if start and end and end <= start: raise m.Problem('下线时间必须晚于上线时间')
            item = {**old, 'id': ident, 'name': m.text(body.get('name'), 20, True), 'description': m.text(body.get('description', ''), 200),
                    'item_ids': ids, 'start': start, 'end': end, 'sort': m.number(body.get('sort', 100), 0, 100000), 'enabled': body.get('enabled', True) is True}
            if any(t['id'] != ident and t['name'] == item['name'] and not t.get('_archived') for t in self.objects(db, 'topic')): raise m.Problem('专题名称已存在', 409)
            now = m.now_ms()
            if not old: item['_created_at'] = now
            item['_updated_at'] = now
            self.put_object(db, 'topic', item); self.changed(db, user['name'], '保存专题', item['name'])
        return item

    def topic_live(self, topic, zone, now=None):
        now = now or datetime.now(zone)
        stamp = lambda v: datetime.strptime(v, '%Y-%m-%dT%H:%M').replace(tzinfo=zone)
        if topic.get('_archived') or not topic.get('enabled'): return False
        if topic.get('start') and now < stamp(topic['start']): return False
        if topic.get('end') and now >= stamp(topic['end']): return False
        return True

    def home_payload(self, db, visible_ids):
        """Home content for TVs; only what the TV needs, no internal fields. Topic items are limited to playable catalog items."""
        settings = self.home_settings(db)
        zone = ZoneInfo(self.kv(db, 'settings')['timezone'])
        guide = sorted([g for g in self.objects(db, 'guide') if g.get('enabled') and not g.get('_archived')], key=lambda g: (g.get('sort', 100), g['name']))
        topics = sorted([t for t in self.objects(db, 'topic') if self.topic_live(t, zone)], key=lambda t: (t.get('sort', 100), t['name']))
        result = {k: settings[k] for k in ('background', 'contact_qr', 'contact_label', 'wifi_qr', 'wifi_ssid', 'wifi_label', 'show_qr')}
        result['wifi_password'] = settings['wifi_password'] if settings['wifi_show_password'] else ''
        result['guide'] = [{'id': g['id'], 'title': g['name'], 'body': g['body'], 'image': g.get('image', '')} for g in guide]
        result['topics'] = [{'id': t['id'], 'name': t['name'], 'description': t.get('description', ''), 'item_ids': [i for i in t['item_ids'] if i in visible_ids]} for t in topics]
        result['topics'] = [t for t in result['topics'] if t['item_ids']]
        result['menu'] = self.home_menu_payload(db, result['topics'])
        result['apps_reporting'] = True
        result['guest_request'] = self.guest_public_config(db)
        return result

    def home_admin_state(self):
        with self.db() as db:
            settings = self.home_settings(db); zone = ZoneInfo(self.kv(db, 'settings')['timezone'])
            topics = self.objects(db, 'topic')
            for t in topics: t['live'] = self.topic_live(t, zone)
            visible_ids = {r['id'] for r in db.execute('SELECT id,body,source FROM media WHERE published=1 AND available=1')
                           if not json.loads(r['body']).get('_archived') and self.source_enabled(db, r['source'])}
            live_topics = [t for t in topics if t['live'] and any(i in visible_ids for i in t['item_ids'])]
            effective = self.home_menu_payload(db, live_topics)
            cards = self.home_cards(db)
            for card in cards:
                try:
                    self.menu_target(db, card)
                    card['target_available'] = not (card['type'] == 'topic' and card['target'] not in {t['id'] for t in live_topics})
                    if card['type'] == 'category': card['target_available'] = self.record(db, 'category', card['target']).get('enabled', True)
                except core().Problem: card['target_available'] = False
            return {'home': settings, 'default_backgrounds': available_backgrounds(self.media), 'guide': self.objects(db, 'guide'), 'topics': topics, 'timezone': str(zone),
                    'menu': cards, 'effective_menu': effective, 'app_inventory': self.home_app_inventory(db), 'revision': self.kv(db, 'revision')}
