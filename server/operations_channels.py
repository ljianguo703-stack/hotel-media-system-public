"""TV02: local virtual channels (I01), generated programme guide (I02) and programme + ad-break scheduling (I03).

Every TV derives what is on from the same server timeline: a channel's sequence (programmes, plus fixed-length ad
breaks between programmes) repeats from an epoch, or loops inside daily time slots. Guests only switch channels; there
is no pause or seek. The server never decides which ad plays in a break: each TV fills the break through its normal ad
rules (placement "channel", A04 limits) or shows a non-commercial ident, so exposure is counted only from real events."""
from datetime import datetime, timedelta
import json
import random
import re
from urllib.parse import unquote
from xml.sax.saxutils import escape, quoteattr
from zoneinfo import ZoneInfo


def core():
    import management
    return management


MODES = ('loop', 'shuffle', 'slots')
WINDOW_BEFORE, WINDOW_AFTER = 0, 12 * 3600000  # the first entry still starts at or before now
MAX_ENTRIES = 600


def hhmm(value, label):
    m = core()
    value = str(value or '').strip()
    if not re.fullmatch(r'\d{2}:\d{2}', value) or int(value[:2]) > 23 or int(value[3:]) > 59: raise m.Problem(label + '格式应为 HH:MM')
    return value


def minutes(value):
    return int(value[:2]) * 60 + int(value[3:])


class ChannelOperations:
    def channel_inserts(self, db, raw, entries):
        m = core()
        if not isinstance(raw, list) or len(raw) > 100: raise m.Problem('每个频道最多100条节目插入规则')
        assets = {a['id']: a for a in self.objects(db, 'asset')}
        campaigns = {r['id']: json.loads(r['body']) for r in db.execute('SELECT id,body FROM campaigns')}
        result, seen = [], set()
        for row in raw:
            if not isinstance(row, dict): raise m.Problem('插入规则格式无效')
            ident = m.text(row.get('id'), 100, True)
            if not re.fullmatch(r'[A-Za-z0-9_-]+', ident) or ident in seen: raise m.Problem('插入规则标识无效或重复')
            seen.add(ident)
            media_id = m.text(row.get('media_id'), 200, True)
            targets = [e for e in entries if e['media_id'] == media_id]
            if not targets: raise m.Problem('插入规则请选择本频道节目')
            position, kind = row.get('position'), row.get('kind')
            if position not in ('before', 'at', 'after') or kind not in ('ad', 'ident'): raise m.Problem('插入位置或类型无效')
            at = m.number(row.get('at_seconds', 1), 1, 86400) if position == 'at' else 0
            if position == 'at' and any(at * 1000 >= int(e['duration'] * 1000) for e in targets): raise m.Problem('插入点须在正片结束之前（剧集规则适用于每一集）')
            rule = {'id': ident, 'media_id': media_id, 'position': position, 'at_seconds': at, 'kind': kind}
            if kind == 'ad':
                rule['seconds'] = m.number(row.get('seconds', 30), 1, 300)
                cid = m.text(row.get('campaign_id', ''), 100)
                if cid and (cid not in campaigns or campaigns[cid]['tier'] != 1 or 'channel' not in campaigns[cid].get('placements', []) or campaigns[cid].get('status') == '已归档'):
                    raise m.Problem('指定广告须是一档、包含频道位置且未归档的任务')
                rule['campaign_id'] = cid
            else:
                asset = assets.get(row.get('asset_id'))
                if not asset or asset.get('_archived') or not asset.get('enabled') or asset.get('format') != '视频': raise m.Problem('台标片请选择启用的视频素材')
                _, info, error = self.probe_file(asset['path'])
                duration = int(float((info or {}).get('format', {}).get('duration') or 0) * 1000)
                if error or not 0 < duration <= 300000: raise m.Problem('台标片须可读取且时长不超过5分钟')
                rule.update(asset_id=asset['id'], duration_ms=duration)
            result.append(rule)
        return result

    # ---------- programmes and durations ----------
    def channel_entries(self, db, media_ids, probe=True):
        """Expand media ids (movies and every episode of series) into programmes with durations from the compat probe."""
        m = core()
        rows = {r['id']: json.loads(r['body']) for r in db.execute('SELECT id,body FROM media')}
        known = {o['route']: o for o in self.objects(db, 'compat')}
        entries, missing = [], []
        for mid in media_ids:
            item = rows.get(mid)
            if not item or item.get('_archived'): missing.append(mid); continue
            files = [(e['path'], '%s · %s' % (item.get('title', ''), e.get('title', ''))) for e in item.get('episodes', []) if not e.get('_pending_publish')] if item.get('kind') == 'series' else [(item.get('path', ''), item.get('title', ''))]
            for route, title in files:
                if not route: continue
                duration = float((known.get(route) or {}).get('info', {}).get('duration') or 0)
                entries.append({'route': route, 'media_id': mid, 'title': title, 'description': item.get('description', ''), 'duration': duration})
        return entries, missing

    def ensure_durations(self, media_ids, actor):
        """Probe files whose duration is unknown so the timeline can be computed."""
        with self.db() as db:
            entries, _ = self.channel_entries(db, media_ids)
            todo = sorted({e['media_id'] for e in entries if e['duration'] <= 0})
            revision = self.kv(db, 'revision')
        if todo:
            if not self.compat_tools()['ffprobe']: raise core().Problem('计算节目时长需要服务器安装 ffprobe（ffmpeg 套件）', 409)
            self.compat_probe(todo, actor, revision)

    # ---------- saving ----------
    def save_channel(self, body, user, revision):
        m = core()
        ident = m.text(body.get('id'), 100, True)
        if not re.fullmatch(r'[A-Za-z0-9_-]+', ident): raise m.Problem('标识格式无效')
        mode = body.get('mode', 'loop')
        if mode not in MODES: raise m.Problem('编排方式无效')
        def id_list(value, label):
            if not isinstance(value, list) or len(value) > 500 or any(not isinstance(i, str) for i in value): raise m.Problem(label + '无效（最多 500 部）')
            return value
        items = id_list(body.get('items') or [], '节目列表')
        slots = []
        if mode == 'slots':
            raw = body.get('slots') or []
            if not isinstance(raw, list) or not 1 <= len(raw) <= 24: raise m.Problem('请设置 1–24 个时段')
            for i, s in enumerate(raw):
                start, end = hhmm(s.get('start'), '时段开始'), hhmm(s.get('end'), '时段结束')
                if start == end: raise m.Problem('时段开始和结束不能相同')
                slots.append({'start': start, 'end': end, 'items': id_list(s.get('items') or [], '时段节目')})
            covered = []
            for s in slots:
                a, b = minutes(s['start']), minutes(s['end'])
                spans = [(a, b)] if a < b else [(a, 1440), (0, b)]
                for x, y in spans:
                    if any(x < q and p < y for p, q in covered): raise m.Problem('时段之间不能重叠')
                    covered.append((x, y))
            if not any(s['items'] for s in slots): raise m.Problem('至少一个时段需要节目')
        elif not items: raise m.Problem('请至少选择一部节目')
        breaks = body.get('breaks') or {}
        breaks = {'enabled': breaks.get('enabled') is True, 'seconds': m.number(breaks.get('seconds', 30), 10, 300), 'every': m.number(breaks.get('every', 1), 1, 20)}
        all_ids = items + [i for s in slots for i in s['items']]
        with self.db() as db:
            if revision != self.kv(db, 'revision'): raise m.Problem('数据已被其他操作更新，请刷新后重试', 409)
        self.ensure_durations(sorted(set(all_ids)), user['name'])
        with self.db() as db:
            if revision != self.kv(db, 'revision'): raise m.Problem('数据已被其他操作更新，请刷新后重试', 409)
            pre_entries, _ = self.channel_entries(db, all_ids)
            # ffprobe may take time; do not hold the configuration write transaction while it runs.
            inserts = self.channel_inserts(db, body.get('inserts', []), pre_entries)
        with self.db() as db:
            self.check_revision(db, revision)
            row = db.execute("SELECT body FROM managed_objects WHERE kind='channel' AND id=?", (ident,)).fetchone()
            old = json.loads(row['body']) if row else {}
            if old.get('_archived'): raise m.Problem('频道已在回收站，请先恢复')
            number = m.number(body.get('number'), 1, 999)
            if any(c['id'] != ident and c.get('number') == number and not c.get('_archived') for c in self.objects(db, 'channel')): raise m.Problem('频道号 %d 已被使用' % number, 409)
            entries, missing = self.channel_entries(db, all_ids)
            if missing: raise m.Problem('影片不存在或已归档：' + '、'.join(missing[:5]))
            unknown = [e['title'] for e in entries if e['duration'] <= 0]
            if unknown: raise m.Problem('无法读取节目时长：' + '、'.join(unknown[:5]))
            item = {**old, 'id': ident, 'number': number, 'name': m.text(body.get('name'), 20, True), 'logo': self.optional_image(body.get('logo')),
                    'mode': mode, 'items': items, 'slots': slots, 'breaks': breaks, 'inserts': inserts, 'enabled': body.get('enabled', True) is True}
            changed_program = (old.get('items'), old.get('slots'), old.get('mode')) != (items, slots, mode)
            if not old or changed_program or body.get('reshuffle') is True:
                zone = ZoneInfo(self.kv(db, 'settings')['timezone'])
                midnight = datetime.now(zone).replace(hour=0, minute=0, second=0, microsecond=0)
                item['epoch'] = int(midnight.timestamp() * 1000)
                keys = [e['route'] for e in entries]
                item['order'] = random.Random(m.now_ms()).sample(keys, len(keys)) if mode == 'shuffle' else []
            now = m.now_ms()
            if not old: item['_created_at'] = now
            item['_updated_at'] = now
            self.put_object(db, 'channel', item)
            self.changed(db, user['name'], '保存频道', '%d %s' % (number, item['name']))
        return item

    # ---------- timeline ----------
    def channel_sequence(self, db, channel, ids, available):
        entries, _ = self.channel_entries(db, ids)
        entries = [e for e in entries if e['duration'] > 0 and e['media_id'] in available]
        if channel['mode'] == 'shuffle' and channel.get('order'):
            pos = {k: i for i, k in enumerate(channel['order'])}
            entries.sort(key=lambda e: pos.get(e['route'], len(pos)))
        seq, b = [], channel.get('breaks') or {}
        assets = {a['id']: a for a in self.objects(db, 'asset')}
        def insert(rule):
            if rule['kind'] == 'ad':
                return {'kind': 'break', 'title': '广告时段', 'length': rule['seconds']*1000, 'campaign_id': rule.get('campaign_id', '')}
            asset = assets.get(rule['asset_id']) or {}
            try:
                if not asset.get('enabled') or asset.get('_archived'): raise OSError('disabled')
                self.media_path(unquote(asset['path']))
            except (OSError, KeyError, core().Problem):
                return {'kind': 'filler', 'title': '台标片暂不可用', 'length': rule['duration_ms']}
            return {'kind': 'ident', 'title': asset['name'], 'route': asset['path'], 'length': rule['duration_ms']}
        for i, e in enumerate(entries):
            length = int(e['duration'] * 1000)
            rules = [r for r in channel.get('inserts', []) if r['media_id'] == e['media_id']]
            def position(r): return 0 if r['position'] == 'before' else length if r['position'] == 'after' else r['at_seconds']*1000
            cursor = 0
            for r in sorted(rules, key=position):
                point = position(r)
                if point > length: continue
                if point > cursor:
                    seq.append({'kind': 'program', 'title': e['title'], 'description': e['description'], 'route': e['route'], 'media_id': e['media_id'], 'length': point-cursor, 'offset_ms': cursor})
                seq.append(insert(r)); cursor = point
            if cursor < length:
                seq.append({'kind': 'program', 'title': e['title'], 'description': e['description'], 'route': e['route'], 'media_id': e['media_id'], 'length': length-cursor, 'offset_ms': cursor})
            if b.get('enabled') and (i + 1) % b.get('every', 1) == 0 and not any(r['kind']=='ad' and r['position']=='after' for r in rules):
                seq.append({'kind': 'break', 'title': '广告时段', 'length': b.get('seconds', 30) * 1000})
        return seq

    @staticmethod
    def run_sequence(seq, anchor, t0, t1, stop=None):
        """Lay a repeating sequence on the clock from anchor; return entries overlapping [t0, t1), cut at stop."""
        cycle = sum(s['length'] for s in seq)
        if cycle <= 0: return []
        # Loop channels extend backwards from the epoch; slot sequences always begin at the slot start.
        start = anchor if stop and t0 <= anchor else anchor + ((t0 - anchor) // cycle) * cycle
        out, i = [], 0
        limit = min(t1, stop) if stop else t1
        while start < limit and len(out) < MAX_ENTRIES:
            s = seq[i % len(seq)]; end = start + s['length']
            if end > t0: out.append({**{k: v for k, v in s.items() if k != 'length'}, 'start_ms': start, 'end_ms': min(end, stop) if stop else end})
            start, i = end, i + 1
        return out

    def channel_timeline(self, db, channel, t0, t1, zone, available):
        if channel['mode'] != 'slots':
            return self.run_sequence(self.channel_sequence(db, channel, channel['items'], available), channel['epoch'], t0, t1)
        out = []
        day = datetime.fromtimestamp(t0 / 1000, zone).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        while int(day.timestamp() * 1000) < t1:
            for s in channel['slots']:
                a = day + timedelta(minutes=minutes(s['start'])); b = day + timedelta(minutes=minutes(s['end']))
                if b <= a: b += timedelta(days=1)
                a_ms, b_ms = int(a.timestamp() * 1000), int(b.timestamp() * 1000)
                if b_ms <= t0 or a_ms >= t1: continue
                out += self.run_sequence(self.channel_sequence(db, channel, s['items'], available), a_ms, t0, t1, stop=b_ms)
            day += timedelta(days=1)
        out.sort(key=lambda e: e['start_ms'])
        filled, cursor = [], t0
        for e in out:  # gaps between slots become an explicit "off air" filler so TVs never guess
            if e['start_ms'] > cursor: filled.append({'kind': 'filler', 'title': '暂停播出', 'start_ms': cursor, 'end_ms': e['start_ms']})
            filled.append(e); cursor = max(cursor, e['end_ms'])
        if cursor < t1: filled.append({'kind': 'filler', 'title': '暂停播出', 'start_ms': cursor, 'end_ms': t1})
        return filled[:MAX_ENTRIES]

    def channel_payload(self, now=None, before=WINDOW_BEFORE, after=WINDOW_AFTER, include_disabled=False):
        m = core(); now = now or m.now_ms()
        with self.db() as db:
            zone = ZoneInfo(self.kv(db, 'settings')['timezone'])
            available = {r['id'] for r in db.execute('SELECT id FROM media WHERE available=1')}
            routes = self.compat_routes(db)
            media = {r['id']: json.loads(r['body']) for r in db.execute('SELECT id,body FROM media')}
            channels = sorted([c for c in self.objects(db, 'channel') if not c.get('_archived') and (include_disabled or c.get('enabled'))], key=lambda c: c['number'])
            result = []
            for c in channels:
                entries = self.channel_timeline(db, c, now - before, now + after, zone, available)
                for e in entries:
                    if e.get('route'):
                        if e['kind'] == 'program': e['subtitles'] = self.subtitle_choices(media.get(e['media_id'], {}), e['route'])
                        e['path'] = routes.get(e['route'], e['route']) if e['kind'] == 'program' else e['route']
                        if e['path'] != e['route']: e['source_path'] = e['route']
                result.append({'id': c['id'], 'number': c['number'], 'name': c['name'], 'logo': c.get('logo', ''), 'enabled': c.get('enabled', True), 'entries': entries})
        return result

    def tv_channels(self):
        # A slot channel that is off air right now still appears, showing "off air" until its next slot.
        channels = [{k: v for k, v in c.items() if k != 'enabled'} for c in self.channel_payload() if c['entries']]
        for c in channels:
            for e in c['entries']: e.pop('route', None)
        return {'channels': channels}

    def channel_admin_state(self):
        m = core(); now = m.now_ms()
        preview = {c['id']: c['entries'] for c in self.channel_payload(now, 0, 6 * 3600000, include_disabled=True)}
        with self.db() as db:
            available = {r['id'] for r in db.execute('SELECT id FROM media WHERE available=1')}
            rows = []
            for c in sorted([c for c in self.objects(db, 'channel') if not c.get('_archived')], key=lambda c: c['number']):
                ids = c['items'] + [i for s in c.get('slots', []) for i in s['items']]
                entries, missing = self.channel_entries(db, ids)
                warnings = ['影片已删除或归档：' + '、'.join(missing[:3])] if missing else []
                gone = sorted({e['title'] for e in entries if e['media_id'] not in available})
                if gone: warnings.append('源文件缺失，暂时跳过：' + '、'.join(gone[:3]))
                seq = self.channel_sequence(db, c, c['items'] if c['mode'] != 'slots' else ids, available)
                warnings += sorted({e['title'] for e in seq if e['kind']=='filler'})
                rows.append({**c, 'programmes': len(entries), 'cycle_minutes': sum(e['length'] for e in seq) // 60000,
                             'preview': preview.get(c['id'], [])[:40], 'warnings': warnings})
        return {'channels': rows}

    def channel_xmltv(self, hours=24):
        """XMLTV export of the generated guide (next 24 hours) for use in other players."""
        m = core(); now = m.now_ms()
        fmt = lambda ms: datetime.utcfromtimestamp(ms / 1000).strftime('%Y%m%d%H%M%S +0000')
        parts = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="hotel-tv">']
        channels = self.channel_payload(now, 0, hours * 3600000)
        for c in channels: parts.append('<channel id=%s><display-name>%s</display-name></channel>' % (quoteattr('ch%d' % c['number']), escape(c['name'])))
        for c in channels:
            for e in c['entries']:
                if e['kind'] != 'program': continue
                parts.append('<programme start=%s stop=%s channel=%s><title lang="zh">%s</title>%s</programme>' % (
                    quoteattr(fmt(e['start_ms'])), quoteattr(fmt(e['end_ms'])), quoteattr('ch%d' % c['number']), escape(e['title']),
                    '<desc lang="zh">%s</desc>' % escape(e['description']) if e.get('description') else ''))
        parts.append('</tv>')
        return {'xml': '\n'.join(parts)}
