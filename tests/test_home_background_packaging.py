import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def module(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


class BackgroundPackaging(unittest.TestCase):
    def test_fresh_install_payload_has_images_and_can_initialize_without_workstation_paths(self):
        bootstrap = module('background_bootstrap', 'deploy/bootstrap.py')
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp)
            bootstrap.prepare_seed(stage, {'hotel': '背景测试', 'tv_token': 'test-only-device-token'})
            for image in (ROOT / 'admin/home-backgrounds').glob('*.png'):
                self.assertEqual((stage / 'admin/home-backgrounds' / image.name).read_bytes(), image.read_bytes())
            script = '''from pathlib import Path
import json
from management import Store
root=Path.cwd()
s=Store(root/'seed',root/'seed/media',root/'seed/devices.json')
with s.db() as db:
    bg=s.home_settings(db)['background']
assert bg.startswith('/media/_home/defaults/')
assert (root/'seed/media'/bg[len('/media/'):]).is_file()
print('isolated fresh-install background PASS')
'''
            result = subprocess.run([sys.executable, '-c', script], cwd=stage,
                                    env={'PATH': '/usr/bin:/bin', 'PYTHONPATH': str(stage / 'server'), 'PYTHONDONTWRITEBYTECODE': '1'}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_normal_software_package_and_inspector_include_the_four_images(self):
        package = module('background_package', 'deploy/software/package.py')
        sys.path.insert(0, str(ROOT / 'server'))
        import software_packages
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'backgrounds.zip'
            package.build(ROOT, target, 'background-check-new', 'background-check-old')
            with zipfile.ZipFile(target) as z:
                names = z.namelist()
                images = [n for n in names if n.startswith('admin/home-backgrounds/') and n.endswith('.png')]
                self.assertEqual(len(images), 4)
                self.assertNotIn('server/test_home_backgrounds.py', names)
                manifest = json.loads(z.read('hotel-software.json'))
                self.assertTrue(set(images) <= manifest['files'].keys())
            result = software_packages.inspect(target, target.name)
            self.assertEqual(result['kind'], 'system')


if __name__ == '__main__':
    unittest.main()
