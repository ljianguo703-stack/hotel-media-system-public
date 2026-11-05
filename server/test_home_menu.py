"""PD07: real HTTP, persistence, permissions, archive and stale menu editor coverage."""
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
import test_management as fixture
import test_orders
from management import Store


class HomeMenuE2E(unittest.TestCase):
    setUp = fixture.ManagementE2E.setUp
    tearDown = fixture.ManagementE2E.tearDown
    request = fixture.ManagementE2E.request
    state = fixture.ManagementE2E.state
    mutate = fixture.ManagementE2E.mutate
    op = test_orders.OrdersE2E.op
    login_as = test_orders.OrdersE2E.login_as

    def home(self): return self.request('/api/admin/operations?section=home', auth=True)[2]
    def cards(self): return [c for c in self.home()['menu'] if not c.get('_archived')]
    def public(self): return self.request('/api/v1/catalog', tv=True)[2]['home']['menu']
    def save(self, cards): return self.op('home', 'home_menu_save', cards=cards)

    def test_defaults_persist_and_background_save_does_not_overwrite_menu(self):
        self.assertEqual([c['target'] for c in self.public()], ['movie', 'series', 'apps', 'more'])
        cards = self.cards(); cards.reverse(); cards[0]['name'] = '其他'
        self.assertEqual(self.save(cards)[0], 200)
        revision = self.state()['revision']
        again = Store(self.data, self.media, self.config, [self.media, self.external])
        self.assertEqual(again.state()['revision'], revision)
        self.assertEqual(self.op('home', 'home_save', wifi_ssid='')[0], 200)
        self.assertEqual([c['name'] for c in self.public()], ['其他', '已装应用', '电视剧', '电影'])

    def test_validation_caps_viewer_and_stale_editor_are_atomic(self):
        cards = self.cards()
        fifth = dict(id='music', name='音乐', type='app', target='test.music', icon='music', enabled=True)
        self.assertEqual(self.save(cards+[fifth])[0], 200)
        saved = self.cards(); revision = self.state()['revision']
        for bad in (saved+[dict(fifth,id='sixth')], [], [dict(fifth,target=[])], [dict(fifth,icon=[])],
                    [dict(fifth,enabled='true')], [dict(fifth,type='url',target='https://example.com')],
                    [dict(fifth,name='x'*13)], [fifth, fifth]):
            self.assertEqual(self.save(bad)[0], 400, bad)
            self.assertEqual(self.state()['revision'], revision)
            self.assertEqual(self.cards(), saved)
        viewer = self.login_as('menu-viewer','viewer')
        self.assertEqual(self.op('home','home_menu_save',headers=viewer,cards=saved)[0],403)
        body = dict(kind='home', action='home_menu_save', cards=saved, revision=self.state()['revision'])
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: self.request('/api/admin/operations',body,auth=True)[0], range(2)))
        self.assertEqual(sorted(outcomes), [200,409])

    def test_delete_is_recyclable_restore_is_hidden_and_limit_cannot_be_bypassed(self):
        cards=self.cards(); extra=dict(id='music',name='音乐',type='app',target='test.music',icon='music',enabled=True)
        self.assertEqual(self.save(cards+[extra])[0],200)
        self.assertEqual(self.save(cards)[0],200)
        trash=self.request('/api/admin/operations?section=trash',auth=True)[2]
        self.assertTrue(any(c['kind']=='home_card' and c['id']=='music' for c in trash['rows']))
        self.assertEqual(self.op('home_card','restore',ids=['music'])[2]['ok'],['music'])
        self.assertFalse(next(c for c in self.cards() if c['id']=='music')['enabled'])
        self.assertEqual(self.op('home_card','enable',ids=['music'])[2]['ok'],['music'])
        hidden=dict(extra,id='hidden',enabled=False)
        self.assertEqual(self.save(self.cards()+[hidden])[0],200)
        self.assertFalse(self.op('home_card','enable',ids=['hidden'])[2]['ok'])
        one=[dict(extra,id='only')]; self.assertEqual(self.save(one)[0],200)
        self.assertFalse(self.op('home_card','archive',ids=['only'])[2]['ok'])
        self.assertFalse(self.op('home_card','disable',ids=['only'])[2]['ok'])
        self.assertEqual(self.op('home','home_menu_reset')[0],200)
        self.assertEqual([c['target'] for c in self.public()],['movie','series','apps','more'])
        with self.store.db() as db:
            card=self.store.record(db,'home_card','only');card['_archived_at']=1;self.store.put_object(db,'home_card',card)
        self.store.cleanup_expired()
        self.assertFalse(any(c['id']=='only' for c in self.home()['menu']))

    def test_category_rename_and_withdrawn_topic_do_not_leave_a_blank_home(self):
        self.assertEqual(self.mutate('/categories/music',dict(name='音乐',kind='all',sort=0,enabled=True,aliases=[]))[0],200)
        card=dict(id='music-link',name='音乐',type='category',target='music',icon='music',enabled=True)
        self.assertEqual(self.save([card])[0],200);self.assertEqual(self.public()[0]['category'],'音乐')
        self.assertEqual(self.mutate('/categories/music',dict(name='演唱会',kind='all',sort=0,enabled=True,aliases=[]))[0],200)
        self.assertEqual(self.public()[0]['category'],'演唱会')
        self.assertEqual(self.mutate('/categories/music',dict(name='演唱会',kind='all',sort=0,enabled=False,aliases=[]))[0],200)
        self.assertEqual(len(self.public()),4)
        self.assertEqual(self.op('topic','save',item=dict(id='t',name='暂未上架',item_ids=[],enabled=False))[0],200)
        self.assertEqual(self.save([dict(card,type='topic',target='t')])[0],200)
        self.assertEqual(len(self.public()),4)

    def test_app_inventory_is_device_authenticated_bounded_and_not_a_business_revision(self):
        revision=self.state()['revision']
        body={'apps':[{'package':'test.music','label':'音乐播放器'}]}
        self.assertEqual(self.request('/api/v1/apps',body,tv=True,headers={'X-TV-Token':''})[0],401)
        self.assertEqual(self.request('/api/v1/apps',body,tv=True)[0],200)
        inventory=self.home()['app_inventory'];self.assertEqual(inventory[0]['room'],'101')
        self.assertEqual(inventory[0]['apps'],body['apps'])
        self.assertEqual(self.state()['revision'],revision)
        for bad in ({'apps':body['apps']*201},{'apps':[{'package':'bad; cmd','label':'bad'}]},[],{'apps':[{}]}):
            self.assertEqual(self.request('/api/v1/apps',bad,tv=True)[0],400)
        self.assertNotIn('app_inventory',self.request('/api/v1/catalog',tv=True)[2]['home'])

    def test_hidden_withdrawn_targets_do_not_block_unrelated_menu_edits(self):
        hidden=dict(id='old-topic',name='旧专题',type='topic',target='withdrawn',icon='music',enabled=False)
        self.assertEqual(self.save(self.cards()+[hidden])[0],200)
        home=self.home();self.assertFalse(next(c for c in home['menu'] if c['id']=='old-topic')['target_available'])
        self.assertEqual(len(home['effective_menu']),4)
        cards=self.cards();cards[0]['name']='影片';self.assertEqual(self.save(cards)[0],200)
        self.assertFalse(self.op('home_card','enable',ids=['old-topic'])[2]['ok'])


if __name__=='__main__': unittest.main()
