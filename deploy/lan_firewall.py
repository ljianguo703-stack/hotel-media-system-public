#!/usr/bin/env python3
"""Native nftables policy for this application's ports, including Docker forwarding."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

DEFAULT_NETWORKS = ['10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16']
CORE_TCP = [9090, 18790, 18791]
FULL_TCP = [139, 445, 5001, 5055, 6767, 6801, 7878, 8095, 8096, 8098, 8989, 9090, 9117, 9696, 18790, 18791]
TABLE = 'hotel_tv_lan'
SERVICE = 'hotel-tv-lan-guard.service'


def networks(values):
    result = []
    for value in values:
        net = ipaddress.ip_network(value, strict=False)
        if net.version != 4 or not any(net.subnet_of(ipaddress.ip_network(cidr)) for cidr in DEFAULT_NETWORKS):
            raise ValueError('允许网段须为酒店私有IPv4网段')
        if str(net) not in result:
            result.append(str(net))
    if not result:
        raise ValueError('至少需要一个酒店私有网段')
    return result


def render(profile, allowed):
    allowed = networks(allowed)
    if profile not in ('core', 'full'):
        raise ValueError('无效安装profile')
    tcp = CORE_TCP if profile == 'core' else FULL_TCP
    udp = [] if profile == 'core' else [137, 138]
    permitted = ', '.join(allowed)
    text = f'table inet {TABLE} {{\n'
    for chain, hook in [('host_input', 'input'), ('container_forward', 'forward')]:
        text += f' chain {chain} {{\n  type filter hook {hook} priority -5; policy accept;\n'
        text += '  iifname "lo" return\n  ct state established,related return\n'
        for protocol, ports in [('tcp', tcp), ('udp', udp)]:
            if not ports:
                continue
            port_list = ', '.join(map(str, ports))
            port_match = f'{protocol} dport' if hook == 'input' else f'meta l4proto {protocol} ct original proto-dst'
            # Original destination ports cover published ports before Docker's DNAT.
            text += f'  meta nfproto ipv4 {port_match} {{ {port_list} }} ip saddr != {{ {permitted} }} drop\n'
            text += f'  meta nfproto ipv6 {port_match} {{ {port_list} }} drop\n'
        text += ' }\n'
    return text + '}\n'


def apply(config):
    profile, allowed = config['profile'], networks(config['allowed_networks'])
    rules = render(profile, allowed)
    existing = subprocess.run(['nft', 'list', 'table', 'inet', TABLE], capture_output=True).returncode == 0
    transaction = (f'delete table inet {TABLE}\n' if existing else '') + rules
    subprocess.run(['nft', '--check', '-f', '-'], input=transaction, text=True, check=True)
    subprocess.run(['nft', '-f', '-'], input=transaction, text=True, check=True)


def install(state, private_json, run, config_root=Path('/etc/hotel-tv'), unit_root=Path('/etc/systemd/system'), program_root=Path('/usr/local/lib/hotel-tv-setup')):
    config = {'schema': 1, 'profile': state['profile'], 'allowed_networks': networks(state.get('lan_cidrs', DEFAULT_NETWORKS))}
    config_path = config_root / 'network-policy.json'
    private_json(config_path, config)
    program_root.mkdir(parents=True, exist_ok=True)
    helper = program_root / 'lan_firewall.py'
    shutil.copyfile(Path(__file__).resolve(), helper)
    helper.chmod(0o644)
    unit = unit_root / SERVICE
    unit.write_text('[Unit]\nDescription=Hotel application LAN ports\nAfter=local-fs.target\nBefore=docker.service hotel-tv.service\n\n'
                    '[Service]\nType=oneshot\nRemainAfterExit=yes\n'
                    f'ExecStart=/usr/bin/python3 {helper} --config {config_path}\n\n[Install]\nWantedBy=multi-user.target\n')
    unit.chmod(0o644)
    # Do not reset or enable/disable UFW, and never change SSH rules. If it is already
    # active, add only the application's scoped LAN allowances alongside its policy.
    ufw_active = False
    if shutil.which('ufw'):
        status = subprocess.run(['ufw', 'status'], capture_output=True, text=True, env={**os.environ, 'LC_ALL': 'C'})
        if status.returncode:
            raise RuntimeError('无法读取既有UFW规则，未假定端口已经放行')
        ufw_active = 'Status: active' in status.stdout
        if ufw_active:
            tcp = CORE_TCP if state['profile'] == 'core' else FULL_TCP
            for cidr in config['allowed_networks']:
                for start in range(0, len(tcp), 15):
                    run(['ufw', 'allow', 'from', cidr, 'to', 'any', 'port', ','.join(map(str, tcp[start:start+15])), 'proto', 'tcp', 'comment', 'hotel-tv'])
                if state['profile'] == 'full':
                    run(['ufw', 'allow', 'from', cidr, 'to', 'any', 'port', '137,138', 'proto', 'udp', 'comment', 'hotel-tv'])
    run(['systemctl', 'daemon-reload'])
    run(['systemctl', 'enable', SERVICE])
    run(['systemctl', 'restart', SERVICE])
    private_json(config_root / 'network-report.json', {**config, 'tcp_ports': CORE_TCP if state['profile'] == 'core' else FULL_TCP,
                 'udp_ports': [] if state['profile'] == 'core' else [137, 138], 'service': SERVICE,
                 'host_and_container_forwarding': True, 'existing_ufw_active': ufw_active,
                 'ssh_installed_or_reconfigured': False, 'ipv6_application_ports': 'blocked; this installer configures IPv4'})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('端口规则需要root权限')
    apply(json.loads(args.config.read_text()))
