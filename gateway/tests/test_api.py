"""Gateway contract tests that don't need Postgres/RabbitMQ: validation, error shape,
Constraint A at submit time, and metadata parsing. Full flows run in the compose
integration test (ops/smoke_test.sh)."""

import asyncio

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import youtube_meta
from app.main import create_app
from app.state import AppState
from vidnotes_common.config import Settings


class Boom:
    """Any attribute access means the route touched a dependency it shouldn't have."""

    def __getattr__(self, name):
        raise AssertionError(f"unexpected use of .{name}")


@pytest.fixture
def client():
    app = create_app(with_lifespan=False)
    s = Settings(_env_file=None, token_enc_key=Fernet.generate_key().decode(), max_duration_sec=1200)
    app.state.vn = AppState(settings=s, engine=Boom(), publisher=Boom(), redis=Boom(), http=Boom(),
                            cipher=None)
    return TestClient(app)


def test_invalid_url_is_400_with_error_body(client):
    r = client.post("/jobs", json={"youtube_url": "https://vimeo.com/123", "notion_token": "t"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_URL"
    assert set(r.json()["error"]) >= {"code", "message"}


def test_missing_token_is_401(client):
    r = client.post("/jobs", json={"youtube_url": "https://youtu.be/dQw4w9WgXcQ"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "MISSING_NOTION_TOKEN"


def test_malformed_body_is_400_not_422(client):
    r = client.post("/jobs", content=b"{nope", headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_REQUEST"


def test_too_long_video_rejected_at_submit_without_queueing(client, monkeypatch):
    async def fake_lookup(video_id, settings, http):
        return youtube_meta.VideoMeta(video_id, "Long lecture", 2040, False, "test")

    monkeypatch.setattr(youtube_meta, "lookup", fake_lookup)
    r = client.post("/jobs", json={"youtube_url": "https://youtu.be/dQw4w9WgXcQ", "notion_token": "t"})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "VIDEO_TOO_LONG"
    assert err["message"] == "This video is 34 min; the limit is 20 min."
    assert err["details"]["duration_sec"] == 2040


def test_live_stream_rejected(client, monkeypatch):
    async def fake_lookup(video_id, settings, http):
        return youtube_meta.VideoMeta(video_id, "Live", None, True, "test")

    monkeypatch.setattr(youtube_meta, "lookup", fake_lookup)
    r = client.post("/jobs", json={"youtube_url": "https://youtu.be/dQw4w9WgXcQ", "notion_token": "t"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "LIVE_STREAM"


def test_unknown_job_ids_are_404(client):
    assert client.get("/jobs/not-a-uuid").status_code == 404
    assert client.get("/jobs/not-a-uuid/results").json()["error"]["code"] == "JOB_NOT_FOUND"


def test_unknown_route_uses_error_shape(client):
    r = client.get("/nope")
    assert r.status_code == 404 and "error" in r.json()


def test_admin_requires_token(client):
    assert client.get("/admin/jobs").status_code == 401


def test_parse_iso_duration():
    assert youtube_meta.parse_iso_duration("PT19M7S") == 1147
    assert youtube_meta.parse_iso_duration("PT1H2M3S") == 3723
    assert youtube_meta.parse_iso_duration("P0D") is None or youtube_meta.parse_iso_duration("P0D") == 0
    assert youtube_meta.parse_iso_duration("garbage") is None


def test_parse_watch_page():
    page = ('<meta name="title" content="Neural networks &amp; you">'
            '..."videoDetails":{"videoId":"x","title":"ignored","lengthSeconds":"1147","isLiveContent":false}')
    m = youtube_meta.parse_watch_page("x", page)
    assert (m.title, m.duration_sec, m.is_live) == ("Neural networks & you", 1147, False)
    assert youtube_meta.parse_watch_page("x", "<html>private video</html>") is None


def test_lookup_times_out_to_none():
    async def slow(*a, **k):
        await asyncio.sleep(1)

    s = Settings(_env_file=None, metadata_timeout_sec=0.01)
    orig = youtube_meta._via_watch_page
    youtube_meta._via_watch_page = slow
    try:
        assert asyncio.run(youtube_meta.lookup("x", s, None)) is None
    finally:
        youtube_meta._via_watch_page = orig


def test_debug_page_is_served_without_token_or_dependencies(client):
    r = client.get("/debug")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "/admin/debug/overview" in r.text


def test_debug_api_requires_token(client):
    assert client.get("/admin/debug/overview").status_code == 401
    assert client.get("/admin/debug/frame", params={"key": "jobs/x/frame_1.jpg"}).status_code == 401


def test_debug_frame_rejects_keys_outside_jobs_prefix(client):
    h = {"X-Admin-Token": client.app.state.vn.settings.admin_token}
    for key in ("secrets/x.jpg", "jobs/../etc/passwd"):
        r = client.get("/admin/debug/frame", params={"key": key}, headers=h)
        assert r.status_code == 400
