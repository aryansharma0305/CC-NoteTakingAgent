# Demo script

Narration for the graded demo. Rehearse it twice end to end. Every step maps to a row in the README's
runbook (section 6).

## Before the grader arrives

```bash
docker compose down -v && docker compose up --build -d
ops/smoke_test.sh
export NOTION_TOKEN=ntn_...            # integration with access to the demo page
```

Open three windows: Chrome with a YouTube tab and the extension popup pinned; the RabbitMQ UI
(<http://localhost:15672>, Queues tab); a terminal with `docker compose logs -f worker`.

## 0. Sanity (30 s)

`curl localhost:8000/healthz`: "Five dependencies, all reachable. If one were down, this returns 503
and names it."

## 1. Happy path near 20 minutes (~90 s)

Open a ~19-minute lecture, click **Take Notes** in the player toolbar. In the popup: the stepper goes
Queued → Transcribing → Summarizing → Extracting frames → Publishing → Done. Click **View in Notion**:
TL;DR, timestamped section headings that link back into the video, bullets, and frames under their
sections, each captioned "[mm:ss] what it shows — why it matters".

## 2. Too long (10 s)

Click Take Notes on a 35-minute video: red "This video is 35 min; the limit is 20 min." immediately.
"Rejected at submission, nothing was queued, and the transcript stage checks again in case metadata
was unavailable."

## 3. Burst (~2–3 min)

`ops/burst_submit.sh` (or click Take Notes on six videos in a row). Show six independent cards and the
`vn.work` depth falling in the RabbitMQ UI. The script ends with "zero LLM 429s": "every worker takes
a token from a shared Redis bucket before calling the LLM, so the pool as a whole stays under
`LLM_RPM`."

## 4. 429 handling (~1 min)

`ops/force_429.sh <url> llm:0.6`. The card turns amber: "Rate-limited by the LLM provider — retrying
in 10 s (attempt 2 of 5)", then continues. "A 429 from any of the three APIs is parked in a TTL retry
queue with exponential backoff; the job shows waiting, not failed." `ops/force_429.sh --off`.

## 5. Worker crash (~1 min)

`ops/kill_worker_mid_frames.sh <url>`. The script waits for EXTRACTING_FRAMES, finds the container
holding the job from `jobs.worker_id`, and `docker kill -s KILL`s it. The timeline shows the stage
re-claimed by the other worker a second or two later, then the page published. Run
`python ops/check_constraints.py <job_id>`: "exactly one page with this job's marker. Acks only happen
after a stage is durable, and publishing adopts a page it already created instead of making a second."

## 6. Partial frames (~1 min)

`FAULT_INJECT_FRAME_FAIL=0.5 docker compose up -d --no-deps worker`, submit a video. Amber "Done, with
warnings — 3 of 6 frames added; the one at 7:41 couldn't be captured". The page has the text and only
the frames that succeeded, nothing substituted. Restore with `docker compose up -d --no-deps worker`.

## 7. Dead-letter queue (~90 s)

`ops/dlq_demo.sh <private-video-url>`. Five attempts at 5/10/20/40 s, then `DEAD_LETTERED`; the
message is visible in `vn.dlq`. Optional: `POST /admin/dlq/<id>/requeue`. `ops/dlq_demo.sh --off`.

## 8. Scaling (30 s)

`docker compose up -d --scale worker=4`; `docker compose ps`: new workers joined and nothing else
restarted.
