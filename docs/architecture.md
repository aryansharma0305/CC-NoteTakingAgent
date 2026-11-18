# Architecture notes

The README (section 7) covers the design end to end. This file records the details a reviewer asks
about and the contracts both halves depend on.

## Ownership

| Person | Owns | Boundary |
|---|---|---|
| A: client & gateway | `extension/`, `gateway/`, `notion_publisher/` | `notion_publisher.publish(job, synthesis, frames, token, parent_id, store) -> PublishResult` |
| B: pipeline & infra | `worker/`, `infra/`, `ops/`, `docker-compose.yml` | calls the function above from `worker/app/stages/publish.py` |
| Both | `shared/`, `README.md`, `docs/`, `.env.example` | changes by reviewed PR only (`.github/CODEOWNERS`) |

## Message handling (worker/app/dispatcher.py)

1. Take `pg_try_advisory_lock(hashtext(job_id))` on a dedicated connection. Busy → re-park the
   message in the shortest retry queue with `deferred=true` (no attempt consumed), ack.
2. Load the job. Terminal, or already past this stage → duplicate: ack and drop. Exception: the job is
   exactly one stage ahead and `dispatch_pending` is set → the previous worker committed but died
   before the next message was confirmed, so re-publish it.
3. Claim: `QUEUED → TRANSCRIBING` by compare-and-set, or record the claim on the current stage.
   Clears `waiting_until`, records `worker_id` (the container hostname, used by the crash script).
4. If the stage's artifact already exists, skip the work. Otherwise run the stage.
5. Success: one transaction upserts the artifact and moves the job to the next stage's status with
   `dispatch_pending = true`; publish the next message with publisher confirms; clear the flag; ack.
6. `RetryableError` / `RateLimited`: publish `attempt+1` to `vn.retry.<delay>s`, set
   `waiting_until`/`retry_reason`, ack. Budget spent → publish to `vn.dlx`, mark `DEAD_LETTERED`, ack.
7. `FatalError`: mark `FAILED` with `{code, message, stage}`, ack.
8. Infrastructure failure (DB or broker down): `nack(requeue=True)`; the quorum delivery limit bounds it.

## Broker topology

| Object | Type | Arguments |
|---|---|---|
| `vn.jobs` | direct exchange | routing key `work` → `vn.work` |
| `vn.work` | quorum queue | `x-delivery-limit: 10`, DLX `vn.dlx`, `at-least-once` dead-lettering, `reject-publish` overflow |
| `vn.retry.<N>s` | quorum queue per `RETRY_DELAYS` value | `x-message-ttl: N*1000`, dead-letters to `vn.jobs`/`work` |
| `vn.dlx` → `vn.dlq` | fanout + quorum queue | `x-delivery-limit: -1` so inspecting never drops messages |

Static objects are loaded from `infra/rabbitmq/definitions.json` at boot and also declared (with
identical arguments) by `vidnotes_common.broker.declare_topology`; retry queues are only declared in
code, named by delay, so changing `RETRY_DELAYS` never conflicts with an existing queue.

## Data model

`jobs` is the only table the API reads. `job_artifacts(job_id, stage)` holds `transcript`,
`synthesis` and `frames` outputs (upserts). `job_events` is the audit trail: `submitted`, `claimed`,
`stage_done`, `retry_scheduled`, `dead_lettered`, `failed`, `requeued_from_dlq`. Recovery time after a
crash is the gap between the kill and the next `claimed` event by a different worker.

## Known limits

* A worker that hangs (not dies) while holding the advisory lock blocks its job until AMQP heartbeats
  (~60 s) close its connection; the redelivered message keeps deferring until the lock is released.
* A message that crashes every worker is dead-lettered by the consumer on its 9th redelivery
  (`x-delivery-count`), so both the queue and the job row show it. The broker's own delivery limit
  (10) is the backstop if even that fails.
* Speech-to-text for videos without captions is out of scope (`NO_TRANSCRIPT`).
