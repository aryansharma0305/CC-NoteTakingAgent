"""Prompt for the synthesis call (one call per transcript chunk)."""

from __future__ import annotations

from vidnotes_common.youtube import fmt_ts

SYSTEM_PROMPT = """You turn a timestamped YouTube transcript into structured study notes.

Return one JSON object with exactly these keys:
- "title": a concise title for the notes.
- "tldr": 2-3 sentences summarizing the whole excerpt.
- "sections": ordered list of {"heading", "start_sec", "bullets"}. Follow the order of the video.
  start_sec is where the section begins (integer seconds). Each section has 2-6 bullets; each bullet
  is a self-contained, factual study note (definitions, steps, numbers, conclusions), not a teaser.
- "visual_moments": moments where something important is SHOWN on screen, as
  {"timestamp_sec", "caption", "reason", "section_index"}.

Rules for visual_moments:
- Flag a moment only when the transcript signals something visible: a slide, diagram, chart, table,
  equation, code, a demo or screen ("as you can see", "this slide", "this diagram", "look at the code",
  "on the screen", "here we have", "this graph").
- Pick timestamp_sec 2-3 seconds AFTER the verbal cue so the visual is fully on screen.
- caption: one sentence saying what the frame shows, based on what the speaker describes.
- reason: one short sentence saying why it matters for studying (why it was flagged).
- section_index: the 0-based index of the section in YOUR "sections" list that the moment belongs to.
- At most {max_frames} moments; prefer the most information-dense visuals. If nothing visual is
  referenced, return an empty list. Never invent visuals the transcript does not suggest.
- All timestamps must lie inside the excerpt's time range.

Write in the transcript's language. Output only the JSON object."""


def system_prompt(max_frames: int) -> str:
    return SYSTEM_PROMPT.replace("{max_frames}", str(max_frames))


def user_prompt(
    *,
    video_title: str | None,
    duration_sec: int | None,
    lines: list[str],
    part: int,
    parts: int,
    start_sec: float,
    end_sec: float,
) -> str:
    head = [f"Video title: {video_title or 'unknown'}"]
    if duration_sec:
        head.append(f"Video duration: {fmt_ts(duration_sec)} ({duration_sec} s)")
    if parts > 1:
        head.append(
            f"This is part {part} of {parts} of the transcript, covering {fmt_ts(start_sec)}-{fmt_ts(end_sec)} "
            f"({int(start_sec)}-{int(end_sec)} s). Only summarize this part; sections continue across parts."
        )
    head.append("")
    head.append("Transcript ([mm:ss] text):")
    return "\n".join(head + lines)


def repair_prompt(original_user: str, bad_output: str, error: str) -> str:
    return (
        f"{original_user}\n\nYour previous answer was not valid for the required JSON format "
        f"({error[:300]}). Previous answer (truncated):\n{bad_output[:1500]}\n\n"
        "Return ONLY the corrected JSON object."
    )
