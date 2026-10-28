"""Copy the run's state out of the machine's local disk into the job's output storage.

SQLite databases are copied with SQLite's own backup API (a consistent snapshot even while
workers are writing), checked, then renamed into place, so the output always holds a usable
copy: a snapshot that fails SQLite's integrity check never replaces the previous good one.

    python backup.py WORK OUT [GLOB]     (GLOB selects the databases; default *.sqlite)
"""
import shutil
import sqlite3
import sys
from pathlib import Path


def backup(work: Path, out: Path, pattern: str = "*.sqlite") -> None:
    out.mkdir(parents=True, exist_ok=True)
    for db in work.glob(pattern):
        tmp = out / (db.name + ".tmp")
        src, dst = sqlite3.connect(db, timeout=120), sqlite3.connect(tmp)
        try:
            src.backup(dst)
            ok = dst.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        except sqlite3.DatabaseError:
            ok = False
        finally:
            src.close()
            dst.close()
        if not ok:
            tmp.unlink(missing_ok=True)
            print(f"WARNING: snapshot of {db.name} failed its integrity check; previous copy kept", flush=True)
            continue
        tmp.replace(out / db.name)
    for pattern in ("*.csv", "*.xlsx", "*.json"):
        for f in work.glob(pattern):
            shutil.copy2(f, out / f.name)
    if (work / "logs").exists():
        shutil.copytree(work / "logs", out / "logs", dirs_exist_ok=True)


if __name__ == "__main__":
    backup(Path(sys.argv[1]), Path(sys.argv[2]), *(sys.argv[3:4]))
    print(f"backup saved to {sys.argv[2]}", flush=True)
