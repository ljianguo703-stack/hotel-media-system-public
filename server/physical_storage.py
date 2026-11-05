"""Bounded, read-only inventory. Filesystem changes belong to the authenticated Cockpit manager."""
import json
import platform
import subprocess


def _mounts(node):
    return [str(node['mountpoint'])] if node.get('mountpoint') else []


def parse_inventory(data):
    if not isinstance(data, dict) or not isinstance(data.get('blockdevices'), list): raise ValueError('invalid lsblk data')
    devices = []
    for node in data['blockdevices']:
        if not isinstance(node, dict) or node.get('type') != 'disk': continue
        def item(value):
            return {'name': str(value.get('name') or '')[:80], 'path': str(value.get('path') or '')[:160],
                    'size': max(0, int(value.get('size') or 0)), 'fstype': str(value.get('fstype') or '')[:40],
                    'label': str(value.get('label') or '')[:100], 'uuid': str(value.get('uuid') or '')[:100],
                    'mounts': _mounts(value), 'readonly': value.get('ro') in (True, 1, '1')}
        parts = []
        def children(parent):
            for child in parent.get('children') or []:
                if not isinstance(child, dict): continue
                parts.append(item(child)); children(child)
        children(node)
        row = item(node)
        row.update(model=str(node.get('model') or '').strip()[:100], serial=str(node.get('serial') or '')[:100], partitions=parts)
        row['system'] = any(m == '/' or m == '/boot' or m.startswith('/boot/') for p in [row] + parts for m in p['mounts'])
        row['mounted'] = any(p['mounts'] for p in [row] + parts)
        devices.append(row)
    return devices


def snapshot():
    if platform.system() != 'Linux': return {'status': 'unsupported', 'devices': [], 'message': '物理硬盘状态由 Ubuntu 主机提供'}
    try:
        result = subprocess.run(['lsblk', '--json', '--bytes', '--output', 'NAME,PATH,SIZE,TYPE,FSTYPE,LABEL,UUID,MOUNTPOINT,RO,MODEL,SERIAL'],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3, check=True)
        if len(result.stdout) > 1024 * 1024: raise ValueError('oversized lsblk data')
        return {'status': 'ok', 'devices': parse_inventory(json.loads(result.stdout))}
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, RecursionError):
        return {'status': 'unavailable', 'devices': [], 'message': '暂时无法读取物理硬盘，请打开硬盘管理查看'}
