#!/usr/bin/env bash
# Shared helpers for the demo / chaos scripts. Source it: . "$(dirname "$0")/lib.sh"
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
ADMIN_TOKEN="${ADMIN_TOKEN:-$(grep -E '^ADMIN_TOKEN=' "$(dirname "$0")/../.env" 2>/dev/null | cut -d= -f2- || true)}"
ADMIN_TOKEN="${ADMIN_TOKEN:-change-me}"
NOTION_TOKEN="${NOTION_TOKEN:-}"
NOTION_PARENT_ID="${NOTION_PARENT_ID:-}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if command -v python3 >/dev/null 2>&1 && python3 -c "" >/dev/null 2>&1; then PY=python3; else PY=python; fi

c_reset=$'\033[0m'; c_dim=$'\033[2m'; c_red=$'\033[31m'; c_grn=$'\033[32m'; c_yel=$'\033[33m'; c_blu=$'\033[34m'
say()  { printf '%s==>%s %s\n' "$c_blu" "$c_reset" "$*"; }
ok()   { printf '%s ok %s %s\n' "$c_grn" "$c_reset" "$*"; }
warn() { printf '%s !! %s %s\n' "$c_yel" "$c_reset" "$*"; }
die()  { printf '%sERR%s %s\n' "$c_red" "$c_reset" "$*" >&2; exit 1; }

# json_get '<python expr on d>'  -- reads JSON from stdin
json_get() { $PY -c "import json,sys; d=json.load(sys.stdin); v=($1); print('' if v is None else v)"; }

require_token() {
  [ -n "$NOTION_TOKEN" ] || die "Set NOTION_TOKEN (a Notion integration token with access to a page), e.g. export NOTION_TOKEN=ntn_..."
}

# submit <youtube-url>  -> prints job_id (or exits)
submit() {
  local url="$1" body resp code
  body=$($PY -c "import json,sys; print(json.dumps({'youtube_url': sys.argv[1], 'notion_token': sys.argv[2], 'notion_parent_id': sys.argv[3] or None}))" "$url" "$NOTION_TOKEN" "$NOTION_PARENT_ID")
  resp=$(curl -s -w '\n%{http_code}' -X POST "$BASE_URL/jobs" -H 'Content-Type: application/json' -d "$body")
  code=$(printf '%s' "$resp" | tail -n1)
  resp=$(printf '%s' "$resp" | sed '$d')
  if [ "$code" != "202" ]; then
    echo "HTTP $code: $(printf '%s' "$resp" | json_get "d.get('error',{}).get('message')")" >&2
    return 1
  fi
  printf '%s' "$resp" | json_get "d['job_id']"
}

job_json()   { curl -s "$BASE_URL/jobs/$1"; }
job_status() { job_json "$1" | json_get "d.get('status')"; }
admin()      { curl -s -H "X-Admin-Token: $ADMIN_TOKEN" "$BASE_URL$1"; }

is_terminal() { case "$1" in COMPLETED|COMPLETED_WITH_WARNINGS|FAILED|DEAD_LETTERED) return 0;; *) return 1;; esac; }

# one-line status summary for a job
job_line() {
  job_json "$1" | json_get "'%-24s %3s%%  %s%s' % (d.get('status'), d.get('progress'), (d.get('video_title') or '')[:40], ('  [waiting: ' + (d.get('retry_reason') or '') + ', attempt ' + str(d.get('attempt')) + ']') if d.get('waiting_until') else '')"
}

# wait_terminal <job_id> [timeout_sec]
wait_terminal() {
  local id="$1" timeout="${2:-600}" start=$SECONDS st last=""
  while :; do
    st=$(job_status "$id")
    line=$(job_line "$id")
    if [ "$line" != "$last" ]; then printf '   %s[%4ss]%s %s\n' "$c_dim" "$((SECONDS-start))" "$c_reset" "$line"; last="$line"; fi
    is_terminal "$st" && { echo "$st"; return 0; }
    [ $((SECONDS-start)) -ge "$timeout" ] && { warn "timed out waiting for $id"; return 1; }
    sleep 1
  done
}

compose() { (cd "$ROOT" && docker compose "$@"); }
