"""Token estimate, transcript chunking with overlap, and ordered merge of partial results."""

from __future__ import annotations

import bisect
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


_LEAD = re.compile(
    r"^\s*(?:[-*\u2022\u25cf\u25aa]+\s+|\d{1,2}[.)]\s+)?"  # list marker
    r"(?:\[\s*\d{1,2}(?::\d{2}){1,2}\s*\]\s*[-:\u2013\u2014]?\s*)?"  # leading [m:ss]
)
_FILLER = re.compile(r"\b(like and subscribe|subscribe to (?:the|my|our) channel|smash (?:that|the) like)\b", re.I)


def clean_text(text: str) -> str:
    """Tidy one bullet/heading/caption from the model: strip list markers and
    leading [m:ss] stamps, collapse whitespace, drop unbalanced ** markers."""
    t = _LEAD.sub("", str(text or ""), count=1)
    t = re.sub(r"\s+", " ", t).strip()
    if t.count("**") % 2:
        t = t.replace("**", "")
    return t


def clean_heading(text: str) -> str:
    t = clean_text(text).strip("#").strip()
    t = re.sub(r"^(?:section|part|topic)\s*\d+\s*[:.\-\u2013\u2014]\s*", "", t, flags=re.I)
    t = t.replace("**", "").rstrip(".:")
    return t[:120]


def snap(t: float, starts: list[float], lo: float, hi: float) -> int:
    """Clamp t into [lo, hi] and snap it to the latest transcript line starting at or before it."""
    t = min(max(t, lo), hi)
    i = bisect.bisect_right(starts, t) - 1
    return int(starts[i]) if i >= 0 and starts[i] >= lo else int(t)


def clean_partial(p: Synthesis, chunk: list[dict[str, Any]]) -> Synthesis:
    """Make one chunk's output safe to merge: in-range timestamps, tidy text,
    no empty or filler bullets, sections in time order."""
    if not chunk:
        return p
    lo, hi = float(chunk[0]["start"]), float(chunk[-1]["end"])
    starts = [float(x["start"]) for x in chunk]
    sections: list[Section] = []
    for sec in p.sections:
        bullets: list[str] = []
        seen: set[str] = set()
        for b in sec.bullets:
            cb = clean_text(b)
            if len(cb) < 3 or _FILLER.search(cb) or _norm(cb) in seen:
                continue
            seen.add(_norm(cb))
            bullets.append(cb[:600])
        heading = clean_heading(sec.heading)
        if not bullets and not heading:
            continue
        sections.append(Section(heading=heading or "Notes", start_sec=snap(sec.start_sec, starts, lo, hi),
                                bullets=bullets))
    # A section with a heading but no bullets adds nothing on its own: fold it away
    # unless it is all we have.
    if any(s.bullets for s in sections):
        sections = [s for s in sections if s.bullets]
    sections.sort(key=lambda s: s.start_sec)
    moments = [
        m.model_copy(update={
            "timestamp_sec": int(min(max(m.timestamp_sec, lo), max(lo, hi - 1))),
            "caption": clean_text(m.caption).replace("**", ""),
            "reason": clean_text(m.reason).replace("**", ""),
            "section_index": None,
        })
        for m in p.visual_moments
    ]
    return p.model_copy(update={"title": clean_heading(p.title) if p.title else "",
                                "tldr": clean_text(p.tldr), "sections": sections, "visual_moments": moments})


def section_index_for(ts: int, sections: list[Section]) -> int | None:
    best = None
    for i, s in enumerate(sections):
        if s.start_sec <= ts:
            best = i
    return best if best is not None else (0 if sections else None)


def notes_as_text(syn: Synthesis, max_chars: int = 12000) -> str:
    """Compact rendering of merged notes, the input of the overview call."""
    out: list[str] = []
    for s in syn.sections:
        out.append(f"## {s.heading} [{fmt_ts(s.start_sec)}]")
        out.extend(f"- {b}" for b in s.bullets)
    text = "\n".join(out)
    return text if len(text) <= max_chars else text[: max_chars - 1] + "\u2026"


_STOP = {"the", "a", "an", "of", "and", "to", "in", "on", "for", "with", "overview", "introduction", "intro",
         "basics", "explained", "explanation", "summary", "recap", "continued", "part", "more"}


def _similar(a: str, b: str) -> bool:
    """Same topic under a different name: one heading's key words contain the other's,
    or they share most of them."""
    wa = {w for w in _norm(a).split() if w not in _STOP}
    wb = {w for w in _norm(b).split() if w not in _STOP}
    if not wa or not wb:
        return False
    return wa <= wb or wb <= wa or len(wa & wb) / len(wa | wb) >= 0.6


def _first_sentence(text: str) -> str:
    m = re.match(r"(.+?[.!?])(\s|$)", text.strip())
    return m.group(1) if m else text.strip()


def pad_moments(
    moments: list[VisualMoment], sections: list[Section], *, duration_sec: int, target: int
) -> list[VisualMoment]:
    """Add evenly spaced frames until there are `target`, keeping clear of existing ones.
    Captions are honest: they name the section being discussed, not invented visuals."""
    out = list(moments)
    if duration_sec <= 0 or len(out) >= target:
        return out
    min_gap = min(DEDUPE_WINDOW_SEC, duration_sec / (2 * target))
    slots = [int(duration_sec * (i + 0.5) / (target * 4)) for i in range(target * 4)]
    while len(out) < target:
        def gap(t: int) -> float:
            return min((abs(t - m.timestamp_sec) for m in out), default=float("inf"))

        best = max(slots, key=gap, default=None)
        if best is None or gap(best) < min_gap:
            break
        si = max((i for i, s in enumerate(sections) if s.start_sec <= best), default=0 if sections else None)
        topic = sections[si].heading if si is not None else "the video"
        out.append(VisualMoment(
            timestamp_sec=best,
            caption=f"Scene at {fmt_ts(best)} while the video covers: {topic}.",
            reason="Added to give an even visual overview of the video.",
            section_index=si,
        ))
    return sorted(out, key=lambda m: m.timestamp_sec)


def merge_partials(
    partials: list[Synthesis], *, duration_sec: int | None, max_frames: int, min_frames: int = 0
) -> Synthesis:
    """Concatenate sections in chunk order, collapse overlap duplicates, dedupe and
    clamp visual moments, keep at most max_frames (in time order), and pad up to
    min_frames with evenly spaced frames."""
    sections: list[Section] = []
    moments: list[VisualMoment] = []
    for n, p in enumerate(partials):
        index_map: dict[int, int] = {}
        boundary = len(sections)  # sections from earlier chunks end here
        for idx, sec in enumerate(p.sections):
            # The ~60 s chunk overlap often repeats the previous chunk's last topic under
            # a slightly different name ("Sigmoid" / "Sigmoid function overview"): merge it.
            target = None
            if sections and _norm(sec.heading) == _norm(sections[-1].heading):
                target = len(sections) - 1
            elif n > 0 and idx < 2 and boundary:
                for j in range(max(0, boundary - 2), boundary):
                    if _similar(sec.heading, sections[j].heading):
                        target = j
            if target is not None:
                seen = {_norm(b) for b in sections[target].bullets}
                sections[target].bullets.extend(b for b in sec.bullets if _norm(b) not in seen)
                index_map[idx] = target
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
        if not m.caption.strip():
            continue  # Constraint B: no caption -> no frame
        t = max(0, m.timestamp_sec)
        if max_t is not None:
            t = min(t, max_t)
        if cleaned and t - cleaned[-1].timestamp_sec < DEDUPE_WINDOW_SEC:
            continue
        # Frames are attached to sections by time; a small model's own index is unreliable.
        si = section_index_for(t, sections)
        reason = m.reason.strip()
        if not reason:  # keep the "why it was flagged" half of the caption honest
            topic = sections[si].heading if si is not None else "this part of the video"
            reason = f"Flagged as a key visual while the video covers {topic}."
        cleaned.append(m.model_copy(update={"timestamp_sec": t, "section_index": si, "reason": reason}))
    if max_frames <= 0:
        cleaned = []
    elif len(cleaned) > max_frames:
        # Keep max_frames spread across the video, preserving order.
        step = len(cleaned) / max_frames
        cleaned = [cleaned[int(i * step)] for i in range(max_frames)]
    min_frames = min(min_frames, max_frames)  # padding never exceeds the frame budget
    if min_frames > 0 and duration_sec:
        cleaned = pad_moments(cleaned, sections, duration_sec=duration_sec, target=min_frames)

    if len(partials) == 1:
        title, tldr = partials[0].title, partials[0].tldr
    else:
        title = next((p.title for p in partials if p.title), "")
        tldr = " ".join(_first_sentence(p.tldr) for p in partials if p.tldr)[:900]
    return Synthesis(title=title, tldr=tldr, sections=sections, visual_moments=cleaned)
