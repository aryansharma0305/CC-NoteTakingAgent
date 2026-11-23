#!/usr/bin/env bash
# Run lint + unit tests for every Python package. worker/ and gateway/ both use a
# top-level package named `app`, so each suite runs in its own pytest process.
set -euo pipefail
cd "$(dirname "$0")/.."
if command -v python3 >/dev/null 2>&1 && python3 -c "" >/dev/null 2>&1; then PY=python3; else PY=python; fi
$PY -m ruff check shared notion_publisher worker gateway ops
for suite in shared notion_publisher worker gateway; do
  echo "== $suite"
  $PY -m pytest -q "$suite/tests"
done
