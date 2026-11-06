#!/usr/bin/env python3
"""Export and verify one immutable dependency, then publish its OCI archive to Release."""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
RELEASE = 'IN19-dependencies-20261107'
REPOSITORY = 'ljianguo703-stack/hotel-media-system-public'


def constant(relative, name):
    tree = ast.parse((ROOT / relative).read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError('Missing dependency constant ' + name)


def references():
    refs = json.loads((ROOT / 'deploy/images.lock.json').read_text())
    refs['alpine'] = constant('deploy/nas/smart_download.py', 'ALPINE')
    refs['jackett'] = constant('deploy/nas/download-tools/jackett.py', 'IMAGE')
    refs['nginx'] = constant('deploy/nas/download-tools/install.py', 'IMAGE')
    refs['aria2'] = 'hotel-aria2:1.37'
    return refs


def run(args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def build_aria2():
    base = references()['alpine']
    with tempfile.TemporaryDirectory(prefix='hotel-aria2-') as directory:
        path = Path(directory)
        dockerfile = f'FROM {base}\nRUN apk add --no-cache aria2 && aria2c --version | head -1\nCMD ["aria2c", "--conf-path=/config/aria2.conf"]\n'
        (path / 'Dockerfile').write_text(dockerfile)
        run(['docker', 'build', '--platform', 'linux/amd64', '--provenance=false', '-t', 'hotel-aria2:1.37', path])
    version = subprocess.check_output(['docker', 'run', '--rm', '--network', 'none', '--read-only',
                                      'hotel-aria2:1.37', 'aria2c', '--version'], text=True)
    if not version.startswith('aria2 version 1.37.0\n'):
        raise RuntimeError('aria2 build version differs from the installer')


def main():
    refs = references()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('name', choices=refs)
    parser.add_argument('--output', type=Path, default=Path('dependency-output'))
    parser.add_argument('--no-upload', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    reference = refs[args.name]
    path = args.output / (args.name + '-IN19.oci.tar')
    if args.name == 'aria2':
        build_aria2()
        run(['docker', 'save', '-o', path, reference])
    else:
        repository = reference.split('@')[0]
        if ':' in repository.split('/')[-1]:
            repository = repository.rsplit(':', 1)[0]
        run(['regctl', 'image', 'export', '--name', repository + ':hotel-in19-dependency', reference, path])
    with tarfile.open(path) as archive:
        descriptor = json.load(archive.extractfile('index.json'))['manifests'][0]
        manifest = archive.extractfile('blobs/' + descriptor['digest'].replace(':', '/')).read()
        if 'sha256:' + hashlib.sha256(manifest).hexdigest() != descriptor['digest']:
            raise RuntimeError('OCI root digest does not match its content')
        if '@' in reference and descriptor['digest'] != reference.split('@')[1]:
            raise RuntimeError('OCI root differs from the installer pin')
    run(['docker', 'load', '--platform', 'linux/amd64', '--input', path])
    info = json.loads(subprocess.check_output(['docker', 'image', 'inspect', '--platform', 'linux/amd64', reference], text=True))[0]
    if info['Os'] != 'linux' or info['Architecture'] != 'amd64':
        raise RuntimeError('Imported dependency has the wrong architecture')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(block)
    if path.stat().st_size >= 2 * 1024**3:
        raise RuntimeError('Archive exceeds the GitHub Release per-file limit')
    record = {'name': args.name, 'file': path.name, 'reference': reference, 'image_id': info['Id'],
              'root_digest': descriptor['digest'], 'sha256': digest.hexdigest(), 'bytes': path.stat().st_size,
              'repo_digests': info['RepoDigests'], 'source_commit': os.environ.get('GITHUB_SHA', 'local'),
              'licenses': info['Config'].get('Labels') or {}}
    metadata = args.output / ('image-' + args.name + '.json')
    metadata.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(record, ensure_ascii=False))
    if not args.no_upload:
        run(['gh', 'release', 'upload', RELEASE, path, metadata, '--repo', REPOSITORY, '--clobber'])


if __name__ == '__main__':
    main()
