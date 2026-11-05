"""Combined-advertising HTTP persistence, rollback, legacy IDs and group lifecycle."""
import json
import unittest
import test_management as fixture
from management import Problem

class CampaignBundleE2E(unittest.TestCase):
    setUp=fixture.ManagementE2E.setUp
    tearDown=fixture.ManagementE2E.tearDown
    request=fixture.ManagementE2E.request
    state=fixture.ManagementE2E.state
    mutate=fixture.ManagementE2E.mutate
    def tier(self, tier, **extra):
        value={'name':'组合','merchant':'赞助商','tier':tier,'priority':50,'duration':12,'gap_seconds':300,'cap':2,
               'format':'图片' if tier==1 else '文字','status':'草稿','group':'全部客房','text':'全天文字' if tier==2 else '分时文字',
               'path':'/media/missing.png' if tier==1 else '', 'placements':['home'] if tier==1 else ['all'],
               'start':'00:00' if tier!=3 else '18:00','end':'00:00' if tier!=3 else '22:00',
               'days':[1,2,3,4,5,6,7],'timezone':'UTC','from':'','until':''}
        value.update(extra);return value
    def save_bundle(self, items, status='草稿', ident='combo-test', **extra):
        return self.mutate('/campaign-bundles/'+ident,{'name':'赞助商组合','items':items,'status':status,**extra})
    def batch(self, ids, operation):
        return self.mutate('/operations',{'kind':'campaign','action':'campaign_batch','ids':ids,'operation':operation})
    def campaigns(self):return {c['id']:c for c in self.state()['campaigns']}
    def test_combo_http_saves_three_tiers_and_updates_one_revision(self):
        rev=self.state()['revision'];code,_,result=self.save_bundle([self.tier(1),self.tier(2),self.tier(3)])
        self.assertEqual(code,200,result);self.assertEqual(self.state()['revision'],rev+1)
        rows=list(self.campaigns().values());self.assertEqual(len(rows),3)
        self.assertEqual({r['tier'] for r in rows},{1,2,3});self.assertTrue(all(r['_bundle_id']=='combo-test' for r in rows))
        self.assertEqual({r['status'] for r in rows},{'草稿'})
    def test_invalid_tier_rolls_back_entire_create_including_merchant(self):
        before=self.state();code,_,_=self.save_bundle([self.tier(2),self.tier(3,text='')])
        self.assertEqual(code,400);after=self.state();self.assertEqual(after['campaigns'],before['campaigns']);self.assertEqual(after['revision'],before['revision'])
        self.assertEqual(after['advertisers'],before['advertisers'])

    def test_hidden_card_field_errors_name_the_field_and_roll_back_all_tiers(self):
        before=self.state()
        for field,value,label,limit in [('title','','赞助商标题',80),('title','x'*81,'赞助商标题',80),
                                      ('body','x'*501,'宣传文字',500),('contact','x'*301,'联系方式 / 地址',300),
                                      ('qr_label','x'*41,'二维码说明',40),('qr_link','x'*1201,'二维码链接',1200)]:
            with self.subTest(field=field,value_length=len(value)):
                card={'title':'赞助商',field:value}
                code,_,result=self.save_bundle([self.tier(2),self.tier(1,format='图文',text='',path='',creative_card=card)],status='投放中')
                self.assertEqual(code,400,result)
                self.assertIn('一档全屏：'+label,result['error'])
                self.assertIn('不能为空' if not value else '最多'+str(limit)+'个字符',result['error'])
                self.assertNotIn('文字为空或超过允许长度',result['error'])
                after=self.state();self.assertEqual(after['campaigns'],before['campaigns']);self.assertEqual(after['revision'],before['revision'])
                self.assertEqual(after['advertisers'],before['advertisers'])

    def test_card_and_ticker_publish_together_without_requiring_card_ticker_text(self):
        card=self.tier(1,format='图文',text='',path='',creative_card={'title':'赞助商'})
        code,_,result=self.save_bundle([card,self.tier(2,text='全天优惠')],status='投放中')
        self.assertEqual(code,200,result)
        saved=self.campaigns();self.assertEqual(saved['combo-test-t1']['text'],'');self.assertEqual(saved['combo-test-t1']['creative_card']['title'],'赞助商')
        self.assertEqual(saved['combo-test-t2']['text'],'全天优惠');self.assertEqual({r['status'] for r in saved.values()},{'投放中'})
        code,_,result=self.save_bundle([card,self.tier(2,text='')],status='投放中')
        self.assertEqual(code,400);self.assertEqual(result['error'],'二档全天文字：滚动文字不能为空')
        self.assertEqual(self.campaigns(),saved)
    def test_publish_validation_does_not_partially_update_a_group(self):
        self.assertEqual(self.save_bundle([self.tier(2),self.tier(1)])[0],200)
        code,_,r=self.batch(['combo-test-t2'],'publish');self.assertEqual(code,200);self.assertEqual(r['ok'],[]);self.assertEqual(len(r['failed']),1)
        self.assertEqual({r['status'] for r in self.campaigns().values()},{'草稿'})
    def test_group_publish_pause_copy_archive_and_restore(self):
        self.assertEqual(self.save_bundle([self.tier(2),self.tier(3)])[0],200)
        for op,expected in [('publish','投放中'),('pause','已暂停')]:
            r=self.batch(['combo-test-t2'],op)[2];self.assertEqual(len(r['ok']),2,r)
            self.assertEqual({r['status'] for r in self.campaigns().values()},{expected})
        r=self.batch(['combo-test-t2','combo-test-t3'],'copy')[2];self.assertEqual(len(r['created']),2,r)
        r_ids=r['created'];copies=[item for item in self.campaigns().values() if item['id'] in r_ids]
        self.assertEqual(len({r['_bundle_id'] for r in copies}),1);self.assertNotEqual(copies[0]['_bundle_id'],'combo-test')
        self.assertEqual({r['status'] for r in copies},{'草稿'})
        self.assertEqual(len(self.batch(['combo-test-t2'],'archive')[2]['ok']),2)
        body={'kind':'campaign','action':'restore','ids':['combo-test-t3']}
        r=self.mutate('/operations',body)[2];self.assertEqual(len(r['ok']),2,r)
        self.assertEqual({self.campaigns()[i]['status'] for i in ('combo-test-t2','combo-test-t3')},{'草稿'})
    def test_unchecked_tier_retains_id_material_and_can_be_reselected(self):
        self.save_bundle([self.tier(2),self.tier(3)])
        self.assertEqual(self.save_bundle([self.tier(2)],status='投放中')[0],200)
        row=self.campaigns()['combo-test-t3'];self.assertFalse(row['_bundle_active']);self.assertEqual(row['status'],'已暂停');self.assertEqual(row['text'],'分时文字')
        self.assertEqual(self.save_bundle([self.tier(2),self.tier(3,text=row['text'])],status='投放中')[0],200)
        self.assertTrue(self.campaigns()['combo-test-t3']['_bundle_active'])
    def test_legacy_id_and_events_are_preserved_when_adding_a_tier(self):
        self.assertEqual(self.mutate('/campaigns/old-ad',self.tier(2))[0],200)
        with self.store.db() as db:db.execute('INSERT INTO events VALUES(?,?,?,?)',('legacy-event','room-101',1,json.dumps({'type':'ad_started','creative_id':'old-ad'})))
        self.assertEqual(self.save_bundle([self.tier(2),self.tier(3)],legacy_id='old-ad')[0],200)
        self.assertIn('old-ad',self.campaigns());self.assertNotIn('combo-test-t2',self.campaigns())
        with self.store.db() as db:self.assertEqual(db.execute("SELECT count(*) FROM events WHERE id='legacy-event'").fetchone()[0],1)
        self.assertEqual(self.mutate('/campaigns/old-ad',self.tier(2))[0],400,'direct single-tier editing cannot break a group')
    def test_combination_retains_per_tier_report_units_and_public_creative_ids(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        import time
        self.assertEqual(self.save_bundle([self.tier(1),self.tier(2),self.tier(3)])[0],200)
        actor=next(a for a in self.state()['advertisers'] if a['name']=='赞助商')
        package={'id':'pkg-combo','name':'三档套餐','tier':1,'tiers':[1,2,3],'placements':['home'],'placements_by_tier':{'2':['all'],'3':['all']}}
        self.assertEqual(self.mutate('/operations',{'kind':'package','action':'save','item':package})[0],200)
        day=datetime.now(ZoneInfo(self.state()['settings']['timezone'])).date().isoformat()
        order={'id':'order-combo','advertiser_id':actor['id'],'package_id':'pkg-combo','campaign_ids':list(self.campaigns()),'group':'全部客房','start':day,'end':day}
        self.assertEqual(self.mutate('/operations',{'kind':'order','action':'save','item':order})[0],200)
        now=int(time.time()*1000);events=[]
        for row in self.campaigns().values():
            creative=self.store.creatives(row)[0]
            self.assertEqual(creative['campaign_id'],row['id']);self.assertFalse(any(k.startswith('_bundle') for k in creative))
            if row['tier']==1:
                events.extend([{'id':'full-start','type':'ad_started','creative_id':creative['id'],'at_ms':now},{'id':'full-done','type':'ad_completed','creative_id':creative['id'],'at_ms':now}])
            else:
                events.append({'id':'text-'+str(row['tier']),'type':'text_ended','creative_id':creative['id'],'visible_ms':(row['tier']-1)*60000,'at_ms':now})
        self.store.record_events({'id':'room-101','room':'101','group':'A组'},events)
        result=self.request('/api/admin/operations?section=orders',auth=True)[2]['rows'][0]
        by={p['tier']:p for p in result['progress_by_tier']}
        self.assertEqual((by[1]['actual'],by[2]['actual'],by[3]['actual']),(1,1,2))
        report=self.request('/api/admin/operations?section=merchant_report&advertiser='+actor['id'],auth=True)[2]
        self.assertEqual((report['totals']['fullscreen_starts'],report['totals']['fullscreen_completes'],report['totals']['visible_minutes']),(1,1,3))

    def test_revision_permissions_and_scope_are_enforced(self):
        code,_,_=self.request('/api/admin/campaign-bundles/new',{'name':'X','status':'草稿','items':[self.tier(2)],'revision':self.state()['revision']},headers={'Cookie':self.cookie})
        self.assertEqual(code,403)
        code,_,_=self.request('/api/admin/campaign-bundles/new',{'name':'X','status':'草稿','items':[self.tier(2)],'revision':self.state()['revision']-1},auth=True);self.assertEqual(code,409)
        self.assertEqual(self.save_bundle([self.tier(2),self.tier(2)])[0],400)
        self.assertEqual(self.save_bundle([self.tier(2),self.tier(3,merchant='另一商户')])[0],400)
        with self.store.db() as db:
            from management import password_hash
            db.execute('INSERT INTO users VALUES(?,?,?,1)',('readonly',password_hash('test-only-password'),'viewer'))
        _,h,r=self.request('/api/admin/login',{'username':'readonly','password':'test-only-password'})
        code,_,_=self.request('/api/admin/campaign-bundles/new',{'name':'X','status':'草稿','items':[self.tier(2)],'revision':self.state()['revision']},headers={'Cookie':h['Set-Cookie'].split(';')[0],'X-CSRF-Token':r['user']['csrf']});self.assertEqual(code,403)

