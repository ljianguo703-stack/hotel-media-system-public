#!/usr/bin/env python3
"""Apply a pinned program payload through the existing transactional installer."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import stat
import sys
import tempfile
import zipfile

# The installed payload is immutable. Imports must not add files to its manifest.
sys.dont_write_bytecode = True

PACKAGE_ID = 'hotel-tv-system-upgrade'
TARGET = 'v1-0-0-rc5-in20u2'
BASES = ('v1-0-0-rc5-pl04', 'v1-0-0-rc5-rq02', 'v1-0-0-rc5-rq04',
         'v1-0-0-rc5-rq04u1', 'v1-0-0-rc5-in20',
         'v1-0-0-rc5-in20-from-pl04', 'v1-0-0-rc5-in20-from-rq02',
         'v1-0-0-rc5-in20-from-rq04', 'v1-0-0-rc5-in20-from-rq04u1',
         'v1-0-0-rc5-in20u1', TARGET)


class UpgradeError(Exception):
    pass


def digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest() if hasattr(hashlib, 'file_digest') else _digest(source)


def _digest(source):
    value = hashlib.sha256()
    for block in iter(lambda: source.read(1024 * 1024), b''):
        value.update(block)
    return value.hexdigest()


def verify_payload(root):
    manifest = json.loads((root / 'manifest.json').read_text())
    if manifest.get('format') != 1 or manifest.get('target') != TARGET or manifest.get('supported_bases') != list(BASES):
        raise UpgradeError('直升包说明与控制器不一致，未开始升级')
    files = manifest.get('files', {})
    actual = {str(p.relative_to(root / 'program')) for p in (root / 'program').rglob('*') if p.is_file()}
    if set(files) != actual or not {'server/server.py', 'admin/index.html'} <= actual:
        raise UpgradeError('直升包程序清单不完整，未开始升级')
    for name, expected in files.items():
        path = Path(name)
        allowed = ((name.startswith('server/') and path.suffix == '.py') or
                   (name.startswith('admin/') and path.suffix in {'.js', '.html', '.css', '.txt', '.svg', '.png', '.jpg', '.webp'}))
        source = root / 'program' / path
        if path.is_absolute() or '..' in path.parts or any(p.startswith('.') for p in path.parts) or not allowed or source.is_symlink() or digest(source) != expected:
            raise UpgradeError('直升包程序校验失败：' + name)
    return manifest


def current_release(current, releases):
    if not current.is_symlink():
        raise UpgradeError('未找到标准影院 current 版本链接；未停止服务，请核对部署路径')
    previous = current.resolve(strict=True)
    if previous.parent != releases.resolve() or not previous.is_dir():
        raise UpgradeError('影院当前目录不在标准 releases 内；未停止服务')
    if previous.name not in BASES:
        raise UpgradeError('当前影院版本为 ' + previous.name + '，本包尚未验证该起点；未停止服务')
    return previous


def write_bundle(root, manifest, base, target):
    info = {'format': 1, 'kind': 'system', 'id': 'hotel-tv-server', 'name': '酒店影院系统',
            'version': TARGET, 'base_release': base, 'files': manifest['files']}
    # A deterministic archive is essential when retrying a partially staged release.
    with zipfile.ZipFile(target, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        contents = [('hotel-software.json', (json.dumps(info, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode())]
        contents += [(n, (root / 'program' / n).read_bytes()) for n in sorted(manifest['files'])]
        for name, body in contents:
            entry = zipfile.ZipInfo(name, (2026, 11, 7, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | 0o644) << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, body)


def apply(root, data, state, releases, current, *, runner=None, admin_port=18791, deploy_lock=None):
    manifest = verify_payload(root)
    # Only import pinned, verified program files; no workstation modules or hotel config.
    sys.path.insert(0, str(root / 'program/server'))
    from software_agent import Agent
    from software_packages import SoftwareError, inspect
    from software_store import encoded, now
    agent = Agent(data, state, releases, current, admin_port=admin_port, runner=runner)
    if deploy_lock is not None:
        agent.deployment_lock = Path(deploy_lock)
    with agent.repo.db() as db:
        candidates = [json.loads(r[0]) for r in db.execute("SELECT body FROM software_jobs WHERE status='running'")]
    parents = [j for j in candidates if j.get('kind') == 'deb' and j.get('software_id') == PACKAGE_ID]
    if len(parents) > 1 or any(j.get('kind') == 'system' for j in candidates):
        raise UpgradeError('已有影院升级正在执行，请等待结果后再重试')
    parent = parents[0] if parents else None
    job = {'id': 'sw-' + secrets.token_hex(12), 'kind': 'system', 'software_id': 'hotel-tv-server',
           'name': '酒店影院自动直升', 'version': TARGET, 'target': 'server', 'status': 'running',
           'steps': [], 'created_at': now(), 'started_at': now(), 'actor': parent.get('actor', 'root') if parent else 'root',
           'package_id': parent['package_id'] if parent else '', 'unified_upgrade': True,
           'message': '自动识别影院版本；执行器组件号不作为影院版本'}
    agent.save(job)
    try:
        with agent.deploy_guard():
            previous = current_release(current, releases)
            agent.step(job, '已识别当前影院版本 ' + previous.name + '，目标 ' + TARGET)
            if previous.name == TARGET:
                if any(not (previous / n).is_file() or digest(previous / n) != h for n, h in manifest['files'].items()):
                    raise UpgradeError('目标版本号相同但程序字节不同；未覆盖现有程序，请核对本机修改')
                agent.wait_health()
                agent.save(job, status='completed', message='已是最新统一版，程序校验和健康检查通过，无需重复安装', finished_at=now())
                return {'status': 'already_current', 'current': TARGET, 'job_id': job['id']}
            with tempfile.TemporaryDirectory(prefix='unified-', dir=state) as temp:
                archive = Path(temp) / 'hotel-tv-upgrade.zip'
                write_bundle(root, manifest, previous.name, archive)
                item = {**inspect(archive, archive.name), 'sha256': digest(archive)}
                # Keep the original exact-baseline check, snapshot, switch and rollback.
                agent.install_system_locked(job, item, archive)
            with agent.repo.db() as db:
                inventory = {'software_id': 'hotel-tv-server', 'name': '酒店影院系统', 'kind': 'system',
                             'version': TARGET, 'sha256': item['sha256'], 'target': 'server',
                             'job_id': job['id'], 'verified_at': now()}
                db.execute('INSERT OR REPLACE INTO software_inventory VALUES(?,?,?)', ('server', 'hotel-tv-server', encoded(inventory)))
                agent.repo.bump(db)
            agent.save(job, status='completed', message='统一直升完成，目标版本已核验；影片、本店配置和升级前备份保留', finished_at=now())
            return {'status': 'completed', 'previous': previous.name, 'current': TARGET, 'job_id': job['id']}
    except Exception as exc:
        from software_agent import RolledBack
        status = 'rolled_back' if isinstance(exc, RolledBack) else 'failed'
        message = str(exc) if isinstance(exc, (SoftwareError, UpgradeError)) else '直升发生异常，请检查本机安装日志；原备份保留'
        agent.save(job, status=status, message=message, finished_at=now())
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='只检查包和当前版本，不写数据库、不停止服务')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    current, releases = Path('/opt/hotel-tv/current'), Path('/opt/hotel-tv/releases')
    try:
        manifest = verify_payload(root)
        if args.check:
            previous = current_release(current, releases)
            print(json.dumps({'current': previous.name, 'target': TARGET, 'files_verified': len(manifest['files']), 'changed': False}, ensure_ascii=False))
            return
        if sys.platform != 'linux' or os.geteuid() != 0:
            raise UpgradeError('请通过 Ubuntu 后台的软件与更新安装此 DEB')
        values = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
        if values.get('ID', '').strip('"') != 'ubuntu':
            raise UpgradeError('统一直升包仅支持已部署本影院的 Ubuntu')
        os.umask(0o027)
        def terminate(_signum, _frame):
            raise UpgradeError('升级被终止，按已有恢复记录保留并恢复原服务')
        signal.signal(signal.SIGTERM, terminate)
        print(json.dumps(apply(root, Path('/srv/hotel-tv/data'), Path('/var/lib/hotel-tv-installer'), releases, current,
                               deploy_lock='/var/lock/hotel-tv-deploy.lock'), ensure_ascii=False))
    except Exception as exc:
        print('酒店影院直升未完成：' + str(exc), file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
