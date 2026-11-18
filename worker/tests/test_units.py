import asyncio
import io
import json

import pytest
from PIL import Image, ImageDraw

from app import youtube as yt
from app.llm.base import make_llm
from app.llm.fake import FakeLLM
from app.ratelimit.limiter import Limiter, bucket_specs
from app.retry import pick_delay
from app.stages.frames import sharpness
from app.stages.summarize import parse_synthesis
from app.stages.transcribe import check_info
from vidnotes_common.config import Settings
from vidnotes_common.errors import ErrorCode, FatalError, RateLimited, RetryableError


def settings(**kw):
    return Settings(_env_file=None, token_enc_key="x", **kw)


# ---- retry / backoff ----------------------------------------------------------

def test_backoff_schedule_is_bounded_and_monotonic():
    d = [10, 30, 90, 180]
    assert [pick_delay(a, d) for a in (1, 2, 3, 4, 5, 9)] == [10, 30, 90, 180, 180, 180]


def test_retry_after_longer_than_default_wins():
    d = [10, 30, 90, 180]
    assert pick_delay(1, d, retry_after=25) == 30
    assert pick_delay(1, d, retry_after=5) == 10
    assert pick_delay(1, d, retry_after=999) == 180


def test_demo_profile_reaches_dlq_in_about_75s():
    d = [5, 10, 20, 40]
    assert sum(pick_delay(a, d) for a in range(1, 5)) == 75  # 4 waits between 5 attempts


# ---- error classification -------------------------------------------------------

@pytest.mark.parametrize(
    "msg,cls,code",
    [
        ("ERROR: [youtube] abc: Private video. Sign in if you've been granted access", RetryableError,
         ErrorCode.VIDEO_UNAVAILABLE),
        ("ERROR: [youtube] abc: Video unavailable. This video has been removed", RetryableError,
         ErrorCode.VIDEO_UNAVAILABLE),
        ("ERROR: HTTP Error 429: Too Many Requests", RateLimited, ErrorCode.RATE_LIMITED),
        ("ERROR: Sign in to confirm you're not a bot", RetryableError, ErrorCode.YOUTUBE_BLOCKED),
    ],
)
def test_ytdlp_errors_are_classified(msg, cls, code):
    e = yt.classify_ytdlp_error(msg)
    assert type(e) is cls and e.code == code


def test_transcript_api_error_classification():
    names = {"TranscriptsDisabled": "no_captions", "NoTranscriptFound": "no_captions",
             "VideoUnavailable": "unavailable", "RequestBlocked": "blocked", "IpBlocked": "blocked",
             "SomethingElse": "other"}
    for name, kind in names.items():
        assert yt.classify_transcript_api_error(type(name, (Exception,), {})()) == kind


def test_constraint_a_and_live_checks():
    check_info({"duration": 1200}, 1200)  # exactly 20 min is allowed
    with pytest.raises(FatalError) as e:
        check_info({"duration": 2040}, 1200)
    assert e.value.code == ErrorCode.VIDEO_TOO_LONG and "34 min" in e.value.message
    with pytest.raises(FatalError) as e:
        check_info({"duration": 0, "live_status": "is_upcoming"}, 1200)
    assert e.value.code == ErrorCode.LIVE_STREAM


# ---- transcripts ------------------------------------------------------------------

def test_merge_segments_into_30s_windows():
    raw = [{"start": i * 4.0, "duration": 4.0, "text": f"w{i}\n"} for i in range(30)]
    raw.append({"start": 1.0, "duration": 1, "text": "[Music]"})
    segs = yt.merge_segments(raw)
    assert [s["start"] for s in segs] == [0.0, 32.0, 64.0, 96.0]
    assert segs[0]["text"].startswith("w0 w1") and "[Music]" not in segs[0]["text"]
    assert all(s["end"] >= s["start"] for s in segs)


def test_parse_json3_captions():
    data = {"events": [{"tStartMs": 1000, "dDurationMs": 2000, "segs": [{"utf8": "hello "}, {"utf8": "world"}]},
                       {"tStartMs": 3000, "segs": [{"utf8": "\n"}]},
                       {"tStartMs": 4000}]}
    assert yt.parse_json3(data) == [{"start": 1.0, "duration": 2.0, "text": "hello world"}]


def test_pick_caption_track_prefers_english():
    tracks = {"de": [1], "en-GB": [2], "fr-orig": [3]}
    assert yt._pick_track(tracks) == [2]
    assert yt._pick_track({"fr-orig": [3], "de": [1]}) == [3]


# ---- LLM output parsing -------------------------------------------------------------

def test_parse_synthesis_tolerates_fences_and_prose():
    obj = {"title": "t", "tldr": "x", "sections": [{"heading": "h", "start_sec": "12.7", "bullets": ["b"]}],
           "visual_moments": [{"timestamp_sec": 15.2, "caption": "c", "reason": "r", "section_index": 0}]}
    s = parse_synthesis("Sure! ```json\n" + json.dumps(obj) + "\n```")
    assert s.sections[0].start_sec == 12 and s.visual_moments[0].timestamp_sec == 15


def test_parse_synthesis_rejects_garbage():
    with pytest.raises(ValueError):
        parse_synthesis("not json")


def test_fake_llm_produces_valid_synthesis():
    llm = FakeLLM(settings(llm_provider="fake"))
    user = "Video title: Demo\nTranscript ([mm:ss] text):\n[0:00] hello\n[3:10] look at this slide here\n[6:30] end"
    out = asyncio.run(llm.complete_json("sys", user, {}, 1000))
    s = parse_synthesis(out)
    assert s.title == "Demo" and len(s.sections) == 3
    assert s.visual_moments[0].timestamp_sec == 192 and s.visual_moments[0].section_index == 1


def test_make_llm_rejects_unknown_provider():
    with pytest.raises(ValueError):
        make_llm(settings(llm_provider="nope"))


def test_injected_429():
    llm = FakeLLM(settings(llm_provider="fake", fault_inject_429="llm:1.0"))
    with pytest.raises(RateLimited):
        asyncio.run(llm.complete_json("s", "u", {}, 10))


# ---- rate limiter ---------------------------------------------------------------------

class FakeScript:
    """Python mirror of bucket.lua for testing the acquire loop."""

    def __init__(self):
        self.state = {}
        self.now = 0.0

    async def __call__(self, keys, args):
        cap, rate, req = (float(a) for a in args)
        req = min(req, cap)
        tokens, ts = self.state.get(keys[0], (cap, self.now))
        tokens = min(cap, tokens + (self.now - ts) * rate)
        if tokens >= req:
            self.state[keys[0]] = (tokens - req, self.now)
            return [1, 0]
        self.state[keys[0]] = (tokens, self.now)
        return [0, int((req - tokens) * 1000 / rate) + 1]


class FakeRedis:
    def __init__(self):
        self.script = FakeScript()

    def register_script(self, _lua):
        return self.script


def test_bucket_specs_respect_config():
    b = bucket_specs(settings(llm_rpm=10, llm_tpm=60000, notion_rps=2.5, youtube_rps=2))
    assert b["llm_rpm"].rate_per_sec == pytest.approx(10 / 60)
    assert b["llm_tpm"].rate_per_sec == pytest.approx(1000)
    assert b["notion"].capacity == 3 and b["notion"].rate_per_sec == 2.5


def test_limiter_grants_then_raises_rate_limited_when_wait_exceeds_budget():
    r = FakeRedis()
    lim = Limiter(r, settings(llm_rpm=1, rl_max_wait_sec=0.05))

    async def go():
        await lim.acquire("llm_rpm")  # bucket starts full (capacity 1)
        with pytest.raises(RateLimited) as e:
            await lim.acquire("llm_rpm")  # next token in ~60 s > 0.05 s budget
        return e.value

    err = asyncio.run(go())
    assert err.service == "llm" and err.retry_after > 50


def test_limiter_keys_are_per_suffix():
    r = FakeRedis()
    lim = Limiter(r, settings(notion_rps=1))
    asyncio.run(lim.acquire("notion", key_suffix="tokA"))
    asyncio.run(lim.acquire("notion", key_suffix="tokB"))  # separate bucket: no wait
    assert set(r.script.state) == {"rl:notion:tokA", "rl:notion:tokB"}


# ---- frames ---------------------------------------------------------------------------

def test_sharpness_prefers_detailed_frame():
    sharp = Image.new("RGB", (320, 180), "white")
    d = ImageDraw.Draw(sharp)
    for x in range(0, 320, 8):
        d.line([(x, 0), (x, 180)], fill="black")
    blurry = Image.new("RGB", (320, 180), (128, 128, 128))
    assert sharpness(sharp) > sharpness(blurry)
    buf = io.BytesIO()
    sharp.save(buf, "JPEG")
    assert buf.tell() > 0
