#!/usr/bin/env python3
"""Import identity/evidence metadata only. No upstream executable scripts or debloat lists."""
import csv
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]
UPSTREAM = WORKSPACE / 'research/tools/cleanyourtv-app'


# README brand column → the ro.product.brand / manufacturer values those TVs report.
BRAND_ALIASES = {
    '小米': ['Xiaomi', 'Redmi'], 'Redmi': ['Xiaomi', 'Redmi'], '峰米': ['Xiaomi', 'Formovie', 'Fengmi'],
    'TCL': ['TCL', 'FFALCON', 'IFFALCON'], '雷鸟': ['TCL', 'FFALCON', 'IFFALCON'],
    '海信': ['Hisense'], 'Vidda': ['Hisense', 'Vidda'], '创维': ['Skyworth', 'Coocaa'], '酷开': ['Skyworth', 'Coocaa'],
    '索尼': ['Sony'], '康佳': ['Konka', 'KKTV'], '乐视': ['Letv', 'LeEco'], '极米': ['XGIMI', 'Xgimi'],
    '大眼橙': ['OBE', 'OBE_NEW'], '腾讯极光盒子': ['skyworthbox', 'TencentBox', 'Tencent'], '斐讯': ['Phicomm'],
    '当贝': ['Dangbei'], '夏普': ['SHARP', 'Sharp'], '坚果': ['JMGO'], 'OPPO': ['OPPO'], '松下': ['Panasonic'], '爱普生': ['EPSON', 'Epson'],
}
# Fingerprints/brands of chip vendors' reference builds are shared by many TV makers: an
# identifier from such a build cannot prove one maker's TV.
REFERENCE_BUILDS = ('mstar/', 'mediatek/', 'google/', 'amlogic/', 'rockchip/', 'allwinner/', 'realtek/', 'hisilicon/', 'unisoc/', 'qualcomm/')
REFERENCE_BRANDS = {'mstar', 'mediatek', 'amlogic', 'rockchip', 'allwinner', 'realtek', 'google', 'android', 'unknown'}


def brand_aliases(r):
    out = []
    label = r.get('brand_label') or ''
    for key, values in BRAND_ALIASES.items():
        if key in label:
            out += values
    for v in (r.get('brand'), r.get('manufacturer')):
        if v and v.strip() and v.strip().lower() not in REFERENCE_BRANDS:
            out.append(v.strip())
    seen = []
    for v in out:
        if v.lower() not in [x.lower() for x in seen]:
            seen.append(v)
    return seen


GENERIC_MODELS = {'XGIMI TV', 'VIDAA_TV', 'MStar Android TV', 'Smart TV Pro', 'Smart TV', 'BeyondTV', 'BeyondTV2', 'Android TV'}
AERIAL_BLOB = 'https://github.com/pengchujin/aerial-tv/blob/719db23/'
LEVELS = dict(proven='网上已证（Aerial 同源守护实测，可一键）', limited='网上有限制（Aerial 实测部分可用，试装）', avoid='网上已知失败（不建议，可试装）')


def _text(v):
    if v is None:
        return ''
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        return '；'.join(f'{k}:{_text(x)}' for k, x in v.items() if x)
    if isinstance(v, list):
        return '；'.join(_text(x) for x in v if x)
    return str(v)


def aerial_profiles():
    """Field evidence from Aerial (same guard code as our client): exact model/device/SDK only."""
    import re
    ident = re.compile(r'^[A-Za-z0-9 _.+-]+$')
    data = json.loads((ROOT / 'source/aerial-models.json').read_text())
    out = []
    for r in data['records']:
        sdk = r.get('sdk')
        model = (r.get('model') or '').strip()
        device = (r.get('device') or '').strip()
        model = model if ident.match(model) else ''
        device = device if ident.match(device) else ''
        if not isinstance(sdk, int) or sdk < 21 or not (model or device):
            continue
        if model in GENERIC_MODELS and not device:
            continue
        status, conf, hint = r.get('status'), r.get('confidence'), r.get('route_hint')
        if status == 'ok' and conf in ('high', 'medium') and hint in ('standard', 'guard'):
            grade = 'proven'
        elif status == 'fail':
            grade = 'avoid'
        elif status in ('ok', 'limited'):
            grade = 'limited'
        else:
            continue
        route = 'guard' if hint == 'guard' or grade == 'avoid' else 'standard'
        prefix = (r.get('fingerprint_prefix') or '').strip()
        if ' ' in prefix or not prefix.count('/') >= 3:
            prefix = ''
        brands = brand_aliases(r)
        reference = prefix.lower().startswith(REFERENCE_BUILDS) or (r.get('brand') or '').strip().lower() in REFERENCE_BRANDS
        if grade == 'proven' and (not brands or reference):
            # One-click needs evidence that names the maker: no brand, or a chip vendor's
            # reference build whose identifiers other makers share → trial only.
            grade = 'limited'
        src = r.get('source') or []
        src = src if isinstance(src, list) else [src]
        evidence = next((s for s in src if str(s).startswith('http')), '')
        if not evidence and src:
            evidence = AERIAL_BLOB + str(src[0]).split(':')[0]
        parts = []
        if r.get('works'):
            parts.append('可用：' + _text(r['works']))
        if r.get('fails'):
            parts.append('限制/失败：' + _text(r['fails']))
        if r.get('extra_steps'):
            parts.append('需要：' + _text(r['extra_steps']))
        if r.get('later_status'):
            parts.append('后续：' + _text(r['later_status']))
        notes = (r.get('retail_name') or r['id']) + ' · ' + ' / '.join(parts)
        if len(notes) > 420:
            notes = notes[:417] + '…'
        out.append(dict(id='aerial-' + r['id'], name='Aerial 记录 · ' + (r.get('retail_name') or r['id']),
            match=dict(models=[model] if model else [], devices=[device] if device else [], brand='', brands=brands, sdk_min=sdk, sdk_max=sdk, fingerprint_prefix=prefix),
            route=route, grade=grade, evidence=evidence, tested=f"Aerial README {r.get('readme_status') or ''}；置信度 {conf}；属性来源 {r.get('id_source') or ''}",
            notes=notes, level=LEVELS[grade]))
    return out


def main():
    snapshot = ROOT / 'source/profile-meta.json'
    source = json.loads(snapshot.read_text())
    profiles = []
    for name, row in source['profiles'].items():
        m = row['match']
        tested = row.get('tested') or {}
        route = 'blocked' if row['platform'] == 'FIRE_OS' else 'guard' if row['platform'] == 'TCL_CN' else 'standard'
        notes = '参考项目有该机型记录；本工具保留原厂应用，以本客户端守护和实际 HOME 检查验收，不执行上游清理列表。'
        if route == 'blocked':
            notes = 'Fire OS 机型清理记录不证明本影院 HOME/开机/待机可用，本版暂不支持接管；Vega OS 不能运行 Android APK。'
        if route == 'guard':
            notes += ' 此类国行固件使用无 HOME 声明的 guard 安装包，必须取得真实 ADB 授权与无障碍权限。'
        if name.startswith(('freebox-', 'nvidia-shield-', 'onn-', 'xiaomi-mi-box-')):
            route = 'guard'
            notes = '2.0新增盒子受控试装：上游有精确机型记录，本影院尚未在此盒子实测。保留原厂桌面和系统应用，强制重启/HOME/稳定观察；失败自动恢复，实体待机/HDMI-CEC另验。'
        if name.startswith(('google-chromecast-', 'google-tv-streamer-')):
            route = 'blocked'
            notes = 'Google盒子：上游指出替换桌面可能影响投屏，本影院尚无保持投屏与完整接管的验证，本版完整接管暂不支持；可查连接方法。'
        profiles.append(dict(id=name, name=name, match=dict(models=m['models'], devices=m['devices'], brand=m['brand'],
            sdk_min=m.get('sdkMin') or 21, sdk_max=m.get('sdkMax') or 0, fingerprint_prefix=m.get('fingerprintPrefix') or ''), route=route,
            evidence=f'https://github.com/Gathin23/cleanyourtv/tree/{source["upstreamCommit"]}/examples/{name}',
            tested=json.dumps(tested, ensure_ascii=False), notes=notes, level='上游记录（本工具阻止）' if route == 'blocked' else '上游机型方法记录（试装级，影院未实测）'))
    profiles.insert(0, dict(id='hotel-xiaomi-mitv-astp0', name='本店小米 MiTV-ASTP0 / mulan',
        match=dict(models=['MiTV-ASTP0'], devices=['mulan'], brand='Xiaomi', sdk_min=28, sdk_max=28,
            fingerprint='Xiaomi/mulan/mulan:9/PPR1.180610.011/OS3.0.104.0.PSTAATV:user/release-keys'), route='standard',
        evidence='https://github.com/pengchujin/aerial-tv', tested='本项目PD05–PD07及AD10已验单台播放/维护/实体待机；便携工具整套新装仍待现场验收。',
        notes='客户端整合AD10广告及迟到唤醒修复；2026-10-29 用1.2.0在同固件新电视一次装成；1.3.0新流程（重启验证/卸载式还原）待现场复验。', level='本项目实测（一键安装级）'))
    profiles.append(dict(id='tcl-mt9655-no-adb-auth', name='雷鸟 55S595C Ultra / MT9655 授权受限固件',
        match=dict(models=['FF-CN-0015-S595CPRO-TX'], devices=['tcl_mt9655_cn'], brand='TCL', sdk_min=30, sdk_max=0),
        route='guard', grade='avoid', evidence='https://github.com/pengchujin/aerial-tv/blob/719db23/docs/devices/tcl-mt9655-cn.md',
        tested='上游记录：无线ADB强制TLS且无配对入口；用USB A对A线先走有线adb可取得授权（Aerial 后续补充）',
        notes='无线调试拿不到授权时用USB A对A线；设置里没有无障碍入口，Aerial 记为主页键接管不了。', level='网上已知失败（不建议，可试装）'))
    profiles.extend(aerial_profiles())
    for p in profiles:
        name = p['id'].lower()
        box = any(x in name for x in ('box', 'shield', 'chromecast', 'streamer', 'onn-', 'cm311', 'phicomm-t1', 'tvguo', 'tencent-aurora', 'skyworth-xiaopai', 'hk1-', 'fire-tv-stick', 'fire-tv-cube', 'mecool', 'homatics', 'tivo-', 'rocktek')) or '盒子' in p['name']
        p['device_kind'] = 'box' if box else 'projector' if any(x in name for x in ('projector', 'xgimi-', 'jmgo-', 'formovie-', 'fengmi-')) else 'tv'
        if box and p.get('grade') == 'proven':
            p['grade'] = 'limited'
            p['level'] = '上游有HOME方法；本影院盒子路线待实机（受控试装）'
        if name == 'aerial-tencent-aurora-6-q5043':
            p['route'] = 'blocked'
            p['grade'] = ''
            p['notes'] = 'Q5043固件的settings命令会卡adbd约120秒；当前安装器必须读取settings备份，因此暂不支持，不能沿用普通盒子路线。'
    platforms = [
        dict(name='Android 4.0 / API14–15',brands='按实体型号另验，不按品牌放行',status='实验版已随包；正式自动接管暂不支持',reason='API14页面及API15播放/广告/HOME等模拟器证据已附；原生字幕样本失败，实体开机待机未验。支持页可离线获取完整实验包。',sources=['https://github.com/BeatsAD/quiet-tv/releases/tag/v1.3.0']),
        dict(name='Android 4.4 / API19',brands='按实体型号另验，不按品牌放行',status='候选版已随包；实体型号待验',reason='API19模拟器业务、standard HOME/复位及guard HOME已验；ARM实体电视、待机和最新共同广告版仍待整合验收。',sources=['https://github.com/BeatsAD/quiet-tv']),
        dict(name='Android TV / Google TV / AOSP（Android 5+）', brands='小米、Redmi、TCL、雷鸟、索尼、海信、创维、康佳、极米、飞利浦、海尔等', status='本项目实测与网上已证（Aerial）型号一键安装；其他型号勾选“允许试装”；网上已知失败的型号“不建议”但可试装', reason='分级：本项目实测（固件指纹）/网上已证（Aerial 同源守护、精确型号代码）/试装（上游清理记录或无记录）/不建议（Aerial 实测失败）。非本项目实测的一律强制重启验证+2分钟稳定观察，失败自动还原。Android 10+文件级加密的非本项目实测型号只用guard版；Android 5–6只用guard版、不改默认桌面。', sources=['https://github.com/pengchujin/aerial-tv','https://github.com/Gathin23/cleanyourtv','https://github.com/udhaykumarbala/cleanyourtv-app','https://github.com/wukongdaily/tvhelper']),
        dict(name='Fire OS', brands='Amazon、部分TCL/东芝/海信等Fire TV型号', status='本影院接管暂不支持', reason='部分版本可安装Android APK，HOME限制随固件变化；清理或侧载记录不能证明影院开机/待机可用。', sources=['https://github.com/CVasilakis/luncher','https://github.com/Gathin23/cleanyourtv']),
        dict(name='Tizen', brands='三星（按型号/固件分别核验）', status='已有原生工具；本影院客户端暂不支持', reason='quiet-tizen仅在U8000F特定固件实测；原生NUI与Android不是同一运行时，需要保持现有界面的独立原生适配。SDB/私有框架不随本版分发。', sources=['https://github.com/jss8649/quiet-tizen','https://github.com/chris-ritsen/samsung-tv-root','https://github.com/reisxd/TizenBrew','https://github.com/reisxd/TizenBrewInstaller','https://github.com/Apps2Samsung/tizen-community-packages']),
        dict(name='webOS', brands='LG及搭载webOS的授权品牌', status='已有原生侧载/管理工具；本影院暂不支持', reason='Dev Manager/ares-cli可安装IPK，Homebrew路径受固件限制；仍需本影院原生客户端、HOME/开机与媒体适配。', sources=['https://github.com/webosbrew/dev-manager-desktop','https://github.com/webosose/ares-cli','https://github.com/RootMyTV/RootMyTV.github.io','https://github.com/lasharor/cleanyourtv-WebOS']),
        dict(name='VIDAA OS', brands='部分海信、东芝、夏普等；与名字含VIDAA的Android固件区分', status='本影院原生安装暂不支持', reason='有Web应用侧载路径，但固件存在安装返回成功却无图标的情况，且不是现有Android客户端。需另做本地客户端及完整验收。', sources=['https://github.com/Stremio/stremio-hisense-install','https://github.com/kimkha/stremio']),
        dict(name='Roku OS', brands='Roku及采用Roku的TCL/Hisense/Sharp等', status='可开发原生频道；本影院暂不支持', reason='开发者模式可侧载BrightScript/SceneGraph频道；不是Android运行时，需要另做客户端并验证启动/HOME限制。', sources=['https://github.com/rokudev/samples','https://github.com/paramount-engineering/roku-dev-studio']),
        dict(name='HarmonyOS / Honor封闭系统', brands='华为、荣耀（具体系统分别识别）', status='暂不支持', reason='本轮未建立可重复、保持现有影院功能的完整原生安装与桌面接管路径；不是按品牌一概推断。', sources=['https://github.com/pengchujin/aerial-tv/blob/main/docs/devices/adb-by-brand.md']),
        dict(name='Vega / Titan / SAPHI / 其他封闭平台', brands='部分新Fire TV、Philips及其他OEM型号', status='暂不支持，待逐平台研究与实机验证', reason='不假定兼容Android APK；本轮没有完成保持现有影院界面/功能的原生客户端和现场验证。', sources=['https://github.com/CVasilakis/luncher']),
    ]
    # Field knowledge without exact identifiers (Aerial README / docs/devices, same guard code as our client).
    advisories = json.loads((ROOT / 'source/aerial-advisories.json').read_text())
    out=dict(schema=1,version='2026-11-03.2.0',profiles=profiles,platforms=platforms,advisories=advisories)
    (ROOT / 'catalog.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
    with (ROOT / '机型支持清单.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.writer(f,lineterminator="\n");writer.writerow(['品牌','机型记录','型号','设备代号','SDK下限','SDK上限','安装路径','验证级别','限制','来源'])
        for p in profiles:
            m=p['match'];writer.writerow([m['brand'],p['name'],' / '.join(m['models']),' / '.join(m['devices']),m['sdk_min'],m['sdk_max'],p['route'],p['level'],p['notes'],p['evidence']])
    notices=ROOT / 'licenses';notices.mkdir(exist_ok=True)
    if not (notices / 'CleanYourTV-App-MIT.txt').exists():
        shutil.copyfile(UPSTREAM / 'LICENSE',notices / 'CleanYourTV-App-MIT.txt')
    commit='8ec0679f0a3b04cbf1a23d7762f68b35679f8b59'
    provenance=dict(repository='udhaykumarbala/cleanyourtv-app', source_commit=commit, metadata_blob_sha='c2032edf086c5cad595ca30e7298dc85eb19c71e', source_metadata_sha256=hashlib.sha256(snapshot.read_bytes()).hexdigest(), upstream_commit=source['upstreamCommit'], date='2026-11-03', imported_profiles=len(source['profiles']))
    (ROOT / 'profile-provenance.json').write_text(json.dumps(provenance,ensure_ascii=False,indent=2)+'\n')
    (notices / '数据来源.txt').write_text(f'机型元数据：udhaykumarbala/cleanyourtv-app\n线上核对提交：{commit}\nprofile-meta.json标记的上游提交：{source["upstreamCommit"]}\n仅提取品牌、型号、SDK、固件指纹限制与验证元数据；不打包清理列表或执行脚本。\n')
    print(f'{len(profiles)} profiles, {len({p["match"]["brand"].lower() for p in profiles})} normalized brands')


if __name__ == '__main__':
    main()
