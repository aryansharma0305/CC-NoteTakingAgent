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
    assert types[1] == "callout" and blocks[1]["callout"]["icon"]["emoji"] == "💡"  # summary
    assert "A short summary." in "".join(t["text"]["content"] for t in blocks[1]["callout"]["rich_text"])
    assert types.count("bulleted_list_item") == 3
    assert types[-2:] == ["divider", "paragraph"]
    heading = next(b for b in blocks if b["type"] == "heading_2")["heading_2"]["rich_text"]
    assert heading[0]["text"]["link"]["url"].endswith("&t=0s")
    assert "Intro" in heading[-1]["text"]["content"]


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
    summary = blocks[1]["callout"]["rich_text"]
    assert all(len(t["text"]["content"]) <= 2000 for t in summary)
    assert sum(len(t["text"]["content"]) for t in summary) >= 5000  # split, not truncated
    assert page_title("T", JOB).endswith("· #12345678")


# ---- richer formatting ------------------------------------------------------------------

from notion_publisher.blocks import plain, rich, transcript_paragraphs  # noqa: E402


def _ann(item):
    return {k for k, v in (item.get("annotations") or {}).items() if v is True}


def test_inline_markdown_becomes_notion_annotations():
    items = rich("**Gradient descent**: step *downhill* using `lr=0.1`, not snake_case or 2*3*4.")
    assert plain(items) == "Gradient descent: step downhill using lr=0.1, not snake_case or 2*3*4."
    by_text = {i["text"]["content"]: _ann(i) for i in items}
    assert by_text["Gradient descent"] == {"bold"}
    assert by_text["downhill"] == {"italic"}
    assert by_text["lr=0.1"] == {"code"}


def test_rich_splits_long_text_under_notion_limits():
    items = rich("y" * 4500)
    assert [len(i["text"]["content"]) for i in items] == [1900, 1900, 700]


SYN_FULL = {
    **SYN,
    "key_takeaways": ["First **big** idea", "Second idea"],
    "glossary": [{"term": "Neuron", "definition": "Holds a number."}, {"term": "", "definition": "dropped"}],
    "review_questions": [{"question": "Why sigmoid?", "answer": "To squash values."}],
}
SEGS = [{"start": 10 * i, "end": 10 * i + 10, "text": f"words {i}"} for i in range(30)]  # 5 minutes


def test_study_aids_transcript_and_toc_layout():
    blocks = build_blocks(job_id=JOB, video_id="vid", video_title="NN", duration_sec=300, synthesis=SYN_FULL,
                          frames=[frame(30)], transcript=SEGS, model="qwen2.5:3b", generated_on="1 Jan 2026")
    plain_blocks = [b for b in blocks if not isinstance(b, ImageSlot)]
    types = [b["type"] for b in plain_blocks]
    assert types.count("numbered_list_item") == 2
    header = plain(blocks[0]["callout"]["rich_text"])
    assert "qwen2.5:3b" in header and "1 Jan 2026" in header and "1 key frame" in header

    table = next(b for b in plain_blocks if b["type"] == "table")["table"]
    assert table["table_width"] == 2 and table["has_column_header"]
    assert len(table["children"]) == 2  # header + 1 valid term (empty term dropped)

    toggle = next(b for b in plain_blocks if b["type"] == "toggle")["toggle"]
    assert "Why sigmoid?" in plain(toggle["rich_text"])
    assert plain(toggle["children"][0]["paragraph"]["rich_text"]) == "To squash values."

    tr = next(b for b in plain_blocks if b["type"] == "heading_3")["heading_3"]
    assert tr["is_toggleable"] and len(tr["children"]) == 5  # one paragraph per minute
    first = tr["children"][0]["paragraph"]["rich_text"]
    assert first[0]["text"]["link"]["url"].endswith("&t=0s") and "words 0" in plain(first)


def test_short_page_has_no_toc_and_no_empty_extras():
    blocks = build_blocks(job_id=JOB, video_id="v", video_title="NN", duration_sec=60, synthesis=SYN, frames=[])
    types = [b["type"] for b in blocks]
    assert "table_of_contents" not in types and "table" not in types and "toggle" not in types


def test_frame_caption_rich_keeps_constraint_b_prefix_and_links_the_moment():
    slot = next(b for b in build([frame(461, caption="Weights grid", reason="Shows **signs**")])
                if isinstance(b, ImageSlot))
    slot.upload_id = "up1"
    cap = slot.to_block()["image"]["caption"]
    assert plain(cap).startswith("[7:41] Weights grid — Shows signs")
    assert cap[0]["text"]["link"]["url"].endswith("&t=461s")


def test_transcript_paragraphs_stay_under_100_children():
    segs = [{"start": 5 * i, "end": 5 * i + 5, "text": "x"} for i in range(3000)]  # ~4 h
    assert len(transcript_paragraphs(segs, "v")) <= 100
