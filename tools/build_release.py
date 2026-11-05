#!/usr/bin/env python3
"""Export the committed clean tree, never a workstation directory or business data."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile

FORBIDDEN = {'local', 'artifacts', 'dist', '.git', '.env', 'node_modules', '__pycache__', 'signing.properties', 'hotel-defaults.json'}


def validate_names(names):
    for name in names:
        path = Path(name)
        if path.is_absolute() or '..' in path.parts or any(p in FORBIDDEN for p in path.parts):
            raise ValueError('禁止打包私有/生成文件：' + name)
        if path.suffix.lower() in {'.sqlite', '.sqlite3', '.db', '.jks', '.keystore', '.pem', '.key', '.apk'}:
            raise ValueError('禁止打包运行数据或签名：' + name)
        if name.startswith(('server/data/', 'server/media/')):
            raise ValueError('禁止打包酒店数据：' + name)


def build(root, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    if subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=normal'], cwd=root):
        raise RuntimeError('请先审阅并提交干净源码；不从未提交目录制作发布')
    names = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root).decode().strip('\0').split('\0')
    validate_names(names)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists(): raise ValueError('输出已存在，未覆盖')
    subprocess.run(['git', 'archive', '--format=tar.gz', '-o', str(output), 'HEAD'], cwd=root, check=True)
    with tarfile.open(output) as archive:
        validate_names(archive.getnames())
        if any(m.issym() or m.islnk() for m in archive.getmembers()):
            output.unlink(); raise ValueError('发布包不包含符号链接')
    result = {'file': output.name, 'sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
              'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()}
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('--output', type=Path, required=True)
    a = p.parse_args(); print(json.dumps(build(Path(__file__).resolve().parents[1], a.output)))
