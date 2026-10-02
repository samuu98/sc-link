"""Smoke test reale in un container usa-e-getta, senza scaricare il film intero.

Eseguire solo separatamente dal servizio: limita la sorgente a due segmenti.
"""
import json
import logging
import threading

logging.disable(logging.CRITICAL)

from app.core.m3u8 import M3U8_Segments
from app.core.page import search
import link_service as service

original = M3U8_Segments.get_info


def limited(self):
    original(self)
    self.segments = self.segments[:2]


M3U8_Segments.get_info = limited
service.initialize()
url = f"https://{service.SOURCE_HOST}/it/titles/55355-odissea"
metadata = service.inspect_link(url)
assert metadata["id"] == 55355 and metadata["type"] == "movie"
queued = service.enqueue(service.DownloadRequest(url=url))["queued"]
assert len(queued) == 1
with service.connect() as con:
    row = con.execute("SELECT * FROM jobs WHERE id=?", (queued[0],)).fetchone()
service.update(queued[0], status="running")
service.run_job(row, threading.Event())
job = service.downloads()[0]
assert job["status"] == "done", job["error"]
file = service.ROOT / job["output"]
assert file.is_file() and file.stat().st_size > 0
service.verify_video(file)
print(json.dumps({"movie": metadata["name"], "status": job["status"],
                  "clip_bytes": file.stat().st_size, "output": job["output"]}))

series = search("Breaking Bad", service.SOURCE_HOST)
series = next(t for t in series if t["type"] == "tv")
tv = service.inspect_link(f"https://{service.SOURCE_HOST}/it/titles/{series['id']}-{series['slug']}")
assert tv["seasons"] and tv["episodes"]
print(json.dumps({"series": tv["name"], "seasons": len(tv["seasons"]),
                  "episodes_in_first_season": len(tv["episodes"])}))
