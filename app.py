from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
import time
import unicodedata
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import asdict

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request

from providers import AudiusProvider, MusicSourceProvider, NotAllowed, Unavailable, VkProvider

log = logging.getLogger("localify")
for logger_name in ("httpx", "httpcore", "vkpymusic"):
    logging.getLogger(logger_name).setLevel(logging.WARNING)


def normalized(value: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", value).casefold()))


def ordered(query, scope, tracks, limit):
    term = normalized(query)
    unique = {}
    for track in tracks:
        key = (normalized(track.title), normalized(track.artist), round(track.duration_ms / 5000))
        existing = unique.get(key)
        if existing is None or (track.playable, track.downloadable) > (existing.playable, existing.downloadable):
            unique[key] = track

    def rank(track):
        fields = [normalized(track.title), normalized(track.artist), normalized(track.album)]
        preferred = fields[{"track": 0, "artist": 1, "album": 2}[scope]]
        words = set(" ".join(fields).split())
        return (preferred == term, term in fields, term in preferred, any(term in item for item in fields),
                len(words.intersection(term.split())), track.playable, track.downloadable)

    return sorted(unique.values(), key=rank, reverse=True)[:limit]


class Limits:
    def __init__(self, per_minute):
        self.per_minute = per_minute
        self.buckets = OrderedDict()

    def allow(self, key):
        now = time.monotonic()
        amount, previous = self.buckets.pop(key, (self.per_minute, now))
        amount = min(self.per_minute, amount + (now - previous) * self.per_minute / 60)
        self.buckets[key] = (max(0, amount - 1), now)
        if len(self.buckets) > 2048:
            self.buckets.popitem(last=False)
        return amount >= 1


def create_app(key=None, sources=None):
    @asynccontextmanager
    async def lifespan(app):
        secret = key if key is not None else os.environ.get("LOCALIFY_API_KEY", "")
        if not secret or len(secret) < 24:
            raise RuntimeError("Set LOCALIFY_API_KEY to a random value of at least 24 characters")
        app.state.key = hashlib.sha256(secret.encode()).digest()
        app.state.cache = OrderedDict()
        app.state.limit = Limits(75)
        app.state.total = Limits(300)
        app.state.work = asyncio.Semaphore(12)
        async with httpx.AsyncClient(timeout=httpx.Timeout(5, connect=3), trust_env=False,
                                     limits=httpx.Limits(max_connections=18), headers={"User-Agent": "Localify/2.1"}) as http:
            vk = os.environ.get("VK_CATALOG_TOKEN", "").strip()
            vk_user_agent = os.environ.get("VK_CATALOG_USER_AGENT", "").strip() or "Localify/2.1"
            app.state.providers = list(sources) if sources is not None else [
                *([VkProvider(vk, vk_user_agent)] if vk else []), AudiusProvider(http)]
            app.state.hitmox_check = {"state": "pending"}
            async def probe_hitmox():
                try:
                    async with asyncio.timeout(8):
                        async with http.stream("GET", "https://ru.hitmox.net/search",
                                               params={"q": "System of a Down"}) as resp:
                            body = bytearray()
                            if resp.status_code == 200:
                                async for chunk in resp.aiter_bytes(chunk_size=8192):
                                    body.extend(chunk)
                                    if len(body) >= 262144:
                                        break
                            app.state.hitmox_check = {
                                "http": resp.status_code, "mp3_links": body.count(b"/get/music/")
                            }
                except (httpx.HTTPError, TimeoutError) as exc:
                    app.state.hitmox_check = {"error": type(exc).__name__}
                log.warning("HITMOX_CHECK %s", app.state.hitmox_check)
            probe = asyncio.create_task(probe_hitmox())
            try:
                yield
            finally:
                probe.cancel()
                await asyncio.gather(probe, return_exceptions=True)

    app = FastAPI(title="Localify Music", version="2.1", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def response_headers(request, call_next):
        reply = await call_next(request)
        reply.headers["Cache-Control"] = "no-store"
        reply.headers["X-Content-Type-Options"] = "nosniff"
        return reply

    def checked(request, authorization):
        client = request.client.host if request.client else "unknown"
        if not request.app.state.limit.allow(client):
            raise HTTPException(429, "Слишком много запросов", headers={"Retry-After": "15"})
        supplied = authorization.removeprefix("Bearer ") if authorization and authorization.startswith("Bearer ") else ""
        if not hmac.compare_digest(hashlib.sha256(supplied.encode()).digest(), request.app.state.key):
            raise HTTPException(401, "Проверьте ключ сервера")
        if not request.app.state.total.allow("global"):
            raise HTTPException(429, "Сервер занят", headers={"Retry-After": "15"})

    @app.get("/hitmox-check-20261122")
    async def hitmox_check(request: Request):
        return request.app.state.hitmox_check

    @app.get("/healthz")
    async def health():
        return {"status": "ok", "api": 1}

    @app.get("/v1/status")
    async def status(request: Request, authorization: str | None = Header(default=None)):
        checked(request, authorization)
        return {"status": "ok", "sources": [source.name for source in request.app.state.providers]}

    @app.get("/v1/search")
    async def search(request: Request, q: str = Query(min_length=2, max_length=120),
                     scope: str = Query(default="track", pattern="^(track|artist|album)$"),
                     limit: int = Query(default=30, ge=1, le=40), authorization: str | None = Header(default=None)):
        checked(request, authorization)
        q = " ".join(q.split())
        if len(normalized(q)) < 2:
            raise HTTPException(422, "Введите название или имя исполнителя")
        state = request.app.state
        cache_key = (q.casefold(), scope, limit)
        if old := state.cache.get(cache_key):
            if old[0] > time.monotonic():
                state.cache.move_to_end(cache_key)
                return old[1]

        async def load(provider: MusicSourceProvider):
            try:
                return await asyncio.wait_for(provider.search(q, scope, limit), 13)
            except Exception:
                log.warning("search unavailable provider=%s", provider.name)
                return None

        async with state.work:
            outcome = await asyncio.gather(*(load(provider) for provider in state.providers))
        if all(batch is None for batch in outcome):
            raise HTTPException(503, "Музыкальные источники временно недоступны")
        tracks = ordered(q, scope, (track for batch in outcome if batch is not None for track in batch), limit)
        blocked = [provider.name for provider, batch in zip(state.providers, outcome) if batch is None]
        result = {"tracks": [asdict(track) for track in tracks], "unavailable": blocked}
        state.cache[cache_key] = (time.monotonic() + (20 if blocked else 120), result)
        if len(state.cache) > 192:
            state.cache.popitem(last=False)
        return result

    @app.get("/v1/tracks/{track_id}/{mode}")
    async def media(request: Request, track_id: str, mode: str, authorization: str | None = Header(default=None)):
        checked(request, authorization)
        if mode not in {"stream", "download"} or not re.fullmatch(r"[a-z]{2,16}:[A-Za-z0-9_-]{1,32}", track_id):
            raise HTTPException(404, "Нет такого трека")
        name, value = track_id.split(":", 1)
        provider = next((item for item in request.app.state.providers if item.name == name), None)
        if provider is None:
            raise HTTPException(404, "Источник не подключён")
        try:
            async with request.app.state.work:
                url = await asyncio.wait_for(provider.resolve(value, mode), 12)
        except NotAllowed:
            raise HTTPException(403, "Источник не разрешает этот способ получения") from None
        except (Unavailable, TimeoutError):
            log.warning("media unavailable provider=%s mode=%s", name, mode)
            raise HTTPException(503, "Источник временно недоступен") from None
        return {"url": url}

    return app


app = create_app()

