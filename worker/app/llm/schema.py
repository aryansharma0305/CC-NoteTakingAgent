"""Pydantic models for the LLM's strict JSON output, plus the JSON Schema we send."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class Section(BaseModel):
    heading: str
    start_sec: int = 0
    bullets: list[str] = Field(default_factory=list)

    @field_validator("start_sec", mode="before")
    @classmethod
    def _int(cls, v):
        return int(float(v or 0))


class VisualMoment(BaseModel):
    timestamp_sec: int
    caption: str
    reason: str
    section_index: int | None = None

    @field_validator("timestamp_sec", mode="before")
    @classmethod
    def _int(cls, v):
        return int(float(v))


class Synthesis(BaseModel):
    title: str = ""
    tldr: str = ""
    sections: list[Section] = Field(default_factory=list)
    visual_moments: list[VisualMoment] = Field(default_factory=list)


SYNTHESIS_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "tldr": {"type": "string"},
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "heading": {"type": "string"},
                    "start_sec": {"type": "integer"},
                    "bullets": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["heading", "start_sec", "bullets"],
                "additionalProperties": False,
            },
        },
        "visual_moments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "timestamp_sec": {"type": "integer"},
                    "caption": {"type": "string"},
                    "reason": {"type": "string"},
                    "section_index": {"type": "integer"},
                },
                "required": ["timestamp_sec", "caption", "reason", "section_index"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["title", "tldr", "sections", "visual_moments"],
    "additionalProperties": False,
}
