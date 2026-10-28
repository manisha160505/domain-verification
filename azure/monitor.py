"""Watch the shard jobs: status, progress and ETA from each job's live run log.

    python azure/monitor.py --minutes 110 --every 15

Reads azure_runs/full_run/jobs.json. Every --every minutes it downloads each job's logs
(small; the run log carries the per-worker progress table and sheet counts every 5 minutes)
and appends a report to azure_runs/full_run/monitor.log. It exits early when any job fails or
is cancelled (so it can be resumed from its backup) or when all jobs have completed.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

AZ = r"C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd"
WS = ["-w", "intel-partner-program", "-g", "accurix-resource-group"]
RUN_DIR = Path(__file__).resolve().parent.parent / "azure_runs" / "full_run"
TERMINAL_BAD = {"Failed", "Canceled", "NotResponding"}


def az(*args: str, timeout: int = 600) -> str:
    done = subprocess.run([AZ, *args, *WS], capture_output=True, text=True, timeout=timeout)
    return done.stdout.strip()


def compute_state(compute: str) -> str:
    return az("ml", "compute", "show", "-n", compute, "--query", "state", "-o", "tsv") or "Unknown"


def stop_compute(compute: str) -> None:
    az("ml", "compute", "stop", "-n", compute, "--no-wait")


def job_status(job: str) -> str:
    return az("ml", "job", "show", "-n", job, "--query", "status", "-o", "tsv") or "Unknown"


ARTIFACTS = ("https://eastus2.api.azureml.ms/artifact/v2.0/subscriptions/cc186077-8dcb-4061-9ea6-c1bef3be27c1/"
             "resourceGroups/accurix-resource-group/providers/Microsoft.MachineLearningServices/workspaces/"
             "intel-partner-program/artifacts/prefix/contentinfo/ExperimentRun/dcid.{job}/user_logs")


def fetch_log(job: str, shard: str) -> str:
    """Live run log of a (running) job via the artifact API's short-lived read link."""
    import urllib.request
    done = subprocess.run([AZ, "rest", "--method", "get", "--url", ARTIFACTS.format(job=job),
                           "--resource", "https://management.azure.com"], capture_output=True, text=True, timeout=300)
    try:
        items = json.loads(done.stdout or "{}").get("value", [])
    except json.JSONDecodeError:
        return ""
    uri = next((i["contentUri"] for i in items if i.get("path") == "user_logs/std_log.txt"), "")
    if not uri:
        return ""
    with urllib.request.urlopen(uri, timeout=120) as response:
        text = response.read().decode("utf-8", errors="replace")
    target = RUN_DIR / shard / "std_log.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return text


def progress(log: str) -> tuple[int, int, dict]:
    """Rows done / owned from the latest live sheet counts (written every 5 minutes; the
    progress table itself is buffered on Azure and appears late)."""
    counts = re.findall(r"sheet refreshed: (\{.*\})", log)
    if not counts:
        return 0, 0, {}
    status = json.loads(counts[-1])
    pending = status.pop("PENDING", 0)
    done = sum(status.values())
    return done, done + pending, status


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--minutes", type=float, default=110)
    parser.add_argument("--every", type=float, default=15)
    args = parser.parse_args()
    jobs = json.loads((RUN_DIR / "jobs.json").read_text(encoding="utf-8-sig"))
    started = time.time()
    history: dict[str, list[tuple[float, int]]] = {}
    while True:
        lines = [f"=== {datetime.now():%Y-%m-%d %H:%M}"]
        statuses = {}
        total_done = total_owned = 0
        for shard, info in sorted(jobs.items()):
            status = job_status(info["job"])
            statuses[shard] = status
            machine = compute_state(info["compute"])
            log = fetch_log(info["job"], shard)
            done, owned, counts = progress(log)
            total_done += done
            total_owned += owned or 0
            history.setdefault(shard, []).append((time.time(), done))
            rate = ""
            # speed over the last ~30 minutes, ignoring checks made before progress was visible
            points = [p for p in history[shard] if p[1] > 0][-3:]
            first = points[0] if points else (time.time(), done)
            if done > first[1] and time.time() > first[0]:
                per_hour = (done - first[1]) / ((time.time() - first[0]) / 3600)
                eta = (owned - done) / per_hour if per_hour and owned else 0
                rate = f" | {per_hour:.0f} rows/h, ETA {eta:.1f} h"
            errors = log.count("Traceback")
            health = re.findall(r"search health: (limit [^,]+), blocks since last check (\d+)", log)
            blocks = f"{health[-1][0]}, +{health[-1][1]} blocks/5min" if health else "health n/a"
            note = ""
            if status in TERMINAL_BAD | {"Completed"} and machine == "Running":
                stop_compute(info["compute"])  # never leave a machine running without a job
                note = " | machine stop requested"
            elif status == "Running" and machine != "Running":
                note = f" | WARNING machine is {machine} while the job runs"
            lines.append(f"{shard} {info['compute']}({machine}) {status:<10} {done}/{owned or '?'} rows{rate}"
                         f" | {blocks}{' | ERRORS: ' + str(errors) if errors else ''}{note} | {counts}")
        lines.append(f"total {total_done}/{total_owned or '?'}")
        report = "\n".join(lines)
        print(report, flush=True)
        with open(RUN_DIR / "monitor.log", "a", encoding="utf-8") as f:
            f.write(report + "\n")
        bad = {k: v for k, v in statuses.items() if v in TERMINAL_BAD}
        if bad:
            print("ACTION NEEDED: job(s) stopped:", bad, flush=True)
            return
        if all(s == "Completed" for s in statuses.values()):
            print("ALL JOBS COMPLETED (machines stop requested)", flush=True)
            return
        if time.time() - started + args.every * 60 > args.minutes * 60:
            print("monitor window ended (jobs still running)", flush=True)
            return
        time.sleep(args.every * 60)


if __name__ == "__main__":
    main()
