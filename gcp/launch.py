"""Run the resolver on GCP Compute Engine VMs, one shard per VM, driven from this laptop.

    python gcp/launch.py setup  --project P --bucket B            # upload code, shards, id reference
    python gcp/launch.py create --project P --bucket B --shards 1-13 [--limit 5] [--spot]
    python gcp/launch.py status --project P --bucket B            # one report
    python gcp/launch.py watch  --project P --bucket B --every 15 # report every N minutes, restart dead VMs
    python gcp/launch.py download --project P --bucket B          # results -> gcp_runs/<run>/shard_N/
    python gcp/launch.py delete --project P --shards 1-13

Each VM gets its own public IP (search engines see 13 separate machines), runs gcp/startup.sh,
keeps its state on a persistent disk (kept across stop/start and Spot preemption) and backs it
up to gs://B/<run>/shard_N/results (results every 5 minutes, caches every 30; the bucket keeps
older versions), and powers itself off when its shard is finished. A VM that stops early (Spot preemption, crash) is started again by
`watch` and resumes from the saved state.
"""
from __future__ import annotations

import argparse
import io
import json
import subprocess
import tarfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SDK = Path.home() / "AppData/Local/Google/Cloud SDK/google-cloud-sdk/bin"
GCLOUD = str(SDK / "gcloud.cmd") if (SDK / "gcloud.cmd").exists() else "gcloud"
CODE_PARTS = ["verifier", "gcp", "azure/backup.py", "azure/requirements-azure.txt"]


def run(*args: str, check: bool = True, timeout: int = 900) -> str:
    done = subprocess.run([GCLOUD, *args], capture_output=True, text=True, timeout=timeout)
    if check and done.returncode != 0:
        raise SystemExit(f"gcloud {' '.join(args[:3])} failed:\n{done.stderr.strip()}")
    return done.stdout.strip()


def shard_list(spec: str) -> list[int]:
    out: list[int] = []
    for part in spec.split(","):
        lo, _, hi = part.partition("-")
        out += list(range(int(lo), int(hi or lo) + 1))
    return out


def code_bundle() -> bytes:
    """Code as a tar.gz; shell scripts get Unix line endings."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for part in CODE_PARTS:
            for path in ([ROOT / part] if (ROOT / part).is_file() else sorted((ROOT / part).rglob("*"))):
                if not path.is_file() or "__pycache__" in path.parts or path.suffix in {".pyc", ".sqlite"}:
                    continue
                data = path.read_bytes()
                if path.suffix == ".sh":
                    data = data.replace(b"\r\n", b"\n")
                info = tarfile.TarInfo(path.relative_to(ROOT).as_posix())
                info.size, info.mode = len(data), 0o755 if path.suffix == ".sh" else 0o644
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def setup(args) -> None:
    bucket = f"gs://{args.bucket}"
    if run("storage", "buckets", "describe", bucket, "--project", args.project, check=False) == "":
        run("storage", "buckets", "create", bucket, "--project", args.project, "--location", args.region,
            "--uniform-bucket-level-access")
        print("bucket created:", bucket)
    # Keep the 5 previous copies of every backup file, so a bad copy can always be rolled back.
    rules = ROOT / "gcp_runs" / "lifecycle.json"
    rules.parent.mkdir(exist_ok=True)
    rules.write_text(json.dumps({"rule": [{"action": {"type": "Delete"},
                                           "condition": {"isLive": False, "numNewerVersions": 5}}]}))
    run("storage", "buckets", "update", bucket, "--versioning", f"--lifecycle-file={rules}")
    bundle = ROOT / "gcp_runs" / "code.tar.gz"
    bundle.parent.mkdir(exist_ok=True)
    bundle.write_bytes(code_bundle())
    run("storage", "cp", str(bundle), f"{bucket}/code.tar.gz")
    run("storage", "cp", str(ROOT / args.id_reference), f"{bucket}/id_reference.csv")
    shards = sorted((ROOT / args.shards_dir).glob("shard_*.csv"))
    run("storage", "cp", *map(str, shards), f"{bucket}/{args.run}/input/")
    print(f"uploaded code ({bundle.stat().st_size // 1024} KB), id reference and {len(shards)} shards")


def create(args) -> None:
    for shard in shard_list(args.shards):
        meta = (f"bucket={args.bucket},shard={shard},run={args.run},workers={args.workers},"
                f"searches={args.searches},limit={args.limit},retry={args.retry}")
        cmd = ["compute", "instances", "create", f"{args.prefix}-{shard}", "--project", args.project,
               "--zone", args.zone, "--machine-type", args.machine, "--image-family", "debian-12",
               "--image-project", "debian-cloud", "--boot-disk-size", "30GB",
               "--scopes", "storage-rw,logging-write", "--metadata", meta,
               "--metadata-from-file", f"startup-script={ROOT / 'gcp' / 'startup.sh'}"]
        if args.spot:
            cmd += ["--provisioning-model", "SPOT", "--instance-termination-action", "STOP"]
        run(*cmd)
        print(f"created {args.prefix}-{shard}")


def vm_states(args) -> dict[str, str]:
    rows = json.loads(run("compute", "instances", "list", "--project", args.project,
                          "--filter", f"name~^{args.prefix}-", "--format", "json(name,status)") or "[]")
    return {r["name"]: r["status"] for r in rows}


def report(args, history: dict) -> list[str]:
    """One status pass; returns the VMs that stopped before finishing."""
    states = vm_states(args)
    listing = run("storage", "ls", f"gs://{args.bucket}/{args.run}/*/DONE", check=False)
    done_shards = {int(p.split("/shard_")[1].split("/")[0]) for p in listing.split() if "/shard_" in p}
    lines, dead, total_done, total_rows = [f"=== {datetime.now():%Y-%m-%d %H:%M}"], [], 0, 0
    for name in sorted(states, key=lambda n: int(n.rsplit("-", 1)[1])):
        shard = int(name.rsplit("-", 1)[1])
        text = run("storage", "cat", f"gs://{args.bucket}/{args.run}/shard_{shard}/progress.txt", check=False)
        counts, health = {}, ""
        for line in text.splitlines():
            if "sheet refreshed:" in line:
                counts = json.loads(line.split("sheet refreshed:", 1)[1])
            elif "search health:" in line:
                health = line.split("search health:", 1)[1].split(", total")[0].strip()
        pending = counts.pop("PENDING", 0)
        done = sum(counts.values())
        total_done, total_rows = total_done + done, total_rows + done + pending
        history.setdefault(name, []).append((time.time(), done))
        points = [p for p in history[name] if p[1] > 0][-3:]
        rate = ""
        if len(points) > 1 and done > points[0][1]:
            per_hour = (done - points[0][1]) / ((time.time() - points[0][0]) / 3600)
            rate = f" | {per_hour:.0f} rows/h, ETA {pending / per_hour:.1f} h"
        finished = shard in done_shards
        if states[name] == "TERMINATED" and not finished:
            dead.append(name)
        lines.append(f"{name} {states[name]:<10} {'DONE ' if finished else ''}{done}/{done + pending or '?'}"
                     f"{rate} | {health or 'health n/a'} | {counts}")
    lines.append(f"total {total_done}/{total_rows or '?'} | finished shards {len(done_shards)}/{len(states)}")
    text = "\n".join(lines)
    print(text, flush=True)
    log = ROOT / "gcp_runs" / args.run / "monitor.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        f.write(text + "\n")
    return dead


def watch(args) -> None:
    history: dict = {}
    while True:
        dead = report(args, history)
        for name in dead:  # preempted or crashed before finishing: start it again, it resumes
            run("compute", "instances", "start", name, "--project", args.project, "--zone", args.zone, "--async",
                check=False)
            print(f"restarted {name} (stopped before finishing)", flush=True)
        states = vm_states(args)
        if states and all(s == "TERMINATED" for s in states.values()) and not dead:
            print("ALL SHARDS FINISHED; every VM is powered off", flush=True)
            return
        time.sleep(args.every * 60)


def download(args) -> None:
    for shard in shard_list(args.shards):
        target = ROOT / "gcp_runs" / args.run / f"shard_{shard}"
        target.mkdir(parents=True, exist_ok=True)
        run("storage", "cp", "-r", f"gs://{args.bucket}/{args.run}/shard_{shard}/results", str(target), timeout=3600)
        print("downloaded", target)


def delete(args) -> None:
    names = [f"{args.prefix}-{s}" for s in shard_list(args.shards)]
    run("compute", "instances", "delete", *names, "--project", args.project, "--zone", args.zone, "--quiet",
        timeout=1800)
    print("deleted", ", ".join(names))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["setup", "create", "status", "watch", "download", "delete"])
    parser.add_argument("--project", required=True)
    parser.add_argument("--bucket")
    parser.add_argument("--region", default="us-central1")
    parser.add_argument("--zone", default="us-central1-a")
    parser.add_argument("--run", default="gcp_run")
    parser.add_argument("--prefix", default="dv-shard", help="VM name prefix")
    parser.add_argument("--shards", default="1-13")
    parser.add_argument("--shards-dir", default="shards_gcp")
    parser.add_argument("--id-reference", default="work_queue_all.csv")
    parser.add_argument("--machine", default="e2-standard-8")
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--searches", type=int, default=20)
    parser.add_argument("--limit", type=int, default=0, help="rows per worker (pilot); 0 = all")
    parser.add_argument("--retry", type=int, default=0, help="1 = re-check unreachable rows")
    parser.add_argument("--spot", action="store_true")
    parser.add_argument("--every", type=float, default=15)
    args = parser.parse_args()
    if args.command not in {"delete"} and not args.bucket:
        parser.error("--bucket is required")
    {"setup": setup, "create": create, "status": lambda a: report(a, {}), "watch": watch,
     "download": download, "delete": delete}[args.command](args)


if __name__ == "__main__":
    main()
