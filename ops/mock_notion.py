#!/usr/bin/env python3
"""Local mock of the Notion API (stdlib only) for offline end-to-end runs.

    python3 ops/mock_notion.py            # listens on 0.0.0.0:8765
    # .env:  NOTION_API_URL=http://host.docker.internal:8765/v1
    # then submit jobs with any notion_token (use "bad" to simulate a revoked token)
    open http://localhost:8765/preview     # rendered pages, with the captured frames

It implements just what notion_publisher uses (search, pages, block children,
file uploads) and enforces the request limits real Notion rejects with a 400:
rich_text items <= 2000 chars, <= 100 items per rich_text array, <= 100 children
per array, nesting depth <= 2 per request, <= 1000 blocks per request, image
blocks must reference an uploaded file. State is in memory.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT_ID = "00000000-0000-4000-8000-000000000001"
LOCK = threading.Lock()
PAGES: dict[str, dict] = {ROOT_ID: {"id": ROOT_ID, "title": "Video Notes (mock root)", "parent": None}}
CHILDREN: dict[str, list[dict]] = {ROOT_ID: []}  # block id -> child blocks (top level of that block)
UPLOADS: dict[str, dict] = {}
STATS = {"requests": 0, "pages_created": 0, "rejected": 0}


class Invalid(Exception):
    pass


def _check_rich(items, where: str) -> None:
    if not isinstance(items, list):
        raise Invalid(f"{where} should be an array")
    if len(items) > 100:
        raise Invalid(f"{where}.length should be <= 100, instead was {len(items)}")
    for i, it in enumerate(items):
        content = (it.get("text") or {}).get("content", "")
        if len(content) > 2000:
            raise Invalid(f"{where}[{i}].text.content.length should be <= 2000, instead was {len(content)}")


def _validate(blocks: list[dict], depth: int, counter: list[int], where: str) -> None:
    if len(blocks) > 100:
        raise Invalid(f"{where}.length should be <= 100, instead was {len(blocks)}")
    for i, b in enumerate(blocks):
        counter[0] += 1
        t = b.get("type")
        body = b.get(t)
        if not t or not isinstance(body, dict):
            raise Invalid(f"{where}[{i}] has no body for type {t!r}")
        if "rich_text" in body:
            _check_rich(body["rich_text"], f"{where}[{i}].{t}.rich_text")
        if t == "image":
            _check_rich(body.get("caption", []), f"{where}[{i}].image.caption")
            fid = (body.get("file_upload") or {}).get("id")
            if body.get("type") != "file_upload" or fid not in UPLOADS:
                raise Invalid(f"{where}[{i}].image must reference an uploaded file")
            if UPLOADS[fid].get("attached"):
                raise Invalid(f"file upload {fid} was already attached")
        if t == "table_row":
            for c, cell in enumerate(body.get("cells", [])):
                _check_rich(cell, f"{where}[{i}].table_row.cells[{c}]")
        kids = body.get("children")
        if kids:
            if depth >= 2:
                raise Invalid(f"{where}[{i}] nests children deeper than 2 levels in one request")
            if t == "table":
                width = body.get("table_width")
                bad = [r for r in kids if len(r.get("table_row", {}).get("cells", [])) != width]
                if bad:
                    raise Invalid(f"{where}[{i}].table rows must have table_width={width} cells")
            _validate(kids, depth + 1, counter, f"{where}[{i}].{t}.children")
    if counter[0] > 1000:
        raise Invalid("a request may contain at most 1000 blocks")


def _store(parent_id: str, blocks: list[dict]) -> None:
    for b in blocks:
        b = json.loads(json.dumps(b))
        b["id"] = str(uuid.uuid4())
        t = b["type"]
        kids = b[t].pop("children", None) if isinstance(b.get(t), dict) else None
        b["has_children"] = bool(kids)
        if t == "image":
            UPLOADS[b["image"]["file_upload"]["id"]]["attached"] = True
        for it in b.get(t, {}).get("rich_text", []) + b.get(t, {}).get("caption", []):
            it["plain_text"] = it.get("text", {}).get("content", "")
        CHILDREN.setdefault(parent_id, []).append(b)
        CHILDREN.setdefault(b["id"], [])
        if kids:
            _store(b["id"], kids)


# ------------------------------------------------------------------ render --


def _rt(items) -> str:
    out = []
    for it in items or []:
        s = html.escape((it.get("text") or {}).get("content", "")).replace("\n", "<br>")
        a = it.get("annotations") or {}
        if a.get("code"):
            s = f"<code>{s}</code>"
        if a.get("bold"):
            s = f"<b>{s}</b>"
        if a.get("italic"):
            s = f"<i>{s}</i>"
        if a.get("color") and a["color"] != "default":
            s = f'<span class="c-{a["color"]}">{s}</span>'
        link = ((it.get("text") or {}).get("link") or {}).get("url")
        if link:
            s = f'<a href="{html.escape(link)}" target="_blank">{s}</a>'
        out.append(s)
    return "".join(out)


def _render(block_id: str) -> str:
    out: list[str] = []
    open_list = None
    for b in CHILDREN.get(block_id, []):
        t = b["type"]
        body = b[t]
        list_kind = {"bulleted_list_item": "ul", "numbered_list_item": "ol"}.get(t)
        if open_list and list_kind != open_list:
            out.append(f"</{open_list}>")
            open_list = None
        if list_kind and not open_list:
            out.append(f"<{list_kind}>")
            open_list = list_kind
        kids = _render(b["id"]) if b.get("has_children") else ""
        text = _rt(body.get("rich_text"))
        color = body.get("color", "default")
        if list_kind:
            out.append(f"<li>{text}{kids}</li>")
        elif t.startswith("heading_"):
            lvl = int(t[-1]) + 1
            if body.get("is_toggleable"):
                out.append(f'<details class="c-{color}"><summary><h{lvl}>{text}</h{lvl}></summary>{kids}</details>')
            else:
                out.append(f'<h{lvl} class="c-{color}">{text}</h{lvl}>')
        elif t == "paragraph":
            out.append(f'<p class="c-{color}">{text}</p>{kids}')
        elif t == "callout":
            icon = (body.get("icon") or {}).get("emoji", "")
            out.append(f'<div class="callout c-{color}"><span>{icon}</span><div>{text}{kids}</div></div>')
        elif t == "toggle":
            out.append(f"<details><summary>{text}</summary>{kids}</details>")
        elif t == "divider":
            out.append("<hr>")
        elif t == "table_of_contents":
            out.append('<div class="toc">[table of contents]</div>')
        elif t == "image":
            fid = body["file_upload"]["id"]
            cap = _rt(body.get("caption"))
            out.append(f'<figure><img src="/files/{fid}"><figcaption>{cap}</figcaption></figure>')
        elif t == "table":
            rows = CHILDREN.get(b["id"], [])
            trs = []
            for r, row in enumerate(rows):
                tag = "th" if r == 0 and body.get("has_column_header") else "td"
                trs.append("<tr>" + "".join(f"<{tag}>{_rt(c)}</{tag}>" for c in row["table_row"]["cells"]) + "</tr>")
            out.append(f"<table>{''.join(trs)}</table>")
        else:
            out.append(f"<p><i>[{html.escape(t)}]</i> {text}</p>")
    if open_list:
        out.append(f"</{open_list}>")
    return "".join(out)


CSS = """body{font:16px/1.6 system-ui,sans-serif;max-width:760px;margin:40px auto;padding:0 16px;color:#37352f}
a{color:#2eaadc}h2{margin-top:1.6em}.callout{display:flex;gap:12px;padding:14px 16px;border-radius:6px;margin:10px 0}
.c-gray_background{background:#f1f1ef}.c-blue_background{background:#e7f3f8}.c-gray{color:#787774}
figure{margin:16px 0}img{max-width:100%;border-radius:4px}figcaption{font-size:14px;color:#555;margin-top:4px}
table{border-collapse:collapse;width:100%}
td,th{border:1px solid #e9e9e7;padding:6px 10px;text-align:left;vertical-align:top}
th{background:#f7f6f3}details{margin:6px 0}summary{cursor:pointer}summary h4{display:inline}
.toc{color:#787774;font-size:13px}code{background:#f1f1ef;padding:1px 4px;border-radius:3px;color:#eb5757}"""


def _page_html(pid: str) -> str:
    p = PAGES[pid]
    return (f"<!doctype html><meta charset=utf-8><title>{html.escape(p['title'])}</title><style>{CSS}</style>"
            f"<p><a href='/preview'>← all pages</a></p><h1>{p.get('icon', '')} {html.escape(p['title'])}</h1>"
            + _render(pid))


# ----------------------------------------------------------------- handler --


class Handler(BaseHTTPRequestHandler):
    server_version = "MockNotion/1.0"

    def log_message(self, fmt, *args):  # quieter
        print("[mock-notion]", self.command, self.path.split("?")[0], args[1] if len(args) > 1 else "")

    def _send(self, code: int, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else (json.dumps(body) if ctype == "application/json" else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _err(self, code: int, ncode: str, msg: str):
        if code == 400:
            STATS["rejected"] += 1
        self._send(code, {"object": "error", "status": code, "code": ncode, "message": msg})

    def _auth(self) -> bool:
        tok = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        if not tok or tok == "bad":
            self._err(401, "unauthorized", "API token is invalid.")
            return False
        return True

    def _json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        path = u.path
        if path in ("/", "/preview"):
            items = "".join(f'<li><a href="/preview/{pid}">{html.escape(p["title"])}</a></li>'
                            for pid, p in PAGES.items() if pid != ROOT_ID)
            return self._send(200, f"<!doctype html><meta charset=utf-8><style>{CSS}</style><h1>Mock Notion</h1>"
                                   f"<p>{json.dumps(STATS)}</p><ul>{items or '<li>No pages yet</li>'}</ul>",
                              "text/html; charset=utf-8")
        if path.startswith("/preview/"):
            pid = path.rsplit("/", 1)[1]
            if pid not in PAGES:
                return self._send(404, "not found", "text/plain")
            return self._send(200, _page_html(pid), "text/html; charset=utf-8")
        if path.startswith("/files/"):
            up = UPLOADS.get(path.rsplit("/", 1)[1])
            return self._send(200, up["data"], "image/jpeg") if up and up.get("data") else self._send(404, b"")
        if path == "/stats":
            return self._send(200, {**STATS, "pages": [{"id": k, "title": v["title"]} for k, v in PAGES.items()]})
        if not self._auth():
            return
        m = re.match(r"^/v1/pages/([\w-]+)$", path)
        if m:
            pid = m.group(1)
            pid = next((k for k in PAGES if k.replace("-", "") == pid.replace("-", "")), None)
            if not pid:
                return self._err(404, "object_not_found", "Could not find page.")
            return self._send(200, {"object": "page", "id": pid, "url": f"http://localhost:8765/preview/{pid}",
                                    "archived": False, "in_trash": False})
        if re.match(r"^/v1/databases/", path):
            return self._err(404, "object_not_found", "Could not find database.")
        m = re.match(r"^/v1/blocks/([\w-]+)/children$", path)
        if m:
            bid = next((k for k in CHILDREN if k.replace("-", "") == m.group(1).replace("-", "")), None)
            if bid is None:
                return self._err(404, "object_not_found", "Could not find block.")
            q = parse_qs(u.query)
            start = int((q.get("start_cursor") or ["0"])[0] or 0)
            size = min(100, int((q.get("page_size") or ["100"])[0]))
            kids = CHILDREN[bid]
            page = kids[start : start + size]
            more = start + size < len(kids)
            return self._send(200, {"object": "list", "results": page, "has_more": more,
                                    "next_cursor": str(start + size) if more else None})
        self._err(404, "invalid_request_url", f"Unknown endpoint {path}")

    def do_POST(self):  # noqa: N802
        STATS["requests"] += 1
        path = urlparse(self.path).path
        if not self._auth():
            return
        if path == "/v1/search":
            pages = [{"object": "page", "id": pid, "last_edited_time": "2026-01-01T00:00:00Z"}
                     for pid in PAGES if PAGES[pid]["parent"] is None]
            return self._send(200, {"object": "list", "results": pages, "has_more": False})
        if path == "/v1/file_uploads":
            body = self._json()
            fid = str(uuid.uuid4())
            UPLOADS[fid] = {"filename": body.get("filename"), "status": "pending"}
            return self._send(200, {"object": "file_upload", "id": fid, "status": "pending"})
        m = re.match(r"^/v1/file_uploads/([\w-]+)/send$", path)
        if m:
            fid = m.group(1)
            if fid not in UPLOADS:
                return self._err(404, "object_not_found", "Unknown upload")
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n)
            # Pull the JPEG out of the multipart body (good enough for a mock).
            s, e = raw.find(b"\xff\xd8"), raw.rfind(b"\xff\xd9")
            if s < 0 or e < 0:
                return self._err(400, "validation_error", "file is not a JPEG")
            UPLOADS[fid].update(data=raw[s : e + 2], status="uploaded")
            return self._send(200, {"object": "file_upload", "id": fid, "status": "uploaded"})
        if path == "/v1/pages":
            body = self._json()
            parent = body.get("parent") or {}
            pid_parent = parent.get("page_id")
            pid_parent = next((k for k in PAGES if k.replace("-", "") == str(pid_parent).replace("-", "")), None)
            if not pid_parent:
                return self._err(404, "object_not_found", "Could not find parent page.")
            props = body.get("properties") or {}
            title = "".join(t["text"]["content"] for p in props.values() for t in p.get("title", []))
            kids = body.get("children") or []
            try:
                with LOCK:
                    _validate(kids, 1, [0], "body.children")
            except Invalid as ex:
                return self._err(400, "validation_error", str(ex))
            with LOCK:
                pid = str(uuid.uuid4())
                PAGES[pid] = {"id": pid, "title": title, "parent": pid_parent,
                              "icon": (body.get("icon") or {}).get("emoji", "")}
                CHILDREN[pid] = []
                CHILDREN[pid_parent].append({"object": "block", "id": pid, "type": "child_page",
                                             "child_page": {"title": title}, "has_children": True})
                _store(pid, kids)
                STATS["pages_created"] += 1
            return self._send(200, {"object": "page", "id": pid, "url": f"http://localhost:8765/preview/{pid}"})
        self._err(404, "invalid_request_url", f"Unknown endpoint {path}")

    def do_PATCH(self):  # noqa: N802
        STATS["requests"] += 1
        path = urlparse(self.path).path
        if not self._auth():
            return
        m = re.match(r"^/v1/blocks/([\w-]+)/children$", path)
        if not m:
            return self._err(404, "invalid_request_url", f"Unknown endpoint {path}")
        bid = next((k for k in CHILDREN if k.replace("-", "") == m.group(1).replace("-", "")), None)
        if bid is None:
            return self._err(404, "object_not_found", "Could not find block.")
        kids = self._json().get("children") or []
        try:
            with LOCK:
                _validate(kids, 1, [0], "body.children")
                _store(bid, kids)
        except Invalid as ex:
            return self._err(400, "validation_error", str(ex))
        return self._send(200, {"object": "list", "results": []})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    print(f"Mock Notion on http://{a.host}:{a.port}/v1  ·  previews at http://localhost:{a.port}/preview")
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
