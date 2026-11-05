"""Render the offline connection handbook from the same data embedded in the Go UI."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    installer = ROOT / 'tv-installer'
    guides = json.loads((installer / 'connection-guides.json').read_text())['guides']
    profiles = json.loads((installer / 'catalog.json').read_text())['profiles']
    examples = json.loads((installer / 'source/community-index.json').read_text())['examples']
    lines = [
        '# 电视／盒子连接开启方式与机型索引', '',
        '核对日期2026-11-03。设备连接、APK安装、HOME接管、开机／待机、AC来电亮屏分别验收。', '',
        f'包含{len(guides)}类连接方法、{len(profiles)}条本工具机型记录及{len(examples)}条上游目录链接。记录数不是本影院已验型号数。', '',
        '手机模式：先在Ubuntu启动“启动手机局域网安装.sh”，手机打开终端网址并输入8位手机连接码。以下步骤在目标电视／盒子上操作；6位ADB配对码不是手机连接码或员工PIN。', '',
        '## 按品牌和设备类型连接', '',
    ]
    for row in guides:
        lines += ['### ' + row['name'], '', '**状态：' + row['status'] + '**', '']
        lines += [f'{i}. {step}' for i, step in enumerate(row['steps'], 1)]
        lines += ['', *['- ' + note for note in row['notes']], '']
        lines += ['方法依据：' + '，'.join(f'[来源]({url})' for url in row['sources']), '']
    lines += ['## 本工具机型记录', '', '未知型号和固件不按品牌一概放行；“受控试装”需要员工主动选择，并保留自动还原。非Android系统明确暂不支持。', '',
              '| 设备类型 | 记录 | 型号／代号 | SDK | 路线与证据 |', '|---|---|---|---|---|']
    escape = lambda value: str(value).replace('|', '\\|').replace('\n', ' ')
    for row in profiles:
        match = row['match']
        identity = ' / '.join([*match['models'], *match['devices']])
        lines.append('| ' + ' | '.join(map(escape, [row.get('device_kind', 'tv'), row['name'], identity,
                      f'{match["sdk_min"]}–{match["sdk_max"] or "以上"}', f'{row["route"]} · {row["level"]}'])) + ' |')
    lines += ['', '精确固件限制、已知问题及原始来源见随包“机型支持清单.csv”和网页支持范围。', '',
              '## 上游更多机型资料', '', '下面只是固定提交的目录索引，没有将这些记录自动加入可安装名单。按目录的型号／固件与成功条件继续研究。', '']
    lines += [f'- [{row["id"]}]({row["url"]})' for row in examples]
    (installer / '设备连接开启方式.md').write_text('\n'.join(lines) + '\n')
    print(f'{len(guides)} connection families, {len(profiles)} installer records, {len(examples)} source directories')


if __name__ == '__main__':
    main()
