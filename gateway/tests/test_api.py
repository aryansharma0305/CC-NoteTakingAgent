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


def test_invalid_job_options_are_400_before_any_work(client):
    r = client.post("/jobs", json={"youtube_url": "https://youtu.be/dQw4w9WgXcQ", "notion_token": "t",
                                   "options": {"detail": "everything", "max_frames": 50}})
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "INVALID_REQUEST" and "options" in err["message"]


def test_valid_job_options_reach_the_queue_message(client, monkeypatch):
    sent = {}

    class Pub:
        async def publish(self, msg, headers=None):
            sent["msg"] = msg

    class Conn:
        async def execute(self, *a, **k):
            return None

    class Begin:
        async def __aenter__(self):
            return Conn()

        async def __aexit__(self, *exc):
            return False

    class Engine:
        def begin(self):
            return Begin()

    async def no_meta(video_id, settings, http):
        return None

    monkeypatch.setattr(youtube_meta, "lookup", no_meta)
    st = client.app.state.vn
    st.publisher, st.engine = Pub(), Engine()
    st.cipher = type("C", (), {"encrypt": staticmethod(lambda t: "enc:" + t)})()
    r = client.post("/jobs", json={"youtube_url": "https://youtu.be/dQw4w9WgXcQ", "notion_token": "t",
                                   "options": {"detail": "brief", "max_frames": 0, "language": "German"}})
    assert r.status_code == 202
    o = sent["msg"].options
    assert (o.detail, o.max_frames, o.language) == ("brief", 0, "German")


# ---- metrics ------------------------------------------------------------------------------

def test_dashboard_page_is_served_and_metrics_need_the_token(client):
    r = client.get("/dashboard")
    assert r.status_code == 200 and "Video Notes" in r.text and "/admin/metrics" in r.text
    assert client.get("/admin/metrics").status_code == 401


def test_metrics_compute_latency_retries_recovery_llm_and_cache():
    import uuid as _uuid
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace as NS

    from app.routes_metrics import compute, summarize

    now = datetime(2026, 11, 23, 12, 30, tzinfo=UTC)
    t0 = now - timedelta(minutes=50)
    a, b, c = _uuid.uuid4(), _uuid.uuid4(), _uuid.uuid4()
    rows = [
        NS(id=a, status="COMPLETED", created_at=t0, updated_at=t0 + timedelta(seconds=300), duration_sec=1100,
           video_title="Long lecture", error=None),
        NS(id=b, status="DEAD_LETTERED", created_at=t0, updated_at=t0 + timedelta(seconds=80), duration_sec=200,
           video_title="Private", error={"code": "RETRIES_EXHAUSTED", "last_error": {"code": "VIDEO_UNAVAILABLE"}}),
        NS(id=c, status="COMPLETED", created_at=t0, updated_at=t0 + timedelta(seconds=20), duration_sec=1100,
           video_title="Long lecture again", error=None),
    ]
    at = lambda s: t0 + timedelta(seconds=s)  # noqa: E731
    ev = [
        NS(id=1, job_id=a, at=at(2), worker_id="w1", detail={"event": "claimed", "stage": "TRANSCRIBE"}),
        NS(id=2, job_id=a, at=at(4), worker_id="w1",
           detail={"event": "stage_done", "stage": "TRANSCRIBE", "seconds": 2}),
        NS(id=3, job_id=a, at=at(14), worker_id="w2",
           detail={"event": "claimed", "stage": "SUMMARIZE", "deferred": True}),
        NS(id=4, job_id=a, at=at(100), worker_id="w3", detail={"event": "claimed", "stage": "SUMMARIZE",
                                                              "recovered_from": "w2", "recovery_sec": 3.2}),
        NS(id=5, job_id=a, at=at(250), worker_id="w3", detail={"event": "stage_done", "stage": "SUMMARIZE",
                                                              "seconds": 150}),
        NS(id=6, job_id=b, at=at(1), worker_id="w1", detail={"event": "retry_scheduled",
                                                            "error": {"code": "VIDEO_UNAVAILABLE"}}),
        NS(id=7, job_id=b, at=at(2), worker_id="w1", detail={"event": "retry_scheduled",
                                                            "error": {"code": "RATE_LIMITED"}}),
        NS(id=8, job_id=c, at=at(1), worker_id="w1", detail={"event": "stage_done", "stage": "SUMMARIZE",
                                                            "seconds": 0.1, "cached_from": str(a)}),
    ]
    syn = [NS(job_id=a, data={"model": "ollama:qwen", "llm_trace": {"calls": [
        {"part": 1, "resumed_from_checkpoint": True},
        {"part": 2, "seconds": 50, "queued_sec": 4, "parse_error": None, "provider_stats": {"tokens_per_sec": 12.0}},
        {"part": "overview", "seconds": 40, "queued_sec": 0, "parse_error": "bad", "provider_stats": {}},
    ]}}), NS(job_id=c, data={"cached_from": str(a), "llm_trace": {"calls": []}})]

    m = compute(rows, ev, syn, since=now - timedelta(hours=2), hours=2, now=now)
    assert m["jobs"]["total"] == 3 and sum(h["submitted"] for h in m["jobs"]["per_hour"]) == 3
    assert m["latency"]["by_video_length"]["15–20 min"]["count"] == 2
    assert m["latency"]["handoff_wait_sec"]["max"] == 10.0  # stage_done@4 -> next claim@14 (a lock wait)
    assert m["latency"]["stage_sec"]["SUMMARIZE"]["count"] == 2
    assert m["reliability"]["retries_total"] == 2 and m["reliability"]["llm_429s"] == 1
    assert m["reliability"]["lock_deferrals"] == 1 and m["reliability"]["dead_lettered"] == 1
    assert m["reliability"]["failures_by_code"] == {"DEAD_LETTERED:VIDEO_UNAVAILABLE": 1}
    assert m["recovery"]["count"] == 1 and m["recovery"]["events"][0]["from_worker"] == "w2"
    assert m["recovery"]["recovery_sec"]["avg"] == 3.2
    assert m["llm"]["calls"] == 2 and m["llm"]["parse_errors"] == 1 and m["llm"]["resumed_from_checkpoint"] == 1
    assert m["llm"]["tokens_per_sec"]["p50"] == 12.0
    assert m["cache"] == {"hits_by_stage": {"SUMMARIZE": 1}, "hits": 1}
    assert summarize([]) == {"count": 0, "avg": None, "p50": None, "p95": None, "max": None}
    assert summarize([1, 2, 3, 4, 100])["p95"] == 100
