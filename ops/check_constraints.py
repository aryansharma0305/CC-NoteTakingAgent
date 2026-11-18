#!/usr/bin/env python3
"""Constraint checker for published pages (stdlib only).

For each job id it opens the Notion page and verifies:
  B1  every image block has a non-empty caption that starts with "[mm:ss]"
  B2  every image is an uploaded file (never an external/generated URL)
  B3  each image's bytes hash-match a frame captured by the worker (S3 sha256)
  A   the video duration is <= MAX_DURATION_SEC
  D   exactly one page carries the job's marker under the parent (no duplicates)

Usage:
  NOTION_TOKEN=... ADMIN_TOKEN=... python ops/check_constraints.py <job_id> [<job_id> ...]
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.request

BASE = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
ADMIN = os.environ.get("ADMIN_TOKEN", "change-me")
TOKEN = os.environ.get("NOTION_TOKEN", "")
VERSION = os.environ.get("NOTION_VERSION", "2022-06-28")
MAX_DURATION = int(os.environ.get("MAX_DURATION_SEC", "1200"))
CAPTION_RE = re.compile(r"^\[\d+:\d{2}(?::\d{2})?\]\s+\S")


def get(url: str, headers: dict[str, str] | None = None, raw: bool = False):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read()
    return body if raw else json.loads(body)


def notion(path: str):
    return get(f"https://api.notion.com/v1/{path}",
               {"Authorization": f"Bearer {TOKEN}", "Notion-Version": VERSION})


def children(block_id: str):
    cursor = None
    while True:
        q = f"blocks/{block_id}/children?page_size=100" + (f"&start_cursor={cursor}" if cursor else "")
        d = notion(q)
        yield from d["results"]
        if not d.get("has_more"):
            return
        cursor = d["next_cursor"]


def check(job_id: str) -> bool:
    adm = {"X-Admin-Token": ADMIN}
    job = get(f"{BASE}/admin/jobs/{job_id}", adm)
    print(f"\n== {job_id}  {job['status']}  {job.get('video_title') or ''}")
    ok = True

    def res(flag: bool, label: str) -> None:
        nonlocal ok
        ok &= flag
        print(f"   [{'PASS' if flag else 'FAIL'}] {label}")

    res((job.get("duration_sec") or 0) <= MAX_DURATION, f"A  duration {job.get('duration_sec')} s <= {MAX_DURATION} s")
    page_id = job.get("notion_page_id")
    if not page_id:
        res(job["status"] in ("FAILED", "DEAD_LETTERED"), "no page (job did not publish)")
        return ok

    try:
        frames = get(f"{BASE}/admin/jobs/{job_id}/artifacts/frames", adm)["data"]["frames"]
    except Exception:  # noqa: BLE001
        frames = []
    hashes = {f["sha256"]: f for f in frames if f.get("status") == "ok"}

    images = [b for b in children(page_id) if b["type"] == "image"]
    print(f"   {len(images)} image(s) on page; {len(hashes)} frame(s) captured")
    for b in images:
        img = b["image"]
        caption = "".join(t.get("plain_text", "") for t in img.get("caption", []))
        res(bool(CAPTION_RE.match(caption)), f"B1 caption: {caption[:70]!r}")
        res(img.get("type") == "file", f"B2 uploaded file (type={img.get('type')})")
        if img.get("type") == "file":
            data = get(img["file"]["url"], raw=True)
            h = hashlib.sha256(data).hexdigest()
            res(h in hashes, f"B3 bytes match a captured frame (sha256 {h[:12]}…)")

    page = notion(f"pages/{page_id}")
    parent = page.get("parent", {})
    marker = f"#{job_id[:8]}"
    if parent.get("type") == "page_id":
        dupes = [c for c in children(parent["page_id"])
                 if c["type"] == "child_page" and c["child_page"]["title"].endswith(marker)
                 and not c.get("archived")]
        res(len(dupes) == 1, f"D  exactly one page with marker {marker} under parent (found {len(dupes)})")
    return ok


def main() -> None:
    if not TOKEN:
        sys.exit("Set NOTION_TOKEN")
    ids = sys.argv[1:]
    if not ids:
        sys.exit(__doc__)
    results = [check(i) for i in ids]
    print(f"\n{sum(results)}/{len(results)} job(s) passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
