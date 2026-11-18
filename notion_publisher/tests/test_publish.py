"""publish() against a mocked Notion API (respx): create, adopt, resume, degrade."""

import json

import httpx
import pytest
import respx

from notion_publisher import FrameInput, NotionClient, PublishJob, publish
from vidnotes_common.errors import ErrorCode, FatalError, RateLimited

API = "https://api.notion.com/v1"
JOB = "12345678-aaaa-bbbb-cccc-1234567890ab"
PARENT = "parent-page"
SYN = {
    "title": "Notes",
    "tldr": "tl;dr",
    "sections": [{"heading": "S", "start_sec": 0, "bullets": [f"b{i}" for i in range(150)]}],
}


class Store:
    def __init__(self):
        self.calls = []
        self.page = None
        self.cursor = 0

    async def mark_creating(self):
        self.calls.append("creating")

    async def mark_created(self, page_id, page_url, cursor):
        self.calls.append("created")
        self.page, self.cursor = page_id, cursor

    async def set_cursor(self, cursor):
        self.cursor = cursor


async def _jpeg():
    return b"\xff\xd8jpeg"


def frames(*ts):
    return [FrameInput(t, f"cap {t}", "why", 0, f"jobs/{JOB}/frame_{t}.jpg", _jpeg) for t in ts]


def job(**kw):
    return PublishJob(job_id=JOB, video_id="vid12345678", youtube_url="u", video_title="T", duration_sec=600, **kw)


def mock_common(router, upload_status=200):
    router.get(f"{API}/pages/{PARENT}").respond(200, json={"id": PARENT, "object": "page"})
    n = {"i": 0}

    def create_upload(request):
        n["i"] += 1
        return httpx.Response(200, json={"id": f"up{n['i']}", "status": "pending"})

    router.post(f"{API}/file_uploads").mock(side_effect=create_upload)
    router.post(url__regex=rf"{API}/file_uploads/.+/send").respond(
        upload_status, json={"status": "uploaded"} if upload_status == 200 else
        {"object": "error", "code": "validation_error", "message": "too big"})


@pytest.mark.asyncio
async def test_create_page_then_append_in_batches():
    with respx.mock(assert_all_called=False) as r:
        mock_common(r)
        create = r.post(f"{API}/pages").respond(200, json={"id": "newpage", "url": "https://notion.so/newpage"})
        append = r.patch(f"{API}/blocks/newpage/children").respond(200, json={})
        store = Store()
        async with NotionClient("tok") as c:
            res = await publish(job(), SYN, frames(10, 20), "tok", PARENT, store, client=c)

    body = json.loads(create.calls[0].request.content)
    assert len(body["children"]) == 100
    assert body["parent"] == {"page_id": PARENT}
    assert body["properties"]["title"]["title"][0]["text"]["content"].endswith("#12345678")
    total = 100 + sum(len(json.loads(c.request.content)["children"]) for c in append.calls)
    # callout + tldr(2) + heading + 150 bullets + 2 images + divider + footer
    assert total == 1 + 2 + 1 + 150 + 2 + 2
    assert all(len(json.loads(c.request.content)["children"]) <= 100 for c in append.calls)
    assert store.calls == ["creating", "created"] and store.cursor == total
    assert (res.page_url, res.frames_requested, res.frames_published, res.warnings) == (
        "https://notion.so/newpage", 2, 2, [])


@pytest.mark.asyncio
async def test_adopts_page_created_before_a_crash_instead_of_duplicating():
    with respx.mock(assert_all_called=False) as r:
        mock_common(r)
        r.get(url__regex=rf"{API}/blocks/{PARENT}/children(\?.*)?$").respond(200, json={
            "results": [{"id": "other", "type": "child_page", "child_page": {"title": "x · #99999999"}},
                        {"id": "mine", "type": "child_page", "child_page": {"title": "Notes · #12345678"}}],
            "has_more": False})
        r.get(f"{API}/pages/mine").respond(200, json={"id": "mine", "url": "https://notion.so/mine"})
        # The adopted page already received its first 100 children at creation.
        r.get(url__regex=rf"{API}/blocks/mine/children(\?.*)?$").respond(200, json={
            "results": [{"id": f"b{i}", "type": "paragraph"} for i in range(100)], "has_more": False})
        create = r.post(f"{API}/pages").respond(500)
        append = r.patch(f"{API}/blocks/mine/children").respond(200, json={})
        store = Store()
        async with NotionClient("tok") as c:
            res = await publish(job(publish_state="creating"), SYN, frames(10), "tok", PARENT, store, client=c)
    assert not create.called
    assert res.page_id == "mine"
    appended = sum(len(json.loads(c.request.content)["children"]) for c in append.calls)
    assert 100 + appended == 1 + 2 + 1 + 150 + 1 + 2


@pytest.mark.asyncio
async def test_resume_uses_actual_child_count_not_stale_cursor():
    with respx.mock(assert_all_called=False) as r:
        mock_common(r)
        r.get(url__regex=rf"{API}/blocks/pg/children(\?.*)?$").respond(200, json={
            "results": [{"id": f"b{i}"} for i in range(156)], "has_more": False})
        append = r.patch(f"{API}/blocks/pg/children").respond(200, json={})
        store = Store()
        async with NotionClient("tok") as c:
            await publish(job(notion_page_id="pg", notion_page_url="u", publish_state="created", publish_cursor=100),
                          SYN, frames(10), "tok", PARENT, store, client=c)
    # 157 blocks total; 156 already there -> exactly one more block appended
    assert sum(len(json.loads(c.request.content)["children"]) for c in append.calls) == 1


@pytest.mark.asyncio
async def test_rejected_upload_is_omitted_with_warning_never_substituted():
    with respx.mock(assert_all_called=False) as r:
        mock_common(r, upload_status=400)
        create = r.post(f"{API}/pages").respond(200, json={"id": "p", "url": "https://notion.so/p"})
        r.patch(f"{API}/blocks/p/children").respond(200, json={})
        async with NotionClient("tok") as c:
            res = await publish(job(), {**SYN, "sections": [{"heading": "S", "start_sec": 0, "bullets": ["x"]}]},
                                frames(10, 20), "tok", PARENT, Store(), client=c)
    kinds = [b["type"] for b in json.loads(create.calls[0].request.content)["children"]]
    assert "image" not in kinds
    assert res.frames_published == 0 and res.frames_requested == 2
    assert [w["code"] for w in res.warnings] == [ErrorCode.FRAME_UPLOAD_FAILED] * 2


@pytest.mark.asyncio
async def test_notion_429_becomes_rate_limited_with_retry_after():
    with respx.mock() as r:
        r.get(f"{API}/pages/{PARENT}").respond(429, headers={"Retry-After": "17"},
                                                 json={"code": "rate_limited", "message": "slow down"})
        async with NotionClient("tok") as c:
            with pytest.raises(RateLimited) as e:
                await publish(job(), SYN, [], "tok", PARENT, Store(), client=c)
    assert e.value.retry_after == 17


@pytest.mark.asyncio
async def test_notion_401_is_fatal_reconnect():
    with respx.mock() as r:
        r.get(f"{API}/pages/{PARENT}").respond(401, json={"code": "unauthorized", "message": "bad token"})
        async with NotionClient("tok") as c:
            with pytest.raises(FatalError) as e:
                await publish(job(), SYN, [], "tok", PARENT, Store(), client=c)
    assert e.value.code == ErrorCode.NOTION_UNAUTHORIZED


@pytest.mark.asyncio
async def test_no_parent_uses_most_recent_shared_page():
    with respx.mock(assert_all_called=False) as r:
        mock_common(r)
        r.post(f"{API}/search").respond(200, json={"results": [{"object": "page", "id": "recent"}]})
        create = r.post(f"{API}/pages").respond(200, json={"id": "p", "url": "https://notion.so/p"})
        async with NotionClient("tok") as c:
            await publish(job(), {"title": "t", "tldr": "", "sections": []}, [], "tok", None, Store(), client=c)
    assert json.loads(create.calls[0].request.content)["parent"] == {"page_id": "recent"}
