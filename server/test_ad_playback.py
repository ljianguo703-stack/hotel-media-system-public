"""AD05 real HTTP and persistence: rules, sponsor cards, protocol fallback and manual industry data."""
import json
import unittest
import test_management as fixture
import test_orders
from management import Store

class AdPlaybackE2E(unittest.TestCase):
    setUp = fixture.ManagementE2E.setUp
    tearDown = fixture.ManagementE2E.tearDown
    request = fixture.ManagementE2E.request
    state = fixture.ManagementE2E.state
    mutate = fixture.ManagementE2E.mutate
    campaign = fixture.ManagementE2E.campaign
    op = test_orders.OrdersE2E.op
    login_as = test_orders.OrdersE2E.login_as

    def manifest(self, protocol=2):
        return self.request('/api/v1/ads',tv=True,headers={'X-TV-Ad-Protocol':str(protocol)})[2]

    def test_rules_are_validated_versioned_and_persist_across_restart(self):
        rules=dict(pause_repeat=False,enabled=True,midroll_percent=55,midroll_delay_seconds=180,text_item_seconds=10,rotate_group_start=False,sponsor_ids=[],pause_max_minutes=0)
        revision=self.state()['revision']
        self.assertEqual(self.op('ad_control','ad_playback',rules=rules)[0],200)
        self.assertEqual(self.state()['revision'],revision+1)
        self.assertEqual(self.manifest()['playback']['midroll_percent'],55)
        self.assertNotIn('playback',self.manifest(1))
        for bad in ({'midroll_percent':100},{'midroll_delay_seconds':179},{'text_item_seconds':True},{'pause_max_minutes':121},{'sponsor_ids':['missing']},{'enabled':'yes'}):
            before=self.state()['revision']
            self.assertEqual(self.op('ad_control','ad_playback',rules={**rules,**bad})[0],400)
            self.assertEqual(self.state()['revision'],before)
        stale=self.request('/api/admin/operations',dict(kind='ad_control',action='ad_playback',rules=rules,revision=0),auth=True)
        self.assertEqual(stale[0],409)
        reopened=Store(self.data,self.media,self.config,[self.media,self.external])
        with reopened.db() as db:self.assertEqual(reopened.playback_rules(db),rules)

    def test_roles_and_manual_industry_no_unique_industry_or_three_sponsor_limit(self):
        for i in range(4):
            self.assertEqual(self.op('advertiser','save',item={'id':'a'+str(i),'name':'商户'+str(i),'industry':'餐饮'})[0],200)
        self.assertEqual(len([a for a in self.state()['advertisers'] if a.get('industry')=='餐饮']),4)
        operator=self.login_as('ad-operator','operator');viewer=self.login_as('ad-viewer','viewer')
        for headers in (operator,viewer):self.assertEqual(self.op('ad_control','ad_playback',headers=headers,rules={})[0],403)

    def test_card_local_qr_and_rotation_fields_are_saved_and_only_new_clients_receive_it(self):
        card={'title':'赞助商','body':'欢迎光临','contact':'电话 123456','qr_link':'https://example.com/contact','accent':'#123456'}
        body=self.campaign(name='图文广告',merchant='餐饮商户',tier=1,format='图文',text='',placements=['home','preroll'],creative_card=card,rotation_order=12)
        self.assertEqual(self.mutate('/campaigns/card',body)[0],200)
        new=self.manifest();ads=[a for a in new['ads'] if a['campaign_id']=='card']
        self.assertEqual(len(ads),2);self.assertTrue(all(a['kind']=='card' and a['rotation_order']==12 for a in ads))
        self.assertTrue(ads[0]['merchant_id']);qr=ads[0]['card']['qr'];self.assertTrue(qr.startswith('/media/'))
        status,_,raw=self.request(qr,tv=True,raw=True);self.assertEqual(status,200);self.assertTrue(raw.startswith(b'\x89PNG'))
        self.assertEqual(self.op('asset','save',item={'id':'card-qr','name':'图文二维码','path':qr,'enabled':True})[0],200)
        asset=next(a for a in self.state()['assets'] if a['id']=='card-qr')
        self.assertEqual(asset['references'],1)
        self.assertFalse(any(a['campaign_id']=='card' for a in self.manifest(1)['ads']))
        self.assertEqual(self.mutate('/campaigns/card',{**body,'rotation_enabled':False})[0],200)
        self.assertFalse(next(a for a in self.manifest()['ads'] if a['campaign_id']=='card')['rotation_enabled'])
        for bad in ({'qr_link':'javascript:alert(1)'},{'qr_link':'https://example.com/'+'x'*220},{'accent':'red'},{'image':'http://example.com/a.png'},{'qr':'/media/missing.png'}):
            self.assertIn(self.mutate('/campaigns/card',{**body,'creative_card':{**card,**bad}})[0],(400,404))

    def test_merchant_disable_removes_published_ads_without_mutating_campaign_and_stop_still_wins(self):
        body=self.campaign(name='商家文字',merchant='赞助商',tier=2,format='文字',text='商家信息',placements=['all'])
        self.assertEqual(self.mutate('/campaigns/text',body)[0],200)
        a=next(a for a in self.state()['advertisers'] if a['name']=='赞助商')
        self.assertEqual(self.op('advertiser','disable',ids=[a['id']])[2]['ok'],[a['id']])
        self.assertFalse(any(c['campaign_id']=='text' for c in self.manifest()['ads']))
        self.assertEqual(next(c for c in self.state()['campaigns'] if c['id']=='text')['status'],'投放中')
        self.op('advertiser','enable',ids=[a['id']]);self.op('ad_control','ad_control',stopped=True)
        self.assertFalse(any(c['campaign_id']=='text' for c in self.manifest()['ads']))

if __name__=='__main__':unittest.main()
