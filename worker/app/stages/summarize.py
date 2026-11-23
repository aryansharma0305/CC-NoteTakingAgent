"""SUMMARIZING (25 -> 55): LLM synthesis with chunking, ordered merge and study aids.

Crash-resume: every chunk's parsed output is checkpointed in job_artifacts
(`synth_part_<i>`, keyed by a hash of the exact prompts and model). If a worker is
killed after finishing 3 of 4 chunks, the worker that picks up the redelivered
message reuses those 3 and only calls the model for the last one. With a slow
local model this turns a crash from "start the summary over" into "lose at most
one call".
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import update

from vidnotes_common.errors import ErrorCode, RetryableError
from vidnotes_common.messages import MAX_FRAMES_CAP, JobOptions
from vidnotes_common.models import jobs
from vidnotes_common.status import Stage, stage_progress

from .. import repo
from ..context import WorkerContext
from ..llm import prompts as prompt_texts
from ..llm.chunking import (
    chunk_segments,
    clean_heading,
    clean_partial,
    clean_text,
    estimate_tokens,
    merge_partials,
    notes_as_text,
    segment_line,
)
from ..llm.prompts import (
    overview_system_prompt,
    overview_user_prompt,
    repair_prompt,
    section_target,
    system_prompt,
    user_prompt,
)
from ..llm.schema import OVERVIEW_JSON_SCHEMA, SYNTHESIS_JSON_SCHEMA, Overview, Synthesis
from . import StageResult

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.M)
TRACE_TEXT_LIMIT = 60_000  # chars kept per prompt/response in the debug trace
OVERVIEW_MAX_TOKENS = 2000
_GENERIC_TITLES = {"video notes", "notes", "study notes", "untitled", "title", "video"}


def parse_json(text: str, model: type[BaseModel] = Synthesis):
    cleaned = _FENCE.sub("", text.strip())
    if not cleaned.startswith("{"):
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start >= 0 and end > start:
            cleaned = cleaned[start : end + 1]
    return model.model_validate(json.loads(cleaned))


def parse_synthesis(text: str) -> Synthesis:
    return parse_json(text, Synthesis)


def checkpoint_key(model: str, *parts: str) -> str:
    h = hashlib.sha256(model.encode())
    for p in parts:
        h.update(b"\0" + p.encode())
    return h.hexdigest()[:16]


async def _load_checkpoint(ctx: WorkerContext, job, name: str, key: str) -> dict[str, Any] | None:
    art = await repo.get_artifact(ctx.engine, job.id, name)
    if art and art.get("key") == key:
        return art.get("data")
    return None


async def _save_checkpoint(ctx: WorkerContext, job, name: str, key: str, data: dict[str, Any]) -> None:
    async with ctx.engine.begin() as conn:
        await repo.put_artifact(conn, job.id, name, {"key": key, "data": data})


async def _note_waiting(ctx: WorkerContext, job, position: int) -> None:
    """Shown in the popup while this job queues for the (local) model."""
    ahead = "1 call" if position <= 1 else f"{position} calls"
    async with ctx.engine.begin() as conn:
        await conn.execute(
            update(jobs).where(jobs.c.id == job.id).values(
                retry_reason=f"Waiting for the AI model ({ahead} ahead of this one)", waiting_until=None,
            )
        )
    if ctx.heartbeat:
        await ctx.heartbeat.note(state="waiting_llm")


async def _clear_waiting(ctx: WorkerContext, job) -> None:
    await repo.update_job(ctx.engine, job.id, retry_reason=None, waiting_until=None)
    if ctx.heartbeat:
        await ctx.heartbeat.note(state="busy")


async def call_llm(ctx: WorkerContext, system: str, user: str, trace: list[dict] | None = None,
                   part: int | str = 1, *, schema: dict[str, Any] = SYNTHESIS_JSON_SCHEMA,
                   model: type[BaseModel] = Synthesis, max_tokens: int | None = None, job=None):
    """One validated LLM call (plus one repair retry on invalid JSON).
    trace (optional) receives one entry per attempt: prompt, raw output, timing, parse error."""
    s = ctx.settings
    max_out = max_tokens or s.llm_max_output_tokens
    text = ""
    last_err = ""
    for attempt in range(2):
        prompt = user if attempt == 0 else repair_prompt(user, text, last_err)
        await ctx.limiter.acquire("llm_rpm", 1)
        await ctx.limiter.acquire("llm_tpm", estimate_tokens(system + prompt) + max_out)
        waited = 0.0
        t0 = time.monotonic()
        if ctx.llm_gate is not None and ctx.llm_gate.limit > 0:
            waited_for = False

            async def on_wait(pos: int) -> None:
                nonlocal waited_for
                waited_for = True
                if job is not None:
                    await _note_waiting(ctx, job, pos)

            async with ctx.llm_gate.slot(on_wait=on_wait) as slot:
                waited = slot.waited_sec
                if waited_for and job is not None:
                    await _clear_waiting(ctx, job)
                t0 = time.monotonic()
                text = await ctx.llm.complete_json(system, prompt, schema, max_out)
        else:
            text = await ctx.llm.complete_json(system, prompt, schema, max_out)
        entry = {
            "part": part, "attempt": attempt + 1, "seconds": round(time.monotonic() - t0, 2),
            "queued_sec": round(waited, 2),
            "prompt_tokens_est": estimate_tokens(system + prompt), "output_tokens_est": estimate_tokens(text),
            "provider_stats": getattr(ctx.llm, "last_stats", None),
            "user_prompt": prompt[:TRACE_TEXT_LIMIT], "raw_output": text[:TRACE_TEXT_LIMIT], "parse_error": None,
        }
        if trace is not None:
            trace.append(entry)
        try:
            return parse_json(text, model)
        except (ValueError, ValidationError) as e:
            last_err = str(e)
            entry["parse_error"] = last_err[:1000]
    raise RetryableError(ErrorCode.LLM_BAD_OUTPUT, f"LLM returned invalid JSON twice: {last_err[:200]}")


def frames_target(s, opts: JobOptions) -> int:
    n = opts.max_frames if opts.max_frames is not None else s.max_frames
    return max(0, min(MAX_FRAMES_CAP, int(n)))


def apply_overview(merged: Synthesis, ov: Overview) -> Synthesis:
    title = clean_heading(ov.title)
    upd: dict[str, Any] = {
        "key_takeaways": [t for t in (clean_text(x) for x in ov.key_takeaways) if re.search(r"\w", t)][:6],
        "glossary": [g.model_copy(update={"term": clean_text(g.term).replace("**", "")[:80],
                                          "definition": clean_text(g.definition)[:400]})
                     for g in ov.glossary][:10],
        "review_questions": [q.model_copy(update={"question": clean_text(q.question)[:300],
                                                  "answer": clean_text(q.answer)[:600]})
                             for q in ov.review_questions][:6],
    }
    if title and title.lower() not in _GENERIC_TITLES:
        upd["title"] = title
    if len(clean_text(ov.tldr)) > 40:
        upd["tldr"] = clean_text(ov.tldr)
    return merged.model_copy(update=upd)


def synthesis_cache_key(*, model_id: str, segments: list[dict], frames: int, words: int, study: bool,
                        language: str | None, min_frames: int, chunk_tokens: int) -> str:
    """Everything that changes the notes: model, transcript, the job's note style, the
    relevant server settings and the prompt text itself (editing a prompt invalidates the cache)."""
    h = hashlib.sha256()
    for part in (model_id, prompt_texts.SYSTEM_PROMPT, prompt_texts.OVERVIEW_PROMPT,
                 json.dumps([frames, words, study, language or "", min_frames, chunk_tokens]),
                 json.dumps([[x.get("start"), x.get("text")] for x in segments], ensure_ascii=False)):
        h.update(part.encode())
        h.update(b"\0")
    return "syn1:" + h.hexdigest()[:24]


async def run(ctx: WorkerContext, job, msg) -> StageResult:
    s = ctx.settings
    opts: JobOptions = getattr(msg, "options", None) or JobOptions()
    transcript = await repo.get_artifact(ctx.engine, job.id, "transcript")
    if not transcript:
        raise RetryableError(ErrorCode.UNEXPECTED_ERROR, "transcript artifact missing")
    segments = transcript["segments"]
    duration = job.duration_sec or transcript.get("duration_sec")
    chunks = chunk_segments(segments, s.chunk_tokens)
    n = len(chunks)
    frames = frames_target(s, opts)
    words = int(s.notes_words * opts.words_factor())
    study = s.study_aids if opts.study_aids is None else opts.study_aids
    steps = n + (1 if study else 0)
    model_id = f"{s.llm_provider}:{s.llm_model}"
    cache_key = synthesis_cache_key(model_id=model_id, segments=segments, frames=frames, words=words, study=study,
                                    language=opts.language, min_frames=min(s.min_frames, frames),
                                    chunk_tokens=s.chunk_tokens)

    # Same video + same settings already summarized by an earlier job: reuse it, no LLM calls.
    if s.result_cache and not opts.fresh:
        hit = await repo.find_cached_artifact(ctx.engine, video_id=job.video_id, stage="synthesis",
                                              cache_key=cache_key, exclude_job=job.id)
        if hit:
            src_id, cached, _ = hit
            data = {k: v for k, v in cached.items() if k != "llm_trace"}
            data.update(cached_from=str(src_id), options=opts.model_dump(),
                        llm_trace={"cached_from": str(src_id), "calls": [], "resumed_chunks": [],
                                   "chunk_ranges": (cached.get("llm_trace") or {}).get("chunk_ranges", [])})
            return StageResult(artifact=data, note={"cached_from": str(src_id)})

    partials: list[Synthesis] = []
    calls: list[dict] = []
    resumed: list[int] = []
    sysp = ""
    for i, ch in enumerate(chunks, start=1):
        lo, hi = ch[0]["start"], ch[-1]["end"]
        per_frames = -(-frames // n) if frames else 0
        n_sections = section_target(lo, hi)
        sysp = system_prompt(per_frames, words // n, sections=n_sections, language=opts.language)
        user = user_prompt(
            video_title=job.video_title, duration_sec=duration, lines=[segment_line(x) for x in ch],
            part=i, parts=n, start_sec=lo, end_sec=hi, frames=per_frames, sections=n_sections,
        )
        name, key = f"synth_part_{i}", checkpoint_key(model_id, sysp, user)
        cached = await _load_checkpoint(ctx, job, name, key)
        if cached is not None:
            partial = Synthesis.model_validate(cached)
            resumed.append(i)
            calls.append({"part": i, "resumed_from_checkpoint": True})
        else:
            partial = await call_llm(ctx, sysp, user, calls, part=i, job=job)
            await _save_checkpoint(ctx, job, name, key, partial.model_dump())
        partials.append(clean_partial(partial, ch))
        await repo.update_job(ctx.engine, job.id, progress=stage_progress(Stage.SUMMARIZE, i / steps))

    merged = merge_partials(
        partials, duration_sec=duration, max_frames=frames, min_frames=min(s.min_frames, frames)
    )

    overview_note: str | None = None
    if study and merged.sections:
        osys = overview_system_prompt(language=opts.language)
        ouser = overview_user_prompt(video_title=job.video_title, notes=notes_as_text(merged))
        key = checkpoint_key(model_id, osys, ouser)
        cached = await _load_checkpoint(ctx, job, "synth_overview", key)
        try:
            if cached is not None:
                ov = Overview.model_validate(cached)
                calls.append({"part": "overview", "resumed_from_checkpoint": True})
            else:
                ov = await call_llm(ctx, osys, ouser, calls, part="overview", schema=OVERVIEW_JSON_SCHEMA,
                                    model=Overview, max_tokens=min(OVERVIEW_MAX_TOKENS, s.llm_max_output_tokens),
                                    job=job)
                await _save_checkpoint(ctx, job, "synth_overview", key, ov.model_dump())
            merged = apply_overview(merged, ov)
        except RetryableError as e:
            if e.code != ErrorCode.LLM_BAD_OUTPUT:
                raise  # model unreachable / rate-limited: retry the stage (chunks are checkpointed)
            overview_note = e.message  # unreadable study aids are optional: publish without them
    if not merged.title:
        merged = merged.model_copy(update={"title": job.video_title or ""})

    data = merged.model_dump()
    data["chunks"] = n
    data["model"] = model_id
    data["options"] = opts.model_dump()
    data["cache_key"] = cache_key
    # Debug only (shown at /debug): exactly what the LLM saw and said, before merging.
    data["llm_trace"] = {
        "system_prompt": sysp,
        "settings": {"max_output_tokens": s.llm_max_output_tokens, "chunk_tokens": s.chunk_tokens,
                     "max_frames": frames, "min_frames": s.min_frames, "notes_words": words,
                     "study_aids": study, "language": opts.language, "llm_concurrency": s.llm_concurrency},
        "chunk_ranges": [[c[0]["start"], c[-1]["end"]] for c in chunks],
        "resumed_chunks": resumed,
        "overview_error": overview_note,
        "calls": calls,
        "partials": [p.model_dump() for p in partials],
    }
    return StageResult(artifact=data)
