"""Run code on Colab Enterprise runtimes from this laptop, through each runtime's Jupyter API.

    python gcp/colab.py list                                   # the data-processing-N runtimes
    python gcp/colab.py exec data-processing-1 "!df -h"        # run Python (or !shell) on one runtime
    python gcp/colab.py exec all "!uptime"                     # ... on every data-processing runtime

Authentication is the gcloud login (an OAuth token sent to the runtime's proxy URL). Each call
starts its own kernel and deletes it afterwards, so the user's notebook kernels are untouched.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import websocket

SDK = Path.home() / "AppData/Local/Google/Cloud SDK/google-cloud-sdk/bin"
GCLOUD = str(SDK / "gcloud.cmd") if (SDK / "gcloud.cmd").exists() else "gcloud"
PROJECT, REGION, PREFIX = "data-processing-510610", "us-central1", "data-processing-"
_token: tuple[float, str] | None = None


def token() -> str:
    global _token
    if _token is None or time.time() - _token[0] > 1800:
        out = subprocess.run([GCLOUD, "auth", "print-access-token"], capture_output=True, text=True, timeout=120)
        _token = (time.time(), out.stdout.strip())
    return _token[1]


def runtimes(project: str = PROJECT, region: str = REGION) -> list[dict]:
    """data-processing-N runtimes, sorted by N (duplicates of a name are kept, newest last)."""
    out = subprocess.run([GCLOUD, "colab", "runtimes", "list", "--project", project, "--region", region,
                          "--format", "json"], capture_output=True, text=True, timeout=300)
    rows = [r for r in json.loads(out.stdout or "[]") if r["displayName"].startswith(PREFIX)]
    return sorted(rows, key=lambda r: (int(r["displayName"][len(PREFIX):] or 0), r["createTime"]))


def api(proxy: str, method: str, path: str, body: dict | None = None, timeout: int = 60, tries: int = 6):
    """Jupyter REST call through the runtime proxy (it answers 502/503 now and then: retried)."""
    for attempt in range(tries):
        req = Request(f"https://{proxy}{path}", method=method,
                      data=json.dumps(body).encode() if body is not None else None,
                      headers={"Authorization": f"Bearer {token()}", "Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=timeout) as resp:
                text = resp.read().decode()
                return json.loads(text) if text else None
        except HTTPError as exc:
            if exc.code < 500 or attempt == tries - 1:
                raise
        except URLError:
            if attempt == tries - 1:
                raise
        time.sleep(5 * (attempt + 1))


def execute(proxy: str, code: str, timeout: float = 600) -> str:
    """Run code in a fresh kernel on the runtime (deleted afterwards) and return everything it
    printed (or the error). Processes the code starts detached keep running after that."""
    kid = api(proxy, "POST", "/api/kernels", {"name": "python3"})["id"]
    for _ in range(30):  # wait for the kernel to be ready
        try:
            if api(proxy, "GET", f"/api/kernels/{kid}")["execution_state"] != "starting":
                break
        except HTTPError:
            pass
        time.sleep(2)
    try:
        return _run(proxy, kid, code, timeout)
    finally:
        try:
            api(proxy, "DELETE", f"/api/kernels/{kid}", tries=2)
        except Exception:
            pass


def _run(proxy: str, kid: str, code: str, timeout: float) -> str:
    ws = websocket.create_connection(f"wss://{proxy}/api/kernels/{kid}/channels",
                                     header=[f"Authorization: Bearer {token()}"], timeout=timeout)
    msg_id = uuid.uuid4().hex
    ws.send(json.dumps({"header": {"msg_id": msg_id, "username": "dv", "session": uuid.uuid4().hex,
                                   "msg_type": "execute_request", "version": "5.3"},
                        "parent_header": {}, "metadata": {}, "channel": "shell",
                        "content": {"code": code, "silent": False, "store_history": False,
                                    "user_expressions": {}, "allow_stdin": False, "stop_on_error": True}}))
    out: list[str] = []
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            msg = json.loads(ws.recv())
            if msg.get("parent_header", {}).get("msg_id") != msg_id:
                continue
            kind, content = msg["msg_type"], msg["content"]
            if kind == "stream":
                out.append(content["text"])
            elif kind in ("execute_result", "display_data"):
                out.append(content["data"].get("text/plain", "") + "\n")
            elif kind == "error":
                out.append(f"ERROR {content['ename']}: {content['evalue']}\n")
            elif kind == "status" and content["execution_state"] == "idle":
                break
    finally:
        ws.close()
    return "".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["list", "exec"])
    parser.add_argument("target", nargs="?", help="runtime display name, runtime id, or 'all'")
    parser.add_argument("code", nargs="?")
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args()
    rows = runtimes()
    if args.command == "list":
        for r in rows:
            print(f'{r["displayName"]:<22} {r["runtimeState"]:<9} {r["machineSpec"]["machineType"]:<14} '
                  f'id={r["name"].rsplit("/", 1)[1]} created={r["createTime"][:19]}')
        return
    chosen = rows if args.target == "all" else [r for r in rows if args.target in (r["displayName"], r["name"].rsplit("/", 1)[1])]
    if not chosen:
        sys.exit(f"no runtime matches {args.target}")
    for r in chosen:
        print(f"===== {r['displayName']} ({r['name'].rsplit('/', 1)[1]})", flush=True)
        try:
            print(execute(r["proxyUri"], args.code, args.timeout), flush=True)
        except Exception as exc:
            print(f"FAILED: {exc}", flush=True)


if __name__ == "__main__":
    main()
