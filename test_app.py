import httpx
from fastapi.testclient import TestClient

from app import create_app
from providers import AudiusProvider, NotAllowed, Track, Unavailable

KEY = "a-local-test-key-with-more-than-24-chars"
AUTH = {"Authorization": "Bearer " + KEY}


class Listing:
    name = "audius"

    async def search(self, query, scope, limit):
        return [Track("audius:weak", "Огонь", "Новая группа", duration_ms=192000),
                Track("audius:real", "Огонь", "Новая группа", duration_ms=192000, playable=True, downloadable=True),
                Track("audius:other", "Огонь в ночи", "Другой артист")]

    async def resolve(self, track_id, mode):
        if track_id != "real" or mode == "download":
            raise NotAllowed()
        return "https://api.audius.co/v1/tracks/real/stream"


class Broken:
    name = "vk"

    async def search(self, query, scope, limit):
        raise Unavailable()

    async def resolve(self, track_id, mode):
        raise NotAllowed()


def test_search_health_auth_relevance_dedup_and_partial_failure():
    with TestClient(create_app(KEY, [Broken(), Listing()])) as client:
        assert client.get("/healthz").json()["status"] == "ok"
        assert client.get("/v1/search", params={"q": "Огонь"}).status_code == 401
        result = client.get("/v1/search", params={"q": "Огонь"}, headers=AUTH)
        assert result.status_code == 200
        assert [track["id"] for track in result.json()["tracks"]] == ["audius:real", "audius:other"]
        assert result.json()["unavailable"] == ["vk"]


def test_resolution_is_authorized_and_permissions_are_rechecked():
    with TestClient(create_app(KEY, [Listing()])) as client:
        assert client.get("/v1/tracks/audius:real/stream").status_code == 401
        assert client.get("/v1/tracks/audius:real/stream", headers=AUTH).json()["url"].endswith("/stream")
        assert client.get("/v1/tracks/audius:real/download", headers=AUTH).status_code == 403


def test_audius_requires_opt_in_and_access():
    base = {"id": "demo", "title": "Track", "is_streamable": True, "access": {"stream": True, "download": True}}
    assert AudiusProvider.track(base).playable
    assert not AudiusProvider.track(base).downloadable
    assert AudiusProvider.track(dict(base, is_downloadable=True)).downloadable
    assert not AudiusProvider.track(dict(base, is_downloadable=True, is_download_gated=True)).downloadable
    assert not AudiusProvider.track(dict(base, is_stream_gated=True)).playable


def test_album_search_uses_artist_album_titles():
    def respond(request):
        if request.url.path.endswith("/playlists/search"):
            return httpx.Response(200, json={"data": [{"id": "ab", "playlist_name": "Album", "is_album": True}]})
        assert request.url.path.endswith("/playlists/ab/tracks")
        return httpx.Response(200, json={"data": [{"id": "tr", "title": "Song"}]})

    class TestAudius(AudiusProvider):
        def __init__(self):
            pass

        async def search(self, query, scope, limit):
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as self.http:
                return await super().search(query, scope, limit)

    with TestClient(create_app(KEY, [TestAudius()])) as client:
        result = client.get("/v1/search", params={"q": "Album", "scope": "album"}, headers=AUTH).json()
        assert result["tracks"][0]["album"] == "Album"

