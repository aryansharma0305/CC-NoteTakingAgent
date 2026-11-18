"""PUBLISHING (80 -> 100): thin wrapper around notion_publisher.publish()."""

from __future__ import annotations

from typing import Any

from notion_publisher import FrameInput, PublishJob, publish
from vidnotes_common.crypto import token_fingerprint
from vidnotes_common.errors import ErrorCode, RetryableError
from vidnotes_common.status import JobStatus

from .. import repo
from ..context import WorkerContext
from . import StageResult


async def run(ctx: WorkerContext, job, msg) -> StageResult:
    s = ctx.settings
    synthesis = await repo.get_artifact(ctx.engine, job.id, "synthesis")
    frames_art = await repo.get_artifact(ctx.engine, job.id, "frames") or {"frames": []}
    if synthesis is None:
        raise RetryableError(ErrorCode.UNEXPECTED_ERROR, "synthesis artifact missing")

    warnings: list[dict[str, Any]] = []
    frames: list[FrameInput] = []
    for f in frames_art.get("frames", []):
        if f.get("status") == "ok":
            key = f["object_key"]
            frames.append(
                FrameInput(
                    timestamp_sec=int(f["timestamp_sec"]),
                    caption=f.get("caption") or "",
                    reason=f.get("reason") or "",
                    section_index=f.get("section_index"),
                    object_key=key,
                    load=(lambda k=key: ctx.storage.get(k)),
                    sha256=f.get("sha256"),
                )
            )
        else:
            warnings.append({"code": f.get("error") or ErrorCode.FRAME_EXTRACTION_FAILED,
                             "timestamp_sec": f.get("timestamp_sec")})

    token = ctx.cipher.decrypt(msg.enc_notion_token)
    fp = token_fingerprint(token)
    parent_id = msg.notion_parent_id or s.default_notion_parent_id or None

    result = await publish(
        PublishJob(
            job_id=str(job.id),
            video_id=job.video_id,
            youtube_url=job.youtube_url,
            video_title=job.video_title,
            duration_sec=job.duration_sec,
            notion_page_id=job.notion_page_id,
            notion_page_url=job.notion_page_url,
            publish_state=job.publish_state,
            publish_cursor=job.publish_cursor or 0,
        ),
        synthesis,
        frames,
        token,
        parent_id,
        repo.DBPublishStore(ctx.engine, job.id),
        frames_requested=int(frames_art.get("requested", len(frames_art.get("frames", [])))),
        notion_version=s.notion_version,
        acquire=lambda: ctx.limiter.acquire("notion", 1, key_suffix=fp),
        fault_429_rate=s.fault_429_rates().get("notion", 0.0),
    )
    warnings.extend(result.warnings)
    if frames_art.get("stream_error"):
        warnings.append({"code": ErrorCode.STREAM_UNAVAILABLE, "detail": frames_art["stream_error"][:200]})
    complete = result.frames_published == result.frames_requested and not warnings
    return StageResult(
        job_fields={
            "notion_page_id": result.page_id,
            "notion_page_url": result.page_url,
            "frames_requested": result.frames_requested,
            "frames_published": result.frames_published,
            "warnings": warnings,
            "publish_state": "done",
        },
        final_status=JobStatus.COMPLETED if complete else JobStatus.COMPLETED_WITH_WARNINGS,
    )
