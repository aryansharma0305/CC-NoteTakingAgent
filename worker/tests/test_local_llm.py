"""Tests for the local-model pipeline: lenient schema parsing, output cleanup,
chunk checkpoint resume, study aids, the Ollama adapter and the LLM gate."""

import asyncio
import json
import os
import uuid
from types import SimpleNamespace

import httpx
import pytest
import respx

from app.llm.chunking import clean_partial, merge_partials, notes_as_text
from app.llm.ollama import OllamaLLM, ollama_base
from app.llm.prompts import overview_system_prompt, section_target, user_prompt
from app.llm.schema import Overview, Synthesis, parse_ts
from app.stages import summarize
from vidnotes_common.config import Settings
from vidnotes_common.errors import ErrorCode, FatalError, RetryableError
from vidnotes_common.messages import JobOptions, StageMessage


def settings(**kw):
    return Settings(_env_file=None, **kw)


# ---- schema ------------------------------------------------------------------------------

@pytest.mark.parametrize("raw,sec", [
    (85, 85), (85.9, 85), ("85", 85), ("1:25", 85), ("01:25", 85), ("[1:25]", 85),
    ("1:02:03", 3723), ("at 3:05", 185), ("", 0), (None, 0), (-4, 0),
])
def test_parse_ts_accepts_what_small_models_write(raw, sec):
    assert parse_ts(raw) == sec


def test_parse_ts_rejects_garbage():
    with pytest.raises(ValueError):
        parse_ts("soon")


def test_synthesis_accepts_model_facing_names_and_dumps_internal_ones():
    s = Synthesis.model_validate({
        "title": "T", "summary": "S",
        "sections": [{"heading": "H", "start": "2:05", "bullets": ["a", "", "b"]}],
        "visuals": [{"time": "2:10", "caption": "c", "reason": "r"}],
    })
    d = s.model_dump()
    assert d["tldr"] == "S" and d["sections"][0]["start_sec"] == 125
    assert d["sections"][0]["bullets"] == ["a", "b"]
    assert d["visual_moments"][0]["timestamp_sec"] == 130
    # Old artifacts (internal names) still load.
    assert Synthesis.model_validate(d) == s


def test_overview_drops_half_filled_items():
    o = Overview.model_validate({
        "title": "T", "summary": "S", "key_takeaways": ["x", 3, None, {"a": 1}],
        "glossary": [{"term": "A", "definition": "B"}, {"term": "only"}, "junk"],
        "review_questions": [{"question": "Q?", "answer": ""}, {"question": "Q2?", "answer": "A2"}],
    })
    assert o.key_takeaways == ["x", "3"]
    assert [g.term for g in o.glossary] == ["A"]
    assert [q.question for q in o.review_questions] == ["Q2?"]


# ---- cleanup / merge ---------------------------------------------------------------------

CHUNK = [{"start": 100.0 + 10 * i, "end": 110.0 + 10 * i, "text": f"line {i}"} for i in range(30)]  # 100..400 s


def test_clean_partial_snaps_and_clamps_timestamps_into_the_chunk():
    p = Synthesis.model_validate({
        "title": "**Neural nets**", "summary": "s",
        "sections": [
            {"heading": "Section 2: Later topic", "start": "5:07", "bullets": ["- [5:07] **Key**: fact two"]},
            {"heading": "Early", "start": "0:10", "bullets": ["1. fact one", "fact one",
                                                             "Please like and subscribe to the channel"]},
            {"heading": "Empty", "start": "3:00", "bullets": []},
            {"heading": "Way past the end", "start": "59:00", "bullets": ["fact three"]},
        ],
        "visuals": [{"time": "0:01", "caption": "**c**", "reason": "r"},
                    {"time": "99:00", "caption": "c2", "reason": ""}],
    })
    c = clean_partial(p, CHUNK)
    assert [(s.heading, s.start_sec) for s in c.sections] == [
        ("Early", 100), ("Later topic", 300), ("Way past the end", 390)]
    assert c.sections[0].bullets == ["fact one"]  # deduped, list marker and filler removed
    assert c.sections[1].bullets == ["**Key**: fact two"]  # leading stamp gone, bold kept
    assert [m.timestamp_sec for m in c.visual_moments] == [100, 399]
    assert c.visual_moments[0].caption == "c" and c.title == "Neural nets"


def test_merge_folds_renamed_overlap_section_and_assigns_frames_by_time():
    a = Synthesis.model_validate({"sections": [
        {"heading": "Intro", "start_sec": 0, "bullets": ["i"]},
        {"heading": "Sigmoid function", "start_sec": 400, "bullets": ["s1"]}],
        "visual_moments": [{"timestamp_sec": 420, "caption": "curve", "reason": "", "section_index": 0}]})
    b = Synthesis.model_validate({"sections": [
        {"heading": "Sigmoid Function Overview", "start_sec": 430, "bullets": ["s1", "s2"]},
        {"heading": "ReLU", "start_sec": 600, "bullets": ["r"]}]})
    m = merge_partials([a, b], duration_sec=900, max_frames=5)
    assert [s.heading for s in m.sections] == ["Intro", "Sigmoid function", "ReLU"]
    assert m.sections[1].bullets == ["s1", "s2"]
    vm = m.visual_moments[0]
    assert vm.section_index == 1  # by timestamp, not the model's 0
    assert "Sigmoid function" in vm.reason  # empty reason gets an honest fallback


def test_zero_frames_means_text_only():
    a = Synthesis.model_validate({"sections": [{"heading": "A", "start_sec": 0, "bullets": ["x"]}],
                                  "visual_moments": [{"timestamp_sec": 5, "caption": "c", "reason": "r"}]})
    assert merge_partials([a], duration_sec=100, max_frames=0, min_frames=3).visual_moments == []


def test_prompt_helpers():
    assert section_target(0, 1200) == 8 and section_target(0, 60) == 2
    u = user_prompt(video_title="V", duration_sec=600, lines=["[0:01] hi"], part=1, parts=2,
                    start_sec=0, end_sec=300, frames=3, sections=2)
    assert u.rstrip().endswith("copied from the transcript.") and "exactly 3 visuals" in u
    assert "4-5" in overview_system_prompt(takeaways=5) and "in French" in overview_system_prompt(language="French")


# ---- job options -------------------------------------------------------------------------

def test_job_options_validation_and_back_compat():
    o = JobOptions.model_validate({"detail": "brief", "max_frames": 0, "language": "Español<script>", "x": 1})
    assert o.language == "Españolscript" and o.words_factor() == 0.5
    with pytest.raises(ValueError):
        JobOptions.model_validate({"max_frames": 99})
    with pytest.raises(ValueError):
        JobOptions.model_validate({"detail": "huge"})
    old = StageMessage.from_bytes(json.dumps({"job_id": str(uuid.uuid4()), "stage": "SUMMARIZE",
                                              "enc_notion_token": "x"}).encode())
    assert old.options == JobOptions()
    nxt = old.model_copy(update={"options": o}).next(old.stage)
    assert nxt.options.detail == "brief"  # options travel with every stage message


# ---- summarize: checkpoint resume + study aids ---------------------------------------------

class ScriptedLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.last_stats = None

    async def complete_json(self, system, user, schema, max_tokens):
        self.calls.append(("overview" if "key_takeaways" in schema["properties"] else "chunk", user))
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r if isinstance(r, str) else json.dumps(r)


class FreeLimiter:
    async def acquire(self, *a, **k):
        return None


def chunk_reply(t0):
    return {"title": "T", "summary": "S", "sections": [
        {"heading": f"Topic at {t0}", "start": f"{t0 // 60}:{t0 % 60:02d}", "bullets": [f"fact {t0}"]}],
        "visuals": [{"time": f"{(t0 + 30) // 60}:{(t0 + 30) % 60:02d}", "caption": "c", "reason": "r"}]}


OVERVIEW_REPLY = {"title": "A specific title", "summary": "A long enough summary sentence about the whole video.",
                  "key_takeaways": ["one", "two", "three"], "glossary": [{"term": "T", "definition": "D"}],
                  "review_questions": [{"question": "Q?", "answer": "A."}]}


def make_ctx(llm, store, **kw):
    s = settings(**{"chunk_tokens": 600, "max_frames": 4, "notes_words": 600, **kw})
    return SimpleNamespace(settings=s, llm=llm, limiter=FreeLimiter(), llm_gate=None, heartbeat=None, engine=None,
                           store=store)


@pytest.fixture
def fake_repo(monkeypatch):
    store: dict[str, dict] = {}
    segs = [{"start": 30.0 * i, "end": 30.0 * i + 30, "text": "word " * 60} for i in range(14)]  # 7 min, 3 chunks
    store["transcript"] = {"segments": segs, "duration_sec": 420}

    async def get_artifact(_engine, _jid, name):
        return store.get(name)

    async def update_job(*a, **k):
        return None

    async def save(ctx, job, name, key, data):
        store[name] = {"key": key, "data": data}

    async def find_cached_artifact(_engine, *, video_id, stage, cache_key, exclude_job):
        for (vid, st, key, jid), data in store.get("_cache", {}).items():
            if (vid, st, key) == (video_id, stage, cache_key) and jid != exclude_job:
                return jid, data, SimpleNamespace(video_title="Cached", duration_sec=420)
        return None

    monkeypatch.setattr(summarize.repo, "get_artifact", get_artifact)
    monkeypatch.setattr(summarize.repo, "find_cached_artifact", find_cached_artifact)
    monkeypatch.setattr(summarize.repo, "update_job", update_job)
    monkeypatch.setattr(summarize, "_save_checkpoint", save)
    return store


def _job():
    return SimpleNamespace(id=uuid.uuid4(), video_id="vid12345678", video_title="Video", duration_sec=420)


def _msg(**opts):
    return SimpleNamespace(options=JobOptions(**opts))


def test_summarize_resumes_from_chunk_checkpoints_after_a_crash(fake_repo):
    job = _job()
    # First worker: chunk 1 succeeds, then the model "dies" on chunk 2 (worker crash stand-in).
    llm1 = ScriptedLLM([chunk_reply(0), RetryableError(ErrorCode.LLM_ERROR, "boom")])
    with pytest.raises(RetryableError):
        asyncio.run(summarize.run(make_ctx(llm1, fake_repo), job, _msg(study_aids=False)))
    assert "synth_part_1" in fake_repo and "synth_part_2" not in fake_repo
    n_chunks = len([c for c in llm1.calls if c[0] == "chunk"])

    # Redelivered to a second worker: chunk 1 comes from the checkpoint, no model call.
    replies = [chunk_reply(60 * i) for i in range(1, 10)]
    llm2 = ScriptedLLM(replies)
    res = asyncio.run(summarize.run(make_ctx(llm2, fake_repo), job, _msg(study_aids=False)))
    trace = res.artifact["llm_trace"]
    assert trace["resumed_chunks"] == [1]
    assert n_chunks == 2 and len(llm2.calls) == len(trace["chunk_ranges"]) - 1
    assert res.artifact["key_takeaways"] == []


def test_checkpoint_is_ignored_when_the_prompt_changes(fake_repo):
    job = _job()
    asyncio.run(summarize.run(make_ctx(ScriptedLLM([chunk_reply(60 * i) for i in range(9)]), fake_repo), job,
                              _msg(study_aids=False)))
    llm = ScriptedLLM([chunk_reply(60 * i) for i in range(9)])
    res = asyncio.run(summarize.run(make_ctx(llm, fake_repo), job, _msg(study_aids=False, language="German")))
    assert res.artifact["llm_trace"]["resumed_chunks"] == []  # different language -> different prompt


def test_study_aids_are_merged_and_unreadable_ones_are_skipped(fake_repo):
    llm = ScriptedLLM([chunk_reply(0), OVERVIEW_REPLY])
    ctx = make_ctx(llm, fake_repo, chunk_tokens=100000)  # one chunk
    res = asyncio.run(summarize.run(ctx, _job(), _msg(detail="detailed", max_frames=2)))
    a = res.artifact
    assert a["title"] == "A specific title" and a["key_takeaways"] == ["one", "two", "three"]
    assert a["glossary"] == [{"term": "T", "definition": "D"}] and len(a["review_questions"]) == 1
    assert a["llm_trace"]["settings"]["notes_words"] == 1020 and a["options"]["detail"] == "detailed"
    assert len(a["visual_moments"]) <= 2

    # Garbage twice from the overview call: publish without study aids instead of failing.
    fake_repo.pop("synth_overview", None)
    llm2 = ScriptedLLM(["not json", "still not json"])
    res2 = asyncio.run(summarize.run(make_ctx(llm2, fake_repo, chunk_tokens=100000), _job(),
                                     _msg(detail="detailed", max_frames=2)))  # chunk resumes from checkpoint
    assert res2.artifact["key_takeaways"] == [] and res2.artifact["llm_trace"]["overview_error"]


def test_overview_title_never_generic():
    m = Synthesis(title="Chunk title")
    out = summarize.apply_overview(m, Overview(title="Video Notes", tldr="short"))
    assert out.title == "Chunk title" and out.tldr == ""


def test_notes_as_text_is_compact():
    s = Synthesis.model_validate({"sections": [{"heading": "A", "start_sec": 65, "bullets": ["x", "y"]}]})
    assert notes_as_text(s) == "## A [1:05]\n- x\n- y"


# ---- Ollama adapter ------------------------------------------------------------------------

def test_ollama_base_strips_openai_suffix():
    assert ollama_base("http://h:11434/v1/") == "http://h:11434"
    assert ollama_base("") == "http://host.docker.internal:11434"


@respx.mock
def test_ollama_sends_schema_and_context_and_reads_stats():
    route = respx.post("http://ollama:11434/api/chat").mock(return_value=httpx.Response(200, json={
        "message": {"content": "{\"ok\": true}"}, "done_reason": "stop",
        "prompt_eval_count": 100, "eval_count": 50, "eval_duration": 5e9, "prompt_eval_duration": 1e9,
    }))
    llm = OllamaLLM(settings(llm_provider="ollama", llm_model="qwen2.5:3b", llm_base_url="http://ollama:11434/v1",
                             llm_num_ctx=8192, llm_temperature=0.1))
    out = asyncio.run(llm.complete_json("sys", "user", {"type": "object"}, 777))
    body = json.loads(route.calls[0].request.content)
    assert out == '{"ok": true}'
    assert body["format"] == {"type": "object"} and body["stream"] is False
    assert body["options"]["num_ctx"] == 8192 and body["options"]["num_predict"] == 777
    assert body["options"]["temperature"] == 0.1
    assert llm.last_stats["tokens_per_sec"] == 10.0


@respx.mock
@pytest.mark.parametrize("resp,exc,code", [
    (httpx.Response(404, json={"error": "model not found"}), FatalError, ErrorCode.LLM_ERROR),
    (httpx.Response(500, text="oops"), RetryableError, ErrorCode.LLM_ERROR),
    (httpx.Response(200, json={"message": {"content": "{"}, "done_reason": "length"}), RetryableError,
     ErrorCode.LLM_BAD_OUTPUT),
])
def test_ollama_error_mapping(resp, exc, code):
    respx.post("http://ollama:11434/api/chat").mock(return_value=resp)
    llm = OllamaLLM(settings(llm_provider="ollama", llm_model="m", llm_base_url="http://ollama:11434"))
    with pytest.raises(exc) as e:
        asyncio.run(llm.complete_json("s", "u", {}, 10))
    assert e.value.code == code
    if resp.status_code == 404:
        assert "ollama pull m" in e.value.message


def test_make_llm_knows_ollama():
    from app.llm.base import make_llm
    assert make_llm(settings(llm_provider="ollama")).name == "ollama"


# ---- LLM gate (needs a real Redis: REDIS_TEST_URL=redis://localhost:6379/15) ---------------

@pytest.mark.skipif(not os.environ.get("REDIS_TEST_URL"), reason="set REDIS_TEST_URL to run against Redis")
def test_concurrency_gate_is_fair_and_frees_crashed_slots():
    from redis.asyncio import Redis

    from app.ratelimit import limiter as L

    async def go():
        r = Redis.from_url(os.environ["REDIS_TEST_URL"])
        name = f"t{uuid.uuid4().hex[:6]}"
        gate = L.ConcurrencyGate(r, name, limit=1, max_wait_sec=30)
        order: list[str] = []

        async def user(tag, hold):
            async with gate.slot():
                order.append(tag)
                await asyncio.sleep(hold)

        await asyncio.gather(user("a", 0.3), user("b", 0.1), user("c", 0.1))
        assert sorted(order) == ["a", "b", "c"] and (await gate.status())["in_flight"] == 0

        # A crashed holder (acquired, never refreshed or released) frees its slot after the lease.
        L.LEASE_MS, old = 1500, L.LEASE_MS
        try:
            granted, *_ = await gate._call("acquire", "dead-worker")
            assert granted
            t0 = asyncio.get_running_loop().time()
            async with gate.slot():
                waited = asyncio.get_running_loop().time() - t0
            assert 0.5 < waited < 5
        finally:
            L.LEASE_MS = old
            await r.aclose()

    asyncio.run(go())


# ---- result cache -------------------------------------------------------------------------

def test_same_video_and_settings_reuse_the_summary_without_llm_calls(fake_repo):
    first = _job()
    res = asyncio.run(summarize.run(make_ctx(ScriptedLLM([chunk_reply(0), OVERVIEW_REPLY]), fake_repo,
                                             chunk_tokens=100000), first, _msg()))
    key = res.artifact["cache_key"]
    fake_repo["_cache"] = {("vid12345678", "synthesis", key, first.id): res.artifact}
    for k in [k for k in fake_repo if k.startswith("synth_")]:
        fake_repo.pop(k)  # no per-job checkpoints: only the cross-job cache can help

    llm = ScriptedLLM([])  # any model call would fail the test
    hit = asyncio.run(summarize.run(make_ctx(llm, fake_repo, chunk_tokens=100000), _job(), _msg()))
    assert llm.calls == []
    assert hit.artifact["cached_from"] == str(first.id) and hit.note == {"cached_from": str(first.id)}
    assert hit.artifact["sections"] == res.artifact["sections"]
    assert hit.artifact["key_takeaways"] == ["one", "two", "three"]


def test_different_settings_or_fresh_bypass_the_cache(fake_repo):
    first = _job()
    res = asyncio.run(summarize.run(make_ctx(ScriptedLLM([chunk_reply(0), OVERVIEW_REPLY]), fake_repo,
                                             chunk_tokens=100000), first, _msg()))
    fake_repo["_cache"] = {("vid12345678", "synthesis", res.artifact["cache_key"], first.id): res.artifact}
    for opts in ({"detail": "brief"}, {"fresh": True}):
        for k in [k for k in fake_repo if k.startswith("synth_")]:
            fake_repo.pop(k)
        llm = ScriptedLLM([chunk_reply(0), OVERVIEW_REPLY])
        out = asyncio.run(summarize.run(make_ctx(llm, fake_repo, chunk_tokens=100000), _job(), _msg(**opts)))
        assert "cached_from" not in out.artifact and len(llm.calls) == 2, opts


def test_cache_off_by_setting(fake_repo):
    first = _job()
    res = asyncio.run(summarize.run(make_ctx(ScriptedLLM([chunk_reply(0), OVERVIEW_REPLY]), fake_repo,
                                             chunk_tokens=100000), first, _msg()))
    fake_repo["_cache"] = {("vid12345678", "synthesis", res.artifact["cache_key"], first.id): res.artifact}
    for k in [k for k in fake_repo if k.startswith("synth_")]:
        fake_repo.pop(k)
    llm = ScriptedLLM([chunk_reply(0), OVERVIEW_REPLY])
    out = asyncio.run(summarize.run(make_ctx(llm, fake_repo, chunk_tokens=100000, result_cache=False), _job(), _msg()))
    assert "cached_from" not in out.artifact and len(llm.calls) == 2


def test_cache_key_changes_with_prompt_text(monkeypatch):
    kw = dict(model_id="m", segments=[{"start": 0, "text": "a"}], frames=3, words=600, study=True, language=None,
              min_frames=0, chunk_tokens=3500)
    k1 = summarize.synthesis_cache_key(**kw)
    monkeypatch.setattr(summarize.prompt_texts, "SYSTEM_PROMPT", "edited prompt")
    assert summarize.synthesis_cache_key(**kw) != k1
