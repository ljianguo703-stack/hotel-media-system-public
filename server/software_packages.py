"""Inspect software without executing it. All archive paths and sizes are bounded."""
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
import zipfile

MAX_UPLOAD = 2 * 1024 ** 3
MAX_EXPANDED = 4 * 1024 ** 3
IDENT = re.compile(r'[a-z][a-z0-9-]{1,62}')
VERSION = re.compile(r'[A-Za-z0-9][A-Za-z0-9.+_~:-]{0,99}')
PACKAGE = re.compile(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+')


class SoftwareError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class WaitAuthorization(SoftwareError):
    pass


class DeferredInstall(SoftwareError):
    def __init__(self, status, message):
        super().__init__(message)
        self.wait_status = status


def digest(path):
    result = hashlib.sha256()
    with open(path, 'rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def tool(name):
    value = os.environ.get('HOTEL_TV_' + name.upper(), '') or shutil.which(name)
    if not value:
        raise SoftwareError('缺少校验工具 ' + name + '，请先按安装说明配置执行环境', 409)
    return value


def checked(command, timeout=60):
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise SoftwareError('安装包校验工具无法运行或超时')
    if result.returncode:
        raise SoftwareError('安装包格式或签名校验失败')
    return result.stdout.decode('utf-8', 'replace')


def safe_name(value):
    if not isinstance(value, str) or '\\' in value or '\x00' in value:
        raise SoftwareError('压缩包文件路径无效')
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or not path.parts or str(path) != value:
        raise SoftwareError('压缩包含越界或不规范路径')
    return value


def zip_members(archive):
    entries = archive.infolist()
    if not 1 <= len(entries) <= 10000:
        raise SoftwareError('压缩包文件数量无效')
    names, expanded = set(), 0
    for entry in entries:
        name = entry.filename[:-1] if entry.is_dir() else entry.filename
        safe_name(name)
        if entry.filename in names:
            raise SoftwareError('压缩包含重复文件')
        names.add(entry.filename)
        mode = entry.external_attr >> 16
        if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise SoftwareError('压缩包不能包含链接或特殊文件')
        if entry.flag_bits & 1:
            raise SoftwareError('不支持加密压缩包')
        expanded += entry.file_size
        if expanded > MAX_EXPANDED:
            raise SoftwareError('压缩包解压后超过 4 GiB')
    return {e.filename: e for e in entries if not e.is_dir()}


def read_small(archive, name, maximum=1024 * 1024):
    info = archive.getinfo(name)
    if info.file_size > maximum:
        raise SoftwareError('安装包说明或清单过大')
    return archive.read(name)


def inspect_apk(path):
    with zipfile.ZipFile(path) as archive:
        members = zip_members(archive)
        if 'AndroidManifest.xml' not in members:
            raise SoftwareError('不是有效的 APK')
    raw = checked([tool('aapt'), 'dump', 'badging', str(path)])
    match = re.search(r"^package: name='([^']+)' versionCode='([0-9]+)' versionName='([^']*)'", raw, re.M)
    if not match or not PACKAGE.fullmatch(match[1]):
        raise SoftwareError('无法读取 APK 包名与版本')
    signature = checked([tool('apksigner'), 'verify', '--print-certs', str(path)])
    signers = sorted(set(re.findall(r'certificate SHA-256 digest: ([0-9a-fA-F]{64})', signature)))
    if not signers:
        raise SoftwareError('APK 缺少可验证签名')
    label = re.search(r"^application-label(?:-zh(?:-CN)?)?:'([^']+)'", raw, re.M)
    sdk = re.search(r"^sdkVersion:'([0-9]+)'", raw, re.M)
    return {'kind': 'apk', 'platform': 'tv', 'software_id': match[1], 'version': match[3] or match[2],
            'version_code': int(match[2]), 'name': label[1] if label else match[1],
            'signers': signers, 'min_sdk': int(sdk[1]) if sdk else 1}


def inspect_deb(path):
    # Read ar and the control tar only; never extract or run maintainer scripts.
    control = None
    with open(path, 'rb') as source:
        if source.read(8) != b'!<arch>\n':
            raise SoftwareError('不是有效的 Debian 安装包')
        debian_version = False
        has_data = False
        while True:
            header = source.read(60)
            if not header:
                break
            if len(header) != 60 or header[-2:] != b'`\n':
                raise SoftwareError('Debian 安装包损坏')
            name = header[:16].decode('ascii').strip().rstrip('/')
            size = int(header[48:58])
            if size < 0 or source.tell() + size > path.stat().st_size:
                raise SoftwareError('Debian 安装包长度无效')
            if name == 'debian-binary':
                if size > 16 or source.read(size).strip() != b'2.0':
                    raise SoftwareError('Debian 格式版本无效')
                debian_version = True
            elif name.startswith('control.tar'):
                if size > 32 * 1024 ** 2 or control is not None:
                    raise SoftwareError('Debian 控制清单过大或重复')
                control = source.read(size)
            else:
                if name.startswith('data.tar'):
                    has_data = True
                source.seek(size, 1)
            if size % 2:
                source.seek(1, 1)
    if not debian_version or control is None or not has_data:
        raise SoftwareError('缺少 Debian 控制清单')
    if shutil.which('dpkg-deb'):
        text = checked([shutil.which('dpkg-deb'), '--field', str(path)])
    else:
        text = None
        expanded = 0
        count = 0
        with tarfile.open(fileobj=io.BytesIO(control), mode='r|*') as archive:
            for member in archive:
                expanded += member.size
                count += 1
                if expanded > 32 * 1024 ** 2 or count > 100:
                    raise SoftwareError('Debian control archive exceeds limits')
                if member.name in ('control', './control'):
                    if text is not None or not member.isfile() or member.size > 1024 * 1024:
                        raise SoftwareError('Invalid Debian control file')
                    text = archive.extractfile(member).read().decode('utf-8')
        if text is None:
            raise SoftwareError('Missing Debian control file')
    values = dict(line.split(':', 1) for line in text.splitlines() if ':' in line and not line.startswith((' ', '\t')))
    values = {k: v.strip() for k, v in values.items()}
    name, version, arch = (values.get(k, '') for k in ('Package', 'Version', 'Architecture'))
    if not re.fullmatch(r'[a-z0-9][a-z0-9+.-]{1,100}', name) or not VERSION.fullmatch(version) or not re.fullmatch(r'[a-z0-9-]{2,30}', arch):
        raise SoftwareError('Debian 包名、版本或架构无效')
    return {'kind': 'deb', 'platform': 'server', 'software_id': name, 'version': version,
            'architecture': arch, 'name': name, 'description': values.get('Description', '')[:500]}


def inspect_bundle(path):
    with zipfile.ZipFile(path) as archive:
        members = zip_members(archive)
        manifest = json.loads(read_small(archive, 'hotel-software.json'))
        if not isinstance(manifest, dict) or manifest.get('format') != 1:
            raise SoftwareError('软件包清单版本无效')
        kind, ident, version = (manifest.get(k, '') for k in ('kind', 'id', 'version'))
        if kind != 'system':
            raise SoftwareError('容器应用请在已有 Dockge 中管理；ZIP 仅用于影院系统升级')
        if ident != 'hotel-tv-server' or not VERSION.fullmatch(version) or not VERSION.fullmatch(manifest.get('base_release', '')):
            raise SoftwareError('影院更新包的标识、版本或 base_release 无效')
        name = manifest.get('name', ident)
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 160:
            raise SoftwareError('软件名称无效')
        files = manifest.get('files')
        if not isinstance(files, dict) or not {'server/server.py', 'admin/index.html'} <= files.keys() or set(members) != set(files) | {'hotel-software.json'}:
            raise SoftwareError('影院更新包文件清单不完整')
        for filename, expected in files.items():
            safe_name(filename)
            extension = PurePosixPath(filename).suffix.lower()
            allowed_program = (filename.startswith('server/') and extension == '.py') or (filename.startswith('admin/') and extension in ('.html', '.css', '.js', '.svg', '.txt', '.jpg', '.png', '.webp'))
            if not allowed_program or not re.fullmatch(r'[0-9a-f]{64}', str(expected)) or any(p.startswith('.') or p == '__pycache__' for p in PurePosixPath(filename).parts):
                raise SoftwareError('影院更新包仅允许程序文件')
            result = hashlib.sha256()
            with archive.open(filename) as source:
                for block in iter(lambda: source.read(1024 * 1024), b''):
                    result.update(block)
            if result.hexdigest() != expected:
                raise SoftwareError('影院更新包文件校验失败：' + filename)
        summary = {'base_release': manifest['base_release'], 'file_count': len(files)}
        return {'kind': kind, 'platform': 'server', 'software_id': ident, 'name': name.strip(), 'version': version, **summary}


def inspect(path, filename):
    try:
        suffix = Path(filename).suffix.lower()
        if suffix == '.apk':
            return inspect_apk(path)
        if suffix == '.deb':
            return inspect_deb(path)
        if suffix == '.zip':
            return inspect_bundle(path)
        raise SoftwareError('请选择 APK、DEB 或符合说明的 ZIP 软件包')
    except SoftwareError:
        raise
    except (ValueError, TypeError, KeyError, UnicodeError, OSError, zipfile.BadZipFile, tarfile.TarError, EOFError):
        raise SoftwareError('软件包损坏或清单格式无效')
