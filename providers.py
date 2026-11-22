from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

import httpx

ID = re.compile(r"[A-Za-z0-9_-]{1,32}")
AUDIUS = "https://api.audius.co/v1"


def clean(value: object, size: int = 180) -> str:
    return value.strip()[:size] if isinstance(value, str) else ""


def public_url(value: object) -> str:
    address = clean(value, 1400)
    try:
        parts = urlsplit(address)
        if parts.scheme == "https" and parts.hostname and "." in parts.hostname and not parts.username and not parts.password and parts.port in (None, 443):
            return address
    except ValueError:
        pass
    return ""


@dataclass(frozen=True)
class Track:
    id: str
    title: str
    artist: str
    album: str = ""
    duration_ms: int = 0
    cover: str = ""
    source: str = ""
    playable: bool = False
    downloadable: bool = False


class MusicSourceProvider(Protocol):
    name: str

    async def search(self, query: str, scope: str, limit: int) -> list[Track]: ...
    async def resolve(self, track_id: str, mode: str) -> str: ...


class Unavailable(RuntimeError):
    pass


class NotAllowed(RuntimeError):
    pass


class AudiusProvider:
    name = "audius"

    def __init__(self, http: httpx.AsyncClient):
        self.http = http

    async def get(self, path: str, **params):
        for attempt in range(2):
            try:
                reply = await self.http.get(AUDIUS + path, params=params)
                if reply.status_code == 404:
                    return None
                if reply.status_code == 429:
                    raise Unavailable("rate limited")
                reply.raise_for_status()
                return reply.json().get("data")
            except (httpx.HTTPError, ValueError):
                if attempt:
                    raise Unavailable("Audius unavailable") from None
                await asyncio.sleep(.25)

    @staticmethod
    def track(row: dict, album: str = "") -> Track | None:
        key, title = clean(row.get("id")), clean(row.get("title"))
        if not ID.fullmatch(key) or not title:
            return None
        rights = row.get("access") or {}
        artwork = row.get("artwork") or {}
        seconds = row.get("duration")
        return Track("audius:" + key, title, clean((row.get("user") or {}).get("name")),
                     album or clean(row.get("album_name") or row.get("album")),
                     min(86400000, max(0, int(seconds * 1000))) if isinstance(seconds, (int, float)) else 0,
                     public_url(artwork.get("480x480")), "Audius",
                     row.get("is_streamable") is True and rights.get("stream") is True and row.get("is_stream_gated") is not True,
                     row.get("is_downloadable") is True and rights.get("download") is True and row.get("is_download_gated") is not True)

    async def search(self, query: str, scope: str, limit: int) -> list[Track]:
        if scope == "album":
            rows = await self.get("/playlists/search", query=query, limit=30) or []
            candidates = [row for row in rows if ID.fullmatch(clean(row.get("id")))]
            albums = [row for row in candidates if row.get("is_album") is True]
            # Some artists publish releases as playlists and identify them by title.
            if not albums:
                marker = re.compile(r"\b(?:album|альбом)\b", re.IGNORECASE)
                albums = [row for row in candidates if marker.search(clean(row.get("playlist_name")))]
            albums = albums[:3]
            responses = await asyncio.gather(*(self.get("/playlists/" + row["id"] + "/tracks") for row in albums),
                                             return_exceptions=True)
            if responses and all(isinstance(batch, Exception) for batch in responses):
                raise Unavailable("Audius album tracks unavailable")
            return [track for album, batch in zip(albums, responses) if isinstance(batch, list)
                    for row in batch[:limit] if (track := self.track(row, clean(album.get("playlist_name"))))]
        if scope == "artist":
            users = await self.get("/users/search", query=query, limit=4) or []
            users = [user for user in users if ID.fullmatch(clean(user.get("id")))][:3]
            responses = await asyncio.gather(*(self.get("/users/" + user["id"] + "/tracks", limit=limit) for user in users),
                                             return_exceptions=True)
            if responses and all(isinstance(batch, Exception) for batch in responses):
                raise Unavailable("Audius artist tracks unavailable")
            return [track for batch in responses if isinstance(batch, list) for row in batch[:limit]
                    if (track := self.track(row))]
        rows = await self.get("/tracks/search", query=query, limit=limit) or []
        return [track for row in rows if (track := self.track(row))]

    async def resolve(self, track_id: str, mode: str) -> str:
        if not ID.fullmatch(track_id):
            raise NotAllowed()
        row = await self.get("/tracks/" + track_id)
        song = self.track(row) if isinstance(row, dict) else None
        if not song or not (song.downloadable if mode == "download" else song.playable):
            raise NotAllowed()
        return AUDIUS + "/tracks/" + track_id + ("/download?original=false" if mode == "download" else "/stream")


class VkProvider:
    name = "vk"

    def __init__(self, token: str):
        self.token = token

    async def search(self, query: str, scope: str, limit: int) -> list[Track]:
        if scope == "album":
            return []
        try:
            from vkpymusic import Service
            for name, logger in logging.Logger.manager.loggerDict.items():
                if name.startswith("vkpymusic") and isinstance(logger, logging.Logger):
                    logger.disabled = True
            service = Service("Localify/2.1", self.token)
            songs = await asyncio.wait_for(service.search_songs_by_text_async(query, count=limit), 5)
        except Exception:
            raise Unavailable("VK catalog unavailable") from None
        tracks = []
        for song in songs:
            key = str(song.owner_id) + "_" + str(song.track_id)
            if ID.fullmatch(key) and clean(song.title):
                tracks.append(Track("vk:" + key, clean(song.title), clean(song.artist),
                                    duration_ms=max(0, int(song.duration)) * 1000, source="VK"))
        return tracks

    async def resolve(self, track_id: str, mode: str) -> str:
        # An audio URL from audio.getById does not establish permission for third-party use.
        raise NotAllowed()

