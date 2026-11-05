"""1.1.0 media automation center: the few owner actions a fresh install may still need, in the hotel admin.

Reads the installer's private integration file (endpoints + API keys of the loopback-only native apps), never
returns keys to the browser, and only calls 127.0.0.1. Everything here is optional: without the file (core
install, or the existing production server) the page simply reports that automation is not installed."""
import json
from pathlib import Path
from datetime import datetime, timezone
import shutil
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

HEVC_POLICIES = {'allow': (0, 0), 'prefer_h264': (-100, -10000), 'h264_only': (-10000, 0)}
HEVC_LABELS = {'allow': '允许 HEVC（默认）', 'prefer_h264': '优先 H.264，没有再用 HEVC', 'h264_only': '只要 H.264'}
SERVICES = [('seerr', 'Seerr 加片', '/api/v1/status'), ('radarr', 'Radarr 电影', '/api/v3/system/status'),
            ('sonarr', 'Sonarr 剧集', '/api/v3/system/status'), ('prowlarr', 'Prowlarr 搜索源', '/api/v1/system/status'),
            ('bazarr', 'Bazarr 字幕', '/api/system/status')]
IGNORED_HEALTH = {'UpdateCheck', 'SystemTimeCheck'}   # not actionable for a hotel owner
CACHE_SECONDS = 15
REFRESH_BUDGET = 25   # seconds; a hanging app must not hold the admin request


class NativeError(Exception):
    """Sanitized: never carries an upstream URL, key or body."""


class MediaCenter:
    # ---------- plumbing ----------
    def media_center_config(self):
        path = self.data / 'media-automation.json'
        if not path.exists(): return None
        try: config = json.loads(path.read_text())
        except (OSError, ValueError): raise NativeError('片库自动化私有配置不可读')
        for app, value in config.items():
            if isinstance(value, dict) and 'url' in value:
                url = urlsplit(value['url'])
                if url.scheme != 'http' or url.hostname != '127.0.0.1' or url.path not in ('', '/') or url.query or url.username:
                    raise NativeError('片库自动化私有配置无效')
        return config

    def _native(self, config, app, path, body=None, method=None, form=False, timeout=6):
        service = config.get(app) or {}
        if not service.get('url'): raise NativeError(app + ' 未安装')
        headers = {'X-Api-Key': service['api_key']} if service.get('api_key') else {}
        data = None
        if body is not None:
            headers['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
            data = (urlencode(body, doseq=True) if form else json.dumps(body)).encode()
        request = Request(service['url'].rstrip('/') + path, data=data, headers=headers, method=method)
        try:
            with build_opener(ProxyHandler({})).open(request, timeout=timeout) as response:
                raw = response.read(8 * 1024 * 1024)
        except HTTPError as exc:
            detail = ''
            try:
                payload = json.loads(exc.read(65536) or b'null')
                if isinstance(payload, list): detail = '；'.join(str(x.get('errorMessage') or x.get('message') or '') for x in payload if isinstance(x, dict))[:300]
                elif isinstance(payload, dict): detail = str(payload.get('message') or payload.get('error') or '')[:300]
            except (ValueError, OSError): pass
            raise NativeError('%s 返回 %d%s' % (app, exc.code, ('：' + detail) if detail else '')) from None
        except (URLError, OSError, TimeoutError):
            raise NativeError(app + ' 无法连接') from None
        try: return json.loads(raw) if raw else None
        except ValueError: return raw.decode('utf-8', 'replace')

    # ---------- snapshot ----------
    def media_center_status(self, refresh=False):
        cache = self.__dict__.get('_media_center_cache')
        if cache and not refresh and time.monotonic() - cache[0] < CACHE_SECONDS: return cache[1]
        lock = self.__dict__.setdefault('_media_center_lock', threading.Lock())
        if not lock.acquire(blocking=False):
            return {**(cache[1] if cache else {'enabled': self.automation_enabled(), 'issues': [], 'checked_at': None}), 'refreshing': True}
        try:
            snapshot = self._media_center_snapshot()
            self.__dict__['_media_center_cache'] = (time.monotonic(), snapshot)
            return snapshot
        finally:
            lock.release()

    def _media_center_snapshot(self):
        from management import now_ms
        enabled = self.automation_enabled()
        result = {'enabled': enabled, 'checked_at': now_ms(), 'services': [], 'issues': [], 'indexers': [],
                  'subtitles': {}, 'policy': {}, 'storage': [], 'compat': {}, 'pending_publish': 0}
        if not enabled: return result
        try: config = self.media_center_config()
        except NativeError as exc:
            result['issues'].append(self._issue('error', '片库自动化配置异常', str(exc), None)); return result
        if not config:
            result['issues'].append(self._issue('warning', '缺少片库自动化私有配置', '服务仍在运行；请用安装包 --resume 补齐，或联系维护人员', None))
            return result
        deadline = time.monotonic() + REFRESH_BUDGET
        def budget(): return time.monotonic() < deadline
        for app, name, path in SERVICES:
            entry = {'id': app, 'name': name, 'ok': False, 'detail': ''}
            if budget():
                try:
                    status = self._native(config, app, path, timeout=5) or {}
                    data = status.get('data', status) if isinstance(status, dict) else {}
                    entry.update(ok=True, detail=str(data.get('version') or data.get('bazarr_version') or '运行中'))
                except NativeError as exc: entry['detail'] = str(exc)
            else: entry['detail'] = '本次检查超时，稍后重试'
            result['services'].append(entry)
            if not entry['ok']:
                result['issues'].append(self._issue('error', name + ' 无法连接', entry['detail'] + '；可在“NAS 与应用 → 服务器检测”查看，或在 Dockge 重启该应用', 'open:' + app))
        up = {s['id'] for s in result['services'] if s['ok']}
        for app in ('radarr', 'sonarr'):
            if app not in up or not budget(): continue
            label = 'Radarr' if app == 'radarr' else 'Sonarr'
            try:
                clients = self._native(config, app, '/api/v3/downloadclient') or []
                tested = self._native(config, app, '/api/v3/downloadclient/testall', {}, 'POST', timeout=15) or []
                if not any(c.get('enable', True) for c in clients):   # e.g. a hand-configured server where Sonarr never got qBittorrent
                    result['issues'].append(self._issue('error', label + ' 没有配置下载器', '自动搜片请求的%s无法下载；点“修复下载器连接”会自动接上内置 qBittorrent' % ('电影' if app == 'radarr' else '剧集'), 'repair_downloaders'))
                elif any(not t.get('isValid', True) for t in tested):
                    result['issues'].append(self._issue('error', label + ' 连不上下载器', '新片会一直停在“处理中”。常见原因是改过 qBittorrent 密码', 'repair_downloaders'))
            except NativeError as exc:
                result['issues'].append(self._issue('error', label + ' 连不上下载器', str(exc), 'repair_downloaders'))
            try:
                for h in self._native(config, app, '/api/v3/health') or []:
                    if h.get('source') in IGNORED_HEALTH or h.get('type') not in ('error', 'warning'): continue
                    if h.get('source') == 'DownloadClientCheck': continue   # covered by the test above, with a repair action
                    detail = str(h.get('message', ''))[:300]
                    if 'Indexer' in str(h.get('source','')) or 'indexer' in detail.lower(): detail='资源搜索源故障或未配置，自动请求暂时无法抓取；请检查 Prowlarr，或贴链接加片。'
                    elif 'root folder' in detail.lower():detail='片库根目录缺失或不可访问；请确认数据盘已挂载，并在原生应用核对媒体根目录。'
                    elif 'disk space' in detail.lower():detail='可用磁盘空间不足；请腾出数据盘空间后重试。'
                    result['issues'].append(self._issue(h['type'], label + ' 提示', detail, 'open:' + app))
            except NativeError: pass
        if 'prowlarr' in up and budget():
            try:
                indexers = self._native(config, 'prowlarr', '/api/v1/indexer') or []
                failing=set()
                for status in self._native(config,'prowlarr','/api/v1/indexerstatus') or []:
                    if status.get('disabledTill'):
                        try:
                            until=datetime.fromisoformat(status['disabledTill'].replace('Z','+00:00'))
                            if until.tzinfo is None:until=until.replace(tzinfo=timezone.utc)
                            if until.timestamp()>time.time():failing.add(status.get('indexerId'))
                        except (ValueError,TypeError):failing.add(status.get('indexerId'))
                result['indexers'] = [{'name': i.get('name', ''), 'enabled': bool(i.get('enable')), 'ok': bool(i.get('enable')) and i.get('id') not in failing} for i in indexers]
                usable = [i for i in result['indexers'] if i['ok']]
                service=next(s for s in result['services'] if s['id']=='prowlarr')
                service.update(ok=bool(usable),detail='%d 个可用搜索源 / %d 个已配置' % (len(usable),len(indexers)))
                if not usable:
                    result['issues'].append(self._issue('error', '没有可用的资源搜索源', '新片将找不到资源。点“重新测试搜索源”，或在 Prowlarr 添加公开源', 'retest_indexers'))
                elif len(usable) < len([i for i in result['indexers'] if i['enabled']]):
                    result['issues'].append(self._issue('warning', '部分搜索源暂时不可用', '、'.join(i['name'] for i in result['indexers'] if i['enabled'] and not i['ok']), 'retest_indexers'))
            except NativeError as exc:
                result['issues'].append(self._issue('warning', '无法读取搜索源状态', str(exc), 'open:prowlarr'))
        if 'seerr' in up and budget():
            try:
                requests=self._native(config,'seerr','/api/v1/request?take=20&sort=added&sortDirection=desc') or {}
                waiting=[]
                queues={}
                for app in ('radarr','sonarr'):
                    if app in up and budget():
                        try:queues[app]=(self._native(config,app,'/api/v3/queue?page=1&pageSize=100&includeMovie=true&includeSeries=true') or {}).get('records',[])
                        except NativeError:queues[app]=[]
                for request in requests.get('results',[]):
                    media=request.get('media') or {}
                    if request.get('status')!=2 or media.get('status')==5:continue
                    try:
                        created=datetime.fromisoformat(request['createdAt'].replace('Z','+00:00'))
                        if created.tzinfo is None:created=created.replace(tzinfo=timezone.utc)
                        if time.time()-created.timestamp()<1800:continue
                    except (KeyError,ValueError,TypeError):continue
                    app='sonarr' if media.get('mediaType')=='tv' else 'radarr'
                    field='series' if app=='sonarr' else 'movie';ident='tvdbId' if app=='sonarr' else 'tmdbId'
                    if any((q.get(field) or {}).get(ident)==media.get(ident) for q in queues.get(app,[]) if media.get(ident)):continue
                    waiting.append(request.get('id'))
                if waiting:
                    result['issues'].append(self._issue('warning','%d 个已批准请求超过30分钟仍未开始下载' % len(waiting),'没有可用结果、搜索源故障或质量规则不符均可能导致等待。请在 Seerr 核对请求；可在 Radarr/Sonarr 交互搜索查看拒绝原因，或贴链接加片。','open:seerr'))
            except NativeError:pass
        if 'seerr' in up and budget():
            try:
                found = self._native(config, 'seerr', '/api/v1/search?' + urlencode({'query': '功夫', 'language': 'zh-CN'}), timeout=10) or {}
                result['tmdb'] = bool(found.get('results'))
            except NativeError: result['tmdb'] = False
            if not result['tmdb']:
                result['issues'].append(self._issue('warning', '暂时连不上影片资料库（TMDB）', '新片可能搜不到或缺海报；通常是网络问题，稍后重新检查', None))
        if 'bazarr' in up and budget():
            try:
                result['subtitles'] = self._subtitle_status(config)
                sub = result['subtitles']
                if not sub['online_sources']:
                    result['issues'].append(self._issue('warning', '尚未配置在线字幕来源', '现有字幕仍可播放；填写 Assrt Token 或 OpenSubtitles 账号后，才能联网补缺。', 'open:bazarr'))
                if sub['provider_failures']:
                    result['issues'].append(self._issue('warning', '部分字幕来源暂时不可用', '、'.join(sub['provider_failures']) + '；请在 Bazarr 查看来源状态，配置完成不等于下载成功。', 'open:bazarr'))
            except NativeError:
                result['issues'].append(self._issue('warning', '字幕规则暂时无法读取', '请稍后重新检查，或打开 Bazarr 查看。', 'open:bazarr'))
        with self.db() as db:
            guarded=db.execute("SELECT value FROM kv WHERE key='media_storage_guard'").fetchone()
            guard=json.loads(guarded[0]) if guarded else {}
            if guard.get('low') or guard.get('error'):
                result['issues'].append(self._issue('error','数据盘保护已暂停下载',guard.get('error') or '余量不足，腾出空间并确认挂载后自动恢复本系统暂停的任务','view:cleanup'))
            row = db.execute("SELECT value FROM kv WHERE key='media_policy'").fetchone()
            policy = (json.loads(row[0]) if row else {}).get('hevc', 'allow')
            result['policy'] = {'hevc': policy, 'label': HEVC_LABELS.get(policy, policy), 'choices': HEVC_LABELS}
            counts, pending = {}, 0
            for r in db.execute('SELECT body,published,available FROM media'):
                item = json.loads(r['body'])
                if item.get('_archived') or not r['available']: continue
                label = self.automation_readiness(db, item)['label']
                counts[label] = counts.get(label, 0) + 1
                if not r['published'] and label == '兼容检查通过': pending += 1
        result['compat'] = counts; result['pending_publish'] = pending
        result['issues'].append(self._issue('info','自动搜索依赖搜索源与质量规则','请求不等于开始下载；没有结果、源故障或质量不符时，可调整原生质量规则或贴链接加片。','open:seerr'))
        def count(*needles): return sum(n for label, n in counts.items() if any(x in label for x in needles))
        if count('磁盘空间不足'):
            result['issues'].append(self._issue('error', '%d 部影片因磁盘空间不足暂停兼容处理' % count('磁盘空间不足'), '清理冷门影片或更换更大的数据盘，腾出空间后自动继续', 'view:cleanup'))
        if count('兼容副本失败', '文件预检失败'):
            result['issues'].append(self._issue('warning', '%d 部影片兼容检查失败' % count('兼容副本失败', '文件预检失败'), '多为下载残缺或文件损坏；在“兼容检查”中重试，或在 Radarr/Sonarr 重新下载', 'view:compat'))
        if count('源文件或兼容副本不可读'):
            result['issues'].append(self._issue('warning', '%d 部影片文件不可读' % count('源文件或兼容副本不可读'), '数据盘可能未挂载或权限变化；在“NAS 与应用 → 服务器检测”查看', 'view:health'))
        disks = {}
        for label, path in [('片库与下载', config.get('storage_root')), ('兼容副本', config.get('compat_root')), ('系统盘', '/')]:
            if not path: continue
            try:
                probe = Path(path)
                while not probe.exists() and probe != probe.parent: probe = probe.parent   # e.g. compat root before the first copy
                device, usage = probe.stat().st_dev, shutil.disk_usage(probe)
            except OSError:
                result['storage'].append({'label': label, 'paths': [path], 'total': 0, 'free': 0})
                result['issues'].append(self._issue('error', label + '不可用', path + ' 未挂载或无法读取', 'view:health'))
                continue
            if device in disks:   # same physical disk: one bar, several roles
                disks[device]['label'] += ' · ' + label; disks[device]['paths'].append(path); continue
            disks[device] = {'label': label, 'paths': [path], 'total': usage.total, 'free': usage.free}
            result['storage'].append(disks[device])
        for disk in result['storage']:
            if disk['total'] and disk['free'] < max(10 * 1024 ** 3, disk['total'] * 0.08):
                result['issues'].append(self._issue('warning', disk['label'] + ' 所在磁盘剩余空间不足', '剩余 %.1f GB；空间不足时新片无法下载或生成兼容副本' % (disk['free'] / 1024 ** 3), 'view:cleanup'))
        result['issues'].extend(self.media_add_issues())
        result['add'] = self.media_add_list()['attention']
        order = {'error': 0, 'warning': 1}
        result['issues'].sort(key=lambda i: order.get(i['level'], 2))
        return result

    @staticmethod
    def _issue(level, title, detail, action):
        return {'level': level, 'title': title, 'detail': detail, 'action': action}

    # ---------- owner actions (admin only; routed by admin_http) ----------
    def media_center_action(self, body, actor):
        from management import Problem
        action = body.get('action')
        if action not in ('refresh', 'repair_downloaders', 'retest_indexers', 'set_policy', 'set_subtitles', 'subtitle_policy', 'search_subtitles'): raise Problem('操作无效')
        if not self.automation_enabled(): raise Problem('本机未安装片库自动化', 409)
        try:
            config = self.media_center_config()
            if not config: raise Problem('缺少片库自动化私有配置；请用安装包 --resume 补齐', 409)
            if action == 'refresh': message = '已重新检查'
            elif action == 'repair_downloaders': message = self._repair_downloaders(config, actor)
            elif action == 'retest_indexers': message = self._retest_indexers(config, actor)
            elif action == 'set_policy': message = self._set_policy(config, body.get('hevc'), actor)
            elif action == 'subtitle_policy': message = self._configure_subtitle_policy(config, actor)
            elif action == 'search_subtitles': message = self._search_subtitles(config, actor)
            else: message = self._set_subtitles(config, body, actor)
        except NativeError as exc:
            raise Problem(str(exc), 502) from None
        return {'message': message, 'status': self.media_center_status(refresh=True)}

    def _repair_downloaders(self, config, actor):
        from management import Problem
        try: secrets = json.loads((self.data / 'download-secrets.json').read_text())
        except (OSError, ValueError): secrets = {}
        password = (secrets.get('qbittorrent') or {}).get('password')
        if not password: raise Problem('后台没有保存 qBittorrent 密码；请先在“密码管理”中设置一次 qBittorrent 密码', 409)
        fixed = []
        for app, category in (('radarr', 'radarr'), ('sonarr', 'sonarr')):
            clients = self._native(config, app, '/api/v3/downloadclient') or []
            client = next((c for c in clients if c.get('name') == 'Hotel qB'), None)
            if client is None:
                client = next(x for x in self._native(config, app, '/api/v3/downloadclient/schema') if x.get('implementation') == 'QBittorrent')
                client.update(name='Hotel qB', enable=True, removeCompletedDownloads=False, removeFailedDownloads=False)
            values = {'host': 'hotel-qbittorrent', 'port': 8098, 'username': 'admin', 'password': password, 'movieCategory': category, 'tvCategory': category}
            for field in client.get('fields', []):
                if field.get('name') in values: field['value'] = values[field['name']]
            # Updating a native client does not prove its credentials work. In particular, a password changed
            # in qB's own UI cannot be inferred from our saved copy; use the existing password manager to resync it.
            try: self._native(config, app, '/api/v3/downloadclient/test', client, 'POST', timeout=20)
            except NativeError:
                raise Problem('下载器连接测试未通过；如果在 qBittorrent 原生页改过密码，请先在“密码管理”重新设置 qBittorrent 密码，再修复连接', 409) from None
            if client.get('id'): self._native(config, app, '/api/v3/downloadclient/%d' % int(client['id']), client, 'PUT', timeout=20)
            else: self._native(config, app, '/api/v3/downloadclient', client, 'POST', timeout=20)
            fixed.append('Radarr' if app == 'radarr' else 'Sonarr')
        with self.db() as db: self.audit(db, actor, '修复下载器连接', '、'.join(fixed))
        return '、'.join(fixed) + ' 已重新连接 qBittorrent'

    def _retest_indexers(self, config, actor):
        try: results = self._native(config, 'prowlarr', '/api/v1/indexer/testall', {}, 'POST', timeout=60) or []
        except NativeError as exc:
            if '返回 400' not in str(exc): raise
            results = []   # Prowlarr answers 400 when any indexer fails; the snapshot shows which
        self._native(config, 'prowlarr', '/api/v1/command', {'name': 'ApplicationIndexerSync'}, 'POST')
        with self.db() as db: self.audit(db, actor, '重新测试搜索源', '%d 个' % len(results))
        return '已重新测试搜索源并同步到 Radarr、Sonarr'

    def _set_policy(self, config, policy, actor):
        from management import Problem
        if policy not in HEVC_POLICIES: raise Problem('片源规则无效')
        score, minimum = HEVC_POLICIES[policy]
        for app in ('radarr', 'sonarr'):
            service = config.get(app) or {}
            if not service.get('format') or not service.get('profile'): raise Problem('缺少片源规则配置；请用安装包 --resume 补齐', 409)
            profile = self._native(config, app, '/api/v3/qualityprofile/%d' % int(service['profile']))
            items = [x for x in profile.get('formatItems', []) if x.get('format', x.get('formatId')) == service['format']]
            if not items: raise Problem(app + ' 中找不到 HEVC 规则', 409)
            for item in items: item['score'] = score
            profile['minFormatScore'] = minimum
            self._native(config, app, '/api/v3/qualityprofile/%d' % int(service['profile']), profile, 'PUT')
            saved = self._native(config, app, '/api/v3/qualityprofile/%d' % int(service['profile']))
            if saved.get('minFormatScore') != minimum or not any(x.get('format', x.get('formatId')) == service['format'] and x.get('score') == score for x in saved.get('formatItems', [])):
                raise Problem(app + ' 片源规则读回不符，请重试', 502)
        with self.db() as db:
            self.put_kv(db, 'media_policy', {'hevc': policy})
            self.audit(db, actor, '修改片源规则', HEVC_LABELS[policy])
        return '片源规则已改为：' + HEVC_LABELS[policy] + '；对之后的新请求生效'

    @staticmethod
    def _subtitle_sources(settings):
        providers = list((settings.get('general') or {}).get('enabled_providers') or [])
        return [p for p in providers if isinstance(p, str) and p != 'embeddedsubtitles'
                and (p != 'assrt' or (settings.get('assrt') or {}).get('token'))
                and (p != 'opensubtitlescom' or ((settings.get('opensubtitlescom') or {}).get('username') and (settings.get('opensubtitlescom') or {}).get('password')))]

    def _subtitle_status(self, config):
        settings = self._native(config, 'bazarr', '/api/system/settings', timeout=4) or {}
        if not isinstance(settings, dict): raise NativeError('字幕设置返回无效')
        general = settings.get('general') or {}
        providers = general.get('enabled_providers') or []
        subsync = settings.get('subsync') or {}
        sync_enabled = subsync.get('use_subsync') is True
        gates = [subsync.get(key) for key in ('use_subsync_threshold', 'use_subsync_movie_threshold')]
        sync_scope = ('disabled' if not sync_enabled else 'score_limited' if any(value is True for value in gates)
                      else 'all_downloads' if all(value is False for value in gates) else 'unknown')
        result = {'embedded': 'embeddedsubtitles' in providers,
                  'assrt': 'assrt' in providers and bool((settings.get('assrt') or {}).get('token')),
                  'opensubtitlescom': 'opensubtitlescom' in providers and bool((settings.get('opensubtitlescom') or {}).get('username')),
                  'auto_sync': sync_enabled, 'auto_sync_scope': sync_scope,
                  'online_sources': self._subtitle_sources(settings), 'policy_ready': False,
                  'wanted_movies': None, 'wanted_episodes': None, 'provider_failures': []}
        try:
            profiles = self._native(config, 'bazarr', '/api/system/languages/profiles', timeout=2) or []
            profile = next((p for p in profiles if p.get('name') == '酒店简中优先'), None)
            result['policy_ready'] = bool(profile and all(general.get(key) is True for key in ('movie_default_enabled', 'serie_default_enabled'))
                and all(str(general.get(key)) == str(profile['profileId']) for key in ('movie_default_profile', 'serie_default_profile')))
        except (NativeError, TypeError, KeyError): pass
        for kind in ('movies', 'episodes'):
            try:
                count = (self._native(config, 'bazarr', '/api/%s/wanted?start=0&length=1' % kind, timeout=2) or {}).get('total')
                if type(count) is int and count >= 0: result['wanted_' + kind] = count
            except (NativeError, AttributeError): pass
        try:
            rows = (self._native(config, 'bazarr', '/api/providers', timeout=2) or {}).get('data') or []
            result['provider_failures'] = [str(p.get('name', ''))[:80] for p in rows
                if p.get('name') in providers and p.get('status') not in ('Good', 'History', None)]
        except (NativeError, AttributeError, TypeError): pass
        return result

    def _configure_subtitle_policy(self, config, actor):
        import copy
        from management import Problem
        # Bazarr replaces the entire profile list: retain every existing profile, including custom fields.
        profiles = self._native(config, 'bazarr', '/api/system/languages/profiles')
        languages = self._native(config, 'bazarr', '/api/system/languages')
        if not isinstance(profiles, list) or not all(isinstance(p, dict) and type(p.get('profileId')) is int for p in profiles):
            raise Problem('无法完整读取字幕规则，未修改现有配置', 502)
        if not isinstance(languages, list): raise Problem('无法读取字幕语言，未修改现有配置', 502)
        profiles = copy.deepcopy(profiles)
        profile = next((p for p in profiles if p.get('name') == '酒店简中优先'), None)
        if profile is None:
            profile = {'profileId': max([p['profileId'] for p in profiles] + [0]) + 1, 'name': '酒店简中优先'}
            profiles.append(profile)
        profile.update(cutoff=1, items=[{'id': i, 'language': lang, 'audio_exclude': 'False', 'audio_only_include': 'False',
            'hi': 'False', 'forced': 'False'} for i, lang in enumerate(('zh', 'zt'), 1)], mustContain=[], mustNotContain=[], originalFormat=False)
        enabled = list(dict.fromkeys([p['code2'] for p in languages if p.get('enabled') and p.get('code2')] + ['zh', 'zt']))
        form = {'languages-enabled': enabled, 'languages-profiles': json.dumps(profiles),
                'settings-general-movie_default_enabled': 'true', 'settings-general-serie_default_enabled': 'true',
                'settings-general-movie_default_profile': str(profile['profileId']), 'settings-general-serie_default_profile': str(profile['profileId']),
                'settings-subsync-use_subsync': 'true', 'settings-subsync-force_audio': 'true',
                'settings-subsync-use_original_language': 'true', 'settings-subsync-no_fix_framerate': 'false',
                'settings-subsync-gss': 'true', 'settings-subsync-max_offset_seconds': '60',
                # Provider match scores do not prove timing. A real 91.67% Assrt match was 13.11s early.
                # Native ffsubsync still owns alignment and failure handling; do not bypass its checks.
                'settings-subsync-use_subsync_threshold': 'false',
                'settings-subsync-use_subsync_movie_threshold': 'false'}
        self._native(config, 'bazarr', '/api/system/settings', form, 'POST', form=True)
        check = self._subtitle_status(config)
        if not check['policy_ready'] or check['auto_sync_scope'] != 'all_downloads':
            raise Problem('字幕规则已提交，但读回不完整，请在 Bazarr 核对后重试', 502)
        with self.db() as db: self.audit(db, actor, '配置中文默认字幕与自动校时', '新片简中目标，繁中回退；新下载字幕均尝试音频校时；保留已有单片规则')
        return '已保存中文默认规则；新下载字幕均尝试音频校时，现有单片规则保留' + ('。尚需配置在线字幕来源' if not check['online_sources'] else '')

    def _search_subtitles(self, config, actor):
        from management import Problem
        settings = self._native(config, 'bazarr', '/api/system/settings') or {}
        if not self._subtitle_sources(settings): raise Problem('请先配置可用的在线字幕来源，再搜索缺失字幕', 409)
        # Native scheduler owns queues, concurrency and retries; never block guest playback on a search.
        tasks = self._native(config, 'bazarr', '/api/system/tasks') or {}
        available = {p.get('job_id') for p in tasks.get('data', [])}
        requested = ['wanted_search_missing_subtitles_movies', 'wanted_search_missing_subtitles_series']
        if not set(requested).issubset(available): raise Problem('Bazarr 自动搜索任务不可用，请打开原生应用检查', 409)
        for task in requested:
            self._native(config, 'bazarr', '/api/system/tasks', {'taskid': task}, 'POST', form=True)
        with self.db() as db: self.audit(db, actor, '搜索缺失字幕', '已交给 Bazarr 原生后台任务')
        return '已交给 Bazarr 后台搜索；是否找到匹配字幕请查看待补数量和原生任务结果'

    def _set_subtitles(self, config, body, actor):
        from management import Problem, text
        current = self._native(config, 'bazarr', '/api/system/settings') or {}
        providers = list((current.get('general') or {}).get('enabled_providers') or [])
        form, changed = {}, []
        if 'assrt_token' in body:
            token = text(body.get('assrt_token') or '', 200)
            form['settings-assrt-token'] = token
            providers = [p for p in providers if p != 'assrt'] + (['assrt'] if token else [])
            changed.append('射手网（Assrt）' + ('已启用' if token else '已关闭'))
        if 'opensubtitles_username' in body:
            user = text(body.get('opensubtitles_username') or '', 100)
            password = text(body.get('opensubtitles_password') or '', 200)
            if user and not password: raise Problem('请同时填写 OpenSubtitles 密码')
            if password and not user: raise Problem('请同时填写 OpenSubtitles 用户名；两项都留空才会关闭此字幕源')
            form['settings-opensubtitlescom-username'] = user
            form['settings-opensubtitlescom-password'] = password
            providers = [p for p in providers if p != 'opensubtitlescom'] + (['opensubtitlescom'] if user else [])
            changed.append('OpenSubtitles ' + ('已启用' if user else '已关闭'))
        if not changed: raise Problem('没有需要保存的字幕源')
        if 'embeddedsubtitles' not in providers: providers.insert(0, 'embeddedsubtitles')
        form['settings-general-enabled_providers'] = providers
        self._native(config, 'bazarr', '/api/system/settings', form, 'POST', form=True)
        with self.db() as db: self.audit(db, actor, '修改字幕源', '；'.join(changed))   # never the token itself
        return '；'.join(changed)
