"""Adopt-or-create publishing with batched appends.

Idempotency under at-least-once redelivery:

1. Before calling Notion, the store records publish_state='creating' (committed).
2. The page is created with a deterministic marker: its title ends with
   "· #<first 8 chars of job_id>" and the first callout contains the full job id.
3. Right after creation the store saves notion_page_id + publish_state='created'.
4. On redelivery: if notion_page_id is set it is reused. If the state is
   'creating' but no id was saved, the parent's children are listed (read-your-
   writes, unlike search) looking for the marker; found -> adopt, else create.
5. Appends resume from the page's *actual* top-level child count, so a crash
   between an append and the cursor save never duplicates a batch.

All frames are uploaded first, before any page write, so the final block list is
fixed for this attempt. A frame whose upload is rejected (4xx) is omitted with a
warning, never substituted; 429/5xx bubble up and retry the whole stage.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from vidnotes_common.errors import ErrorCode, FatalError

from .blocks import FrameInput, ImageSlot, batches, build_blocks, page_marker, page_title
from .client import NotionAPIError, NotionClient
from .uploads import upload_image


@dataclass
class PublishJob:
    job_id: str
    video_id: str
    youtube_url: str
    video_title: str | None
    duration_sec: int | None
    notion_page_id: str | None = None
    notion_page_url: str | None = None
    publish_state: str | None = None
    publish_cursor: int = 0


class PublishStateStore(Protocol):
    async def mark_creating(self) -> None: ...
    async def mark_created(self, page_id: str, page_url: str, cursor: int) -> None: ...
    async def set_cursor(self, cursor: int) -> None: ...


@dataclass
class PublishResult:
    page_id: str
    page_url: str
    frames_requested: int
    frames_published: int
    warnings: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Parent:
    kind: str  # "page_id" | "database_id"
    id: str
    title_prop: str = "title"

    def as_parent(self) -> dict[str, str]:
        return {self.kind: self.id}


async def resolve_parent(client: NotionClient, parent_id: str | None) -> Parent:
    if parent_id:
        pid = parent_id.strip()
        try:
            page = await client.get(f"pages/{pid}")
            return Parent("page_id", page["id"])
        except NotionAPIError as e:
            if e.status not in (400, 404):
                raise
        try:
            db = await client.get(f"databases/{pid}")
            title_prop = next(
                (name for name, p in db.get("properties", {}).items() if p.get("type") == "title"), "Name"
            )
            return Parent("database_id", db["id"], title_prop)
        except NotionAPIError as e:
            if e.status in (400, 404):
                raise FatalError(ErrorCode.NOTION_PARENT_NOT_FOUND) from e
            raise
    # No destination configured: newest page shared with the integration.
    found = await client.post(
        "search",
        {
            "filter": {"property": "object", "value": "page"},
            "sort": {"direction": "descending", "timestamp": "last_edited_time"},
            "page_size": 10,
        },
    )
    for r in found.get("results", []):
        if r.get("object") == "page" and not r.get("archived") and not r.get("in_trash"):
            return Parent("page_id", r["id"])
    raise FatalError(ErrorCode.NO_NOTION_DESTINATION)


async def find_marked_page(client: NotionClient, parent: Parent, job_id: str) -> tuple[str, str] | None:
    marker = page_marker(job_id)
    if parent.kind == "page_id":
        async for b in client.list_children(parent.id):
            if b.get("type") == "child_page" and b.get("child_page", {}).get("title", "").endswith(marker):
                page = await client.get(f"pages/{b['id']}")
                if not page.get("archived") and not page.get("in_trash"):
                    return page["id"], page["url"]
        return None
    res = await client.post(
        f"databases/{parent.id}/query",
        {"filter": {"property": parent.title_prop, "title": {"contains": marker}}, "page_size": 5},
    )
    for page in res.get("results", []):
        if not page.get("archived") and not page.get("in_trash"):
            return page["id"], page["url"]
    return None


async def count_children(client: NotionClient, page_id: str) -> int:
    n = 0
    async for _ in client.list_children(page_id):
        n += 1
    return n


def _title_props(parent: Parent, title: str) -> dict[str, Any]:
    return {parent.title_prop: {"title": [{"type": "text", "text": {"content": title}}]}}


async def _upload_frames(
    client: NotionClient, blocks: list[Any], warnings: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Upload every image slot; drop slots whose upload is rejected. Returns plain blocks."""
    final: list[dict[str, Any]] = []
    for b in blocks:
        if not isinstance(b, ImageSlot):
            final.append(b)
            continue
        f = b.frame
        try:
            data = await f.load()
            b.upload_id = await upload_image(client, data, f"frame_{f.timestamp_sec}.jpg")
        except NotionAPIError as e:
            warnings.append({"code": ErrorCode.FRAME_UPLOAD_FAILED, "timestamp_sec": f.timestamp_sec,
                             "detail": e.message[:200]})
            continue
        except (ValueError, FileNotFoundError, KeyError) as e:
            warnings.append({"code": ErrorCode.FRAME_UPLOAD_FAILED, "timestamp_sec": f.timestamp_sec,
                             "detail": str(e)[:200]})
            continue
        final.append(b.to_block())
    return final


async def publish(
    job: PublishJob,
    synthesis: dict[str, Any],
    frames: list[FrameInput],
    token: str,
    parent_id: str | None,
    store: PublishStateStore,
    *,
    frames_requested: int | None = None,
    notion_version: str = "2022-06-28",
    acquire: Callable[[], Awaitable[None]] | None = None,
    fault_429_rate: float = 0.0,
    client: NotionClient | None = None,
) -> PublishResult:
    own = client is None
    client = client or NotionClient(token, version=notion_version, acquire=acquire, fault_429_rate=fault_429_rate)
    try:
        warnings: list[dict[str, Any]] = []
        parent = await resolve_parent(client, parent_id)
        planned = build_blocks(
            job_id=job.job_id,
            video_id=job.video_id,
            video_title=job.video_title,
            duration_sec=job.duration_sec,
            synthesis=synthesis,
            frames=frames,
        )
        blocks = await _upload_frames(client, planned, warnings)
        published = sum(1 for b in blocks if b.get("type") == "image")
        title = page_title(synthesis.get("title") or job.video_title or "Video notes", job.job_id)

        page_id, page_url = job.notion_page_id, job.notion_page_url
        if not page_id and job.publish_state == "creating":
            adopted = await find_marked_page(client, parent, job.job_id)
            if adopted:
                page_id, page_url = adopted
                await store.mark_created(page_id, page_url, 0)

        if not page_id:
            await store.mark_creating()
            first = blocks[:100]
            page = await client.post(
                "pages",
                {
                    "parent": parent.as_parent(),
                    "icon": {"type": "emoji", "emoji": "📝"},
                    "properties": _title_props(parent, title),
                    "children": first,
                },
            )
            page_id, page_url = page["id"], page["url"]
            cursor = len(first)
            await store.mark_created(page_id, page_url, cursor)
        else:
            # Resume: trust the page's real child count over the saved cursor.
            cursor = await count_children(client, page_id)
            if cursor != job.publish_cursor:
                await store.set_cursor(cursor)

        for batch in batches(blocks[cursor:]):
            await client.patch(f"blocks/{page_id}/children", {"children": batch})
            cursor += len(batch)
            await store.set_cursor(cursor)

        requested = frames_requested if frames_requested is not None else len(frames)
        return PublishResult(page_id, page_url or "", requested, published, warnings)
    finally:
        if own:
            await client.aclose()
