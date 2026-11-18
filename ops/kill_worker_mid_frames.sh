#!/usr/bin/env bash
# Scenario 5: worker crash. Submit a job, wait until it is EXTRACTING_FRAMES, then
# SIGKILL exactly the worker container holding it. RabbitMQ sees the TCP close,
# requeues the unacked message, and another worker finishes the job.
#   NOTION_TOKEN=... ops/kill_worker_mid_frames.sh <youtube-url> [STAGE]
#   STAGE defaults to EXTRACTING_FRAMES (also try SUMMARIZING or PUBLISHING)
. "$(dirname "$0")/lib.sh"
require_token
url="${1:?usage: kill_worker_mid_frames.sh <youtube-url> [STAGE]}"
target="${2:-EXTRACTING_FRAMES}"

workers=$(compose ps -q worker | wc -l | tr -d ' ')
[ "$workers" -ge 2 ] || die "need >= 2 running workers (docker compose up -d --scale worker=2)"

id=$(submit "$url") || die "submit failed"
ok "job $id"
say "Waiting for $target"
while :; do
  st=$(job_status "$id")
  [ "$st" = "$target" ] && break
  is_terminal "$st" && die "job reached $st before $target"
  sleep 0.5
done
victim=$(admin "/admin/jobs/$id" | json_get "d['worker_id']")
[ -n "$victim" ] || die "no worker_id recorded"
say "Job is $target on worker $victim -> docker kill -s KILL $victim"
docker kill -s KILL "$victim" >/dev/null
kill_ts=$(date +%s)

st=$(wait_terminal "$id" 900 | tail -n1)
say "Final status: $st"

say "Timeline (job_events):"
admin "/admin/jobs/$id" | json_get "'\n'.join('   %s %-14s %-20s %s' % (e['at'][11:23], e['worker_id'] or '', (e['detail'] or {}).get('event', ''), (e['detail'] or {}).get('stage', '') or (e.get('to') or '')) for e in d['events'])"

$PY - "$BASE_URL" "$ADMIN_TOKEN" "$id" "$victim" "$kill_ts" <<'EOF'
import json, sys, urllib.request
from datetime import datetime
base, token, job, victim, kill_ts = sys.argv[1:6]
req = urllib.request.Request(f"{base}/admin/jobs/{job}", headers={"X-Admin-Token": token})
d = json.load(urllib.request.urlopen(req))
kill = float(kill_ts)
after = [e for e in d["events"] if e["worker_id"] not in (victim, "gateway", None)
         and (e["detail"] or {}).get("event") == "claimed"
         and datetime.fromisoformat(e["at"]).timestamp() >= kill - 1]
if after:
    t = datetime.fromisoformat(after[0]["at"]).timestamp()
    print(f"   recovery: stage re-claimed by {after[0]['worker_id']} {t - kill:.1f} s after the kill")
print(f"   notion_page_id: {d.get('notion_page_id')}  (one page id per job; see ops/check_constraints.py)")
EOF
