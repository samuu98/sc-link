"""Source normalization, pagination, cross-source identities and legacy migration."""
import json
import sys
import types
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException
from test_service import ServiceFixture
import link_service as s
import animeunity_provider as au


class AnimeUnityTest(ServiceFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.url=f'https://{au.HOST}/anime/42-serie-di-prova-ita'
        self.title={'id':42,'slug':'serie-di-prova-ita','title_eng':'Anime di prova (ITA)',
                    'type':'ONA','dub':1,'date':'2026','episodes_count':2}
        self.episodes=[{'id':101,'anime_id':42,'number':'1','public':1,'hidden':0},
                       {'id':102,'anime_id':42,'number':'2','public':1,'hidden':0}]
        self.meta={**self.metadata,'url':self.url,'type':'tv','provider':'animeunity',
                   'dubbed':True,'season':1,'seasons':[1],
                   'episodes':[{'id':101,'number':'1','name':'Episodio 1'}]}
        s.stop.clear()

    def test_only_allowed_https_anime_urls(self):
        self.assertEqual(s.validate_url(self.url),(42,None))
        self.assertEqual(s.validate_url(self.url.replace('www.','')),(42,None))
        for url in [self.url.replace('https:','http:'),self.url.replace(au.HOST,'127.0.0.1'),
                    self.url.replace(au.HOST,au.HOST+'.evil.invalid'),self.url.replace(au.HOST,'x:y@'+au.HOST),
                    self.url.replace(au.HOST,au.HOST+':8080'),f'https://{au.HOST}/embed-url/42', 'https://[bad']:
            with self.subTest(url=url),self.assertRaises(HTTPException):
                s.validate_url(url)

    def test_only_published_catalog_not_planned_episodes(self):
        with patch.object(au,'fetch_json',side_effect=[self.title,{'episodes':self.episodes}]) as fetch:
            metadata=s.inspect_link(self.url)
        self.assertEqual(metadata['provider'],'animeunity')
        self.assertEqual(metadata['seasons'],[1])
        self.assertEqual(len(metadata['episodes']),2)
        self.assertTrue(metadata['dubbed'])
        self.assertEqual(fetch.call_count,2)
        with self.assertRaises(HTTPException):
            au.inspect_link(self.url,2)

    def test_paginate_long_series_without_skipping_batches(self):
        title={**self.title,'episodes_count':130}
        episodes=[{'id':n,'number':str(n)} for n in range(1,131)]
        with patch.object(au,'fetch_json',side_effect=[title,{'episodes':episodes[:120]}, {'episodes':episodes[120:]}]) as fetch:
            data=au.inspect_link(self.url)
        self.assertEqual(len(data['episodes']),130)
        self.assertEqual(fetch.call_args_list[-1].args[1],{'start_range':121,'end_range':130})

    def test_exact_120_boundary_includes_last_episode(self):
        episodes=[{'id':n,'number':str(n)} for n in range(1,121)]
        with patch.object(au,'fetch_json',side_effect=[{**self.title,'episodes_count':120},
                {'episodes':episodes}]) as fetch:
            data=au.inspect_link(self.url)
        self.assertEqual(data['episodes'][-1]['number'],'120')
        self.assertEqual(fetch.call_args.args[1],{'start_range':1,'end_range':120})

    def test_bundled_episode_ranges_are_preserved(self):
        self.assertEqual(au.normalize_episode({'id':1,'number':'1-12'},42)['number'],'1-12')
        with self.assertRaises(RuntimeError):
            au.normalize_episode({'id':1,'number':'12-1'},42)

    def test_no_episodes_yet_can_be_watched(self):
        with patch.object(au,'fetch_json',return_value={**self.title,'episodes_count':0}):
            data=au.inspect_link(self.url)
        self.assertEqual(data['type'],'tv')
        self.assertEqual(data['episodes'],[])

    def test_partial_failed_catalog_never_becomes_a_baseline(self):
        with patch.object(au,'fetch_json',side_effect=[{**self.title,'episodes_count':130},
                {'episodes':self.episodes},RuntimeError('Batch indisponibile')]),self.assertRaises(RuntimeError):
            au.inspect_link(self.url)
        with patch.object(au,'fetch_json',side_effect=[self.title,{'episodes':[]}]),self.assertRaises(RuntimeError):
            au.inspect_link(self.url)

    def test_specials_hidden_and_duplicate_ids(self):
        raw=[{'id':1,'number':'12.5'},{'id':1,'number':'12.5'},
             {'id':2,'number':'13','hidden':1},{'id':3,'number':'14','public':0}]
        with patch.object(au,'fetch_json',side_effect=[{**self.title,'episodes_count':4}, {'episodes':raw}]):
            data=au.inspect_link(self.url)
        self.assertEqual(data['episodes'],[{'id':1,'number':'12.5','name':'Episodio 12.5'}])
        with self.assertRaises(RuntimeError):
            au.normalize_episode({'id':1,'anime_id':123,'number':'1'},42)

    def test_movie_keeps_single_available_episode_for_engine(self):
        with patch.object(au,'fetch_json',side_effect=[{**self.title,'type':'Movie','episodes_count':1},
                {'episodes':self.episodes[:1]}]):
            data=au.inspect_link(self.url)
        self.assertEqual(data['type'],'movie')
        self.assertEqual(len(data['episodes']),1)

    def test_equal_title_and_episode_ids_on_two_sources_do_not_collide(self):
        sc={**self.meta,'provider':'streamingcommunity','url':self.metadata['url']}
        for metadata in [sc,self.meta]:
            with patch.object(s,'inspect',return_value=metadata):
                s.enqueue(s.DownloadRequest(url=metadata['url'],season=1,episode_ids=[101]))
            with patch.object(s,'series_catalog',return_value=(metadata,{'1':metadata['episodes']})):
                s.add_watch(s.WatchRequest(url=metadata['url']))
        self.assertEqual(len(s.downloads()),2)
        self.assertEqual(len(s.watches()),2)
        with s.connect() as con:
            self.assertEqual({r[0] for r in con.execute('SELECT content_key FROM jobs')}, {'42:101','animeunity:42:101'})

    def test_anime_watch_queues_new_episodes_with_correct_provider(self):
        with patch.object(s,'series_catalog',return_value=(self.meta,{'1':self.meta['episodes']})):
            watch=s.add_watch(s.WatchRequest(url=self.url,mode='auto'))['id']
        new=[*self.meta['episodes'], {'id':102,'number':'2','name':'Episodio 2'}]
        with patch.object(s,'series_catalog',return_value=(self.meta,{'1':new})):
            s.check_watch(watch)
            s.check_watch(watch)
        self.assertEqual(len(s.downloads()),1)
        self.assertEqual(s.watches()[0]['discoveries'][0]['download_status'],'queued')
        with s.connect() as con:
            payload=json.loads(con.execute('SELECT payload FROM jobs').fetchone()[0])
        self.assertEqual(payload['provider'],'animeunity')
        self.assertTrue(payload['dubbed'])

    def test_legacy_watch_migration_preserves_data_and_uniqueness_per_source(self):
        with s.connect() as con:
            con.execute('DROP TABLE watches')
            con.execute('''CREATE TABLE watches (
                id TEXT PRIMARY KEY,title_id INTEGER UNIQUE NOT NULL,name TEXT NOT NULL,url TEXT NOT NULL,
                year TEXT NOT NULL DEFAULT '',mode TEXT NOT NULL,enabled INTEGER NOT NULL,snapshot TEXT NOT NULL,
                last_checked TEXT,scheduled_date TEXT NOT NULL,error TEXT,created_at TEXT NOT NULL)''')
            con.execute('INSERT INTO watches VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                        ('legacy',42,'Serie SC',self.metadata['url'],'2026','notify',1,'{}',None,s.local_date(),None,s.now()))
            con.execute('INSERT INTO watch_items VALUES(?,?,?,?,?,?)',('legacy',101,1,1,'Episodio',s.now()))
        job=self.queue()['queued'][0]
        s.initialize()
        s.initialize()
        self.assertEqual(s.downloads()[0]['id'],job)
        self.assertEqual(s.watches()[0]['id'],'legacy')
        self.assertEqual(s.watches()[0]['provider'],'streamingcommunity')
        self.assertEqual(len(s.watches()[0]['discoveries']),1)
        with patch.object(s,'series_catalog',return_value=(self.meta,{'1':self.meta['episodes']})):
            s.add_watch(s.WatchRequest(url=self.url))
        self.assertEqual(len(s.watches()),2)

    def test_external_engine_receives_episode_and_original_audio(self):
        engine=types.ModuleType('app.core.animeunity')
        engine.download_anime_episode=Mock(return_value='/tmp/episode.mp4')
        app=types.ModuleType('app');core=types.ModuleType('app.core');core.animeunity=engine;app.core=core
        with patch.dict(sys.modules,{'app':app,'app.core':core,'app.core.animeunity':engine}):
            result=au.download({**self.meta,'dubbed':False,'episode':self.meta['episodes'][0]},
                               domain='irrelevant',output_dir='/tmp',strict_audio=True)
        self.assertEqual(result,'/tmp/episode.mp4')
        args=engine.download_anime_episode.call_args.kwargs
        self.assertEqual(args['audio_languages'],['jpn'])
        self.assertEqual(args['episode']['id'],101)
        self.assertNotIn('domain',args)

    def test_metadata_requests_do_not_follow_external_redirects(self):
        response=Mock(is_redirect=True)
        with patch.object(au.requests,'get',return_value=response) as get,self.assertRaises(RuntimeError):
            au.fetch_json('/info_api/42')
        self.assertFalse(get.call_args.kwargs['allow_redirects'])
