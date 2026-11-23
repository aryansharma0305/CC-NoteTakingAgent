from app.llm.chunking import chunk_segments, estimate_tokens, merge_partials, segment_line
from app.llm.schema import Section, Synthesis, VisualMoment


def segs(n, step=30, words=40):
    return [{"start": i * step, "end": i * step + step, "text": f"segment {i} " + "word " * words} for i in range(n)]


def test_short_transcript_is_one_chunk():
    s = segs(10)
    assert chunk_segments(s, 6000) == [s]


def test_long_transcript_is_chunked_in_order_with_overlap_and_full_coverage():
    s = segs(40)  # 20 minutes
    per = estimate_tokens(segment_line(s[0]))
    chunks = chunk_segments(s, per * 10)
    assert len(chunks) > 1
    # Every chunk within budget, in order, and the union covers every segment.
    for c in chunks:
        assert sum(estimate_tokens(segment_line(x)) for x in c) <= per * 10
    starts = [c[0]["start"] for c in chunks]
    assert starts == sorted(starts)
    covered = {x["start"] for c in chunks for x in c}
    assert covered == {x["start"] for x in s}
    # Consecutive chunks overlap (~1 minute) but always advance.
    for a, b in zip(chunks, chunks[1:], strict=False):
        assert b[0]["start"] <= a[-1]["start"]
        assert b[0]["start"] > a[0]["start"]
        assert a[-1]["end"] - b[0]["start"] <= 90


def test_chunking_never_loops_on_a_single_huge_segment():
    s = [{"start": 0, "end": 30, "text": "x" * 100000}, {"start": 30, "end": 60, "text": "y"}]
    chunks = chunk_segments(s, 100)
    assert [len(c) for c in chunks] == [1, 1]


def vm(t, si=0, cap="c", reason="r"):
    return VisualMoment(timestamp_sec=t, caption=cap, reason=reason, section_index=si)


def test_merge_keeps_section_order_and_collapses_overlap_duplicate():
    p1 = Synthesis(title="T", tldr="First part. More.", sections=[
        Section(heading="Intro", start_sec=0, bullets=["a"]),
        Section(heading="Gradient descent", start_sec=300, bullets=["b"])],
        visual_moments=[vm(310, 1)])
    p2 = Synthesis(title="T2", tldr="Second part.", sections=[
        Section(heading="Gradient Descent!", start_sec=330, bullets=["b", "c"]),
        Section(heading="Backprop", start_sec=600, bullets=["d"])],
        visual_moments=[vm(615, 1), vm(312, 0)])
    m = merge_partials([p1, p2], duration_sec=1000, max_frames=8)
    assert [s.heading for s in m.sections] == ["Intro", "Gradient descent", "Backprop"]
    assert m.sections[1].bullets == ["b", "c"]
    # 312 deduped against 310 (within 10 s); 615's section index remapped from p2#1 -> merged #2
    assert [(v.timestamp_sec, v.section_index) for v in m.visual_moments] == [(310, 1), (615, 2)]
    assert m.title == "T" and m.tldr == "First part. Second part."


def test_merge_clamps_drops_uncaptioned_and_caps_frames():
    p = Synthesis(sections=[Section(heading="s", start_sec=0)], visual_moments=[
        vm(-5), vm(5000), vm(100, cap=" "), vm(200, reason="")] + [vm(300 + 20 * i) for i in range(20)])
    m = merge_partials([p], duration_sec=1000, max_frames=8)
    ts = [v.timestamp_sec for v in m.visual_moments]
    assert len(ts) == 8
    assert ts == sorted(ts)
    assert ts[0] == 0 and ts[-1] <= 999
    assert 100 not in ts and 200 not in ts


def test_merge_pads_to_min_frames_evenly_and_keeps_llm_frames():
    p = Synthesis(sections=[Section(heading="Blood", start_sec=0), Section(heading="Weather", start_sec=30)],
                  visual_moments=[vm(2, 1)])
    m = merge_partials([p], duration_sec=60, max_frames=5, min_frames=5)
    ts = [v.timestamp_sec for v in m.visual_moments]
    assert len(ts) == 5 and ts == sorted(ts) and 2 in ts
    assert all(b - a >= 6 for a, b in zip(ts, ts[1:], strict=False))
    assert all(0 <= t <= 59 for t in ts)
    late = [v for v in m.visual_moments if v.timestamp_sec >= 30 and v.timestamp_sec != 2]
    assert late and all(v.section_index == 1 and "Weather" in v.caption for v in late)


def test_merge_without_min_frames_does_not_pad():
    p = Synthesis(sections=[Section(heading="s", start_sec=0)], visual_moments=[vm(100)])
    m = merge_partials([p], duration_sec=1000, max_frames=5)
    assert [v.timestamp_sec for v in m.visual_moments] == [100]


def test_system_prompt_targets():
    from app.llm.prompts import system_prompt
    p = system_prompt(5, 1000, sections=3)
    assert "exactly 5 moments" in p and "about 1000 words" in p and "about 3 sections" in p
    assert "{frames}" not in p and "{language_rule}" not in p
    assert "same language as the transcript" in p
    assert "0 (return an empty list)" in system_prompt(0, 50) and "about 150 words" in system_prompt(0, 50)
    assert "Write all text in German" in system_prompt(3, 500, language="German")
