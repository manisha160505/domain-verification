#!/usr/bin/env bash
# Runs the resolver on an Azure ML compute. Usage:
#   run_on_azure.sh --queue FILE --out DIR --workers N --searches N [--limit N] [--previous DIR]
# State lives on the machine's local disk (SQLite is unreliable on cloud mounts) and is copied
# to --out every 10 minutes and at the end. --previous restores an earlier run's state, so a
# restarted job continues where the last one stopped and never redoes saved rows.
set -euo pipefail
export PYTHONUNBUFFERED=1  # progress tables appear in the live log immediately
LIMIT=0; PREVIOUS=""; IDREF=""; RETRY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --queue) QUEUE="$2"; shift 2;; --out) OUT="$2"; shift 2;; --workers) WORKERS="$2"; shift 2;;
    --searches) SEARCHES="$2"; shift 2;; --limit) LIMIT="$2"; shift 2;; --previous) PREVIOUS="$2"; shift 2;;
    --id-reference) IDREF="$2"; shift 2;;
    --retry-unreachable) RETRY="$2"; shift 2;;
    *) echo "unknown argument $1"; exit 2;;
  esac
done
echo "== setting up Python 3.11 environment"
python -m pip install -q uv
python -m uv venv -q -p 3.11 /tmp/venv
source /tmp/venv/bin/activate
python -m pip --version >/dev/null 2>&1 || true
uv pip install -q -r azure/requirements-azure.txt
python --version

WORK=/tmp/resolver_output
mkdir -p "$WORK"
if [[ -n "$PREVIOUS" && -d "$PREVIOUS" ]]; then
  echo "== restoring saved state from previous run"
  cp -v "$PREVIOUS"/*.sqlite "$WORK"/ 2>/dev/null || echo "(no saved databases found)"
fi

( while true; do sleep 600; python azure/backup.py "$WORK" "$OUT" || true; done ) &
BACKUP_PID=$!
trap 'kill $BACKUP_PID 2>/dev/null || true; python azure/backup.py "$WORK" "$OUT" || true' EXIT

ARGS=(--workers "$WORKERS" --max-searches-per-minute "$SEARCHES" --input "$QUEUE" --output-dir "$WORK")
if [[ "$LIMIT" != "0" ]]; then ARGS+=(--limit-per-worker "$LIMIT"); fi
if [[ -n "$IDREF" ]]; then ARGS+=(--id-reference "$IDREF"); fi
if [[ "$RETRY" == "1" ]]; then ARGS+=(--retry-unreachable); fi  # re-check rows whose site was unreachable
echo "== starting resolver: ${ARGS[*]}"
python -m verifier.run_parallel "${ARGS[@]}"
python -m verifier.run_parallel --status --output-dir "$WORK"
