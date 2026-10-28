#!/usr/bin/env bash
# Runs the resolver on a GCP VM. Usage:
#   run_on_gcp.sh --queue FILE --prefix gs://BUCKET/RUN/shard_N --workers N --searches N
#                 [--limit N] [--id-reference FILE] [--retry-unreachable 0|1] [--log FILE]
# State lives on the VM's persistent disk and is copied to PREFIX/results (Cloud Storage):
#   * results (resolver_state.sqlite, rate limits, sheet, logs) every 5 minutes and at the end
#   * caches (domain/search/register caches, ~1 GB) every 30 minutes and at the end
# Each copy is an integrity-checked SQLite snapshot. On start the local state is used if the
# disk still has it (it is never older than the bucket copy); only when the disk was lost
# (new VM) is the state restored from the bucket. So a stopped, preempted or recreated VM
# continues where it stopped and never redoes saved rows. PREFIX/DONE is written once no row
# is left pending.
set -euo pipefail
export PYTHONUNBUFFERED=1
LIMIT=0; IDREF=""; RETRY=0; LOG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --queue) QUEUE="$2"; shift 2;; --prefix) PREFIX="$2"; shift 2;; --workers) WORKERS="$2"; shift 2;;
    --searches) SEARCHES="$2"; shift 2;; --limit) LIMIT="$2"; shift 2;;
    --id-reference) IDREF="$2"; shift 2;; --retry-unreachable) RETRY="$2"; shift 2;; --log) LOG="$2"; shift 2;;
    *) echo "unknown argument $1"; exit 2;;
  esac
done

echo "== setting up Python 3.11 environment"
export HOME="${HOME:-/root}"; export PATH="$HOME/.local/bin:$PATH"
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
[[ -d /opt/venv ]] || uv venv -q -p 3.11 /opt/venv
source /opt/venv/bin/activate
uv pip install -q -r azure/requirements-azure.txt
python --version

WORK=/var/dv_output
SNAP=/var/dv_snapshot
mkdir -p "$WORK" "$SNAP"
if [[ -f "$WORK/resolver_state.sqlite" ]]; then
  echo "== resuming from the state on this VM's disk"
else
  echo "== disk has no state; restoring from $PREFIX/results"
  gsutil -q cp "$PREFIX/results/*.sqlite" "$WORK/" 2>/dev/null || echo "(no saved state; fresh start)"
fi

# The backup functions run in a subshell with errexit off: a failed copy or a grep with no
# match must never stop the backup loop.
push() (  # results: consistent snapshot -> bucket, plus a small progress file for the monitor
  set +e
  python azure/backup.py "$WORK" "$SNAP" "resolver_state.sqlite" >/dev/null || return 0
  python azure/backup.py "$WORK" "$SNAP" "rate_limits.sqlite" >/dev/null || true
  gsutil -q cp "$SNAP/resolver_state.sqlite" "$SNAP/rate_limits.sqlite" "$PREFIX/results/" || true
  gsutil -q -m cp "$SNAP"/*.csv "$SNAP"/*.json "$SNAP"/*.xlsx "$PREFIX/results/" 2>/dev/null || true
  [[ -d "$SNAP/logs" ]] && gsutil -q -m cp -r "$SNAP/logs" "$PREFIX/results/" || true
  if [[ -n "$LOG" ]]; then
    { date -u +%FT%TZ; grep -a "sheet refreshed" "$LOG" | tail -1; grep -a "search health" "$LOG" | tail -1;
      echo "tracebacks $(grep -ac Traceback "$LOG")"; echo "load $(cut -d" " -f1-3 /proc/loadavg) cpus $(nproc) | mem used $(free -m | awk '/Mem/{print $3 "/" $2}') MB"; } > /tmp/progress.txt
    gsutil -q cp /tmp/progress.txt "$PREFIX/progress.txt" || true
    tail -c 2000000 "$LOG" > /tmp/run_tail.log; gsutil -q cp /tmp/run_tail.log "$PREFIX/run.log" || true
  fi
  true
)
push_caches() (  # caches only save time on a restart, so they are copied less often
  set +e
  for db in domain_cache search_cache registers; do
    [[ -f "$WORK/$db.sqlite" ]] || continue
    python azure/backup.py "$WORK" "$SNAP" "$db.sqlite" >/dev/null && gsutil -q cp "$SNAP/$db.sqlite" "$PREFIX/results/"
  done
  true
)
( set +e; n=0; while true; do sleep 300; push; n=$((n + 1)); if (( n % 6 == 0 )); then push_caches; fi; done ) &
PUSH_PID=$!
trap 'kill $PUSH_PID 2>/dev/null || true; push; push_caches' EXIT

ARGS=(--workers "$WORKERS" --max-searches-per-minute "$SEARCHES" --input "$QUEUE" --output-dir "$WORK")
if [[ "$LIMIT" != "0" ]]; then ARGS+=(--limit-per-worker "$LIMIT"); fi
if [[ -n "$IDREF" ]]; then ARGS+=(--id-reference "$IDREF"); fi
if [[ "$RETRY" == "1" ]]; then ARGS+=(--retry-unreachable); fi
echo "== starting resolver: ${ARGS[*]}"
python -m verifier.run_parallel "${ARGS[@]}"
python -m verifier.run_parallel --status --output-dir "$WORK" || true

kill $PUSH_PID 2>/dev/null || true
push
push_caches
PENDING=$(python -c "import json;print(json.load(open('$WORK/summary.json')).get('rows_pending', 1))" 2>/dev/null || echo 1)
echo "== rows pending: $PENDING"
if [[ "$PENDING" == "0" || "$LIMIT" != "0" ]]; then
  echo "finished $(date -u +%FT%TZ)" | gsutil -q cp - "$PREFIX/DONE"
fi
