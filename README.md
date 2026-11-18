# Video Note-Taking Agent

Click **Take Notes** on a YouTube video and get a Notion page with structured study notes and
**real frames** captured from the video, each with an LLM-written caption. Driven entirely from a
Chrome extension; the backend is a queue-backed, rate-limited, crash-tolerant worker pool.

```
Chrome extension ──► API gateway (FastAPI) ──► RabbitMQ ──► worker × N ──► YouTube / LLM / Notion
                         │                       ▲  │            │
                         └──► Postgres (jobs) ◄──┘  └─ retry/DLQ │──► Redis (global rate limits)
                                                                 └──► S3 store (frame JPEGs)
```

| | |
|---|---|
| **Base URL** | `http://localhost:8000` |
| **Health check** | `GET http://localhost:8000/healthz` → gateway, Postgres, RabbitMQ, Redis, S3 frame store |
| **API docs** | `http://localhost:8000/docs` (OpenAPI) |
| **RabbitMQ UI** | `http://localhost:15672` (user `vn`, password `vn`) |
| **S3 frame store** | `http://localhost:8333` (SeaweedFS; keys `minioadmin` / `minioadmin`, set in `infra/s3/s3.json`) |

---

## 1. Run it

Requirements: Docker with Compose v2, Chrome. Run it on a laptop/home network: YouTube blocks most
cloud-provider IPs for transcripts and streams (or set `YT_PROXY_URL`).

```bash
cp .env.example .env          # then fill in LLM_API_KEY and the Notion values (section 3)
docker compose up --build -d  # rabbitmq, postgres, redis, s3, gateway, 2 workers
curl http://localhost:8000/healthz
```

Expected:

```json
{"gateway":"ok","postgres":"ok","rabbitmq":"ok","redis":"ok","s3":"ok"}
```

Any dependency that is down is named and the endpoint returns **503**. The gateway applies the
database migrations on start; workers wait for the gateway to be healthy.

Scale or crash workers without touching anything else:

```bash
docker compose up -d --scale worker=4
docker compose ps worker
docker kill -s KILL <worker-container>     # its in-flight stage is redelivered to another worker
```

## 2. Load the extension (no build step)

1. Open `chrome://extensions`, turn on **Developer mode**.
2. **Load unpacked** → select the `extension/` folder.
3. The extension ID is fixed by the `key` in `manifest.json`: **`odkinkapbnndcekhonhigbeaoclbecfj`**,
   so the OAuth redirect URI is always `https://odkinkapbnndcekhonhigbeaoclbecfj.chromiumapp.org/`.
4. The settings page opens on install. The server address defaults to `http://localhost:8000`.
5. Open any YouTube video. A **Take Notes** button appears in the player toolbar (bottom-right
   controls). Click it, then click the extension icon to watch progress.

## 3. Connect Notion

Either option works. The extension stores the token in `chrome.storage.local` and sends it with each
job; the gateway never stores it (it travels Fernet-encrypted inside queue messages).

**Option A: public integration (OAuth), what end users see**

1. <https://www.notion.so/profile/integrations> → **New integration** → type **Public**.
2. Redirect URI: `https://odkinkapbnndcekhonhigbeaoclbecfj.chromiumapp.org/`
3. Capabilities: read content, update content, insert content.
4. Optional but recommended: set a **Notion URL for optional template** pointing at a public page
   called "Video Notes". Notion duplicates it into the user's workspace during OAuth and that page
   becomes the default destination.
5. Put the OAuth client id and secret in `.env` as `NOTION_CLIENT_ID` / `NOTION_CLIENT_SECRET`, then
   `docker compose up -d gateway`.
6. In the extension popup, click **Connect Notion** once.

**Option B: internal integration token (quickest for grading)**

1. <https://www.notion.so/profile/integrations> → **New integration** → **Internal** → copy the secret.
2. In Notion, open the page that should receive notes → `•••` → **Connections** → add your integration.
3. Extension settings → *Use an internal integration token instead* → paste → **Save token**.

### Default Notion destination

Each job publishes under the first of these that exists:

1. The page id set in the extension settings (**Destination page**), sent as `notion_parent_id`.
2. The template page Notion duplicated during OAuth (`duplicated_template_id`), sent as `notion_parent_id`.
3. `DEFAULT_NOTION_PARENT_ID` from the server's `.env`.
4. The **most recently edited page shared with the integration** (Notion search).

If none exist the job fails with "No Notion page is shared with the integration". A database id also
works as a parent; the page title goes in the database's title property.

## 4. Configuration

Every variable is read by `shared/vidnotes_common/config.py`. Starred rows are the spec's required
minimum. In Docker, the four connection strings are set by `docker-compose.yml` to the container
hostnames; the `.env` values are for running services on the host.

| Variable | Default / example | Used by | Purpose |
|---|---|---|---|
| `LLM_PROVIDER` * | `anthropic` | worker | `anthropic`, `openai` (any OpenAI-compatible API), `gemini`, `fake` (offline test double) |
| `LLM_API_KEY` * | | worker | Provider API key |
| `LLM_MODEL` * | `claude-haiku-5-5` | worker | Model name for the provider |
| `LLM_BASE_URL` | | worker | Override endpoint for OpenAI-compatible providers |
| `LLM_RPM` * | `10` | worker | **Configured rate limit**: LLM requests/min across *all* workers (set ~80% of your real limit) |
| `LLM_TPM` | `200000` | worker | LLM tokens/min across all workers (input estimate + max output) |
| `LLM_MAX_OUTPUT_TOKENS` | `16000` | worker | Per-call output cap |
| `NOTION_CLIENT_ID` * / `NOTION_CLIENT_SECRET` * | | gateway | Public integration OAuth credentials |
| `NOTION_REDIRECT_URI` | `https://odkinkapbnndcekhonhigbeaoclbecfj.chromiumapp.org/` | gateway | Must match the integration settings |
| `NOTION_VERSION` | `2022-06-28` | worker | Pinned Notion API version |
| `NOTION_RPS` | `2.5` | worker | Notion calls/sec per token, across all workers |
| `DEFAULT_NOTION_PARENT_ID` | blank | worker | Fallback destination (see above) |
| `RABBITMQ_URL` * | `amqp://vn:vn@rabbitmq:5672/` | gateway, worker | **Broker connection string** |
| `DATABASE_URL` | `postgresql+asyncpg://vn:vn@postgres:5432/vn` | gateway, worker | Job state store |
| `REDIS_URL` | `redis://redis:6379/0` | gateway, worker | Shared rate-limit buckets |
| `S3_ENDPOINT` / `S3_ACCESS_KEY` / `S3_SECRET_KEY` / `S3_BUCKET` | `http://s3:8333`, `minioadmin`, `minioadmin`, `vidnotes` | gateway, worker | Frame storage |
| `MAX_ATTEMPTS` * | `5` | worker | **Retry budget** per stage before the DLQ |
| `RETRY_DELAYS` * | `10,30,90,180` (demo: `5,10,20,40`) | worker, gateway | **Backoff schedule** in seconds; one TTL queue per value |
| `RL_MAX_WAIT_SEC` | `60` | worker | Longest a worker waits for a rate-limit token before parking the job in a retry queue |
| `YOUTUBE_RPS` | `2` | worker | YouTube calls/sec across all workers |
| `YOUTUBE_API_KEY` | blank | gateway | Optional: YouTube Data API for the submit-time duration check (otherwise the watch page is read) |
| `METADATA_TIMEOUT_SEC` | `3` | gateway | Budget for the submit-time check; on timeout the worker enforces the limit |
| `MAX_DURATION_SEC` | `1200` | gateway, worker | Constraint A: 20 minutes |
| `MAX_FRAMES` / `CHUNK_TOKENS` | `8` / `6000` | worker | Frames per page / transcript tokens per LLM call |
| `FRAME_PARALLELISM` / `FRAME_BEST_OF` / `FRAME_TIMEOUT_SEC` | `3` / `3` / `30` | worker | Parallel ffmpeg grabs / keep the sharpest of N frames 1 s apart / per-frame timeout |
| `YT_PROXY_URL` | blank | worker | Optional residential proxy for YouTube |
| `TOKEN_ENC_KEY` * | dev key in `.env.example` | gateway, worker | Fernet key that encrypts the Notion token inside queue messages. Generate your own |
| `ADMIN_TOKEN` | `change-me-admin-token` | gateway | `X-Admin-Token` for `/admin/*` |
| `GATEWAY_BASE_URL` | `http://localhost:8000` | extension (settings) | Where the extension sends jobs |
| `FAULT_INJECT_429` | blank | worker | Demo only, e.g. `llm:0.3,notion:0.2,youtube:0.1` |
| `FAULT_INJECT_FRAME_FAIL` | blank | worker | Demo only, probability 0–1 that a frame capture fails |

## 5. REST API

| Method + path | Success | Errors |
|---|---|---|
| `POST /jobs` `{youtube_url, notion_token, notion_parent_id?}` | **202**, `Location: /jobs/{id}`, `{job_id, status:"QUEUED", video_id}` | 400 `INVALID_URL` / `INVALID_REQUEST`, 401 `MISSING_NOTION_TOKEN`, 422 `VIDEO_TOO_LONG` / `LIVE_STREAM`, 503 `BROKER_UNAVAILABLE` |
| `GET /jobs/{id}` | 200 `{job_id, status, progress, video_title, duration_sec, warnings, error, result, waiting_until, retry_reason, attempt, max_attempts, …}` | 404 `JOB_NOT_FOUND` |
| `GET /jobs/{id}/results` | 200 `{status, result:{notion_page_url, frames_requested, frames_published}, warnings}` | 409 `JOB_NOT_FINISHED` before a terminal state, 404 |
| `GET /healthz` | 200 | 503 naming the failing dependency |
| `GET /oauth/notion/config`, `POST /oauth/notion/exchange` | OAuth support for the extension | |
| `GET /admin/jobs`, `GET /admin/jobs/{id}` (with event timeline), `GET /admin/jobs/{id}/artifacts/{stage}`, `GET /admin/dlq`, `POST /admin/dlq/{id}/requeue` | demo/ops, header `X-Admin-Token` | 401 |

All errors use `{"error": {"code", "message"}}`. `POST /jobs` only validates, inserts a row and
publishes a message: no pipeline work happens inside the request. `GET /jobs/{id}` is a pure
`SELECT`. `FAILED` / `DEAD_LETTERED` jobs return 200 from `/results` with `result: null` and the error.

Accepted URL forms: `watch?v=`, `youtu.be/`, `shorts/`, `embed/`, `live/`, or a bare 11-character id.

## 6. Demo runbook

The scripts live in `ops/` (bash; on Windows run them from Git Bash). Most need a Notion token
with access to a page: `export NOTION_TOKEN=ntn_...` (and optionally `NOTION_PARENT_ID`).

| # | Scenario | Command | What you should see |
|---|---|---|---|
| 0 | Sanity | `ops/smoke_test.sh` | healthz all ok; 400 / 401 / 404 / 409 / 202 contracts pass |
| 1 | Happy path near 20 min | Take Notes on a ~19 min lecture | Stepper moves through every stage; "View in Notion" |
| 2 | Too long | Take Notes on a 35 min video | Immediate red "This video is 35 min; the limit is 20 min."; nothing queued |
| 3 | Burst | `ops/burst_submit.sh` (list in `ops/videos.txt`), or click Take Notes on 6 videos | Independent cards drain; `vn.work` depth falls; "zero LLM 429s" |
| 4 | 429 handling | `ops/force_429.sh <url> llm:0.6` then `ops/force_429.sh --off` | Amber "Rate-limited by the LLM provider — retrying in 10 s", then continues |
| 5 | Worker crash | `ops/kill_worker_mid_frames.sh <url>` | SIGKILL during EXTRACTING_FRAMES; another worker re-claims within seconds; one Notion page |
| 6 | Partial frames | `FAULT_INJECT_FRAME_FAIL=0.5 docker compose up -d --no-deps worker` | Page with some frames; amber "Done, with warnings: 3 of 6 frames added…" |
| 7 | DLQ | `ops/dlq_demo.sh <private-video-url>` then `--off` | 5 attempts in ~75 s → `DEAD_LETTERED`; message visible in `vn.dlq` |
| 8 | Scaling | `docker compose up -d --scale worker=4` | New workers join; nothing else restarts |

Verify Constraint B on published pages: `python ops/check_constraints.py <job_id> …` (every image has a
`[mm:ss]` caption, is an uploaded file, and hash-matches a frame the worker captured; exactly one page
per job).

## 7. How it works

**Stage-level messages.** A queue message is `{job_id, stage, attempt, enc_notion_token,
notion_parent_id, trace_id}`. Any worker can run any stage. A worker acks a message only after that
stage's output is durably saved (Postgres `job_artifacts`, S3) and the next-stage message has been
confirmed by the broker. A killed worker loses at most one stage of work.

**Job lifecycle.** `QUEUED → TRANSCRIBING → SUMMARIZING → EXTRACTING_FRAMES → PUBLISHING →
COMPLETED | COMPLETED_WITH_WARNINGS`; any active state can go to `FAILED` (non-retryable) or
`DEAD_LETTERED` (retry budget spent). Status lives only in Postgres and only moves forward through
compare-and-set updates. A retryable error does not change the status; it sets `waiting_until` and
`retry_reason`, which the popup shows as a countdown.

**Idempotency under redelivery.**

| Crash point | On redelivery |
|---|---|
| During transcribe / summarize | Stage reruns; output is an upsert on `(job_id, stage)` |
| During frame extraction | Frames already in S3 (`jobs/{id}/frame_{t}.jpg`) are reused |
| After commit, before the next message is published | `dispatch_pending` flag → the next-stage message is re-published |
| After publishing, before ack | A per-job Postgres advisory lock serializes workers; the stale message sees the job has moved on and is dropped |
| During publish, after the page was created but before its id was saved | **Adopt-or-create**: the page title ends with `· #<job id prefix>`, so the parent's children are listed (read-your-writes) and the page is adopted, never duplicated |
| While appending block batches | Resume from the page's real child count |

**Retries and DLQ.** `RateLimited` (any 429, from YouTube, the LLM or Notion) and `RetryableError`
(5xx, timeouts, "video unavailable") are re-published to `vn.retry.<delay>s`, a TTL queue that
dead-letters back into `vn.work`. `Retry-After` overrides the delay when longer. After `MAX_ATTEMPTS`
the message goes to `vn.dlx → vn.dlq` and the job becomes `DEAD_LETTERED`. `FatalError` (too long, no
captions, Notion 401/403, missing parent) fails the job immediately. The quorum queue's
`x-delivery-limit: 10` stops a message that crashes every worker from looping forever.

**Global rate limiting.** One atomic Redis Lua token bucket per external API (`rl:llm:rpm`,
`rl:llm:tpm`, `rl:notion:<token-hash>`, `rl:youtube`), shared by every worker. Workers wait for a token
before each call, so a burst drains without provider 429s; if the wait would exceed `RL_MAX_WAIT_SEC`
the job is parked in a retry queue instead of holding a worker.

**Real frames only (Constraint B).** `yt-dlp` resolves a ≤720p stream URL; `ffmpeg -ss t -i URL`
seeks with HTTP range requests and grabs 3 frames 1 s apart; the sharpest is kept (edge variance),
resized to ≤1280 px JPEG q80 (~150–300 KB). Captions come from the same LLM output that chose the
timestamp; the Notion block builder refuses any image without a caption or not from this job's S3
frame keys. A failed frame is omitted, never substituted. If every frame fails, the page is text-only.

**Length (Constraint A).** Checked at submit time (YouTube Data API or watch-page metadata, 3 s budget)
and again in the transcript stage, so a video over 20 minutes is never processed.

### Design choices (spec §10)

| Decision | Choice | Why |
|---|---|---|
| Broker | RabbitMQ 4 quorum queues | Redelivery on consumer death, DLX, TTL retry queues, delivery limits, management UI |
| Job state | Postgres: `jobs`, `job_events`, `job_artifacts` | Transactional CAS transitions; audit trail for the demo timeline |
| Global LLM limit | Redis token bucket (Lua) for RPM and TPM | Atomic across workers; proactive, with 429 backoff as the safety net |
| Worker topology | One generic worker image, any stage | `--scale worker=N`; recovery is per stage |
| LLM | Provider adapter; default Claude Haiku 5.5 with JSON-schema output | A 20-minute transcript (~4–6k tokens) fits one cheap call; chunked + ordered merge beyond `CHUNK_TOKENS` |
| Transcript | `youtube-transcript-api`, fallback `yt-dlp` caption tracks | Free and timestamped |
| Frames | `yt-dlp` + `ffmpeg` + Pillow | Real frames from the stream, no full download |
| Extension | Plain JS, Manifest V3, no bundler | Load unpacked, no build step |

## 8. Repository layout

```
shared/vidnotes_common/   contracts: status, messages, errors, models, config, crypto, migrations (both)
extension/                Chrome MV3 extension (Person A)
gateway/                  FastAPI gateway (Person A)
notion_publisher/         Notion block builder, uploads, adopt-or-create publish (Person A)
worker/                   consumer, dispatcher, retry, stages, LLM adapters, rate limiter (Person B)
infra/                    RabbitMQ definitions, Postgres init (Person B)
ops/                      demo + chaos scripts, constraint checker (Person B)
docs/                     architecture notes and demo script (both)
```

## 9. Development

```bash
pip install -e shared -r worker/requirements.txt -r gateway/requirements.txt ruff pytest pytest-asyncio respx
ops/run_tests.sh      # ruff + pytest for shared, notion_publisher, worker, gateway
```

`LLM_PROVIDER=fake` runs the whole pipeline offline against a deterministic summarizer (it only
rearranges transcript lines; never use it for grading).

## 10. Troubleshooting

| Symptom | Fix |
|---|---|
| Job fails with "YouTube is blocking requests from the server" | Run from a home network, or set `YT_PROXY_URL` |
| No Take Notes button | Reload the YouTube tab after loading the extension; if the player layout changed, a floating button appears bottom-right instead |
| "Notion sign-in isn't configured on the server" | Set `NOTION_CLIENT_ID` / `NOTION_CLIENT_SECRET`, or use an internal integration token |
| "The Notion destination page wasn't found" | Share the page with the integration (`•••` → Connections) |
| Frames fail but text publishes | `docker compose exec worker yt-dlp -U` (YouTube changes often), or check `docker compose logs worker` |
