"""AD05 local ad rules and fixed-layout sponsor cards. No external rendering or industry screening."""
import hashlib
import json
import re
from urllib.parse import quote, urlsplit
import qr_image

DEFAULT_RULES = {'enabled': True, 'midroll_percent': 50, 'midroll_delay_seconds': 240,
                 'text_item_seconds': 15, 'rotate_group_start': False, 'sponsor_ids': [], 'pause_max_minutes': 0, 'pause_repeat': False}
IMAGE_SUFFIXES = {'.jpg', '.jpeg', '.png', '.webp'}

def core():
    import management
    return management

class PlaybackAdvertising:
    @staticmethod
    def playback_rules(db):
        row = db.execute("SELECT value FROM kv WHERE key='ad_playback'").fetchone()
        return {**DEFAULT_RULES, **(json.loads(row[0]) if row else {})}

    def save_playback_rules(self, body, user, revision):
        m = core()
        if user['role'] != 'admin': raise m.Problem('播放规则只允许管理员修改', 403)
        raw = body.get('rules')
        if not isinstance(raw, dict): raise m.Problem('请填写播放规则')
        item = {**DEFAULT_RULES, **raw}
        for key in ('enabled', 'rotate_group_start'):
            if type(item[key]) is not bool: raise m.Problem('规则开关无效')
        item = {k: item[k] for k in DEFAULT_RULES}
        if type(item['pause_repeat']) is not bool: raise m.Problem('暂停循环开关无效')
        for field, lo, hi in [('midroll_percent', 1, 99), ('midroll_delay_seconds', 180, 300), ('text_item_seconds', 5, 120), ('pause_max_minutes',0,120)]:
            item[field] = m.number(item[field], lo, hi)
        ids = item['sponsor_ids']
        if not isinstance(ids, list) or len(ids) > 100 or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids): raise m.Problem('轮播商户名单无效')
        with self.db() as db:
            self.check_revision(db, revision)
            available = {a['id'] for a in self.objects(db, 'advertiser') if not a.get('_archived') and a.get('enabled', True)}
            if not set(ids) <= available: raise m.Problem('轮播名单含停用或不存在的商户')
            self.put_kv(db, 'ad_playback', item)
            self.changed(db, user['name'], '保存广告播放规则', '中插阈值%d%%，正片延后%d秒' % (item['midroll_percent'], item['midroll_delay_seconds']))
        return self.playback_admin_state()

    def playback_admin_state(self):
        with self.db() as db:
            rules = self.playback_rules(db); merchants = {a['name']: a for a in self.objects(db, 'advertiser')}; rows = []
            for row in db.execute('SELECT body FROM campaigns'):
                c = json.loads(row['body']); merchant = merchants.get(c['merchant'], {})
                if c['tier'] != 1 or c.get('_archived'): continue
                rows.append({'id': c['id'], 'name': c['name'], 'merchant': c['merchant'], 'merchant_id': merchant.get('id', ''),
                             'industry': merchant.get('industry', ''), 'status': c['status'], 'duration': c['duration'],
                             'rotation_order': c.get('rotation_order', 100), 'rotation_enabled': c.get('rotation_enabled', True), 'placements': c['placements']})
            rows.sort(key=lambda c: (c['rotation_order'], c['id']))
            return {'rules': rules, 'rotation': rows, 'revision': self.kv(db, 'revision'),
                    'basis': '每个位置整组依次播放，每个商户每组一席；未选名单表示所有已发布商户。实际播放还须符合日期、客房范围和总控。'}

    def sponsor_card(self, raw, merchant, published=False):
        m = core()
        if not isinstance(raw, dict): raise m.Problem('图文广告资料无效')
        labels = {'title':'赞助商标题', 'body':'宣传文字', 'contact':'联系方式 / 地址', 'image':'主图片路径',
                  'logo':'商户Logo路径', 'qr':'二维码原图路径', 'qr_link':'二维码链接', 'qr_label':'二维码说明'}
        item = {key: m.text(raw.get(key, default), size, key == 'title', labels[key]) for key, default, size in
                [('title', merchant[:80], 80), ('body', '', 500), ('contact', '', 300), ('image', '', 2000),
                 ('logo', '', 2000), ('qr', '', 2000), ('qr_link', '', 1200), ('qr_label', '扫码了解详情', 40)]}
        item['accent'] = raw.get('accent', '#214B53')
        if not isinstance(item['accent'], str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', item['accent']): raise m.Problem('请选择有效的主题色')
        for key in ('image', 'logo', 'qr'):
            path = item[key]
            if path and not path.startswith('/media/'): raise m.Problem('图文素材必须来自本地素材库')
            if path and published: self.require_media_path(path, IMAGE_SUFFIXES)
        if item['qr_link']:
            url = urlsplit(item['qr_link'])
            if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password: raise m.Problem('二维码链接需为有效的 http/https 地址；也可直接上传二维码原图')
            if len(item['qr_link'].encode('utf-8')) > 213: raise m.Problem('链接过长，请上传现成二维码图片')
        return item

    def prepare_sponsor_card(self, card):
        if card.get('qr_link'):
            folder = self.media / '广告' / '_qr'; folder.mkdir(parents=True, exist_ok=True)
            name = hashlib.sha256(card['qr_link'].encode()).hexdigest()[:24] + '.png'; target = folder / name
            if not target.exists():
                import secrets
                temp = folder / ('.' + name + '.' + secrets.token_hex(8))
                try: temp.write_bytes(qr_image.qr_png(card['qr_link'])); temp.replace(target)
                finally: temp.unlink(missing_ok=True)
            card['qr'] = '/media/' + quote('广告/_qr/' + name)
        return card

    def playback_manifest(self, db, ads, protocol):
        if protocol < 2: return [a for a in ads if a['kind'] != 'card'], None
        rules = self.playback_rules(db); merchants = {a['name']: a for a in self.objects(db, 'advertiser')}
        for ad in ads:
            merchant = merchants.get(ad['merchant'], {}); ad['merchant_id'] = merchant.get('id', ad['merchant'])
        return ads, {'protocol': 2, **rules}
