"""Deterministic offline LLM (LLM_PROVIDER=fake) for integration tests and dry runs.

Builds sections every ~3 minutes and flags transcript lines that mention
something visual. Never use for grading: it only rearranges transcript text.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .base import LLMClient

_LINE = re.compile(r"^\[(?:(\d+):)?(\d+):(\d{2})\]\s*(.*)$")
_VISUAL = re.compile(r"\b(slide|diagram|chart|graph|table|code|screen|see here|look at|figure|equation)\b", re.I)


class FakeLLM(LLMClient):
    name = "fake"

    async def complete_json(self, system: str, user: str, schema: dict[str, Any], max_tokens: int) -> str:
        self.maybe_inject_429()
        lines: list[tuple[int, str]] = []
        title = "Video notes"
        for raw in user.splitlines():
            if raw.startswith("Video title:"):
                title = raw.split(":", 1)[1].strip() or title
            m = _LINE.match(raw.strip())
            if m:
                h, mm, ss, text = m.groups()
                lines.append((int(h or 0) * 3600 + int(mm) * 60 + int(ss), text))
        sections: list[dict[str, Any]] = []
        for t, text in lines:
            if not sections or t - sections[-1]["start_sec"] >= 180:
                sections.append({"heading": f"Part {len(sections) + 1}", "start_sec": t, "bullets": []})
            if len(sections[-1]["bullets"]) < 4:
                sections[-1]["bullets"].append(text[:200])
        moments = []
        for t, text in lines:
            if _VISUAL.search(text):
                si = max(i for i, s in enumerate(sections) if s["start_sec"] <= t)
                moments.append({"timestamp_sec": t + 2, "caption": f"Visual referenced: {text[:120]}",
                                "reason": "The speaker points at something on screen here.", "section_index": si})
        if not moments and lines:
            for k in range(1, 4):
                t, text = lines[min(len(lines) - 1, k * len(lines) // 4)]
                si = max(i for i, s in enumerate(sections) if s["start_sec"] <= t)
                moments.append({"timestamp_sec": t, "caption": f"Frame while discussing: {text[:100]}",
                                "reason": "Representative frame for this part of the video.", "section_index": si})
        tldr = " ".join(text for _, text in lines[:3])[:400]
        return json.dumps({"title": title, "tldr": tldr, "sections": sections, "visual_moments": moments})
