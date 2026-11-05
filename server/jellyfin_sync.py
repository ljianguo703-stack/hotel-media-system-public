"""Loopback Jellyfin metadata adapter. TV clients never receive a Jellyfin credential."""
import hashlib
import json
from pathlib import Path
import re
import threading
import time
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler, Request


class NoJellyfinRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the upstream credential to a redirect destination.
        return None


class JellyfinMixin:
    sync_interval_seconds = 120

    def jellyfin_config(self):
        from management import Problem
        path = self.data / 'nas-integration.json'
        if not path.exists(): return None
        try:
            config = json.loads(path.read_text())
            url = urlsplit(config['url'])
            if url.scheme != 'http' or url.hostname != '127.0.0.1' or url.port is None or url.path not in ('', '/') or url.query or url.fragment or url.username:
                raise ValueError()
            if not config.get('token'): raise ValueError()
            return config
        except (ValueError, KeyError, OSError):
            raise Problem('Jellyfin 本机连接配置无效，请检查服务器私有配置', 503)

    def jellyfin_request(self, route, body=None, image=False, timeout=20, subtitle=False):
        from management import Problem
        config = self.jellyfin_config()
        if not config: raise Problem('Jellyfin 尚未配置', 503)
        headers = {'Authorization': 'MediaBrowser Client="Hotel TV", Device="Ubuntu", DeviceId="hotel-tv-sync", Version="1.0", Token="' + config['token'] + '"'}
        if body is not None: headers['Content-Type'] = 'application/json'
        req = Request(config['url'].rstrip('/') + route, headers=headers,
                      data=json.dumps(body).encode() if body is not None else None)
        try:
            with build_opener(ProxyHandler({}), NoJellyfinRedirect()).open(req, timeout=timeout) as response:
                limit = 5 * 1024 * 1024 if image or subtitle else 32 * 1024 * 1024
                raw = response.read(limit + 1)
                if len(raw) > limit: raise ValueError('too large')
                if image:
                    if response.headers.get_content_type() != 'image/jpeg': raise ValueError('not jpeg')
                    return raw
                if subtitle:
                    # Jellyfin performs character decoding and SRT/ASS -> WebVTT conversion.
                    if response.headers.get_content_type() != 'text/vtt' or not raw.decode('utf-8-sig').startswith('WEBVTT'):
                        raise ValueError('not webvtt')
                    return raw
                return json.loads(raw) if raw else {}
        except Exception:
            # Upstream error strings may include private URLs or credentials.
            raise Problem('Jellyfin 暂时无法访问或返回无效数据；现有片库继续保留', 503) from None

    def jellyfin_status(self):
        from management import Problem
        try: configured = bool(self.jellyfin_config())
        except Problem: configured = False
        with self.db() as db:
            row = db.execute("SELECT value FROM kv WHERE key='jellyfin_status'").fetchone()
        return {'configured': configured, **(json.loads(row[0]) if row else {}), **self.sync_control()}

    def update_jellyfin_status(self, db, **fields):
        """Merge into the stored status so a failed attempt never erases the last successful sync."""
        row = db.execute("SELECT value FROM kv WHERE key='jellyfin_status'").fetchone()
        status = json.loads(row[0]) if row else {}
        status.update(fields)
        self.put_kv(db, 'jellyfin_status', status)
        return status

    @staticmethod
    def skip(job, title, reason):
        job['skipped'] += 1
        reasons = job.setdefault('skipped_reasons', [])
        if len(reasons) < 20: reasons.append({'title': str(title)[:80], 'reason': str(reason)[:120]})

    def jellyfin_items(self, types):
        from management import Problem
        items, start = [], 0
        while True:
            payload = self.jellyfin_request('/Items?' + urlencode({'Recursive': 'true', 'IncludeItemTypes': types,
                'Fields': 'Path,Overview,Genres,MediaSources,MediaStreams', 'StartIndex': start, 'Limit': 200}))
            page = payload.get('Items')
            if not isinstance(page, list) or not isinstance(payload.get('TotalRecordCount'), int):
                raise Problem('Jellyfin 分页数据无效，未覆盖原索引', 503)
            items.extend(page); start += len(page)
            if start >= payload['TotalRecordCount']: return items
            if not page or start > 100000: raise Problem('Jellyfin 分页未完整返回，未覆盖原索引', 503)

    def jellyfin_path(self, absolute):
        from management import Problem
        path = Path(absolute).resolve()
        with self.db() as db: sources = [json.loads(r[0]) for r in db.execute('SELECT body FROM sources')]
        for source in sources:
            root = Path(source['path']).resolve()
            if root in path.parents:
                if any((parent / '.hotel-download-task').exists() for parent in path.parents if parent == root or root in parent.parents):
                    raise Problem('该目录是下载文件夹，选择整理入库后再同步')
                if not source.get('enabled',True) or source.get('_archived'):raise Problem('影片所在片库来源已停用，未同步')
                prefix = '/media/' if source['id'] == 'main' else '/media/@sources/' + source['id'] + '/'
                route = prefix + quote(path.relative_to(root).as_posix(), safe='/')
                self.require_media_path(route)
                return route, source['id']
        raise Problem('Jellyfin 媒体不在酒店已接入的可读目录中')

    @staticmethod
    def jellyfin_subtitles(raw):
        """External text tracks of exactly the file served by this hotel entry, never another cut."""
        ident, video = raw.get('Id', ''), raw.get('Path', '')
        if not re.fullmatch(r'[a-fA-F0-9]{32}', ident) or not video: return []
        source = next((s for s in raw.get('MediaSources', []) if s.get('Path') == video), None)
        if not source or not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', source.get('Id', '')): return []
        result, seen = [], set()
        for stream in source.get('MediaStreams', []):
            index = stream.get('Index')
            if stream.get('Type') != 'Subtitle' or stream.get('IsExternal') is not True or type(index) is not int or not 0 <= index <= 4096: continue
            if str(stream.get('Codec', '')).lower() not in ('srt', 'subrip', 'ass', 'ssa', 'webvtt', 'vtt', 'ttml', 'mov_text'): continue
            if index in seen: continue
            seen.add(index)
            identity = [ident, source['Id'], video, index, stream.get('Path'), stream.get('Codec'), stream.get('Language')]
            key = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:32]
            result.append({'key': key, 'item': ident, 'source': source['Id'], 'index': index,
                           'language': str(stream.get('Language') or '')[:40],
                           'label': str(stream.get('DisplayTitle') or stream.get('Title') or '外挂字幕')[:160],
                           'default': stream.get('IsDefault') is True, 'forced': stream.get('IsForced') is True})
        return result[:64]

    @staticmethod
    def subtitle_choices(item, route):
        return [{'id': s['key'], 'path': '/api/v1/subtitles/' + s['key'] + '.vtt',
                 'language': s['language'], 'label': s['label'], 'default': s['default'], 'forced': s['forced']}
                for s in item.get('_jellyfin_subtitles', {}).get(route, [])]

    def subtitle_document(self, key):
        """Only an indexed, available programme can select an upstream subtitle. No client URLs."""
        from management import Problem
        if not re.fullmatch(r'[a-f0-9]{32}', key): raise Problem('字幕不存在', 404)
        selected = None
        with self.db() as db:
            channel_ids = {mid for c in self.objects(db, 'channel') if c.get('enabled') and not c.get('_archived') for mid in c.get('items', [])}
            for row in db.execute('SELECT id,body,source,published FROM media WHERE available=1'):
                item = json.loads(row['body'])
                if item.get('_archived') or not self.source_enabled(db, row['source']) or not (row['published'] or row['id'] in channel_ids): continue
                routes = [item['path']] if item.get('path') else [e['path'] for e in item.get('episodes', []) if not e.get('_pending_publish')]
                for route in routes:
                    if not self.automation_readiness(db,{**item,'path':route})['ready']: continue
                    for sub in item.get('_jellyfin_subtitles', {}).get(route, []):
                        if sub['key'] == key: selected = (route, sub)
        if not selected: raise Problem('字幕不存在或节目已下架', 404)
        route, sub = selected
        self.require_media_path(route)
        upstream = '/Videos/%s/%s/Subtitles/%d/Stream.vtt' % (sub['item'], sub['source'], sub['index'])
        return self.jellyfin_request(upstream, subtitle=True)

    def start_jellyfin_sync(self, actor, refresh=False):
        from management import Problem, encoded, now_ms
        if not self.jellyfin_config(): raise Problem('Jellyfin 尚未配置', 503)
        if not self.scan_lock.acquire(blocking=False): raise Problem('片库扫描或同步正在进行', 409)
        job = {'id': 'jellyfin-' + str(now_ms()), 'source_id': 'jellyfin', 'status': 'queued', 'started': now_ms(),
               'added': 0, 'updated': 0, 'missing': 0, 'still_missing': 0, 'restored': 0, 'skipped': 0, 'skipped_reasons': []}
        try:
            if refresh: self.jellyfin_request('/Library/Refresh', {})
            with self.db() as db: db.execute('INSERT INTO jobs VALUES(?,?)', (job['id'], encoded(job)))
            threading.Thread(target=self._jellyfin_sync, args=(job, actor), daemon=True).start()
            return job
        except Exception:
            self.scan_lock.release(); raise

    def _jellyfin_sync(self, job, actor):
        from management import Problem, encoded, now_ms
        try:
            job['status'] = 'running'; self.job(job)
            # Fetch all pages before touching the catalog. A partial outage cannot erase it.
            movies = self.jellyfin_items('Movie')
            shows = self.jellyfin_items('Series')
            episodes = self.jellyfin_items('Episode')
            grouped = {}
            for ep in episodes: grouped.setdefault(ep.get('SeriesId'), []).append(ep)
            prepared = []
            for raw in movies + shows:
                ident = raw.get('Id', '')
                if not re.fullmatch(r'[a-fA-F0-9]{32}', ident):
                    self.skip(job, raw.get('Name', ''), 'Jellyfin 标识无效'); continue
                item = {'id': 'jf-' + ident, 'title': str(raw.get('Name', '未命名'))[:200],
                        'kind': 'series' if raw.get('Type') == 'Series' else 'movie',
                        'category': str(next(iter(raw.get('Genres') or []), '电视剧' if raw.get('Type') == 'Series' else '电影'))[:80],
                        'year': str(raw.get('ProductionYear') or '')[:10], 'description': str(raw.get('Overview') or '')[:5000],
                        'poster': '', 'accent': '#304a57', 'jellyfin_id': ident, '_provider_ids':raw.get('ProviderIds') or {}}
                try:
                    if item['kind'] == 'movie':
                        item['path'], source_id = self.jellyfin_path(raw.get('Path', ''))
                        item['_jellyfin_subtitles'] = {item['path']: self.jellyfin_subtitles(raw)}
                    else:
                        converted = []; item['_jellyfin_subtitles'] = {}
                        for ep in sorted(grouped.get(ident, []), key=lambda e: (e.get('ParentIndexNumber') or 0, e.get('IndexNumber') or 0, e.get('Id', ''))):
                            try: route, source_id = self.jellyfin_path(ep.get('Path', ''))
                            except Problem as exc: self.skip(job, str(item['title']) + ' / ' + str(ep.get('Name') or ''), exc); continue
                            converted.append({'id': 'jf-ep-' + str(ep['Id']), 'title': str(ep.get('Name') or '未命名')[:200], 'path': route,
                                              'season': max(1, int(ep.get('ParentIndexNumber') or 1)), 'number': max(1, int(ep.get('IndexNumber') or 1))})
                            item['_jellyfin_subtitles'][route] = self.jellyfin_subtitles(ep)
                        if not converted: raise Problem('没有可读取分集')
                        item['episodes'] = converted
                except Problem as exc:
                    self.skip(job, item['title'], exc); continue
                if raw.get('ImageTags', {}).get('Primary'):
                    tag = hashlib.sha256(str(raw['ImageTags']['Primary']).encode()).hexdigest()[:12]
                    dest = self.media / '_posters' / ('jellyfin-' + ident + '-' + tag + '.jpg')
                    try:
                        if not dest.exists():
                            image = self.jellyfin_request('/Items/' + ident + '/Images/Primary?format=Jpg&maxWidth=600', image=True)
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            temporary = dest.with_suffix('.part'); temporary.write_bytes(image); temporary.chmod(0o644); temporary.replace(dest)
                        item['poster'] = '/media/_posters/' + dest.name
                    except Problem: pass  # Poster outage does not make a playable film unavailable.
                prepared.append((item, source_id))
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                existing = [dict(r) for r in db.execute('SELECT * FROM media')]
                by_path, by_jellyfin = {}, {}
                def paths(item): return [item['path']] if item.get('path') else [e['path'] for e in item.get('episodes', [])]
                for row in existing:
                    body = json.loads(row['body'])
                    for p in paths(body): by_path[p] = row
                    if body.get('jellyfin_id'): by_jellyfin[body['jellyfin_id']] = row
                changed, written = False, set()
                seen_paths,seen_ids=set(),set()
                for item, source_id in prepared:
                    if not self.source_enabled(db,source_id):
                        self.skip(job,item['title'],'片库来源已停用，未同步');continue
                    quarantined=False
                    for task in self._media_add_tasks(db):
                        destination=(task.get('arr') or {}).get('path')
                        if not destination or task.get('imported_at') or task['stage'] in ('imported','listed'): continue
                        prefix=self._media_route_prefix(destination)
                        if prefix and any(str(path).startswith(prefix) for path in ([item['path']] if item.get('path') else [e['path'] for e in item.get('episodes',[])])):
                            quarantined=True;break
                    if quarantined:
                        self.skip(job,item['title'],'原生导入尚未通过核验，残留文件不进入待上架');continue
                    if self.media_blocked(db,item):
                        self.skip(job,item['title'],'影片已从回收站清空，需手动允许重新入库'); continue
                    item['category'] = self.mapped_category(db, item['category'])
                    old = by_jellyfin.get(item['jellyfin_id']) or next((by_path[p] for p in paths(item) if p in by_path), None)
                    if not old:
                        matches=[]
                        for candidate in existing:
                            prior=json.loads(candidate['body'])
                            same_provider=bool(item.get('_provider_ids',{}).get('Tmdb') and prior.get('_provider_ids',{}).get('Tmdb')==item['_provider_ids']['Tmdb'])
                            same_folder=bool(item.get('path') and prior.get('path') and Path(item['path']).parent==Path(prior['path']).parent and item['title']==prior.get('title') and item.get('year')==prior.get('year'))
                            if candidate['source']==source_id and prior.get('kind','movie')==item['kind'] and (same_provider or same_folder): matches.append(candidate)
                        if len(matches)==1 and matches[0]['id'] not in written: old=matches[0]
                    previous = json.loads(old['body']) if old else {}
                    # Kind rule: a Jellyfin movie never replaces a hotel series on the same files, nor the reverse.
                    if old and previous.get('kind', 'movie') != item['kind']:
                        self.skip(job, item['title'], '类型与酒店现有条目不一致，未覆盖'); continue
                    current_paths=set(paths(item))
                    if current_paths & seen_paths or item['jellyfin_id'] in seen_ids:
                        self.skip(job,item['title'],'源文件或上游标识重复，已保留本轮第一条');continue
                    seen_paths.update(current_paths);seen_ids.add(item['jellyfin_id'])
                    published = old['published'] if old else 0
                    upstream = {k: item[k] for k in ('title', 'category', 'year', 'description', 'poster')}
                    upstream_episodes = {e['path']: {k: e[k] for k in ('title', 'season', 'number')} for e in item.get('episodes', [])}
                    if old:
                        item['id'] = old['id']
                        meta = previous.get('_jellyfin_meta', {})
                        # Override rule, top level: a field the hotel set that differs from what Jellyfin last supplied stays.
                        for k in upstream:
                            if previous.get(k) and (k not in meta or previous[k] != meta[k]):
                                item[k] = previous[k]
                        # Override rule, episodes (matched by file): the id always stays; title/season/number the hotel
                        # changed since the recorded Jellyfin value stay. The first sync that records a value follows Jellyfin.
                        recorded = meta.get('episodes', {})
                        eps = {**previous.get('_episode_memory',{}),**{e['path']:e for e in previous.get('episodes',[])}}
                        for ep in item.get('episodes', []):
                            before = eps.get(ep['path'])
                            if not before:
                                if published: ep['_pending_publish']=True
                                continue
                            if before.get('_pending_publish'): ep['_pending_publish']=True
                            ep['id'] = before['id']
                            record = recorded.get(ep['path'])
                            if record is None: continue
                            for k in ('title', 'season', 'number'):
                                if before.get(k) is not None and before.get(k) != record.get(k): ep[k] = before[k]
                        if item.get('episodes'): item['episodes'].sort(key=lambda e: (e['season'], e['number'], e['id']))
                    for key in ('_archived', '_archived_at', '_pinned', '_created_at', '_updated_at', '_episode_memory', '_renaming'):
                        if key in previous: item[key] = previous[key]
                    rename=item.get('_renaming') or {}
                    if rename.get('status')=='awaiting_sync' and (str(item.get('_provider_ids',{}).get('Tmdb'))==str(rename.get('tmdb_id')) or upstream.get('title')==rename.get('title')):item.pop('_renaming',None)
                    if item.get('_archived'): published = 0
                    for ep in item.get('episodes',[]):upstream_episodes[ep['path']]['id']=ep['id']
                    item['_jellyfin_meta'] = {**upstream, 'episodes': {**previous.get('_jellyfin_meta',{}).get('episodes',{}),**upstream_episodes}}
                    self.remember_episodes(item,previous)
                    job['updated' if old else 'added'] += 1
                    item_changed = self.stamp_media(item, previous if old else None,
                                                    bool(old and (not old['available'] or old['published'] != published)))
                    changed = changed or item_changed
                    db.execute('INSERT OR REPLACE INTO media VALUES(?,?,?,?,1)', (item['id'], source_id, encoded(item), published))
                    written.add(item['id'])
                # Availability rule for linked entries Jellyfin did not supply this round: it only reflects whether
                # the files are readable, and only a change of state counts as a change. Missing upstream entries
                # alone never withdraw hotel content.
                for row in existing:
                    if row['id'] in written or not self.source_enabled(db,row['source']): continue
                    body = json.loads(row['body'])
                    if not body.get('jellyfin_id'): continue
                    try:
                        for path in paths(body): self.require_media_path(path)
                        readable = True
                    except Problem:
                        readable = False
                    if readable != bool(row['available']):
                        self.stamp_media(body, body, state_changed=True)
                        db.execute('UPDATE media SET body=?,available=? WHERE id=?', (encoded(body), int(readable), row['id']))
                        job['restored' if readable else 'missing'] += 1; changed = True
                    elif not readable:
                        job['still_missing'] += 1
                job.update(status='completed', finished=now_ms())
                self.update_jellyfin_status(db, last_sync=now_ms(), last_attempt=now_ms(), status='connected', error='', items=len(prepared),
                                            skipped=job['skipped'], still_missing=job['still_missing'], consecutive_failures=0)
                if changed: self.changed(db, actor, '同步 Jellyfin 片库', str(len(prepared)) + ' 部作品')
                db.execute('UPDATE jobs SET body=? WHERE id=?', (encoded(job), job['id']))
        except Exception:
            job.update(status='failed', error='Jellyfin 同步失败，原片库保留；请检查本机服务、目录和连接配置', finished=now_ms())
            with self.db() as db:
                row = db.execute("SELECT value FROM kv WHERE key='jellyfin_status'").fetchone()
                failures = (json.loads(row[0]) if row else {}).get('consecutive_failures', 0)
                self.update_jellyfin_status(db, status='error', error=job['error'], last_attempt=now_ms(), consecutive_failures=failures + 1)
                db.execute('UPDATE jobs SET body=? WHERE id=?', (encoded(job), job['id']))
        finally: self.scan_lock.release()

    def run_jellyfin_scheduler(self):
        def loop():
            from management import Problem, now_ms
            while True:
                try:
                    with self.db() as db: self.update_jellyfin_status(db, scheduler_alive=now_ms())
                    if not self.sync_control()['paused'] and self.jellyfin_config(): self.start_jellyfin_sync('system')
                except Problem: pass
                except Exception as exc:
                    # A locked database or a full disk must not end automatic synchronisation until the next restart.
                    # Only the exception type is logged: messages may carry paths or upstream details.
                    print('jellyfin scheduler will retry after', type(exc).__name__, flush=True)
                try:interval=self.sync_control()['interval_seconds']
                except Exception:interval=self.sync_interval_seconds
                self.sync_wakeup.wait(interval)
                self.sync_wakeup.clear()
        threading.Thread(target=loop, daemon=True, name='jellyfin-sync').start()
