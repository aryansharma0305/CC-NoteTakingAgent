"""Token estimate, transcript chunking with overlap, and ordered merge of partial results."""

from __future__ import annotations

import re
from typing import Any

from vidnotes_common.youtube import fmt_ts

from .schema import Section, Synthesis, VisualMoment

OVERLAP_SEC = 60
DEDUPE_WINDOW_SEC = 10


def estimate_tokens(text: str) -> int:
    """~4 chars per token; deliberately a bit pessimistic for the TPM bucket."""
    return len(text) // 4 + 1


def segment_line(seg: dict[str, Any]) -> str:
    return f"[{fmt_ts(seg['start'])}] {seg['text']}"


def chunk_segments(
    segments: list[dict[str, Any]], chunk_tokens: int, overlap_sec: int = OVERLAP_SEC
) -> list[list[dict]]:
    """Split on segment boundaries; consecutive chunks overlap by ~overlap_sec."""
    if not segments:
        return []
    total = sum(estimate_tokens(segment_line(s)) for s in segments)
    if total <= chunk_tokens:
        return [segments]
    chunks: list[list[dict]] = []
    i = 0
    n = len(segments)
    while i < n:
        budget = 0
        j = i
        while j < n:
            cost = estimate_tokens(segment_line(segments[j]))
            if budget + cost > chunk_tokens and j > i:
                break
            budget += cost
            j += 1
        chunks.append(segments[i:j])
        if j >= n:
            break
        # Next chunk starts ~overlap_sec before this chunk's end, but always advances.
        end = segments[j - 1]["end"]
        k = j - 1
        while k > i + 1 and segments[k - 1]["start"] >= end - overlap_sec:
            k -= 1
        i = max(k, i + 1)
    return chunks


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _first_sentence(text: str) -> str:
    m = re.match(r"(.+?[.!?])(\s|$)", text.strip())
    return m.group(1) if m else text.strip()


def merge_partials(partials: list[Synthesis], *, duration_sec: int | None, max_frames: int) -> Synthesis:
    """Concatenate sections in chunk order, collapse overlap duplicates, dedupe and
    clamp visual moments, keep at most max_frames (in time order)."""
    sections: list[Section] = []
    moments: list[VisualMoment] = []
    for p in partials:
        index_map: dict[int, int] = {}
        for idx, sec in enumerate(p.sections):
            if sections and _norm(sec.heading) == _norm(sections[-1].heading):
                # Overlap repeated the previous chunk's last section: merge bullets.
                seen = {_norm(b) for b in sections[-1].bullets}
                sections[-1].bullets.extend(b for b in sec.bullets if _norm(b) not in seen)
                index_map[idx] = len(sections) - 1
                continue
            sections.append(sec.model_copy(deep=True))
            index_map[idx] = len(sections) - 1
        for m in p.visual_moments:
            si = index_map.get(m.section_index) if m.section_index is not None else None
            moments.append(m.model_copy(update={"section_index": si}))

    # Sections must stay in time order even if the model wobbles.
    if sections:
        for k in range(1, len(sections)):
            if sections[k].start_sec < sections[k - 1].start_sec:
                sections[k].start_sec = sections[k - 1].start_sec

    max_t = (duration_sec - 1) if duration_sec else None
    cleaned: list[VisualMoment] = []
    for m in sorted(moments, key=lambda x: x.timestamp_sec):
        if not m.caption.strip() or not m.reason.strip():
            continue  # Constraint B: no caption/reason -> no frame
        t = max(0, m.timestamp_sec)
        if max_t is not None:
            t = min(t, max_t)
        if cleaned and t - cleaned[-1].timestamp_sec < DEDUPE_WINDOW_SEC:
            continue
        cleaned.append(m.model_copy(update={"timestamp_sec": t}))
    if len(cleaned) > max_frames:
        # Keep max_frames spread across the video, preserving order.
        step = len(cleaned) / max_frames
        cleaned = [cleaned[int(i * step)] for i in range(max_frames)]

    if len(partials) == 1:
        title, tldr = partials[0].title, partials[0].tldr
    else:
        title = next((p.title for p in partials if p.title), "")
        tldr = " ".join(_first_sentence(p.tldr) for p in partials if p.tldr)[:900]
    return Synthesis(title=title, tldr=tldr, sections=sections, visual_moments=cleaned)
