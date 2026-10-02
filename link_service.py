"""Download da un link SC. Una coda SQLite e un solo worker, senza Jellyfin."""
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid

import requests
import animeunity_provider
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

SOURCE_HOST = os.getenv("SOURCE_HOST", "streamingcommunityz.pictures")
PUBLIC_HOST = os.getenv("PUBLIC_HOST", "localhost:8000")
WATCH_TZ = ZoneInfo(os.getenv("TZ", "Europe/Rome"))
watch_lock = threading.Lock()
ROOT = Path(os.getenv("DOWNLOAD_ROOT", "/downloads"))
STATE = Path(os.getenv("STATE_ROOT", "/config"))
DB = STATE / "jobs.sqlite3"
RESERVE = 5 * 1024 ** 3
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
stop = threading.Event()
wakeup = threading.Event()
active_lock = threading.Lock()
active = {}

# Il motore di terze parti registra URL firmati nei messaggi diagnostici.
# Non attivare questi logger: DB, API e log del servizio non conservano token.
logging.getLogger("app").disabled = True
for name in ("app.core.film", "app.core.tv", "app.core._shared", "app.core.m3u8",
             "app.core.format", "app.core.probe", "app.core.container", "app.core.animeunity"):
    logging.getLogger(name).disabled = True


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect():
    con = sqlite3.connect(DB, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=15000")
    try:
        with con:
            yield con
    finally:
        con.close()


def initialize():
    STATE.mkdir(parents=True, exist_ok=True)
    for name in ("Movies", "TV", ".incomplete"):
        (ROOT / name).mkdir(parents=True, exist_ok=True)
    with connect() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, content_key TEXT NOT NULL, title TEXT NOT NULL,
          payload TEXT NOT NULL, status TEXT NOT NULL, phase TEXT NOT NULL DEFAULT '',
          progress REAL NOT NULL DEFAULT 0, bytes INTEGER NOT NULL DEFAULT 0,
          error TEXT, output TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""")
        watch_schema = """(
          id TEXT PRIMARY KEY, title_id INTEGER NOT NULL, name TEXT NOT NULL,
          url TEXT NOT NULL, year TEXT NOT NULL DEFAULT '', mode TEXT NOT NULL DEFAULT 'notify',
          enabled INTEGER NOT NULL DEFAULT 1, snapshot TEXT NOT NULL, last_checked TEXT,
          scheduled_date TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL,
          provider TEXT NOT NULL DEFAULT 'streamingcommunity', dubbed INTEGER NOT NULL DEFAULT 1,
          UNIQUE(provider,title_id)
        )"""
        con.execute("CREATE TABLE IF NOT EXISTS watches " + watch_schema)
        con.execute("""CREATE TABLE IF NOT EXISTS watch_items (
          watch_id TEXT NOT NULL, episode_id INTEGER NOT NULL, season INTEGER NOT NULL,
          number INTEGER NOT NULL, name TEXT NOT NULL, discovered_at TEXT NOT NULL,
          PRIMARY KEY(watch_id,episode_id)
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS watch_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, watch_id TEXT, name TEXT NOT NULL,
          checked_at TEXT NOT NULL, status TEXT NOT NULL, new_episodes INTEGER NOT NULL,
          new_seasons TEXT NOT NULL, queued INTEGER NOT NULL, error TEXT
        )""")
        if "year" not in {r[1] for r in con.execute("PRAGMA table_info(watches)")}:
            con.execute("ALTER TABLE watches ADD COLUMN year TEXT NOT NULL DEFAULT ''")
        if "provider" not in {r[1] for r in con.execute("PRAGMA table_info(watches)")}:
            # Replace the old unique title_id constraint with a source-aware key.
            con.execute("CREATE TABLE watches_next " + watch_schema)
            cols = "id,title_id,name,url,year,mode,enabled,snapshot,last_checked,scheduled_date,error,created_at"
            con.execute(f"INSERT INTO watches_next ({cols}) SELECT {cols} FROM watches")
            con.execute("DROP TABLE watches")
            con.execute("ALTER TABLE watches_next RENAME TO watches")
        if "dubbed" not in {r[1] for r in con.execute("PRAGMA table_info(watches)")}:
            con.execute("ALTER TABLE watches ADD COLUMN dubbed INTEGER NOT NULL DEFAULT 1")
        columns = {r[1] for r in con.execute("PRAGMA table_info(jobs)")}
        for column in ("verified_at", "verification"):
            if column not in columns:
                con.execute(f"ALTER TABLE jobs ADD COLUMN {column} TEXT")
        con.execute("""CREATE UNIQUE INDEX IF NOT EXISTS jobs_active_content
          ON jobs(content_key) WHERE status IN ('queued','running','done')""")
        con.execute("""UPDATE jobs SET status='error',
          error='Download interrotto dal riavvio. Premi Riprova.', updated_at=?
          WHERE status='running'""", (now(),))
        # Il processo può fermarsi fra il link atomico e l'ultimo commit SQLite.
        rows = con.execute("SELECT id,output FROM jobs WHERE status='error' AND phase='publishing' AND output IS NOT NULL").fetchall()
        for row in rows:
            output = (ROOT / row["output"]).resolve()
            if output.is_relative_to(ROOT.resolve()) and output.is_file():
                try:
                    verify_video(output)
                except Exception:
                    continue
                con.execute("UPDATE jobs SET status='done',phase='done',progress=100,error=NULL,updated_at=? WHERE id=?",
                            (now(), row["id"]))
    os.chmod(DB, 0o600)


def update(job_id, **values):
    allowed = {"status", "phase", "progress", "bytes", "error", "output"}
    if not values or not set(values) <= allowed:
        raise ValueError("Campi stato non validi")
    values["updated_at"] = now()
    with connect() as con:
        con.execute("UPDATE jobs SET " + ",".join(f"{k}=?" for k in values) + " WHERE id=?",
                    [*values.values(), job_id])


def provider_for_url(url):
    try:
        host = urlsplit(url.strip()).hostname
    except ValueError:
        raise HTTPException(400, "Link non valido")
    return "animeunity" if host in animeunity_provider.HOSTS else "streamingcommunity"


def title_key(title_id, provider="streamingcommunity"):
    return f"animeunity:{title_id}" if provider == "animeunity" else str(title_id)


def validate_url(url):
    if provider_for_url(url) == "animeunity":
        return animeunity_provider.validate_url(url)
    try:
        p = urlsplit(url.strip())
        port = p.port
    except ValueError:
        raise HTTPException(400, "Link non valido")
    if p.scheme != "https" or p.hostname != SOURCE_HOST or port not in (None, 443) or p.username or p.password:
        raise HTTPException(400, f"Usa un link HTTPS di {SOURCE_HOST} oppure {animeunity_provider.HOST}")
    match = re.fullmatch(r"/(?:it/)?(?:titles|watch)/(\d+)(?:-[\w-]+)?/?", p.path)
    if not match:
        raise HTTPException(400, "Incolla il link della pagina di un film o di una serie")
    ep = parse_qs(p.query).get("e", [None])[0]
    if ep is not None and not ep.isdigit():
        raise HTTPException(400, "Identificativo episodio non valido")
    return int(match[1]), int(ep) if ep else None


def fetch_props(url):
    # Le pagine Inertia contengono metadati pubblici, senza cookie o credenziali.
    for _ in range(5):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != SOURCE_HOST or parsed.port not in (None, 443) or parsed.username or parsed.password:
            raise RuntimeError("Il sito ha cambiato dominio: aggiorna SOURCE_HOST")
        res = requests.get(url, headers={"User-Agent": UA}, timeout=(5, 20), allow_redirects=False)
        if res.is_redirect:
            from urllib.parse import urljoin
            url = urljoin(url, res.headers["Location"])
            continue
        res.raise_for_status()
        element = BeautifulSoup(res.text, "lxml").find("div", id="app")
        if not element or not element.get("data-page"):
            raise RuntimeError("Pagina non riconosciuta o sorgente temporaneamente indisponibile")
        return json.loads(element["data-page"]).get("props", {})
    raise RuntimeError("Troppi reindirizzamenti dal sito")


def inspect_link(url, season=None):
    if provider_for_url(url) == "animeunity":
        return animeunity_provider.inspect_link(url, season)
    title_id, selected_episode = validate_url(url)
    # Il sito richiede anche lo slug: /titles/55355 da solo risponde 404.
    path = urlsplit(url.strip()).path
    query = f"?e={selected_episode}" if selected_episode else ""
    props = fetch_props(f"https://{SOURCE_HOST}{path}{query}")
    title = props.get("title") or {}
    if int(title.get("id", -1)) != title_id or title.get("type") not in ("movie", "tv"):
        raise RuntimeError("Film o serie non riconosciuti")
    slug = title.get("slug", "")
    if not re.fullmatch(r"[\w-]+", slug):
        raise RuntimeError("Indirizzo del titolo non riconosciuto")
    canonical = f"https://{SOURCE_HOST}/it/titles/{title_id}-{slug}"
    result = {"id": title_id, "name": title["name"], "type": title["type"],
              "year": (title.get("release_date") or "")[:4], "url": canonical, "provider": "streamingcommunity",
              "seasons": [], "season": None, "episodes": [], "selected_episode": selected_episode}
    if title["type"] == "movie":
        return result
    seasons = sorted({int(s["number"]) for s in title.get("seasons", []) if s.get("number") is not None})
    if not seasons:
        seasons = list(range(1, min(int(title.get("seasons_count", 0)), 100) + 1))
    if not seasons:
        raise RuntimeError("La serie non contiene stagioni disponibili")
    chosen = season if season is not None else seasons[0]
    if chosen not in seasons:
        raise HTTPException(400, "Stagione non disponibile")
    props = fetch_props(f"{canonical}/season-{chosen}")
    eps = (props.get("loadedSeason") or {}).get("episodes") or []
    result.update(seasons=seasons, season=chosen,
                  episodes=[{"id": int(e["id"]), "number": e["number"], "name": e.get("name") or ""}
                            for e in eps])
    return result


def safe_error(exc):
    if isinstance(exc, requests.exceptions.Timeout):
        return "La sorgente non risponde in tempo. Premi Riprova."
    if isinstance(exc, requests.exceptions.HTTPError):
        return f"La sorgente ha risposto HTTP {exc.response.status_code}. Premi Riprova."
    text = str(exc)
    # Mai restituire URL firmati, frammenti dell'embed, token o stderr del motore.
    if "http" in text.lower() or "token" in text.lower() or "snippet" in text.lower() or len(text) > 250:
        return "La sorgente video non è disponibile o il formato è cambiato. Premi Riprova."
    return text[:250] or "Download non riuscito"


def publish(source, destination):
    """Hard link sullo stesso filesystem: atomico e senza sovrascritture."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.link(source, destination)
    source.unlink()


def verify_video(path):
    res = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "format=duration:stream=codec_type", "-of", "json", str(path)],
                         capture_output=True, text=True, timeout=45)
    if res.returncode:
        raise RuntimeError("Il file scaricato non è un video valido")
    data = json.loads(res.stdout)
    kinds = {s.get("codec_type") for s in data.get("streams", [])}
    if "video" not in kinds or "audio" not in kinds or float(data.get("format", {}).get("duration", 0)) <= 0:
        raise RuntimeError("Il file scaricato non contiene video e audio validi")


class Progress:
    def __init__(self, job_id, event, total=0, phase="video", prior_bytes=0, **kwargs):
        self.job_id, self.event, self.total, self.phase = job_id, event, total, phase
        self.n, self.size, self.prior_bytes = 0, 0, prior_bytes
        self.last = 0
        self.lock = threading.Lock()
        self.disk_error = False

    @property
    def bytes_done(self):
        return self.prior_bytes + self.size

    def update(self, n=1, bytes=0):
        with self.lock:
            self.n += n
            self.size += bytes
            tick = time.monotonic()
            if tick - self.last < 1 and self.n < self.total:
                return
            self.last = tick
            free = shutil.disk_usage(ROOT).free
            # Una copia segmenti, una concatenata e il file muxato: riserva anche
            # spazio per la lavorazione, con stima prudente dopo 20 segmenti.
            estimated = self.size * self.total / self.n if self.n >= 20 else 0
            if free < RESERVE + max(0, estimated - self.size) + estimated * 2:
                self.disk_error = True
                self.event.set()
            update(self.job_id, phase=self.phase, progress=round(self.n * 100 / self.total, 1) if self.total else 0,
                   bytes=self.bytes_done)

    def emit_status(self, phase):
        update(self.job_id, phase=phase)

    def close(self):
        pass

    def refresh(self):
        pass


def run_job(row, event):
    from app.core.film import download_film
    from app.core.tv import download_episode, get_token
    payload = json.loads(row["payload"])
    # Tutte le scritture del motore, anche eventuali cancellazioni di file
    # sostituiti, restano nell'area privata di questo job.
    scratch = ROOT / ".incomplete" / row["id"]
    scratch.mkdir(parents=True, exist_ok=True)
    bars = []

    def factory(**kwargs):
        prior = bars[-1].bytes_done if bars else 0
        bar = Progress(row["id"], event, prior_bytes=prior, **kwargs)
        bars.append(bar)
        return bar

    common = {"domain": SOURCE_HOST, "output_dir": str(scratch / "output"),
              "temp_dir": str(scratch / "segments"), "progress_factory": factory,
              "cancel_event": event, "audio_languages": ["ita"], "strict_audio": True,
              "year": payload["year"] or None}
    try:
        if shutil.disk_usage(ROOT).free < RESERVE:
            raise RuntimeError("Spazio insufficiente: sono richiesti almeno 5 GiB liberi")
        if payload.get("provider") == "animeunity":
            output = Path(animeunity_provider.download(payload, **common))
            category = "Movies" if payload["type"] == "movie" else "TV"
            target = ROOT / category / output.relative_to(scratch / "output")
        elif payload["type"] == "movie":
            output = Path(download_film(id_film=payload["id"], title_name=payload["name"], **common))
            target = ROOT / "Movies" / output.relative_to(scratch / "output")
        else:
            episode = payload["episode"]
            output = Path(download_episode(tv_id=payload["id"],
                          eps=[{"id": episode["id"], "n": episode["number"], "name": episode["name"]}],
                          ep_index=0, token=get_token(payload["id"], SOURCE_HOST),
                          tv_name=payload["name"], season=payload["season"], **common))
            target = ROOT / "TV" / output.relative_to(scratch / "output")
        if event.is_set():
            raise RuntimeError("Download annullato")
        update(row["id"], phase="verifying")
        verify_video(output)
        if event.is_set():
            raise RuntimeError("Download annullato")
        update(row["id"], output=str(target.relative_to(ROOT)), phase="publishing")
        try:
            publish(output, target)
        except FileExistsError:
            raise RuntimeError("Il file esiste già: non è stato sovrascritto")
        update(row["id"], status="done", phase="done", progress=100, error=None)
        # Solo i temporanei del job completato, mai cartelle media o volumi.
        shutil.rmtree(scratch)
    except Exception as exc:
        disk_error = any(b.disk_error for b in bars)
        status = "cancelled" if event.is_set() and not disk_error else "error"
        message = "Spazio insufficiente per completare e assemblare il video" if disk_error else safe_error(exc)
        update(row["id"], status=status, error=message)


def worker():
    while not stop.is_set():
        with connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if row:
                # Registrare l'evento prima dello stato running evita una
                # cancellazione persa nel passaggio dalla coda al worker.
                with active_lock:
                    event = threading.Event()
                    active[row["id"]] = event
                    con.execute("UPDATE jobs SET status='running', phase='resolving', updated_at=? WHERE id=?",
                                (now(), row["id"]))
        if row:
            run_job(row, event)
            with active_lock:
                active.pop(row["id"], None)
        else:
            wakeup.wait(2)
            wakeup.clear()


@asynccontextmanager
async def lifespan(app):
    stop.clear()
    initialize()
    thread = threading.Thread(target=worker, daemon=True, name="download-worker")
    app.state.worker_thread = thread
    thread.start()
    watcher = threading.Thread(target=watch_scheduler, daemon=True, name="watch-scheduler")
    app.state.watch_thread = watcher
    watcher.start()
    yield
    stop.set()
    with active_lock:
        for event in active.values():
            event.set()
    wakeup.set()
    import asyncio
    await asyncio.to_thread(thread.join, 35)
    await asyncio.to_thread(watcher.join, 5)


api = FastAPI(title="SC Link", lifespan=lifespan, docs_url=None, redoc_url=None)


@api.middleware("http")
async def same_origin(request: Request, call_next):
    host = request.headers.get("host", "")
    valid_hosts = {PUBLIC_HOST, "127.0.0.1:8000", "localhost:8000", "testserver"}
    if host not in valid_hosts:
        return JSONResponse({"detail": "Host non autorizzato"}, status_code=403)
    if request.method not in ("GET", "HEAD"):
        if request.headers.get("origin") not in (None, f"http://{host}"):
            return JSONResponse({"detail": "Origine non autorizzata"}, status_code=403)
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            return JSONResponse({"detail": "Richiesta JSON necessaria"}, status_code=415)
        if int(request.headers.get("content-length", "0")) > 16384:
            return JSONResponse({"detail": "Richiesta troppo grande"}, status_code=413)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


class LinkRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2000)
    season: int | None = Field(default=None, ge=0, le=100)


class DownloadRequest(LinkRequest):
    episode_ids: list[int] = Field(default_factory=list, max_length=200)
    selections: list["SeasonSelection"] = Field(default_factory=list, max_length=101)


class SeasonSelection(BaseModel):
    season: int = Field(ge=0, le=100)
    episode_ids: list[int] = Field(default_factory=list, max_length=200)


DownloadRequest.model_rebuild()


@api.get("/")
def index():
    return FileResponse(Path(__file__).with_name("index.html"))


@api.get("/api/health")
def health():
    with connect() as con:
        con.execute("SELECT 1").fetchone()
    thread = getattr(api.state, "worker_thread", None)
    watcher = getattr(api.state, "watch_thread", None)
    worker_ok = thread is None or thread.is_alive()
    watcher_ok = watcher is None or watcher.is_alive()
    healthy = worker_ok and watcher_ok
    return JSONResponse({"ok": healthy, "worker": worker_ok, "scheduler": watcher_ok}, status_code=200 if healthy else 503)


@api.get("/api/info")
def info():
    disk = shutil.disk_usage(ROOT)
    return {"source_host": SOURCE_HOST, "free_bytes": disk.free,
            "download_path": str(ROOT), "reserve_bytes": RESERVE, "timezone": str(WATCH_TZ), "providers": {"streamingcommunity": SOURCE_HOST, "animeunity": animeunity_provider.HOST}}


@api.post("/api/inspect")
def inspect(body: LinkRequest):
    try:
        return inspect_link(body.url, body.season)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, safe_error(exc))


@api.get("/api/downloads")
def downloads():
    with connect() as con:
        rows = con.execute("SELECT id,title,status,phase,progress,bytes,error,output,created_at,updated_at,verified_at,verification FROM jobs ORDER BY created_at DESC LIMIT 200").fetchall()
    return [dict(row) for row in rows]


def insert_jobs(con, items):
    inserted, existing = [], []
    if not con.in_transaction:
        con.execute("BEGIN IMMEDIATE")
    pending = []
    seen = set()
    for metadata, episode in items:
        key = f"{title_key(metadata['id'], metadata.get('provider', 'streamingcommunity'))}:{episode['id'] if episode else 'movie'}"
        if key in seen:
            continue
        seen.add(key)
        duplicate = con.execute("SELECT id FROM jobs WHERE content_key=? AND status IN ('queued','running','done')", (key,)).fetchone()
        if duplicate:
            existing.append(duplicate[0])
        else:
            pending.append((metadata, episode, key))
    queued = con.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
    if queued + len(pending) > 200:
        raise HTTPException(409, "La coda può contenere al massimo 200 download attivi. Riduci la selezione.")
    if pending and shutil.disk_usage(ROOT).free < RESERVE:
        raise HTTPException(409, "Spazio insufficiente: sono richiesti almeno 5 GiB liberi")
    for metadata, episode, key in pending:
        payload = {k: metadata[k] for k in ("id", "name", "type", "year", "url", "season")}
        payload["provider"] = metadata.get("provider", "streamingcommunity")
        payload["dubbed"] = metadata.get("dubbed", True)
        payload["episode"] = episode
        job_id = uuid.uuid4().hex
        label = metadata["name"]
        if episode:
            label += f" · S{metadata['season']:02d}E{str(episode['number']).zfill(2)}"
        con.execute("INSERT INTO jobs(id,content_key,title,payload,status,created_at,updated_at) VALUES(?,?,?,?,'queued',?,?)",
                    (job_id, key, label, json.dumps(payload), now(), now()))
        inserted.append(job_id)
    return {"queued": inserted, "existing": existing}


@api.post("/api/downloads")
def enqueue(body: DownloadRequest):
    if body.selections:
        if body.episode_ids or body.season is not None:
            raise HTTPException(400, "Usa selections oppure season/episode_ids")
        if len({s.season for s in body.selections}) != len(body.selections):
            raise HTTPException(400, "Stagioni duplicate nella selezione")
        if sum(len(s.episode_ids) for s in body.selections) > 200:
            raise HTTPException(400, "Seleziona al massimo 200 episodi per richiesta")
        selections = body.selections
    else:
        selections = [SeasonSelection(season=body.season or 0, episode_ids=body.episode_ids)]
    items = []
    for selection in selections:
        metadata = inspect(LinkRequest(url=body.url, season=selection.season if body.selections else body.season))
        if metadata["type"] == "tv":
            selected = set(selection.episode_ids)
            episodes = [e for e in metadata["episodes"] if e["id"] in selected]
            if not selected or len(episodes) != len(selected):
                raise HTTPException(400, "Seleziona almeno un episodio disponibile per stagione")
        else:
            if metadata.get("provider") == "animeunity" and not metadata["episodes"]:
                raise HTTPException(409, "Il film AnimeUnity non ha ancora un episodio pubblicato")
            if body.selections or body.episode_ids:
                raise HTTPException(400, "Un film non contiene stagioni o episodi")
            episodes = [None]
        items.extend((metadata, e) for e in episodes)
    with connect() as con:
        result = insert_jobs(con, items)
    wakeup.set()
    return result


@api.post("/api/downloads/{job_id}/cancel")
def cancel(job_id: str):
    # Stesso ordine dei lock del worker: SQLite, poi active_lock.
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Download non trovato")
        if row[0] == "queued":
            con.execute("UPDATE jobs SET status='cancelled', error='Download annullato', updated_at=? WHERE id=?", (now(), job_id))
        elif row[0] == "running":
            with active_lock:
                if job_id in active:
                    active[job_id].set()
    return {"ok": True}


@api.post("/api/downloads/{job_id}/retry")
def retry(job_id: str):
    with connect() as con:
        row = con.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Download non trovato")
        if row[0] not in ("error", "cancelled"):
            raise HTTPException(409, "Questo download non richiede un nuovo tentativo")
        try:
            con.execute("UPDATE jobs SET status='queued',error=NULL,progress=0,phase='',updated_at=? WHERE id=?", (now(), job_id))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Lo stesso contenuto è già in coda o scaricato")
    wakeup.set()
    return {"ok": True}


class WatchRequest(LinkRequest):
    mode: str = Field(default="notify", pattern="^(notify|auto)$")


class WatchSettings(BaseModel):
    mode: str = Field(pattern="^(notify|auto)$")
    enabled: bool


def local_date():
    return datetime.now(WATCH_TZ).date().isoformat()


def next_check():
    current = datetime.now(WATCH_TZ)
    midnight = datetime.combine(current.date() + timedelta(days=1), datetime.min.time(), WATCH_TZ)
    return midnight.isoformat()


def series_catalog(url):
    metadata = inspect_link(url)
    if metadata["type"] != "tv":
        raise HTTPException(400, "Il watch richiede il link di una serie TV")
    catalog = {str(metadata["season"]): metadata["episodes"]}
    for season in metadata["seasons"]:
        if season != metadata["season"]:
            catalog[str(season)] = inspect_link(metadata["url"], season)["episodes"]
    return metadata, catalog


@api.post("/api/watches")
def add_watch(body: WatchRequest):
    title_id, _ = validate_url(body.url)
    with connect() as con:
        provider = provider_for_url(body.url)
        if con.execute("SELECT id FROM watches WHERE title_id=? AND provider=?", (title_id, provider)).fetchone():
            raise HTTPException(409, "Questa serie è già seguita")
    try:
        metadata, catalog = series_catalog(body.url)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, safe_error(exc))
    watch_id = uuid.uuid4().hex
    try:
        with connect() as con:
            con.execute("INSERT INTO watches(id,title_id,name,url,year,mode,snapshot,last_checked,scheduled_date,created_at,provider,dubbed) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (watch_id, metadata["id"], metadata["name"], metadata["url"], metadata["year"], body.mode,
                         json.dumps(catalog), now(), local_date(), now(), provider, metadata.get("dubbed", True)))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Questa serie è già seguita")
    return {"id": watch_id, "baseline_episodes": sum(map(len, catalog.values())), "next_check": next_check()}


@api.get("/api/watches")
def watches():
    with connect() as con:
        rows = con.execute("SELECT * FROM watches ORDER BY created_at DESC").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            snapshot = json.loads(item.pop("snapshot"))
            item["seasons"] = sorted(map(int, snapshot))
            item["episode_count"] = sum(map(len, snapshot.values()))
            discoveries = con.execute("""SELECT w.*, (SELECT j.status FROM jobs j
                WHERE j.content_key=? || ':' || w.episode_id ORDER BY j.created_at DESC LIMIT 1) AS download_status
                FROM watch_items w WHERE watch_id=? ORDER BY season,number""",
                (title_key(row["title_id"], row["provider"]), row["id"])).fetchall()
            item["discoveries"] = [dict(r) for r in discoveries]
            item["next_check"] = next_check() if item["enabled"] else None
            item["checking"] = row["id"] in checking
            result.append(item)
    return result


checking = set()
checking_lock = threading.Lock()


def check_watch(watch_id, scheduled=False):
    with connect() as con:
        row = con.execute("SELECT * FROM watches WHERE id=?", (watch_id,)).fetchone()
    if not row or not row["enabled"]:
        return
    count, seasons, queued, error = 0, [], 0, None
    try:
        metadata, catalog = series_catalog(row["url"])
        previous = json.loads(row["snapshot"])
        known = {e["id"] for episodes in previous.values() for e in episodes}
        new = [(int(s), e) for s, eps in catalog.items() for e in eps if e["id"] not in known]
        seasons = sorted(int(s) for s in catalog if s not in previous)
        with connect() as con:
            con.execute("BEGIN IMMEDIATE")
            # Recheck the settings after a network scan: a pause/delete applies immediately.
            current = con.execute("SELECT mode,enabled FROM watches WHERE id=?", (watch_id,)).fetchone()
            if not current or not current["enabled"]:
                return
            for season, episode in new:
                con.execute("INSERT OR IGNORE INTO watch_items VALUES(?,?,?,?,?,?)",
                            (watch_id, episode["id"], season, episode["number"], episode["name"], now()))
            # Keep previously seen IDs if the source temporarily omits an episode/season.
            merged = {s: {e["id"]: e for e in eps} for s, eps in previous.items()}
            for s, eps in catalog.items():
                merged.setdefault(s, {}).update({e["id"]: e for e in eps})
            con.execute("UPDATE watches SET snapshot=?,last_checked=?,error=NULL,scheduled_date=? WHERE id=?",
                        (json.dumps({s: list(e.values()) for s, e in merged.items()}), now(),
                         local_date() if scheduled else row["scheduled_date"], watch_id))
            count = len(new)
        if current["mode"] == "auto":
            queued = queue_watch_items(watch_id, automatic=True)["queued_count"]
        wakeup.set()
    except Exception as exc:
        error = safe_error(exc)
        with connect() as con:
            con.execute("UPDATE watches SET last_checked=?,error=?,scheduled_date=? WHERE id=?",
                        (now(), error, local_date() if scheduled else row["scheduled_date"], watch_id))
    finally:
        with connect() as con:
            con.execute("INSERT INTO watch_events(watch_id,name,checked_at,status,new_episodes,new_seasons,queued,error) VALUES(?,?,?,?,?,?,?,?)",
                        (watch_id, row["name"], now(), "error" if error else "ok", count, json.dumps(seasons), queued, error))
            con.execute("DELETE FROM watch_events WHERE id NOT IN (SELECT id FROM watch_events ORDER BY id DESC LIMIT 200)")


def queue_watch_items(watch_id, automatic=False):
    with connect() as con:
        row = con.execute("SELECT * FROM watches WHERE id=?", (watch_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Watch non trovato")
        # Failed/cancelled jobs remain visible for explicit retry, never automatic retry storms.
        items = con.execute("""SELECT w.* FROM watch_items w WHERE watch_id=? AND NOT EXISTS
          (SELECT 1 FROM jobs j WHERE j.content_key=? || ':' || w.episode_id)""",
          (watch_id, title_key(row["title_id"], row["provider"]))).fetchall()
    metadata = {"id": row["title_id"], "name": row["name"], "type": "tv", "year": row["year"], "url": row["url"], "provider": row["provider"], "dubbed": bool(row["dubbed"])}
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        current = con.execute("SELECT mode,enabled FROM watches WHERE id=?", (watch_id,)).fetchone()
        if not current or (automatic and (not current["enabled"] or current["mode"] != "auto")):
            return {"queued": [], "existing": [], "queued_count": 0}
        result = insert_jobs(con, [({**metadata, "season": e["season"]},
                                   {"id": e["episode_id"], "number": e["number"], "name": e["name"]}) for e in items])
    wakeup.set()
    return {**result, "queued_count": len(result["queued"])}


def perform_check(watch_id, scheduled=False):
    try:
        check_watch(watch_id, scheduled)
    finally:
        with checking_lock:
            checking.discard(watch_id)
        watch_lock.release()


@api.post("/api/watches/{watch_id}/check")
def manual_check(watch_id: str):
    with connect() as con:
        row = con.execute("SELECT enabled FROM watches WHERE id=?", (watch_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Watch non trovato")
    if not row["enabled"]:
        raise HTTPException(409, "Riattiva il watch prima di controllare")
    if not watch_lock.acquire(blocking=False):
        raise HTTPException(409, "Un controllo è già in corso. Riprova tra poco.")
    with checking_lock:
        checking.add(watch_id)
    threading.Thread(target=perform_check, args=(watch_id,), daemon=True).start()
    return {"checking": True}


@api.post("/api/watches/{watch_id}/settings")
def edit_watch(watch_id: str, body: WatchSettings):
    with connect() as con:
        if not con.execute("SELECT id FROM watches WHERE id=?", (watch_id,)).fetchone():
            raise HTTPException(404, "Watch non trovato")
        con.execute("UPDATE watches SET mode=?,enabled=? WHERE id=?", (body.mode, body.enabled, watch_id))
    return {"ok": True}


@api.post("/api/watches/{watch_id}/remove")
def remove_watch(watch_id: str):
    with connect() as con:
        con.execute("DELETE FROM watches WHERE id=?", (watch_id,))
        con.execute("DELETE FROM watch_items WHERE watch_id=?", (watch_id,))
    return {"ok": True}


@api.post("/api/watches/{watch_id}/download")
def download_watch_updates(watch_id: str):
    return queue_watch_items(watch_id)


def scheduler_tick():
    today = local_date()
    with connect() as con:
        rows = con.execute("SELECT id FROM watches WHERE enabled=1 AND scheduled_date<? ORDER BY created_at", (today,)).fetchall()
    for row in rows:
        if stop.is_set():
            return
        if not watch_lock.acquire(blocking=False):
            return
        with checking_lock:
            checking.add(row["id"])
        perform_check(row["id"], scheduled=True)


def watch_scheduler():
    while not stop.is_set():
        try:
            scheduler_tick()
        except Exception:
            logging.getLogger("sc-link").error("Controllo watch non riuscito; nuovo tentativo al prossimo ciclo")
        stop.wait(20)


@api.get("/api/dashboard")
def dashboard():
    disk = shutil.disk_usage(ROOT)
    with connect() as con:
        counts = {r[0]: r[1] for r in con.execute("SELECT status,count(*) FROM jobs GROUP BY status")}
        watch_count = con.execute("SELECT count(*) FROM watches WHERE enabled=1").fetchone()[0]
        errors = con.execute("SELECT count(*) FROM watches WHERE error IS NOT NULL").fetchone()[0]
        events = [dict(r) for r in con.execute("SELECT * FROM watch_events ORDER BY id DESC LIMIT 30")]
        for e in events:
            e["new_seasons"] = json.loads(e["new_seasons"])
    worker_thread = getattr(api.state, "worker_thread", None)
    watch_thread = getattr(api.state, "watch_thread", None)
    return {"counts": counts, "active_watches": watch_count, "watch_errors": errors,
            "events": events, "checking": list(checking), "next_check": next_check(), "timezone": str(WATCH_TZ),
            "checks": {"database": True, "download_worker": bool(worker_thread and worker_thread.is_alive()),
                       "watch_scheduler": bool(watch_thread and watch_thread.is_alive()), "disk": disk.free >= RESERVE},
            "disk": {"free": disk.free, "total": disk.total, "reserve": RESERVE}, "source_host": SOURCE_HOST, "providers": {"streamingcommunity": SOURCE_HOST, "animeunity": animeunity_provider.HOST}}


@api.post("/api/downloads/{job_id}/verify")
def verify_download(job_id: str):
    with connect() as con:
        row = con.execute("SELECT status,output FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Download non trovato")
    if row["status"] != "done" or not row["output"]:
        raise HTTPException(409, "Puoi verificare solo un download completato")
    target = (ROOT / row["output"]).resolve()
    error = None
    try:
        if not target.is_relative_to(ROOT.resolve()) or not target.is_file():
            raise RuntimeError("Il file non è presente nella libreria")
        verify_video(target)
    except Exception as exc:
        error = safe_error(exc)
    with connect() as con:
        con.execute("UPDATE jobs SET verified_at=?,verification=? WHERE id=?", (now(), error or "ok", job_id))
    return {"ok": error is None, "error": error}


@api.get("/assets/{filename}")
def asset(filename: str):
    if filename not in {"app.js", "style.css"}:
        raise HTTPException(404, "Risorsa non trovata")
    return FileResponse(Path(__file__).with_name(filename))
