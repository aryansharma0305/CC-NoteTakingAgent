"""SUMMARIZING (25 -> 55): LLM synthesis with chunking and ordered merge."""

from __future__ import annotations

import json
import re
import time

from pydantic import ValidationError

from vidnotes_common.errors import ErrorCode, RetryableError
from vidnotes_common.status import Stage, stage_progress

from .. import repo
from ..context import WorkerContext
from ..llm.chunking import chunk_segments, estimate_tokens, merge_partials, segment_line
from ..llm.prompts import repair_prompt, system_prompt, user_prompt
from ..llm.schema import SYNTHESIS_JSON_SCHEMA, Synthesis
from . import StageResult

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.M)


def parse_synthesis(text: str) -> Synthesis:
    cleaned = _FENCE.sub("", text.strip())
    if not cleaned.startswith("{"):
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start >= 0 and end > start:
            cleaned = cleaned[start : end + 1]
    return Synthesis.model_validate(json.loads(cleaned))


TRACE_TEXT_LIMIT = 60_000  # chars kept per prompt/response in the debug trace


async def call_llm(ctx: WorkerContext, system: str, user: str, trace: list[dict] | None = None,
                   part: int = 1) -> Synthesis:
    """trace (optional) receives one entry per LLM call: prompt, raw output, timing, parse error."""
    s = ctx.settings
    max_out = s.llm_max_output_tokens
    text = ""
    last_err = ""
    for attempt in range(2):  # one repair retry on invalid JSON
        prompt = user if attempt == 0 else repair_prompt(user, text, last_err)
        await ctx.limiter.acquire("llm_rpm", 1)
        await ctx.limiter.acquire("llm_tpm", estimate_tokens(system + prompt) + max_out)
        t0 = time.monotonic()
        text = await ctx.llm.complete_json(system, prompt, SYNTHESIS_JSON_SCHEMA, max_out)
        entry = {
            "part": part, "attempt": attempt + 1, "seconds": round(time.monotonic() - t0, 2),
            "prompt_tokens_est": estimate_tokens(system + prompt), "output_tokens_est": estimate_tokens(text),
            "user_prompt": prompt[:TRACE_TEXT_LIMIT], "raw_output": text[:TRACE_TEXT_LIMIT], "parse_error": None,
        }
        if trace is not None:
            trace.append(entry)
        try:
            return parse_synthesis(text)
        except (ValueError, ValidationError) as e:
            last_err = str(e)
            entry["parse_error"] = last_err[:1000]
    raise RetryableError(ErrorCode.LLM_BAD_OUTPUT, f"LLM returned invalid JSON twice: {last_err[:200]}")


async def run(ctx: WorkerContext, job, msg) -> StageResult:
    s = ctx.settings
    transcript = await repo.get_artifact(ctx.engine, job.id, "transcript")
    if not transcript:
        raise RetryableError(ErrorCode.UNEXPECTED_ERROR, "transcript artifact missing")
    segments = transcript["segments"]
    duration = job.duration_sec or transcript.get("duration_sec")
    chunks = chunk_segments(segments, s.chunk_tokens)
    # Targets for the whole video, split evenly across chunks.
    sysp = system_prompt(-(-s.max_frames // len(chunks)), s.notes_words // len(chunks))
    partials: list[Synthesis] = []
    calls: list[dict] = []
    for i, ch in enumerate(chunks, start=1):
        user = user_prompt(
            video_title=job.video_title,
            duration_sec=duration,
            lines=[segment_line(x) for x in ch],
            part=i,
            parts=len(chunks),
            start_sec=ch[0]["start"],
            end_sec=ch[-1]["end"],
        )
        partials.append(await call_llm(ctx, sysp, user, calls, part=i))
        await repo.update_job(ctx.engine, job.id, progress=stage_progress(Stage.SUMMARIZE, i / len(chunks)))

    merged = merge_partials(
        partials, duration_sec=duration, max_frames=s.max_frames, min_frames=min(s.min_frames, s.max_frames)
    )
    data = merged.model_dump()
    data["chunks"] = len(chunks)
    data["model"] = f"{s.llm_provider}:{s.llm_model}"
    # Debug only (shown at /debug): exactly what the LLM saw and said, before merging.
    data["llm_trace"] = {
        "system_prompt": sysp,
        "settings": {"max_output_tokens": s.llm_max_output_tokens, "chunk_tokens": s.chunk_tokens,
                     "max_frames": s.max_frames, "min_frames": s.min_frames, "notes_words": s.notes_words},
        "chunk_ranges": [[c[0]["start"], c[-1]["end"]] for c in chunks],
        "calls": calls,
        "partials": [p.model_dump() for p in partials],
    }
    return StageResult(artifact=data)
