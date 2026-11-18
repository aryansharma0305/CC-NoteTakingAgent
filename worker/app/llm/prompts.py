"""Prompt for the synthesis call (one call per transcript chunk)."""

from __future__ import annotations

from vidnotes_common.youtube import fmt_ts

SYSTEM_PROMPT = """You turn a timestamped YouTube transcript into structured study notes.

Return one JSON object with exactly these keys:
- "title": a concise title for the notes.
- "tldr": 3-5 sentences summarizing the whole excerpt.
- "sections": ordered list of {"heading", "start_sec", "bullets"}. Follow the order of the video.
  Cover EVERY topic in the excerpt, beginning to end; do not stop early or skip the last part.
  start_sec is where the section begins, in integer seconds: take it from the [mm:ss] stamp of the
  transcript line where that topic starts (e.g. [01:25] -> 85). Different sections have different
  start_sec values. Each section has 3-8 bullets; each bullet is a full, self-contained sentence with
  the specifics (definitions, steps, numbers, names, causes, conclusions), not a teaser.
- "visual_moments": moments worth a screenshot, as {"timestamp_sec", "caption", "reason", "section_index"}.

Length: write detailed notes of about {words} words in total (sections + bullets), so the reader can
study from them without watching the video. Use only facts stated in the transcript.

Rules for visual_moments:
- Return {frames} moments, spread across the excerpt (not bunched together, at least 10 s apart).
- Prefer moments the transcript signals as visible: a slide, diagram, chart, table, equation, code,
  demo or screen ("as you can see", "this slide", "this diagram", "on the screen", "this graph").
  Pick timestamp_sec 2-3 seconds AFTER such a cue so the visual is fully on screen.
- Otherwise pick moments where a key fact from your sections is being said, using the timestamp of
  that transcript line.
- caption: one sentence on what is being shown or discussed at that moment. Do not invent specific
  on-screen details the transcript does not mention.
- reason: one short sentence saying why it matters for studying.
- section_index: the 0-based index of the section in YOUR "sections" list that the moment belongs to.
- All timestamps must lie inside the excerpt's time range.

Write in the transcript's language. Output only the JSON object."""


def system_prompt(frames: int, words: int = 600) -> str:
    """frames/words are the targets for ONE transcript chunk."""
    frames_txt = f"exactly {frames}" if frames > 0 else "0 (an empty list)"
    return SYSTEM_PROMPT.replace("{frames}", frames_txt).replace("{words}", str(max(150, words)))


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
