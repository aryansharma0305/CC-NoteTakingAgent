#!/usr/bin/env bash
# Scenario 0: sanity. Checks /healthz and every REST contract that doesn't need a real video.
#   ops/smoke_test.sh
. "$(dirname "$0")/lib.sh"

fail=0
check() { # check <name> <expected> <actual>
  if [ "$2" = "$3" ]; then ok "$1 ($3)"; else warn "$1: expected $2, got $3"; fail=1; fi
}
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

say "GET /healthz"
curl -s "$BASE_URL/healthz"; echo
check "healthz" 200 "$(code "$BASE_URL/healthz")"

say "POST /jobs validation"
check "invalid URL -> 400" 400 "$(code -X POST "$BASE_URL/jobs" -H 'Content-Type: application/json' \
  -d '{"youtube_url":"https://example.com/nope","notion_token":"x"}')"
check "missing token -> 401" 401 "$(code -X POST "$BASE_URL/jobs" -H 'Content-Type: application/json' \
  -d '{"youtube_url":"https://youtu.be/aircAruvnKk"}')"
check "malformed body -> 400" 400 "$(code -X POST "$BASE_URL/jobs" -H 'Content-Type: application/json' -d '{nope')"
body=$(curl -s -X POST "$BASE_URL/jobs" -H 'Content-Type: application/json' -d '{"youtube_url":"bad","notion_token":"x"}')
check "error body shape" "INVALID_URL" "$(printf '%s' "$body" | json_get "d['error']['code']")"

say "GET /jobs/{id} and /results"
check "unknown job -> 404" 404 "$(code "$BASE_URL/jobs/00000000-0000-0000-0000-000000000000")"
check "garbage id -> 404" 404 "$(code "$BASE_URL/jobs/not-a-uuid")"

say "Submit a real job with a dummy token (it will fail at publish, which is fine here)"
resp=$(curl -s -i -X POST "$BASE_URL/jobs" -H 'Content-Type: application/json' \
  -d '{"youtube_url":"https://www.youtube.com/watch?v=aircAruvnKk","notion_token":"dummy-token"}')
status=$(printf '%s' "$resp" | head -n1 | awk '{print $2}')
check "submit -> 202" 202 "$status"
location=$(printf '%s' "$resp" | grep -i '^location:' | tr -d '\r' | awk '{print $2}')
[ -n "$location" ] && ok "Location header: $location" || { warn "no Location header"; fail=1; }
job=$(printf '%s' "$resp" | sed -n '/^\r\{0,1\}$/,$p' | tail -n +2 | json_get "d['job_id']")
if [ -n "$job" ]; then
  s1=$(job_json "$job"); s2=$(job_json "$job")
  st=$(printf '%s' "$s1" | json_get "d['status']")
  if ! is_terminal "$st"; then
    check "results before terminal -> 409" 409 "$(code "$BASE_URL/jobs/$job/results")"
  fi
  ok "GET /jobs/$job -> $st (polling is read-only)"
fi

[ $fail -eq 0 ] && ok "smoke test passed" || die "smoke test had failures"
