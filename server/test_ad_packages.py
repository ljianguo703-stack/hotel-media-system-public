"""AD06 HTTP regression: capabilities, immutable contracts and unlike delivery units."""
import time
import unittest
import test_orders as f

class PackageE2E(unittest.TestCase):
    setUp=f.OrdersE2E.setUp; tearDown=f.OrdersE2E.tearDown
    request=f.OrdersE2E.request; state=f.OrdersE2E.state; mutate=f.OrdersE2E.mutate
    campaign=f.OrdersE2E.campaign; op=f.OrdersE2E.op; base=f.OrdersE2E.base
    order=f.OrdersE2E.order; orders=f.OrdersE2E.orders; report=f.OrdersE2E.report

    def package(self, **kw):
        return dict(id='combo',name='组合全屏套餐',tier=1,tiers=[1,2,3],placements=['home','preroll'],placements_by_tier={'2':['all'],'3':['all']},target=20,**kw)

    def test_tier_inheritance_and_snapshot_survive_template_edit(self):
        self.base()
        pkg=self.package()
        self.assertEqual(self.op('package','save',item=pkg)[0],200)
        self.assertEqual(self.op('package','save',item={**pkg,'tier':2,'placements':['all']})[0],400)
        item=self.order(package_id='combo',campaign_ids=['ad-night'],target=20)
        self.assertEqual(self.op('order','save',item=item)[0],200)
        self.assertEqual(self.op('package','save',item={**pkg,'name':'仅全屏新模板','tiers':[1]})[0],200)
        row=self.orders()['rows'][0]
        self.assertEqual(row['package_name'],'组合全屏套餐'); self.assertEqual(row['tiers'],[1,2,3])
        self.assertEqual(self.op('order','save',item={**item,'notes':'只改备注'})[0],200)
        self.assertEqual(self.orders()['rows'][0]['tiers'],[1,2,3])
        self.assertEqual(self.op('order','save',item={**item,'reapply_template':True})[0],400)
        self.assertEqual(self.op('order','save',item={**item,'reapply_template':True,'campaign_ids':[]})[0],200)
        self.assertEqual(self.orders()['rows'][0]['tiers'],[1])

    def test_units_are_not_added_and_wrong_merchant_or_placement_rejected(self):
        self.base(); self.op('package','save',item=self.package())
        self.mutate('/campaigns/full',self.campaign(tier=1,format='图片',placements=['home'],status='草稿',path=''))
        item=self.order(package_id='combo',campaign_ids=['full','ad-night'],target=20,targets={'1':20,'2':0,'3':60})
        self.assertEqual(self.op('order','save',item=item)[0],200)
        now=int(time.time()*1000)
        self.store.record_events({'id':'room-101','room':'101'},[
            {'id':'f1','type':'ad_started','creative_id':'full~home','at_ms':now},
            {'id':'f2','type':'ad_completed','creative_id':'full~home','at_ms':now},
            {'id':'t1','type':'text_started','creative_id':'ad-night~all','at_ms':now},
            {'id':'t2','type':'text_ended','creative_id':'ad-night~all','visible_ms':90*60000,'at_ms':now}])
        row=self.orders()['rows'][0]; self.assertEqual(row['progress']['actual'],1)
        by={p['tier']:p for p in row['progress_by_tier']}
        self.assertEqual((by[1]['actual'],by[3]['actual'],by[1]['devices']),(1,90,1))
        total=self.report(advertiser='adv-1')[2]['totals']
        self.assertEqual((total['fullscreen_starts'],total['fullscreen_completes'],total['text_starts'],total['visible_minutes']),(1,1,1,90))
        self.store.record_events({'id':'failed-only','room':'102'},[{'id':'bad-only','type':'ad_failed','creative_id':'full~home','at_ms':now,'reason':'startup_timeout'}])
        row=self.orders()['rows'][0]
        self.assertEqual(row['progress_by_tier'][0]['devices'],1)
        self.assertEqual(self.report(advertiser='adv-1')[2]['totals']['rooms'],1)
        self.mutate('/campaigns/other',self.campaign(merchant='另一商户'))
        self.assertEqual(self.op('order','save',item={**item,'campaign_ids':['other']})[0],400)
        self.mutate('/campaigns/wrong',self.campaign(tier=1,format='图片',placements=['postroll'],status='草稿',path=''))
        self.assertEqual(self.op('order','save',item={**item,'campaign_ids':['wrong']})[0],400)

if __name__=='__main__':unittest.main()
