"""AnimeUnity metadata adapter. Stream URLs and tokens stay in the external engine."""
import os
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

import requests
from fastapi import HTTPException

HOST = os.getenv("ANIMEUNITY_HOST", "www.animeunity.so")
HOSTS = {HOST, HOST.removeprefix("www.")}
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
BATCH_SIZE = 120
MAX_EPISODES = 5000


def validate_url(url):
    try:
        parsed = urlsplit(url.strip())
        valid = (parsed.scheme == "https" and parsed.hostname in HOSTS and
                 parsed.port in (None, 443) and not parsed.username and not parsed.password)
    except ValueError:
        valid = False
    if not valid:
        raise HTTPException(400, f"Usa un link HTTPS di {HOST}")
    match = re.fullmatch(r"/anime/(\d+)(?:-[\w-]+)?/?", parsed.path)
    if not match:
        raise HTTPException(400, "Incolla il link della pagina di un anime")
    return int(match[1]), None


def fetch_json(path, params=None):
    response = requests.get(f"https://{HOST}{path}", params=params,
                            headers={"User-Agent": UA, "Accept": "application/json"},
                            timeout=(5, 25), allow_redirects=False)
    if response.is_redirect:
        raise RuntimeError("AnimeUnity ha cambiato indirizzo: verifica ANIMEUNITY_HOST")
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("Risposta AnimeUnity non riconosciuta")
    return data


def normalize_episode(raw, anime_id):
    if raw.get("anime_id") is not None and int(raw["anime_id"]) != anime_id:
        raise RuntimeError("Episodio AnimeUnity associato a un'altra serie")
    if raw.get("hidden") in (1, "1", True) or raw.get("public") in (0, "0", False):
        return None
    try:
        parts = str(raw["number"]).split("-")
        numbers = [Decimal(p) for p in parts]
        episode_id = int(raw["id"])
        if len(parts) > 2 or any(not n.is_finite() or n < 0 for n in numbers) or numbers[-1] < numbers[0] or episode_id < 1:
            raise ValueError()
    except (KeyError, ValueError, InvalidOperation):
        raise RuntimeError("Episodio AnimeUnity non riconosciuto")
    # Keep specials such as 12.5 instead of truncating to a duplicate E12.
    label = "-".join(format(n.normalize(), "f") for n in numbers)
    return {"id": episode_id, "number": label, "name": raw.get("name") or f"Episodio {label}"}


def inspect_link(url, season=None):
    anime_id, _ = validate_url(url)
    if season not in (None, 1):
        raise HTTPException(400, "AnimeUnity raccoglie gli episodi di questa pagina nella stagione 1")
    title = fetch_json(f"/info_api/{anime_id}")
    if int(title.get("id", -1)) != anime_id:
        raise RuntimeError("Anime non riconosciuto")
    slug = title.get("slug") or ""
    name = title.get("title_eng") or title.get("title")
    if not name or not re.fullmatch(r"[\w-]+", slug):
        raise RuntimeError("Titolo AnimeUnity non riconosciuto")
    # info_api counts released episodes, unlike the anime record's planned total.
    count = int(title.get("episodes_count", 0))
    if count < 0 or count > MAX_EPISODES:
        raise RuntimeError("Conteggio episodi AnimeUnity non valido")
    episodes = {}
    for start in range(1, count+1, BATCH_SIZE):
        batch = fetch_json(f"/info_api/{anime_id}/0", {"start_range": start, "end_range": min(start+BATCH_SIZE-1, count)})
        raw_episodes = batch.get("episodes")
        if not isinstance(raw_episodes, list):
            raise RuntimeError("Catalogo episodi AnimeUnity incompleto")
        if not raw_episodes:
            raise RuntimeError("Catalogo episodi AnimeUnity incompleto. Riprova più tardi.")
        for raw in raw_episodes:
            episode = normalize_episode(raw, anime_id)
            if episode:
                episodes[episode["id"]] = episode
    ordered = sorted(episodes.values(), key=lambda e: (Decimal(e["number"].split("-")[0]), e["id"]))
    movie = str(title.get("type", "")).lower() == "movie" and len(ordered) <= 1
    return {"id": anime_id, "name": name, "type": "movie" if movie else "tv",
            "provider": "animeunity", "media_kind": "anime", "dubbed": bool(title.get("dub")),
            "year": str(title.get("date") or "")[:4],
            "url": f"https://{HOST}/anime/{anime_id}-{slug}",
            "seasons": [] if movie else [1], "season": None if movie else 1,
            "episodes": ordered, "selected_episode": None}


def download(payload, **common):
    from app.core import animeunity
    animeunity.ANIMEUNITY_HOST = HOST
    episode = payload.get("episode")
    if episode is None:
        metadata = inspect_link(payload["url"])
        if len(metadata["episodes"]) != 1:
            raise RuntimeError("Il film AnimeUnity non ha un episodio disponibile")
        episode = metadata["episodes"][0]
    # The engine expects no domain parameter: AnimeUnity has its own endpoint.
    common.pop("domain", None)
    audio = "ita" if payload.get("dubbed", True) else "jpn"
    common["audio_languages"] = [audio]
    return animeunity.download_anime_episode(
        anime_id=str(payload["id"]), episode=episode, anime_name=payload["name"],
        anime_type=payload["type"], **common)
