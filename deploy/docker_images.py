#!/usr/bin/env python3
"""Bounded retries for immutable installer images only, never service actions or media jobs."""
import argparse
import os
from pathlib import Path
import platform
import re
import subprocess
import tempfile
import time

RETRY_DELAYS = (10, 30, 60)  # initial attempt plus three retries
PULL_TIMEOUT = 900
PINNED = re.compile(r'[A-Za-z0-9][A-Za-z0-9._/:\-]*@sha256:[0-9a-f]{64}')


def native_platform():
    machine = platform.machine().lower()
    return 'linux/' + {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(machine, machine)


def pull_image(image, *, delays=RETRY_DELAYS, timeout=PULL_TIMEOUT):
    if not isinstance(image, str) or not PINNED.fullmatch(image):
        raise ValueError('安装镜像必须锁定 SHA256 摘要')
    expected = native_platform()
    try:
        cached = subprocess.run(['docker', 'image', 'inspect', '--format', '{{.Os}}/{{.Architecture}}', image],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=15)
        if cached.returncode == 0 and cached.stdout.strip() == expected:
            return
    except subprocess.TimeoutExpired:
        pass
    except FileNotFoundError:
        raise RuntimeError('尚未安装 Docker，镜像拉取停止') from None
    for attempt in range(len(delays) + 1):
        print('拉取固定镜像（%s，第 %d/%d 次）：%s' % (expected, attempt + 1, len(delays) + 1, image), flush=True)
        try:
            subprocess.run(['docker', 'pull', '--platform', expected, image], check=True, timeout=timeout,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            # Registry errors can contain signed URLs. Keep upstream output out of installation logs.
            if attempt == len(delays):
                raise RuntimeError('固定镜像拉取未完成，已用尽 %d 次尝试；保留安装状态，可检查网络后恢复' % (attempt + 1)) from None
            print('镜像暂未拉取成功，%d 秒后重试；锁定摘要与 TLS 校验保持。' % delays[attempt], flush=True)
            time.sleep(delays[attempt])


def copy_helper(directory):
    """Keep the installed standalone native-tool scripts usable without the source checkout."""
    destination = Path(directory) / Path(__file__).name
    if destination.is_symlink():
        raise RuntimeError('镜像工具路径异常，未覆盖')
    if destination.resolve() == Path(__file__).resolve():
        return
    fd, temporary = tempfile.mkstemp(prefix='.docker-images-', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(Path(__file__).read_bytes())
        os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image')
    args = parser.parse_args()
    try:
        pull_image(args.image)
    except (RuntimeError, ValueError) as exc:
        parser.exit(1, str(exc) + '\n')
