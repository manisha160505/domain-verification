#!/bin/bash
# VM startup script (runs as root on every boot). Fetches the code and this VM's shard from
# the bucket, runs the resolver (resuming from the state saved in the bucket), and powers the
# VM off when it ends so a finished machine stops billing. Settings come from instance metadata:
#   bucket, shard, run (folder in the bucket), workers, searches, limit, retry
set -uo pipefail
MD=http://metadata.google.internal/computeMetadata/v1/instance/attributes
meta() { curl -fs -H "Metadata-Flavor: Google" "$MD/$1" || echo "$2"; }
BUCKET=$(meta bucket ""); SHARD=$(meta shard ""); RUN=$(meta run gcp_run)
WORKERS=$(meta workers 10); SEARCHES=$(meta searches 20); LIMIT=$(meta limit 0); RETRY=$(meta retry 0)
PREFIX="gs://$BUCKET/$RUN/shard_$SHARD"
LOG=/var/log/dv_run.log
exec >>"$LOG" 2>&1
echo "===== boot $(date -u +%FT%TZ) shard $SHARD -> $PREFIX"

if gsutil -q stat "$PREFIX/DONE"; then
  echo "shard already finished; powering off"
  shutdown -h now; exit 0
fi

APP=/opt/dv
rm -rf "$APP" && mkdir -p "$APP"
gsutil -q cp "gs://$BUCKET/code.tar.gz" /tmp/code.tar.gz && tar -xzf /tmp/code.tar.gz -C "$APP"
gsutil -q cp "gs://$BUCKET/$RUN/input/shard_$SHARD.csv" "$APP/shard.csv"
gsutil -q cp "gs://$BUCKET/id_reference.csv" "$APP/id_reference.csv"
cd "$APP"

for attempt in 1 2 3; do
  bash gcp/run_on_gcp.sh --queue shard.csv --prefix "$PREFIX" --workers "$WORKERS" --searches "$SEARCHES" \
    --limit "$LIMIT" --id-reference id_reference.csv --retry-unreachable "$RETRY" --log "$LOG"
  if gsutil -q stat "$PREFIX/DONE"; then break; fi
  echo "run ended without finishing every row (attempt $attempt); resuming"
done
gsutil -q cp "$LOG" "$PREFIX/run.log" || true
echo "===== powering off $(date -u +%FT%TZ)"
shutdown -h now
