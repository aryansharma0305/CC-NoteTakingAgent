#!/usr/bin/env bash
# Scenario 4: 429 handling. Restart workers with synthetic 429s injected into the
# LLM (and optionally Notion/YouTube) adapters, submit a job, and watch it wait
# with backoff instead of failing.
#   NOTION_TOKEN=... ops/force_429.sh <youtube-url> [spec]     spec default: llm:0.6
#   ops/force_429.sh --off                                    restore normal workers
. "$(dirname "$0")/lib.sh"

if [ "${1:-}" = "--off" ]; then
  say "Restoring workers without fault injection"
  FAULT_INJECT_429= compose up -d --no-deps worker
  exit 0
fi
require_token
url="${1:?usage: force_429.sh <youtube-url> [llm:0.6,notion:0.3]}"
spec="${2:-llm:0.6}"

say "Recreating workers with FAULT_INJECT_429=$spec"
FAULT_INJECT_429="$spec" compose up -d --no-deps worker
sleep 4

id=$(submit "$url") || die "submit failed"
ok "job $id"
say "Watch the popup: the card turns amber ('Rate-limited … retrying in N s') and then continues."
st=$(wait_terminal "$id" 900 | tail -n1)
say "Final status: $st"
admin "/admin/jobs/$id" | json_get "'\n'.join('   %s %-12s %s' % (e['at'][11:19], e['detail'].get('event',''), (e['detail'].get('error') or {}).get('message','')) for e in d['events'] if e.get('detail'))"
warn "Run 'ops/force_429.sh --off' to restore normal workers."
