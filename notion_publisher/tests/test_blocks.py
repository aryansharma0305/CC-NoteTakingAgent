import pytest

from notion_publisher.blocks import (
    ConstraintViolation,
    FrameInput,
    ImageSlot,
    batches,
    build_blocks,
    frame_caption,
    page_title,
)

JOB = "12345678-aaaa-bbbb-cccc-1234567890ab"


async def _load():
    return b"jpeg"


def frame(t, caption="Slide with the formula", reason="Key equation", section=0, key=None):
    return FrameInput(t, caption, reason, section, key or f"jobs/{JOB}/frame_{t}.jpg", _load)


SYN = {
    "title": "Neural nets",
    "tldr": "A short summary.",
    "sections": [
        {"heading": "Intro", "start_sec": 0, "bullets": ["a", "b"]},
        {"heading": "Math", "start_sec": 300, "bullets": ["c"]},
    ],
}


def build(frames):
    return build_blocks(job_id=JOB, video_id="aircAruvnKk", video_title="NN", duration_sec=1100,
                        synthesis=SYN, frames=frames)


def test_layout_callout_tldr_sections_and_footer():
    blocks = build([])
    types = [b["type"] for b in blocks]
    assert types[0] == "callout"
    assert JOB in "".join(t["text"]["content"] for t in blocks[0]["callout"]["rich_text"])
    assert types[1:3] == ["heading_2", "paragraph"]
    assert types.count("bulleted_list_item") == 3
    assert types[-2:] == ["divider", "paragraph"]
    heading = blocks[3]["heading_2"]["rich_text"]
    assert heading[0]["text"]["link"]["url"].endswith("&t=0s")


def test_frames_are_placed_under_their_section_in_time_order():
    blocks = build([frame(461, section=1), frame(320, section=1), frame(30, section=0)])
    slots = [b for b in blocks if isinstance(b, ImageSlot)]
    assert [s.frame.timestamp_sec for s in slots] == [30, 320, 461]
    math_idx = next(i for i, b in enumerate(blocks) if not isinstance(b, ImageSlot)
                    and b["type"] == "heading_2" and "Math" in str(b))
    assert blocks.index(slots[1]) > math_idx


def test_invalid_section_index_falls_back_to_timestamp():
    blocks = build([frame(400, section=99)])
    slot = next(b for b in blocks if isinstance(b, ImageSlot))
    math_idx = next(i for i, b in enumerate(blocks) if not isinstance(b, ImageSlot) and "Math" in str(b))
    assert blocks.index(slot) > math_idx


def test_caption_format():
    assert frame_caption(frame(461)) == "[7:41] Slide with the formula — Key equation"


def test_constraint_b_rejects_frames_without_caption():
    with pytest.raises(ConstraintViolation):
        build([frame(10, caption="  ")])


def test_constraint_b_rejects_images_not_from_this_jobs_frames():
    with pytest.raises(ConstraintViolation):
        build([frame(10, key="jobs/other-job/frame_10.jpg")])
    with pytest.raises(ConstraintViolation):
        build([frame(10, key="https://example.com/generated.png")])


def test_image_slot_requires_upload():
    s = ImageSlot(frame(5), "cap")
    with pytest.raises(ValueError):
        s.to_block()
    s.upload_id = "up_1"
    b = s.to_block()
    assert b["image"]["type"] == "file_upload" and b["image"]["file_upload"]["id"] == "up_1"
    assert b["image"]["caption"][0]["text"]["content"] == "cap"


def test_batches_of_at_most_100():
    parts = batches(list(range(250)))
    assert [len(p) for p in parts] == [100, 100, 50]


def test_long_text_is_clipped_and_title_has_marker():
    syn = {**SYN, "tldr": "x" * 5000}
    blocks = build_blocks(job_id=JOB, video_id="v", video_title=None, duration_sec=None, synthesis=syn, frames=[])
    assert len(blocks[2]["paragraph"]["rich_text"][0]["text"]["content"]) <= 2000
    assert page_title("T", JOB).endswith("· #12345678")
