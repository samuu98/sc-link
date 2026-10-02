"""Watch state, midnight recovery, catalog diff and atomic multi-season queue."""
import json
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from test_service import ServiceFixture
import link_service as s


class WatchTest(ServiceFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.tv = dict(self.metadata, type='tv', name='Serie di prova', seasons=[1, 2], season=1,
                       episodes=[{'id': 101, 'number': 1, 'name': 'Pilota'}])
        self.catalog = {'1': self.tv['episodes'], '2': [{'id': 201, 'number': 1, 'name': 'Ritorno'}]}
        s.stop.clear()

    def create(self, mode='notify'):
        with patch.object(s, 'series_catalog', return_value=(self.tv, self.catalog)):
            return s.add_watch(s.WatchRequest(url=self.tv['url'], mode=mode))['id']

    def expanded(self):
        return {**self.catalog, '1': [*self.catalog['1'], {'id': 102, 'number': 2, 'name': 'Nuovo'}],
                '3': [{'id': 301, 'number': 1, 'name': 'Nuova stagione'}]}

    def test_watch_baseline_does_not_download_old_episodes(self):
        self.create('auto')
        self.assertEqual(s.downloads(), [])
        self.assertEqual(s.watches()[0]['episode_count'], 2)
        with self.assertRaises(HTTPException) as ctx:
            self.create()
        self.assertEqual(ctx.exception.status_code, 409)

    def test_new_episode_and_season_detected_once(self):
        watch = self.create()
        with patch.object(s, 'series_catalog', return_value=(self.tv, self.expanded())):
            s.check_watch(watch)
            s.check_watch(watch)
        data = s.watches()[0]
        self.assertEqual(len(data['discoveries']), 2)
        self.assertEqual(data['seasons'], [1, 2, 3])
        self.assertEqual(s.downloads(), [])
        events = s.dashboard()['events']
        self.assertEqual(events[0]['new_episodes'], 0)
        self.assertEqual(events[1]['new_seasons'], [3])
        self.assertEqual(s.download_watch_updates(watch)['queued_count'], 2)
        self.assertEqual(s.download_watch_updates(watch)['queued_count'], 0)

    def test_auto_queue_and_no_retry_storm(self):
        watch = self.create('auto')
        with patch.object(s, 'series_catalog', return_value=(self.tv, self.expanded())):
            s.check_watch(watch)
            self.assertEqual(len(s.downloads()), 2)
            job = s.downloads()[0]['id']
            s.update(job, status='error', error='Prova')
            s.check_watch(watch)
        self.assertEqual(len(s.downloads()), 2)
        self.assertEqual(s.downloads()[0]['status'], 'error')

    def test_temporary_source_omission_does_not_rediscover(self):
        watch = self.create()
        with patch.object(s, 'series_catalog', return_value=(self.tv, {'1': []})):
            s.check_watch(watch)
        with patch.object(s, 'series_catalog', return_value=(self.tv, self.catalog)):
            s.check_watch(watch)
        self.assertEqual(s.watches()[0]['discoveries'], [])

    def test_error_preserves_baseline_and_logs_safe_error(self):
        watch = self.create()
        with patch.object(s, 'series_catalog', side_effect=RuntimeError('https://host/?token=secret')):
            s.check_watch(watch, scheduled=True)
        self.assertEqual(s.watches()[0]['episode_count'], 2)
        self.assertNotIn('secret', s.watches()[0]['error'])
        self.assertEqual(s.dashboard()['events'][0]['status'], 'error')

    def test_pause_then_recover_overdue_once(self):
        watch = self.create()
        with s.connect() as con:
            con.execute("UPDATE watches SET scheduled_date='2000-01-01' WHERE id=?", (watch,))
        s.edit_watch(watch, s.WatchSettings(mode='notify', enabled=False))
        with patch.object(s, 'series_catalog', return_value=(self.tv, self.catalog)) as fetch:
            s.scheduler_tick()
            fetch.assert_not_called()
            s.edit_watch(watch, s.WatchSettings(mode='notify', enabled=True))
            s.scheduler_tick()
            s.scheduler_tick()
            fetch.assert_called_once()
        s.initialize()
        self.assertEqual(len(s.watches()), 1)
        self.assertEqual(s.watches()[0]['mode'], 'notify')

    def test_manual_check_does_not_skip_scheduled_midnight(self):
        watch = self.create()
        with s.connect() as con:
            con.execute("UPDATE watches SET scheduled_date='2000-01-01' WHERE id=?", (watch,))
        with patch.object(s, 'series_catalog', return_value=(self.tv, self.catalog)) as fetch:
            s.check_watch(watch)
            s.scheduler_tick()
            self.assertEqual(fetch.call_count, 2)

    def test_atomic_multiple_seasons_and_invalid_episode(self):
        def metadata(body):
            n = body.season
            return dict(self.tv, season=n, episodes=self.catalog[str(n)])
        body = s.DownloadRequest(url=self.tv['url'], selections=[{'season': 1, 'episode_ids': [101]}, {'season': 2, 'episode_ids': [999]}])
        with patch.object(s, 'inspect', side_effect=metadata), self.assertRaises(HTTPException):
            s.enqueue(body)
        self.assertEqual(s.downloads(), [])
        body.selections[1].episode_ids = [201]
        with patch.object(s, 'inspect', side_effect=metadata):
            result = s.enqueue(body)
            again = s.enqueue(body)
        self.assertEqual(len(result['queued']), 2)
        self.assertEqual(len(again['existing']), 2)
        with s.connect() as con:
            payloads = [json.loads(r[0]) for r in con.execute('SELECT payload FROM jobs')]
        self.assertEqual({p['season'] for p in payloads}, {1, 2})

    def test_capacity_failure_leaves_discoveries_available(self):
        watch = self.create('auto')
        with patch.object(s, 'series_catalog', return_value=(self.tv, self.expanded())), patch.object(s, 'RESERVE', 10**30):
            s.check_watch(watch)
        self.assertTrue(s.watches()[0]['error'])
        self.assertEqual(len(s.watches()[0]['discoveries']), 2)
        self.assertEqual(s.downloads(), [])
        self.assertEqual(s.download_watch_updates(watch)['queued_count'], 2)

    def test_remove_preserves_downloads(self):
        watch = self.create()
        self.queue()
        s.remove_watch(watch)
        self.assertEqual(s.watches(), [])
        self.assertEqual(len(s.downloads()), 1)

    def test_missing_file_verification_does_not_erase_job(self):
        job = self.queue()['queued'][0]
        s.update(job, status='done', output='Movies/missing.mp4')
        result = s.verify_download(job)
        self.assertFalse(result['ok'])
        self.assertEqual(s.downloads()[0]['status'], 'done')
        self.assertIsNotNone(s.downloads()[0]['verified_at'])

    def test_watch_api_validation(self):
        client = TestClient(s.api)
        self.assertEqual(client.post('/api/watches', json={'url': self.tv['url'], 'mode': 'anything'}).status_code, 422)
        self.assertEqual(client.get('/assets/not-allowed.py').status_code, 404)
        self.assertEqual(client.post('/api/watches/missing/check', json={}).status_code, 404)

    def test_auto_uses_same_library_year_as_manual_download(self):
        watch = self.create('auto')
        with patch.object(s, 'series_catalog', return_value=(self.tv, self.expanded())):
            s.check_watch(watch)
        with s.connect() as con:
            payloads = [json.loads(r[0]) for r in con.execute('SELECT payload FROM jobs')]
        self.assertTrue(all(p['year'] == self.tv['year'] for p in payloads))

    def test_auto_queue_respects_pause_after_scan(self):
        watch = self.create('auto')
        with patch.object(s, 'series_catalog', return_value=(self.tv, self.expanded())):
            s.check_watch(watch)
        # Add an unqueued discovery and pause before the auto queue step.
        with s.connect() as con:
            con.execute("INSERT INTO watch_items VALUES(?,?,?,?,?,?)", (watch, 999, 3, 2, 'Future', s.now()))
        s.edit_watch(watch, s.WatchSettings(mode='auto', enabled=False))
        self.assertEqual(s.queue_watch_items(watch, automatic=True)['queued_count'], 0)

    def test_midnight_dst_uses_calendar_day(self):
        real = datetime
        class FakeDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return real(2026, 3, 29, 0, 30, tzinfo=ZoneInfo('Europe/Rome'))
        with patch.object(s, 'datetime', FakeDateTime):
            result = s.next_check()
        self.assertEqual(result, '2026-03-30T00:00:00+02:00')
