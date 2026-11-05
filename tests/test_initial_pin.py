import contextlib,hashlib,importlib.util,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('pin_bootstrap',ROOT/'deploy/bootstrap.py');b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)
class InitialPinTests(unittest.TestCase):
    def test_first_pin_matches_private_summary_and_resume_preserves_owner_change(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(b,'DATA',Path(tmp)),patch.object(b,'service_identity',contextlib.nullcontext):
            b.initialize_pin({'staff_pin':'593284'});p=Path(tmp)/'maintenance-pin.json';first=json.loads(p.read_text())
            self.assertEqual(first['sha256'],hashlib.sha256(b'593284').hexdigest());self.assertNotIn('593284',p.read_text());self.assertEqual(p.stat().st_mode&0o777,0o600)
            b.initialize_pin({'staff_pin':'123456'});self.assertEqual(json.loads(p.read_text()),first)
    def test_incomplete_first_install_payload_is_rejected_before_host_changes(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(b,'SOURCE',Path(tmp)),patch.object(b,'run') as command:
            with self.assertRaises(RuntimeError):b.validate_source('full')
            command.assert_not_called()
