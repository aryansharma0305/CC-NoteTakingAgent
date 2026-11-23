#!/usr/bin/env bash
# Scenario 3: burst. Submit several different videos back to back (like clicking
# Take Notes on each), watch the queue drain, and confirm zero LLM 429s.
#   NOTION_TOKEN=... ops/burst_submit.sh [videos-file]      (default: ops/videos.txt)
. "$(dirname "$0")/lib.sh"
require_token

file="${1:-$(dirname "$0")/videos.txt}"
[ -f "$file" ] || die "No video list. Copy ops/videos.example.txt to ops/videos.txt and add 6 URLs (<= 20 min, with captions)."
mapfile -t urls < <(grep -vE '^\s*(#|$)' "$file")
[ ${#urls[@]} -gt 0 ] || die "$file has no URLs"

since=$(date -u +%Y-%m-%dT%H:%M:%SZ)
start=$SECONDS
ids=()
say "Submitting ${#urls[@]} jobs"
for u in "${urls[@]}"; do
  if id=$(submit "$u"); then ids+=("$id"); ok "$id  $u"; else warn "rejected: $u"; fi
done

say "Waiting for the queue to drain (RabbitMQ UI: http://localhost:15672, vn/vn)"
while :; do
  done_n=0
  printf '\033[2K\r'
  for id in "${ids[@]}"; do
    st=$(job_status "$id"); is_terminal "$st" && done_n=$((done_n+1))
  done
  depth=$(admin /admin/dlq | json_get "d['queues'].get('vn.work')")
  printf '   [%4ss] %d/%d finished, vn.work depth=%s' "$((SECONDS-start))" "$done_n" "${#ids[@]}" "$depth"
  [ "$done_n" -eq "${#ids[@]}" ] && break
  sleep 2
done
echo

say "Results"
for id in "${ids[@]}"; do printf '   %s  %s\n' "$id" "$(job_line "$id")"; done
say "Total wall time: $((SECONDS-start)) s"

n429=$(compose logs worker --since "$since" 2>/dev/null | grep -c "Rate-limited by the LLM provider\"" || true)
if [ "$n429" = "0" ]; then ok "zero LLM 429s in worker logs"; else warn "$n429 LLM 429s in worker logs (lower LLM_RPM/LLM_TPM)"; fi
waits=$(compose logs worker --since "$since" 2>/dev/null | grep -c "Waiting for llm capacity" || true)
say "limiter deferrals (proactive, not errors): $waits"
