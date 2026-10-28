#!/usr/bin/env bash
# Run the resolver from a compute-instance terminal (Azure ML Studio -> Compute -> Terminal).
#   bash azure/terminal_run.sh setup              # once: Python 3.11 + packages (~3-5 min)
#   bash azure/terminal_run.sh test               # 80-row test (10 workers x 8 rows), runs in background
#   bash azure/terminal_run.sh full               # whole queue, runs in background (safe to close browser)
#   bash azure/terminal_run.sh status             # progress per worker
#   bash azure/terminal_run.sh save               # copy results to your Notebooks folder for download
# Work happens on the machine's local disk (~/dv); results are saved back with "save".
set -euo pipefail
DV="$HOME/dv"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
WORKERS="${WORKERS:-10}"; SEARCHES="${SEARCHES:-20}"
case "${1:-}" in
  setup)
    mkdir -p "$DV"
    cp -r "$HERE/verifier" "$HERE/azure" "$DV/"
    cp "$HERE/work_queue_master.csv" "$DV/"
    cd "$DV"
    python3 -m pip install -q --user uv
    python3 -m uv venv -q -p 3.11 "$DV/.venv"
    source "$DV/.venv/bin/activate"
    python3 -m uv pip install -q -r azure/requirements-azure.txt
    python -c "import scrapling, curl_cffi; print('setup OK, python', __import__('sys').version.split()[0])";;
  test|full)
    cd "$DV"; source "$DV/.venv/bin/activate"
    LIMIT=(); [[ "$1" == "test" ]] && LIMIT=(--limit-per-worker 8)
    nohup python -m verifier.run_parallel --workers "$WORKERS" --max-searches-per-minute "$SEARCHES" \
      --input work_queue_master.csv --output-dir resolver_output "${LIMIT[@]}" > run.log 2>&1 &
    echo "started in background (pid $!). Follow it with:  tail -f ~/dv/run.log   (Ctrl+C stops only the viewing)";;
  status)
    cd "$DV"; source "$DV/.venv/bin/activate"
    python -m verifier.run_parallel --status --output-dir resolver_output; tail -n 5 run.log 2>/dev/null || true;;
  save)
    cd "$DV"; source "$DV/.venv/bin/activate"
    mkdir -p "$HERE/results"
    python azure/backup.py resolver_output "$HERE/results"
    echo "results copied to $HERE/results (visible in Notebooks; right-click a file -> Download)";;
  *) echo "usage: bash azure/terminal_run.sh setup|test|full|status|save"; exit 2;;
esac
