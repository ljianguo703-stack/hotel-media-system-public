"""PD07: local home cards and optional TV application inventory; no media deletion."""
import json
import re

BUILTINS = {'movie': '电影', 'series': '电视剧', 'apps': '已装应用', 'more': '更多',
            'recent': '继续观看', 'channels': '频道', 'guide': '酒店指南'}
ICONS = {'movie', 'series', 'apps', 'more', 'music', 'collection', 'channels', 'guide'}
PACKAGE = re.compile(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+')


def defaults():
    return [{'id': key, 'name': BUILTINS[key], 'type': 'builtin', 'target': key,
             'icon': key, 'enabled': True, 'sort': i} for i, key in enumerate(('movie', 'series', 'apps', 'more'))]


def core():
    import management
    return management


class HomeMenuOperations:
    def initialize_home_menu(self, db):
        if db.execute("SELECT 1 FROM kv WHERE key='home_menu_initialized'").fetchone():
            return
        for card in defaults():
            self.put_object(db, 'home_card', card)
        self.put_kv(db, 'home_menu_initialized', True)

    def home_cards(self, db):
        return sorted(self.objects(db, 'home_card'), key=lambda c: (c.get('sort', 0), c['id']))

    def menu_target(self, db, card, strict=True):
        m = core()
        kind, target = card.get('type'), card.get('target')
        if not isinstance(target, str) or not target or len(target) > 200: raise m.Problem('菜单目标无效')
        if kind == 'builtin':
            if target not in BUILTINS: raise m.Problem('请选择有效的内置栏目')
        elif kind in ('category', 'topic'):
            if not re.fullmatch(r'[A-Za-z0-9_-]+', target): raise m.Problem('菜单目标标识无效')
            if strict:
                item = self.record(db, kind, target)
                if item.get('_archived'): raise m.Problem('目标已在回收站，请先恢复目标')
        elif kind == 'app':
            if not isinstance(target, str) or len(target) > 200 or not PACKAGE.fullmatch(target):
                raise m.Problem('请输入有效的应用包名，例如 com.example.player')
        else: raise m.Problem('菜单打开方式无效')

    def save_home_menu(self, body, user, revision):
        m = core(); cards = body.get('cards')
        if not isinstance(cards, list) or not 1 <= len(cards) <= 20:
            raise m.Problem('请保留 1–20 张菜单卡片，首页最多显示 5 张')
        with self.db() as db:
            self.check_revision(db, revision)
            old = {c['id']: c for c in self.home_cards(db)}; result = []; ids = set()
            for index, raw in enumerate(cards):
                if not isinstance(raw, dict): raise m.Problem('菜单资料无效')
                ident = m.text(raw.get('id'), 100, True)
                if not re.fullmatch(r'[A-Za-z0-9_-]+', ident) or ident in ids: raise m.Problem('菜单标识无效或重复')
                ids.add(ident)
                if old.get(ident, {}).get('_archived'): raise m.Problem('卡片已在回收站，请先恢复')
                enabled = raw.get('enabled', True)
                if type(enabled) is not bool: raise m.Problem('菜单显示状态无效')
                item = {**old.get(ident, {}), 'id': ident, 'name': m.text(raw.get('name'), 12, True),
                        'type': raw.get('type'), 'target': raw.get('target'), 'icon': raw.get('icon', 'collection'),
                        'enabled': enabled, 'sort': index, '_updated_at': m.now_ms()}
                if not isinstance(item['icon'], str) or item['icon'] not in ICONS: raise m.Problem('菜单图标无效')
                self.menu_target(db, item, strict=enabled)
                result.append(item)
            if not 1 <= sum(c['enabled'] for c in result) <= 5:
                raise m.Problem('首页需显示 1–5 张卡片；请先隐藏其他卡片')
            for card in result:
                card.setdefault('_created_at', m.now_ms()); self.put_object(db, 'home_card', card)
            for ident, card in old.items():
                if ident not in ids and not card.get('_archived'):
                    card.update(_archived=True, _archived_at=m.now_ms(), enabled=False)
                    self.put_object(db, 'home_card', card)
            self.changed(db, user['name'], '保存首页菜单', '显示 ' + str(sum(c['enabled'] for c in result)) + ' 张卡片')
        return self.home_admin_state()

    def reset_home_menu(self, user, revision):
        with self.db() as db:
            self.check_revision(db, revision)
            now = core().now_ms(); default_ids = {c['id'] for c in defaults()}
            for card in self.home_cards(db):
                if card['id'] not in default_ids and not card.get('_archived'):
                    card.update(_archived=True, _archived_at=now, enabled=False)
                    self.put_object(db, 'home_card', card)
            for card in defaults(): self.put_object(db, 'home_card', {**card, '_created_at': now, '_updated_at': now})
            self.changed(db, user['name'], '恢复默认首页菜单', '四张默认卡片；自定义入口移入回收站')
        return self.home_admin_state()

    def check_home_card_lifecycle(self, db, item, action):
        active = [c for c in self.home_cards(db) if c.get('enabled') and not c.get('_archived')]
        if action in ('archive', 'disable') and item['id'] in {c['id'] for c in active} and len(active) <= 1:
            raise core().Problem('至少保留一张显示的首页卡片')
        if action == 'enable':
            self.menu_target(db, item)
            if item['id'] not in {c['id'] for c in active} and len(active) >= 5:
                raise core().Problem('首页最多显示 5 张卡片，请先隐藏其他卡片')

    def home_menu_payload(self, db, topics):
        live_topics = {t['id'] for t in topics}; result = []
        for card in self.home_cards(db):
            if not card.get('enabled') or card.get('_archived'): continue
            try: self.menu_target(db, card)
            except core().Problem: continue
            entry = {k: card[k] for k in ('id', 'name', 'type', 'target', 'icon')}
            if card['type'] == 'topic' and card['target'] not in live_topics: continue
            if card['type'] == 'category':
                category = self.record(db, 'category', card['target'])
                if not category.get('enabled', True): continue
                entry['category'] = category['name']
            result.append(entry)
        # Deleting/withdrawing every linked target must never strand the guest on an empty dock.
        return result[:5] or [{k: c[k] for k in ('id', 'name', 'type', 'target', 'icon')} for c in defaults()]

    def save_tv_apps(self, device, body):
        apps = body.get('apps') if isinstance(body, dict) else None
        if not isinstance(apps, list) or len(apps) > 200: raise core().Problem('应用列表无效')
        clean = []; seen = set()
        for app in apps:
            if not isinstance(app, dict): raise core().Problem('应用资料无效')
            package = app.get('package')
            if not isinstance(package, str) or len(package) > 200 or not PACKAGE.fullmatch(package): raise core().Problem('应用包名无效')
            if package in seen: continue
            seen.add(package); clean.append({'package': package, 'label': core().text(app.get('label'), 100, True)})
        with self.db() as db:
            self.put_object(db, 'tv_apps', {'id': device['id'], 'apps': clean, 'updated_at': core().now_ms()})
        return {'accepted': len(clean)}

    def home_app_inventory(self, db):
        devices = {r['id']: json.loads(r['body']) for r in db.execute('SELECT id,body FROM devices')}
        return [{**item, 'room': devices[item['id']].get('room', item['id'])} for item in self.objects(db, 'tv_apps')
                if item['id'] in devices and not devices[item['id']].get('_archived')]
