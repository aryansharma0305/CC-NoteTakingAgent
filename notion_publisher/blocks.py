"""Synthesis + frames -> Notion blocks.

Constraint B guard lives here: the builder refuses any image that lacks a
non-empty caption, and only accepts images whose bytes come from this job's
S3 frame keys (jobs/{job_id}/frame_{t}.jpg). Nothing else can reach the page.

Page layout (top-level blocks; nested children travel with their parent):

    🎬 callout      video link · duration · sections · frames · model · job id
    table of contents
    💡 callout      summary
    🎯 Key takeaways (numbered)                         [study aids]
    ── divider
    ▶ m:ss  Section heading   (one heading_2 per section, linked to the moment)
        • bullets with **bold** / *italic* / `code` rendered as Notion formatting
        🖼 frames of this section, caption "[m:ss] what it shows — why it was flagged"
    ── divider
    📖 Glossary (2-column table)                         [study aids]
    🧠 Review questions (toggles: question -> answer)    [study aids]
    📜 Full transcript (collapsed toggle heading)         [include_transcript]
    ── divider + footer

The block list is deterministic for a given input, which the publisher's
resume-from-child-count logic relies on.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from vidnotes_common.youtube import fmt_ts, watch_url

MAX_TEXT = 1900  # Notion caps a rich_text item at 2000 chars
MAX_RICH_ITEMS = 100  # ... and a rich_text array at 100 items
APPEND_BATCH = 100  # Notion caps children per request at 100
MAX_CHILDREN = 100
TRANSCRIPT_PARAGRAPH_SEC = 60


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
    caption_rich: list[dict[str, Any]] | None = None

    def to_block(self) -> dict[str, Any]:
        if not self.upload_id:
            raise ValueError("image slot was not uploaded")
        return {
            "object": "block",
            "type": "image",
            "image": {
                "type": "file_upload",
                "file_upload": {"id": self.upload_id},
                "caption": self.caption_rich or rt(self.caption_text),
            },
        }


class ConstraintViolation(ValueError):
    pass


# ---------------------------------------------------------------- rich text --


def _clip(s: str, n: int = MAX_TEXT) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _text_item(content: str, *, link: str | None = None, color: str | None = None,
               **annotations: bool) -> dict[str, Any]:
    item: dict[str, Any] = {"type": "text", "text": {"content": content}}
    if link:
        item["text"]["link"] = {"url": link}
    ann = {k: True for k, v in annotations.items() if v}
    if color and color != "default":
        ann["color"] = color
    if ann:
        item["annotations"] = ann
    return item


def rt(text: str, link: str | None = None, bold: bool = False, *, italic: bool = False,
       code: bool = False, color: str | None = None) -> list[dict[str, Any]]:
    """One plain rich_text item (clipped to Notion's limit; whitespace is kept)."""
    text = text or ""
    if len(text) > MAX_TEXT:
        text = text[: MAX_TEXT - 1] + "…"
    return [_text_item(text, link=link, color=color, bold=bold, italic=italic, code=code)]


# **bold**, `code`, *italic* / _italic_ (not inside words, so snake_case and 2*3*4 survive)
_INLINE = re.compile(
    r"\*\*(?P<b>[^*\n]+?)\*\*"
    r"|`(?P<c>[^`\n]+?)`"
    r"|(?<![\w*])\*(?P<i>[^*\s][^*\n]*?)\*(?![\w*])"
    r"|(?<![\w_])_(?P<u>[^_\s][^_\n]*?)_(?![\w_])"
)


def rich(text: str, *, color: str | None = None) -> list[dict[str, Any]]:
    """Markdown-lite -> Notion rich text: **bold**, *italic*, `code`. Long text is
    split into items of at most MAX_TEXT chars; at most MAX_RICH_ITEMS items."""
    text = (text or "").strip()
    out: list[dict[str, Any]] = []

    def add(content: str, **ann: bool) -> None:
        while content:
            out.append(_text_item(content[:MAX_TEXT], color=color, **ann))
            content = content[MAX_TEXT:]

    pos = 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            add(text[pos : m.start()])
        if m.group("b"):
            add(m.group("b"), bold=True)
        elif m.group("c"):
            add(m.group("c"), code=True)
        else:
            add(m.group("i") or m.group("u"), italic=True)
        pos = m.end()
    if pos < len(text):
        add(text[pos:])
    if len(out) > MAX_RICH_ITEMS:  # pathological input: fall back to plain chunks
        out = [_text_item(text[i : i + MAX_TEXT], color=color) for i in range(0, len(text), MAX_TEXT)]
        out = out[:MAX_RICH_ITEMS]
    return out or [_text_item("")]


def plain(rich_items: list[dict[str, Any]]) -> str:
    return "".join(i.get("text", {}).get("content", "") for i in rich_items)


# ------------------------------------------------------------------- blocks --


def _block(kind: str, rich_items: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {"object": "block", "type": kind, kind: {"rich_text": rich_items, **extra}}


def callout(rich_items: list[dict[str, Any]], emoji: str, color: str = "default",
            children: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    extra: dict[str, Any] = {"icon": {"type": "emoji", "emoji": emoji}, "color": color}
    if children:
        extra["children"] = children
    return _block("callout", rich_items, **extra)


def heading(level: int, rich_items: list[dict[str, Any]], *, children: list[dict[str, Any]] | None = None,
            color: str = "default") -> dict[str, Any]:
    extra: dict[str, Any] = {"color": color}
    if children:
        extra["is_toggleable"] = True
        extra["children"] = children[:MAX_CHILDREN]
    return _block(f"heading_{level}", rich_items, **extra)


def toggle(rich_items: list[dict[str, Any]], children: list[dict[str, Any]]) -> dict[str, Any]:
    return _block("toggle", rich_items, children=children[:MAX_CHILDREN])


def table(rows: list[list[list[dict[str, Any]]]], *, header: bool = True) -> dict[str, Any]:
    width = max((len(r) for r in rows), default=1)
    children = [
        {"object": "block", "type": "table_row", "table_row": {"cells": r + [[] for _ in range(width - len(r))]}}
        for r in rows[:MAX_CHILDREN]
    ]
    return {"object": "block", "type": "table",
            "table": {"table_width": width, "has_column_header": header, "has_row_header": False,
                      "children": children}}


def divider() -> dict[str, Any]:
    return {"object": "block", "type": "divider", "divider": {}}


def toc() -> dict[str, Any]:
    return {"object": "block", "type": "table_of_contents", "table_of_contents": {"color": "gray"}}


# --------------------------------------------------------------------- page --


def page_marker(job_id: str) -> str:
    return f"#{str(job_id)[:8]}"


def page_title(title: str, job_id: str) -> str:
    return f"{_clip(title or 'Video notes', 180)} · {page_marker(job_id)}"


def frame_caption(f: FrameInput) -> str:
    reason = (f.reason or "").strip()
    text = f"[{fmt_ts(f.timestamp_sec)}] {f.caption.strip()}"
    return f"{text} — {reason}" if reason else text


def frame_caption_rich(f: FrameInput, video_id: str) -> list[dict[str, Any]]:
    """Same text as frame_caption (plain text still starts with "[m:ss]"), with the
    stamp linked to that moment of the video and the reason in italics."""
    items = rt(f"[{fmt_ts(f.timestamp_sec)}]", link=watch_url(video_id, f.timestamp_sec), bold=True)
    items += rt(" " + f.caption.strip().replace("**", ""))
    reason = (f.reason or "").strip()
    if reason:
        items += rt(" — " + reason.replace("**", ""), italic=True, color="gray")
    return items


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


def transcript_paragraphs(segments: list[dict[str, Any]], video_id: str) -> list[dict[str, Any]]:
    """Group transcript segments into ~1-minute paragraphs, each led by a linked stamp."""
    if not segments:
        return []
    span = max(1.0, float(segments[-1].get("end") or segments[-1]["start"]))
    window = max(TRANSCRIPT_PARAGRAPH_SEC, span / MAX_CHILDREN + 1)
    groups: list[list[dict[str, Any]]] = []
    for seg in segments:
        if not groups or float(seg["start"]) - float(groups[-1][0]["start"]) >= window:
            groups.append([])
        groups[-1].append(seg)
    out = []
    for g in groups[:MAX_CHILDREN]:
        t = int(float(g[0]["start"]))
        body = " ".join(str(s.get("text") or "").strip() for s in g).strip()
        items = rt(fmt_ts(t), link=watch_url(video_id, t), color="gray") + [_text_item("  ")]
        items += [_text_item(body[i : i + MAX_TEXT]) for i in range(0, len(body), MAX_TEXT)][:MAX_RICH_ITEMS - 2]
        out.append(_block("paragraph", items))
    return out


def _header(job_id: str, video_id: str, video_title: str | None, title: str, duration_sec: int | None,
            n_sections: int, n_frames: int, model: str | None, generated_on: str | None) -> dict[str, Any]:
    meta = []
    if duration_sec:
        meta.append(f"⏱ {fmt_ts(duration_sec)}")
    if n_sections:
        meta.append(f"{n_sections} section{'s' if n_sections != 1 else ''}")
    meta.append(f"{n_frames} key frame{'s' if n_frames != 1 else ''}" if n_frames else "text only")
    items = rt(video_title or title, link=watch_url(video_id), bold=True)
    items += rt("\n" + "  ·  ".join(meta))
    small = []
    if generated_on:
        small.append(f"Generated {generated_on}")
    if model:
        small.append(f"with {model}")
    small.append(f"Job {job_id}")
    items += rt("\n" + " · ".join(small), color="gray")
    return callout(items, "🎬", "gray_background")


def build_blocks(
    *,
    job_id: str,
    video_id: str,
    video_title: str | None,
    duration_sec: int | None,
    synthesis: dict[str, Any],
    frames: list[FrameInput],
    transcript: list[dict[str, Any]] | None = None,
    model: str | None = None,
    generated_on: str | None = None,
) -> list[dict[str, Any] | ImageSlot]:
    """Deterministic block list for the page (image blocks are ImageSlots)."""
    for f in frames:
        validate_frame(f, job_id)

    sections: list[dict[str, Any]] = [s for s in (synthesis.get("sections") or []) if s]
    title = synthesis.get("title") or video_title or "Video notes"
    takeaways = [str(t) for t in (synthesis.get("key_takeaways") or []) if str(t).strip()]
    glossary = [g for g in (synthesis.get("glossary") or []) if g.get("term") and g.get("definition")]
    questions = [q for q in (synthesis.get("review_questions") or []) if q.get("question") and q.get("answer")]

    out: list[dict[str, Any] | ImageSlot] = [
        _header(job_id, video_id, video_title, title, duration_sec, len(sections), len(frames), model, generated_on)
    ]
    if len(sections) >= 3:
        out.append(toc())

    tldr = (synthesis.get("tldr") or "").strip()
    if tldr:
        out.append(callout(rt("Summary", bold=True) + rt("\n") + rich(tldr), "💡", "blue_background"))

    if takeaways:
        out.append(heading(2, rt("🎯 Key takeaways")))
        out.extend(_block("numbered_list_item", rich(t)) for t in takeaways)

    by_section: dict[int, list[FrameInput]] = {}
    if sections:
        for f in frames:
            by_section.setdefault(_section_for(f.timestamp_sec, sections, f.section_index), []).append(f)
        out.append(divider())

    for i, s in enumerate(sections):
        start = int(s.get("start_sec") or 0)
        head = rt(f"▶ {fmt_ts(start)}", link=watch_url(video_id, start), color="gray")
        head += rt("  " + (s.get("heading") or f"Part {i + 1}").replace("**", ""))
        out.append(heading(2, head))
        for b in s.get("bullets") or []:
            if str(b).strip():
                out.append(_block("bulleted_list_item", rich(str(b))))
        for f in sorted(by_section.get(i, []), key=lambda x: x.timestamp_sec):
            out.append(ImageSlot(frame=f, caption_text=frame_caption(f), caption_rich=frame_caption_rich(f, video_id)))

    if not sections and frames:  # no sections: append frames at the end
        out.append(heading(2, rt("🖼 Key visuals")))
        for f in sorted(frames, key=lambda x: x.timestamp_sec):
            out.append(ImageSlot(frame=f, caption_text=frame_caption(f), caption_rich=frame_caption_rich(f, video_id)))

    if glossary or questions or transcript:
        out.append(divider())
    if glossary:
        out.append(heading(2, rt("📖 Glossary")))
        rows = [[rt("Term", bold=True), rt("Definition", bold=True)]]
        rows += [[rt(str(g["term"]).replace("**", ""), bold=True), rich(str(g["definition"]))] for g in glossary]
        out.append(table(rows))
    if questions:
        out.append(heading(2, rt("🧠 Review questions")))
        out.append(_block("paragraph", rt("Try to answer each one before opening it.", italic=True, color="gray")))
        for n, q in enumerate(questions, start=1):
            out.append(toggle(rt(f"Q{n}. ", bold=True) + rich(str(q["question"])),
                              [_block("paragraph", rich(str(q["answer"])))]))
    if transcript:
        paras = transcript_paragraphs(transcript, video_id)
        if paras:
            out.append(heading(3, rt("📜 Full transcript"), children=paras, color="gray"))

    out.append(divider())
    out.append(
        _block(
            "paragraph",
            rt("Generated by Video Notes Agent. Every image is a real frame captured from the video at the "
               "timestamp shown in its caption; click a timestamp to jump to that moment."),
            color="gray",
        )
    )
    return out


def batches(items: list[Any], size: int = APPEND_BATCH) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]
