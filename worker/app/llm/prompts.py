# ruff: noqa: E501  (prompt text is kept as natural, unwrapped lines for the model)
"""Prompts for the two LLM calls.

1. Synthesis (one call per transcript chunk): sections + bullet notes + visual moments.
2. Overview (one call over the merged notes): title, summary and study aids.

Written to work with small local models (e.g. qwen2.5:3b via Ollama) as well as
hosted ones: short numbered rules, an explicit JSON shape, timestamps copied
verbatim as "m:ss" strings, and the key constraints repeated at the END of the
user message, where small models pay the most attention.
"""

from __future__ import annotations

from vidnotes_common.youtube import fmt_ts

SYSTEM_PROMPT = """You are an expert note-taker. You turn a timestamped video transcript into clear, accurate study notes.

Reply with ONE JSON object and nothing else, in exactly this shape:
{
  "title": "Short, specific title for these notes",
  "summary": "3-4 sentences: what this part teaches and its main point.",
  "sections": [
    {"heading": "Name of the topic", "start": "m:ss",
     "bullets": ["**Key term**: one complete sentence stating a concrete fact.", "Another complete sentence."]}
  ],
  "visuals": [
    {"time": "m:ss", "caption": "What is on screen at that moment.", "reason": "Why seeing it helps."}
  ]
}

SECTIONS
1. Write about {sections} sections, one per main topic, in the order of the video. Cover the excerpt from its first line to its LAST line; do not stop early.
2. "start" is the [m:ss] stamp of the transcript line where that topic begins, copied exactly (for example "3:25"). Every section has a different start.
3. Give each section 3-6 bullets. Each bullet is one complete sentence with specifics: definitions, numbers, names, steps, examples, causes or conclusions. Start a bullet with the key term in **bold** when it introduces one.
4. Headings are short topic names (2-6 words), not sentences.
5. Use only facts stated in the transcript. Never invent facts. Skip greetings, sponsor segments and requests to like or subscribe.

VISUALS
6. Pick exactly {frames} moments worth a screenshot, spread across the excerpt and at least 20 seconds apart.
7. Prefer moments where the speaker refers to something on screen: a slide, diagram, chart, table, equation, code, demo, or words like "as you can see", "look at", "this shows", "here we have". Use a time 2-3 seconds after that line's stamp so the visual is fully on screen.
8. Otherwise choose moments where an important idea from your notes is explained.
9. "caption": one sentence describing what is shown, based only on the transcript (do not invent on-screen details). "reason": one short sentence on why it matters for studying.

Write about {words} words in total. {language_rule}"""


OVERVIEW_PROMPT = """You write the study-guide part of a set of video notes. You are given the notes, not the video.

Reply with ONE JSON object and nothing else, in exactly this shape:
{
  "title": "Specific title naming the subject (at most 10 words)",
  "summary": "3-5 sentences: what the video teaches, how, and its main conclusion.",
  "key_takeaways": ["Short sentence with one important idea.", "..."],
  "glossary": [{"term": "Term", "definition": "One-sentence definition taken from the notes."}],
  "review_questions": [{"question": "A question that tests understanding?", "answer": "1-2 sentence answer from the notes."}]
}

Rules:
1. key_takeaways: {takeaways} items, the most important ideas, most important first.
2. glossary: up to {glossary} important technical terms that appear in the notes, with definitions taken from the notes. Use an empty list if there are none.
3. review_questions: {questions} questions that check understanding (why / how / what-if), not trivia. Answers must come from the notes.
4. Use only information in the notes. Do not add outside facts.
5. The title must name the actual subject, never something generic like "Video notes".
{language_rule}"""


def language_rule(language: str | None) -> str:
    if language:
        return f"Write all text in {language}, even if the transcript is in another language."
    return "Write in the same language as the transcript."


def section_target(start_sec: float, end_sec: float) -> int:
    """Roughly one section per 2-3 minutes, at least 2."""
    minutes = max(0.0, (end_sec - start_sec) / 60)
    return max(2, min(8, round(minutes / 2.5)))


def system_prompt(frames: int, words: int = 600, *, sections: int = 4, language: str | None = None) -> str:
    """frames/words/sections are the targets for ONE transcript chunk."""
    frames_txt = str(frames) if frames > 0 else "0 (return an empty list)"
    return (
        SYSTEM_PROMPT.replace("{frames}", frames_txt)
        .replace("{words}", str(max(150, words)))
        .replace("{sections}", str(sections))
        .replace("{language_rule}", language_rule(language))
    )


def user_prompt(
    *,
    video_title: str | None,
    duration_sec: int | None,
    lines: list[str],
    part: int,
    parts: int,
    start_sec: float,
    end_sec: float,
    frames: int | None = None,
    sections: int | None = None,
) -> str:
    head = [f"Video title: {video_title or 'unknown'}"]
    if duration_sec:
        head.append(f"Video duration: {fmt_ts(duration_sec)}")
    rng = f"{fmt_ts(start_sec)} to {fmt_ts(end_sec)}"
    if parts > 1:
        head.append(
            f"This is part {part} of {parts} of the transcript, covering {rng}. "
            "Only write notes for this part; earlier and later parts are handled separately."
        )
    head.append("")
    head.append("Transcript ([m:ss] text):")
    tail = ["", f"Now write the JSON notes for {rng}."]
    reminders = []
    if sections:
        reminders.append(f"about {sections} sections that together cover up to {fmt_ts(end_sec)}")
    if frames is not None:
        reminders.append(f"exactly {frames} visuals" if frames > 0 else "an empty visuals list")
    reminders.append('every "start" and "time" is an m:ss stamp copied from the transcript')
    tail.append("Remember: " + "; ".join(reminders) + ".")
    return "\n".join(head + lines + tail)


def overview_system_prompt(*, takeaways: int = 5, glossary: int = 8, questions: int = 4,
                           language: str | None = None) -> str:
    return (
        OVERVIEW_PROMPT.replace("{takeaways}", f"{max(3, takeaways - 1)}-{takeaways}")
        .replace("{glossary}", str(glossary))
        .replace("{questions}", f"{max(2, questions - 1)}-{questions}")
        .replace("{language_rule}", language_rule(language))
    )


def overview_user_prompt(*, video_title: str | None, notes: str) -> str:
    return (
        f"Video title: {video_title or 'unknown'}\n\nNotes:\n{notes}\n\n"
        "Now write the JSON study guide for these notes."
    )


def repair_prompt(original_user: str, bad_output: str, error: str) -> str:
    return (
        f"{original_user}\n\nYour previous answer was not valid for the required JSON format "
        f"({error[:300]}). Previous answer (truncated):\n{bad_output[:1500]}\n\n"
        "Return ONLY the corrected JSON object, in the exact shape described in the instructions."
    )
