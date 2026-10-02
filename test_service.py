"""Verifiche di coda, recupero, pubblicazione e richieste browser."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

import link_service as service


class ServiceFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [patch.object(service, "ROOT", self.root / "downloads"),
                        patch.object(service, "STATE", self.root / "config"),
                        patch.object(service, "DB", self.root / "config" / "jobs.sqlite3"),
                        patch.object(service, "RESERVE", 0)]
        for p in self.patches:
            p.start()
        service.initialize()
        self.metadata = {"id": 42, "name": "Film di prova", "type": "movie", "year": "2026",
                         "url": f"https://{service.SOURCE_HOST}/it/titles/42-film", "season": None,
                         "episodes": []}

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def queue(self):
        with patch.object(service, "inspect", return_value=self.metadata), patch.object(service, "RESERVE", 0):
            return service.enqueue(service.DownloadRequest(url=self.metadata["url"]))


class ServiceTest(ServiceFixture, unittest.TestCase):
    def test_reject_arbitrary_hosts_credentials_and_non_title_links(self):
        urls = ["https://127.0.0.1/it/titles/42", "https://example.com/it/titles/42",
                f"https://user:password@{service.SOURCE_HOST}/it/titles/42",
                f"https://{service.SOURCE_HOST}/", f"http://{service.SOURCE_HOST}/it/titles/42"]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(HTTPException):
                service.validate_url(url)
        self.assertEqual(service.validate_url(self.metadata["url"]), (42, None))

    def test_duplicate_and_retry_cancelled_job(self):
        first = self.queue()["queued"][0]
        self.assertEqual(self.queue(), {"queued": [], "existing": [first]})
        service.cancel(first)
        self.assertEqual(service.downloads()[0]["status"], "cancelled")
        service.retry(first)
        self.assertEqual(service.downloads()[0]["status"], "queued")

    def test_title_lookup_keeps_the_required_slug(self):
        props = {"title": {"id": 42, "name": "Film", "type": "movie", "slug": "film", "release_date": "2026-01-01"}}
        with patch.object(service, "fetch_props", return_value=props) as fetch:
            result = service.inspect_link(self.metadata["url"])
        fetch.assert_called_once_with(self.metadata["url"])
        self.assertEqual(result["url"], self.metadata["url"])

    def test_restart_preserves_queue_and_marks_active_as_interrupted(self):
        job = self.queue()["queued"][0]
        service.initialize()
        self.assertEqual(service.downloads()[0]["status"], "queued")
        service.update(job, status="running")
        service.initialize()
        restored = service.downloads()[0]
        self.assertEqual(restored["status"], "error")
        self.assertIn("riavvio", restored["error"])

    def test_atomic_publish_never_overwrites_existing_media(self):
        src = self.root / "new.mp4"
        dst = self.root / "Movies" / "same.mp4"
        src.write_bytes(b"new")
        dst.parent.mkdir()
        dst.write_bytes(b"existing")
        with self.assertRaises(FileExistsError):
            service.publish(src, dst)
        self.assertEqual(dst.read_bytes(), b"existing")
        self.assertEqual(src.read_bytes(), b"new")
        other = dst.with_name("other.mp4")
        service.publish(src, other)
        self.assertFalse(src.exists())
        self.assertEqual(other.read_bytes(), b"new")

    def test_recovers_crash_after_publishing(self):
        job = self.queue()["queued"][0]
        target = service.ROOT / "Movies" / "published.mp4"
        target.write_bytes(b"verified-video")
        service.update(job, status="running", phase="publishing", output="Movies/published.mp4")
        with patch.object(service, "verify_video") as verify:
            service.initialize()
        verify.assert_called_once_with(target)
        self.assertEqual(service.downloads()[0]["status"], "done")

    def test_browser_origin_and_host_checks(self):
        # Non avviare il lifespan: il worker vero non deve consumare job di test.
        client = TestClient(service.api)
        r = client.post("/api/inspect", json={"url": self.metadata["url"]}, headers={"Origin": "https://outside.invalid"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(client.get("/api/health", headers={"Host": "outside.invalid"}).status_code, 403)
        self.assertEqual(client.post("/api/inspect", content="url=value", headers={"Content-Type": "text/plain"}).status_code, 415)
        self.assertEqual(client.get("/api/health").status_code, 200)

    def test_errors_do_not_expose_signed_urls(self):
        result = service.safe_error(RuntimeError("Cannot fetch https://video.invalid?token=secret"))
        self.assertNotIn("secret", result)
        self.assertNotIn("https", result)

    def test_cancel_reaches_the_active_worker_without_losing_the_event(self):
        job = self.queue()["queued"][0]
        started = threading.Event()

        def fake_run(row, event):
            started.set()
            self.assertTrue(event.wait(3))
            service.update(row["id"], status="cancelled")

        service.stop.clear()
        with patch.object(service, "run_job", side_effect=fake_run):
            thread = threading.Thread(target=service.worker, daemon=True)
            thread.start()
            try:
                self.assertTrue(started.wait(3))
                service.cancel(job)
                deadline = time.monotonic() + 3
                while service.downloads()[0]["status"] != "cancelled" and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertEqual(service.downloads()[0]["status"], "cancelled")
            finally:
                service.stop.set()
                service.wakeup.set()
                thread.join(3)
            self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
