"""Synthesis + frames -> Notion blocks.

Constraint B guard lives here: the builder refuses any image that lacks a
non-empty caption, and only accepts images whose bytes come from this job's
S3 frame keys (jobs/{job_id}/frame_{t}.jpg). Nothing else can reach the page.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from vidnotes_common.youtube import fmt_ts, watch_url

MAX_TEXT = 1900  # Notion caps a rich_text item at 2000 chars
APPEND_BATCH = 100  # Notion caps children per request at 100


@dataclass
class FrameInput:
    timestamp_sec: int
    caption: str
    reason: str
    section_index: int | None
    object_key: str
    load: Callable[[], Awaitable[bytes]] = field(repr=False)
    sha256: str | None = None


@dataclass
class ImageSlot:
    """Placeholder for an image block; resolved to a file_upload id at publish time."""

    frame: FrameInput
    caption_text: str
    upload_id: str | None = None

    def to_block(self) -> dict[str, Any]:
        if not self.upload_id:
            raise ValueError("image slot was not uploaded")
        return {
            "object": "block",
            "type": "image",
            "image": {
                "type": "file_upload",
                "file_upload": {"id": self.upload_id},
                "caption": rt(self.caption_text),
            },
        }


class ConstraintViolation(ValueError):
    pass


def _clip(s: str, n: int = MAX_TEXT) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def rt(text: str, link: str | None = None, bold: bool = False) -> list[dict[str, Any]]:
    item: dict[str, Any] = {"type": "text", "text": {"content": _clip(text)}}
    if link:
        item["text"]["link"] = {"url": link}
    if bold:
        item["annotations"] = {"bold": True}
    return [item]


def _block(kind: str, rich: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {"object": "block", "type": kind, kind: {"rich_text": rich, **extra}}


def page_marker(job_id: str) -> str:
    return f"#{str(job_id)[:8]}"


def page_title(title: str, job_id: str) -> str:
    return f"{_clip(title or 'Video notes', 180)} · {page_marker(job_id)}"


def frame_caption(f: FrameInput) -> str:
    reason = (f.reason or "").strip()
    text = f"[{fmt_ts(f.timestamp_sec)}] {f.caption.strip()}"
    return f"{text} — {reason}" if reason else text


def validate_frame(f: FrameInput, job_id: str) -> None:
    if not f.caption or not f.caption.strip():
        raise ConstraintViolation(f"frame at {f.timestamp_sec}s has no caption")
    prefix = f"jobs/{job_id}/frame_"
    if not (f.object_key.startswith(prefix) and f.object_key.endswith(".jpg")):
        raise ConstraintViolation(f"frame {f.object_key!r} is not a captured frame of job {job_id}")


def _section_for(ts: int, sections: list[dict[str, Any]], hint: int | None) -> int:
    if hint is not None and 0 <= hint < len(sections):
        return hint
    best = 0
    for i, s in enumerate(sections):
        if int(s.get("start_sec") or 0) <= ts:
            best = i
    return best


def build_blocks(
    *,
    job_id: str,
    video_id: str,
    video_title: str | None,
    duration_sec: int | None,
    synthesis: dict[str, Any],
    frames: list[FrameInput],
) -> list[dict[str, Any] | ImageSlot]:
    """Deterministic block list for the page (image blocks are ImageSlots)."""
    for f in frames:
        validate_frame(f, job_id)

    sections: list[dict[str, Any]] = list(synthesis.get("sections") or [])
    title = synthesis.get("title") or video_title or "Video notes"
    url = watch_url(video_id)

    out: list[dict[str, Any] | ImageSlot] = []
    meta = f"  ·  Duration {fmt_ts(duration_sec)}" if duration_sec else ""
    out.append(
        {
            "object": "block",
            "type": "callout",
            "callout": {
                "icon": {"type": "emoji", "emoji": "🎬"},
                "rich_text": rt(video_title or title, link=url)
                + rt(f"{meta}  ·  Job {job_id}"),
            },
        }
    )
    tldr = (synthesis.get("tldr") or "").strip()
    if tldr:
        out.append(_block("heading_2", rt("TL;DR")))
        out.append(_block("paragraph", rt(tldr)))

    by_section: dict[int, list[FrameInput]] = {}
    if sections:
        for f in frames:
            by_section.setdefault(_section_for(f.timestamp_sec, sections, f.section_index), []).append(f)

    for i, s in enumerate(sections):
        start = int(s.get("start_sec") or 0)
        heading = rt(f"[{fmt_ts(start)}] ", link=watch_url(video_id, start)) + rt(s.get("heading") or f"Part {i + 1}")
        out.append(_block("heading_2", heading))
        for b in s.get("bullets") or []:
            if str(b).strip():
                out.append(_block("bulleted_list_item", rt(str(b))))
        for f in sorted(by_section.get(i, []), key=lambda x: x.timestamp_sec):
            out.append(ImageSlot(frame=f, caption_text=frame_caption(f)))

    if not sections and frames:  # no sections: append frames at the end
        out.append(_block("heading_2", rt("Key visuals")))
        for f in sorted(frames, key=lambda x: x.timestamp_sec):
            out.append(ImageSlot(frame=f, caption_text=frame_caption(f)))

    out.append({"object": "block", "type": "divider", "divider": {}})
    out.append(
        _block(
            "paragraph",
            rt("Generated by Video Notes Agent. Every image is a real frame captured from the video at the "
               "timestamp shown in its caption."),
            color="gray",
        )
    )
    return out


def batches(items: list[Any], size: int = APPEND_BATCH) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]
