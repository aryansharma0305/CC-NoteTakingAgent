"""Pydantic models for the LLM's JSON output, plus the JSON Schemas we send.

The schema the model sees is deliberately small-model friendly:
* timestamps are copied verbatim as "m:ss" strings (3B models are bad at turning
  [03:25] into 205), and we parse them here;
* there is no section_index to get wrong -- frames are attached to sections by
  timestamp after parsing.
Internally (and in the stored artifacts) the field names stay start_sec /
timestamp_sec / tldr / visual_moments, so older artifacts still load.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import AliasChoices, BaseModel, Field, field_validator

_TS = re.compile(r"^\[?\s*(?:(\d+):)?(\d{1,3}):(\d{1,2}(?:\.\d+)?)\s*\]?$")


def parse_ts(v: Any) -> int:
    """85 | 85.4 | "85" | "1:25" | "01:25" | "[1:25]" | "1:02:03" -> seconds."""
    if v is None or v == "":
        return 0
    if isinstance(v, bool):
        raise ValueError("timestamp must be a number or m:ss")
    if isinstance(v, int | float):
        return max(0, int(v))
    s = str(v).strip()
    try:
        return max(0, int(float(s)))
    except ValueError:
        pass
    m = _TS.match(s)
    if not m:
        # Tolerate "at 1:25" / "1:25 - 1:40": take the first stamp in the string.
        m2 = re.search(r"(?:(\d+):)?(\d{1,3}):(\d{2})", s)
        if not m2:
            raise ValueError(f"not a timestamp: {s[:30]!r}")
        h, mm, ss = m2.groups()
    else:
        h, mm, ss = m.groups()
    return int(h or 0) * 3600 + int(mm) * 60 + int(float(ss))


class Section(BaseModel):
    heading: str
    start_sec: int = Field(default=0, validation_alias=AliasChoices("start_sec", "start", "time"))
    bullets: list[str] = Field(default_factory=list)

    @field_validator("start_sec", mode="before")
    @classmethod
    def _ts(cls, v):
        return parse_ts(v)

    @field_validator("bullets", mode="before")
    @classmethod
    def _bullets(cls, v):
        if isinstance(v, str):
            return [x for x in re.split(r"\n+", v) if x.strip()]
        return [str(x) for x in (v or []) if x is not None and str(x).strip()]


class VisualMoment(BaseModel):
    timestamp_sec: int = Field(validation_alias=AliasChoices("timestamp_sec", "time", "timestamp", "start"))
    caption: str
    reason: str = ""
    section_index: int | None = None

    @field_validator("timestamp_sec", mode="before")
    @classmethod
    def _ts(cls, v):
        return parse_ts(v)


class GlossaryItem(BaseModel):
    term: str
    definition: str


class ReviewQuestion(BaseModel):
    question: str
    answer: str


class Synthesis(BaseModel):
    title: str = ""
    tldr: str = Field(default="", validation_alias=AliasChoices("tldr", "summary"))
    sections: list[Section] = Field(default_factory=list)
    visual_moments: list[VisualMoment] = Field(
        default_factory=list, validation_alias=AliasChoices("visual_moments", "visuals")
    )
    # Study aids (filled by the overview call; empty when it is off or failed).
    key_takeaways: list[str] = Field(default_factory=list)
    glossary: list[GlossaryItem] = Field(default_factory=list)
    review_questions: list[ReviewQuestion] = Field(default_factory=list)


class Overview(BaseModel):
    """Output of the final "study aids" call over the merged notes."""

    title: str = ""
    tldr: str = Field(default="", validation_alias=AliasChoices("tldr", "summary"))
    key_takeaways: list[str] = Field(default_factory=list)
    glossary: list[GlossaryItem] = Field(default_factory=list)
    review_questions: list[ReviewQuestion] = Field(default_factory=list)

    @field_validator("glossary", "review_questions", mode="before")
    @classmethod
    def _drop_bad(cls, v, info):
        # A 3B model occasionally emits a bare string or a half-filled object; drop those.
        keys = ("term", "definition") if info.field_name == "glossary" else ("question", "answer")
        if not isinstance(v, list):
            return []
        return [x for x in v if isinstance(x, dict) and all(str(x.get(k) or "").strip() for k in keys)]

    @field_validator("key_takeaways", mode="before")
    @classmethod
    def _strs(cls, v):
        if not isinstance(v, list):
            return []
        return [str(x) for x in v if isinstance(x, str | int | float) and str(x).strip()]


def _obj(props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


_STR = {"type": "string"}
_STRS = {"type": "array", "items": _STR}

SYNTHESIS_JSON_SCHEMA: dict[str, Any] = _obj({
    "title": _STR,
    "summary": _STR,
    "sections": {"type": "array", "items": _obj({"heading": _STR, "start": _STR, "bullets": _STRS})},
    "visuals": {"type": "array", "items": _obj({"time": _STR, "caption": _STR, "reason": _STR})},
})

OVERVIEW_JSON_SCHEMA: dict[str, Any] = _obj({
    "title": _STR,
    "summary": _STR,
    "key_takeaways": _STRS,
    "glossary": {"type": "array", "items": _obj({"term": _STR, "definition": _STR})},
    "review_questions": {"type": "array", "items": _obj({"question": _STR, "answer": _STR})},
})
