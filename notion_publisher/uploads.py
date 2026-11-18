"""Notion File Upload API: create upload -> send bytes -> reference by id.

Uploads must be attached to a block within one hour, so we upload in the
publish stage only, immediately before creating/appending the page.
"""

from __future__ import annotations

from .client import NotionClient

MAX_SINGLE_PART = 20 * 1024 * 1024


async def upload_image(client: NotionClient, data: bytes, filename: str, content_type: str = "image/jpeg") -> str:
    if len(data) > MAX_SINGLE_PART:
        raise ValueError(f"{filename} is {len(data)} bytes; single-part uploads are limited to 20 MB")
    created = await client.post("file_uploads", {"filename": filename, "content_type": content_type})
    upload_id = created["id"]
    sent = await client.request(
        "POST",
        f"file_uploads/{upload_id}/send",
        files={"file": (filename, data, content_type)},
    )
    status = sent.get("status")
    if status not in (None, "uploaded"):
        raise ValueError(f"upload {upload_id} ended in status {status!r}")
    return upload_id
