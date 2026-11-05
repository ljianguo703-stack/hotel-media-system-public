import json
import subprocess
import unittest
from unittest.mock import Mock, patch
from physical_storage import parse_inventory, snapshot


class PhysicalStorageTest(unittest.TestCase):
    def test_inventory_preserves_unmounted_existing_data_and_identifies_system_disk(self):
        disks = parse_inventory({'blockdevices':[
            {'name':'loop0','type':'loop','size':100},
            {'name':'sda','path':'/dev/sda','type':'disk','size':16000900661248,'serial':'A'},
            {'name':'sdc','path':'/dev/sdc','type':'disk','size':16000900661248,'children':[
                {'name':'sdc1','type':'part','size':1024},
                {'name':'sdc5','type':'part','size':2199021158400,'fstype':'ntfs','label':'软件','uuid':'old-data','ro':False}]},
            {'name':'nvme0n1','type':'disk','children':[{'name':'nvme0n1p2','type':'part','mountpoint':'/','fstype':'ext4'}]}
        ]})
        self.assertEqual(len(disks),3); self.assertFalse(disks[0]['mounted']); self.assertFalse(disks[0]['system'])
        self.assertEqual((disks[1]['partitions'][1]['label'],disks[1]['partitions'][1]['uuid']),('软件','old-data'))
        self.assertTrue(disks[2]['system']); self.assertTrue(disks[2]['mounted'])

    def test_probe_is_readonly_has_timeout_and_does_not_use_a_shell(self):
        with patch('physical_storage.platform.system',return_value='Linux'), patch('physical_storage.subprocess.run',return_value=Mock(stdout=b'{"blockdevices":[]}')) as run:
            self.assertEqual(snapshot()['status'],'ok')
            args,kw=run.call_args
            self.assertEqual(args[0][0],'lsblk');self.assertEqual(kw['timeout'],3);self.assertNotIn('shell',kw)

    def test_probe_failure_and_invalid_schema_are_visible(self):
        for failure in (subprocess.TimeoutExpired('lsblk',3),FileNotFoundError(),ValueError()):
            with patch('physical_storage.platform.system',return_value='Linux'),patch('physical_storage.subprocess.run',side_effect=failure):
                self.assertEqual(snapshot()['status'],'unavailable')
        with self.assertRaises(ValueError):parse_inventory([])

    def test_nonlinux_does_not_invoke_disk_commands(self):
        with patch('physical_storage.platform.system',return_value='Darwin'),patch('physical_storage.subprocess.run') as run:
            self.assertEqual(snapshot()['status'],'unsupported');run.assert_not_called()


if __name__=='__main__':unittest.main()
