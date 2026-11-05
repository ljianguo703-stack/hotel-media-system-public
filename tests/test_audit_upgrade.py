import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest
from contextlib import contextmanager
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
m=load('audit_upgrade',ROOT/'deploy/audit_upgrade.py')
quality=load('media_quality',ROOT/'deploy/media_quality.py')

class AuditUpgradeTests(unittest.TestCase):
    def test_native_rule_backups_use_root_and_api_calls_use_service_identity(self):
        sys.path.insert(0,str(ROOT/'deploy'))
        import bootstrap
        active=[False];writes=[];calls=[]
        @contextmanager
        def service():
            self.assertFalse(active[0]);active[0]=True
            try:yield
            finally:active[0]=False
        def native(config,app,route,body=None,method=None):
            self.assertTrue(active[0]);calls.append((app,route,method))
            if body is not None:return body
            return [{'id':4,'items':[{'quality':{'name':'DVD','resolution':480}}]}] if route.endswith('qualityprofile') else {'minimumFreeSpaceWhenImporting':0}
        def save(path,value):
            self.assertFalse(active[0]);writes.append(path.name)
        store=type('Store',(),{})();store._native=native
        with patch.object(bootstrap,'service_identity',service),patch.object(bootstrap,'private_json',save):
            m.update_native_rules(store,{'radarr':{'profile':4},'sonarr':{'profile':4}},Path('/root-only-backup'))
        self.assertEqual(len(writes),4);self.assertEqual(sum(method=='PUT' for _,_,method in calls),4)

    def test_plan_is_read_only_without_linux_root_docker_or_credentials(self):
        reply=subprocess.check_output([sys.executable,str(ROOT/'deploy/audit_upgrade.py'),'--rotate-services'],text=True)
        plan=json.loads(reply);self.assertEqual(plan['mode'],'plan');self.assertTrue(plan['optional_independent_passwords'])

    def test_loopback_migration_preserves_volumes_and_unrelated_ports_and_never_mutates_input(self):
        for mapping in ('8096:8096','0.0.0.0:8096:8096',{'published':8096,'target':8096}):
            original={'services':{'jellyfin':{'ports':[mapping,'7359:7359/udp'],'volumes':['/owner/media:/media'],'environment':{'TZ':'Asia/Shanghai'}}}}
            saved=copy.deepcopy(original);updated=m.loopback_compose(original,'jellyfin',8096,8096)
            self.assertEqual(original,saved);self.assertEqual(updated['services']['jellyfin']['ports'],['127.0.0.1:8096:8096','7359:7359/udp'])
            self.assertEqual(updated['services']['jellyfin']['volumes'],['/owner/media:/media'])
        with self.assertRaises(RuntimeError):m.loopback_compose({'services':{'jellyfin':{'ports':['8100:8096']}}},'jellyfin',8096,8096)

    def test_quality_groups_keep_sd_hd_and_exclude_uhd_remux_without_losing_profile_identity(self):
        profile={'id':4,'cutoff':7,'formatItems':[{'id':9,'score':-10}],'items':[{'quality':{'name':'DVD','resolution':480}},
            {'quality':None,'items':[{'quality':{'name':'Bluray-1080p','resolution':1080}},{'quality':{'name':'Bluray-2160p','resolution':2160}}]},
            {'quality':{'name':'Remux-1080p','resolution':1080}}]}
        quality.allow_library_qualities(profile)
        self.assertEqual([i['allowed'] for i in profile['items']],[True,True,False])
        self.assertEqual([i['allowed'] for i in profile['items'][1]['items']],[True,False])
        self.assertEqual((profile['id'],profile['cutoff'],profile['formatItems']),(4,7,[{'id':9,'score':-10}]))
