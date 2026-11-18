import uuid

import pytest

from vidnotes_common.config import Settings
from vidnotes_common.crypto import TokenCipher, token_fingerprint
from vidnotes_common.errors import ErrorCode, FatalError, RateLimited, RetryableError, api_error, user_message
from vidnotes_common.messages import StageMessage, retry_queue_name
from vidnotes_common.status import (
    STAGE_ORDER,
    TERMINAL,
    JobStatus,
    Stage,
    allowed_from,
    can_transition,
    next_stage,
    stage_progress,
)
from vidnotes_common.youtube import fmt_ts, parse_video_id

# ---- status / transitions ---------------------------------------------------

def test_happy_path_transitions_are_allowed():
    path = [JobStatus.QUEUED, JobStatus.TRANSCRIBING, JobStatus.SUMMARIZING, JobStatus.EXTRACTING_FRAMES,
            JobStatus.PUBLISHING, JobStatus.COMPLETED]
    for a, b in zip(path, path[1:], strict=False):
        assert can_transition(a, b), (a, b)
    assert can_transition(JobStatus.PUBLISHING, JobStatus.COMPLETED_WITH_WARNINGS)


def test_status_never_moves_backwards_or_skips():
    assert not can_transition(JobStatus.SUMMARIZING, JobStatus.TRANSCRIBING)
    assert not can_transition(JobStatus.QUEUED, JobStatus.SUMMARIZING)
    assert not can_transition(JobStatus.TRANSCRIBING, JobStatus.COMPLETED)


@pytest.mark.parametrize("s", [JobStatus.QUEUED, JobStatus.TRANSCRIBING, JobStatus.SUMMARIZING,
                               JobStatus.EXTRACTING_FRAMES, JobStatus.PUBLISHING])
def test_any_non_terminal_can_fail_or_dead_letter(s):
    assert can_transition(s, JobStatus.FAILED)
    assert can_transition(s, JobStatus.DEAD_LETTERED)


@pytest.mark.parametrize("s", [JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_WARNINGS, JobStatus.FAILED])
def test_terminal_states_are_final(s):
    assert s in TERMINAL
    assert not any(can_transition(s, t) for t in JobStatus)


def test_dead_lettered_can_be_requeued_into_a_stage_only():
    assert can_transition(JobStatus.DEAD_LETTERED, JobStatus.SUMMARIZING)
    assert not can_transition(JobStatus.DEAD_LETTERED, JobStatus.COMPLETED)


def test_allowed_from_matches_map():
    assert set(allowed_from(JobStatus.TRANSCRIBING)) == {JobStatus.QUEUED, JobStatus.DEAD_LETTERED}


def test_stage_order_and_progress():
    assert [next_stage(s) for s in STAGE_ORDER] == [Stage.SUMMARIZE, Stage.EXTRACT_FRAMES, Stage.PUBLISH, None]
    assert stage_progress(Stage.TRANSCRIBE, 0) == 5
    assert stage_progress(Stage.PUBLISH, 1) == 100
    assert stage_progress(Stage.EXTRACT_FRAMES, 5) == 80  # clamped


# ---- messages ---------------------------------------------------------------

def test_stage_message_round_trip_and_helpers():
    m = StageMessage(job_id=uuid.uuid4(), stage=Stage.TRANSCRIBE, enc_notion_token="enc", notion_parent_id="p")
    assert StageMessage.from_bytes(m.to_bytes()) == m
    r = m.retry()
    assert r.attempt == 2 and r.trace_id == m.trace_id and r.stage == m.stage
    n = r.next(Stage.SUMMARIZE)
    assert n.attempt == 1 and n.stage == Stage.SUMMARIZE and n.enc_notion_token == "enc"


def test_retry_queue_names_are_per_delay():
    assert retry_queue_name(10) == "vn.retry.10s"


# ---- errors -----------------------------------------------------------------

def test_error_hierarchy_and_bodies():
    rl = RateLimited("notion", retry_after=12)
    assert isinstance(rl, RetryableError) and rl.code == ErrorCode.RATE_LIMITED and rl.retry_after == 12
    f = FatalError(ErrorCode.VIDEO_TOO_LONG, "too long", duration_sec=2000)
    assert f.to_dict()["details"]["duration_sec"] == 2000
    assert api_error("INVALID_URL") == {"error": {"code": "INVALID_URL", "message": user_message("INVALID_URL")}}
    assert "code NOPE" in user_message("NOPE")


# ---- config -----------------------------------------------------------------

def test_retry_delays_parse_from_env(monkeypatch):
    monkeypatch.setenv("RETRY_DELAYS", "40, 5,10,20")
    monkeypatch.setenv("FAULT_INJECT_429", "llm:0.3,notion:0.5,bad")
    monkeypatch.setenv("FAULT_INJECT_FRAME_FAIL", "")
    s = Settings(_env_file=None)
    assert s.retry_delays == [5, 10, 20, 40]
    assert s.fault_429_rates() == {"llm": 0.3, "notion": 0.5}
    assert s.fault_inject_frame_fail == 0.0


# ---- crypto -----------------------------------------------------------------

def test_token_round_trip():
    from cryptography.fernet import Fernet

    c = TokenCipher(Fernet.generate_key().decode())
    blob = c.encrypt("secret_abc")
    assert "secret_abc" not in blob and c.decrypt(blob) == "secret_abc"
    assert token_fingerprint("a") == token_fingerprint("a") != token_fingerprint("b")


def test_cipher_requires_key():
    with pytest.raises(ValueError):
        TokenCipher("")


# ---- youtube urls -----------------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtube.com/watch?v=dQw4w9WgXcQ&t=42s&list=PL123",
        "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ?si=abc",
        "https://www.youtube.com/shorts/dQw4w9WgXcQ",
        "https://www.youtube.com/embed/dQw4w9WgXcQ",
        "https://www.youtube.com/live/dQw4w9WgXcQ",
        "www.youtube.com/watch?v=dQw4w9WgXcQ",
        "dQw4w9WgXcQ",
    ],
)
def test_parse_video_id_accepts_known_forms(url):
    assert parse_video_id(url) == "dQw4w9WgXcQ"


@pytest.mark.parametrize(
    "url",
    ["", "https://example.com/watch?v=dQw4w9WgXcQ", "https://www.youtube.com/watch?v=short",
     "https://www.youtube.com/channel/UC123", "not a url at all", None],
)
def test_parse_video_id_rejects_garbage(url):
    assert parse_video_id(url) is None


def test_fmt_ts():
    assert fmt_ts(461) == "7:41"
    assert fmt_ts(3723) == "1:02:03"
