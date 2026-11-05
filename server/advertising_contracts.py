"""AD06: reusable package capabilities and immutable order snapshots."""
import copy

PLACEMENTS = {1: ('home', 'preroll', 'midroll', 'pause', 'postroll', 'channel'), 2: ('all', 'home', 'playback'), 3: ('all', 'home', 'playback')}

def core():
    import management
    return management

def package_capabilities(raw, previous=None):
    m = core(); old = previous or {}; tier = raw['tier']
    tiers = raw.get('tiers', old.get('tiers', [tier]) if old.get('tier') == tier else [tier])
    if not isinstance(tiers, list) or not tiers or any(type(t) is not int or t < tier or t > 3 for t in tiers) or len(set(tiers)) != len(tiers) or tier not in tiers:
        raise m.Problem('套餐只能包含本档及以下展示方式；二、三档不能包含全屏')
    maps = raw.get('placements_by_tier', old.get('placements_by_tier', {}))
    if not isinstance(maps, dict): raise m.Problem('套餐展示位置无效')
    result = {}
    for t in sorted(tiers):
        values = raw['placements'] if t == tier else maps.get(str(t), ['all'])
        if not isinstance(values, list) or not values or any(not isinstance(p, str) for p in values) or len(set(values)) != len(values) or not set(values) <= set(PLACEMENTS[t]):
            raise m.Problem('请选择各展示方式允许的位置')
        result[str(t)] = list(values)
    return {'tiers': sorted(tiers), 'placements_by_tier': result,
            'default_ad_seconds': m.number(raw.get('default_ad_seconds', old.get('default_ad_seconds', 15)), 1, 120)}

def package_snapshot(package):
    return copy.deepcopy({k: package[k] for k in ('id', 'name', 'tier', 'tiers', 'placements', 'placements_by_tier', 'default_days', 'default_ad_seconds') if k in package})

def contract(order, packages):
    return order.get('package_snapshot') or package_snapshot(packages.get(order.get('package_id'), {}))

def targets(raw, tier, primary, old=None):
    m = core(); supplied = raw if raw is not None else old or {}
    if not isinstance(supplied, dict) or any(k not in ('1', '2', '3') for k in supplied): raise m.Problem('分档交付目标无效')
    result = {k: m.number(supplied.get(k, 0), 0, 1000000000) for k in ('1', '2', '3')}
    result[str(tier)] = primary
    return result
