#!/usr/bin/env bash
set -euo pipefail
umask 077
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python3 -m venv "$BASE/.venv-cloud"
if command -v uv >/dev/null 2>&1; then
  UV_CACHE_DIR="$BASE/.uv-cache" uv pip install --python "$BASE/.venv-cloud/bin/python" -r "$BASE/cloud/requirements.lock.txt"
else
  "$BASE/.venv-cloud/bin/python" -m pip install --cache-dir "$BASE/.pip-cache" -r "$BASE/cloud/requirements.lock.txt"
fi
for directory in "$BASE/data" "$BASE/state"; do
  if [[ -L "$directory" ]]; then
    echo 'Refusing symbolic-link runtime directories' >&2
    exit 2
  fi
  mkdir -p "$directory"
  chmod 700 "$directory"
done
if [[ ! -e "$BASE/config/attention.deployment.json" ]]; then
  cp "$BASE/config/attention.deployment.example.json" "$BASE/config/attention.deployment.json"
fi
echo 'Set BILI_ATTENTION_RECIPIENT and restore the latest state/run-ledger.json backup.'
echo 'For a brand-new deployment only, run cloud/runner.py init-ledger.'
echo 'Credential entry is a separate, account-owner-only step; see CLOUD_DELIVERY.md.'
