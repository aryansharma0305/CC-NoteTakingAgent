#!/usr/bin/env bash
# Scenario 7: dead-letter queue. Restart workers with the fast demo retry profile,
# submit a private/unavailable video, and watch 5 attempts (~75 s) end in
# DEAD_LETTERED with the message parked in vn.dlq.
#   NOTION_TOKEN=... ops/dlq_demo.sh <private-video-url>
#   ops/dlq_demo.sh --off      restore the default retry profile
. "$(dirname "$0")/lib.sh"

if [ "${1:-}" = "--off" ]; then
  say "Restoring default retry profile"
  compose up -d --no-deps worker
  exit 0
fi
require_token
url="${1:?usage: dlq_demo.sh <private-or-unavailable-video-url>}"

say "Recreating workers with RETRY_DELAYS=5,10,20,40 MAX_ATTEMPTS=5"
RETRY_DELAYS=5,10,20,40 MAX_ATTEMPTS=5 compose up -d --no-deps worker
sleep 4

id=$(submit "$url") || die "submit rejected (if it says 'too long' or 'live', pick another video)"
ok "job $id"
st=$(wait_terminal "$id" 300 | tail -n1)
say "Final status: $st"
admin "/admin/jobs/$id" | json_get "'\n'.join('   %s %-18s attempt=%s delay=%s %s' % (e['at'][11:19], (e['detail'] or {}).get('event',''), (e['detail'] or {}).get('attempt',''), (e['detail'] or {}).get('delay_sec',''), ((e['detail'] or {}).get('error') or {}).get('code','')) for e in d['events'])"
say "Queues:"
admin /admin/dlq | json_get "'\n'.join('   %-16s %s' % kv for kv in d['queues'].items())"
say "Requeue it later with: curl -X POST -H 'X-Admin-Token: \$ADMIN_TOKEN' $BASE_URL/admin/dlq/$id/requeue"
warn "Run 'ops/dlq_demo.sh --off' to restore the default retry profile."
