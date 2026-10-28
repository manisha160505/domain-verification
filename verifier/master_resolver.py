"""Resolve the work queue: verify each row's domain and find the correct one where needed.

    python -m verifier.master_resolver --limit 200                 # pilot, priority order
    python -m verifier.master_resolver                             # whole queue (resumable)
    python -m verifier.master_resolver --export-only               # rebuild outputs from saved results

Rows are processed in priority order (UNVERIFIED_INACTIVE, NOT_FOUND, REPLACED_INACTIVE,
PRE_EXISTING_ACTIVE). For each row the supplied `Domain URL` is checked against the company
(`original_company_name`, falling back to the sales territory name). If it cannot be the
answer (missing, dead, parked, mismatched) or is unproven, the current website is searched for
with free tools and every candidate must prove the company before it is accepted.

Inputs are never modified; everything is written to --output-dir. Progress is saved per row,
so an interrupted run continues where it stopped.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import hashlib
import logging
import os
import re
import sqlite3
import subprocess
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .cache import SQLiteCache
from .config import Config
from .decision import ACCEPT, REJECT, REVIEW, NEEDS_DISCOVERY, STRONG, decide, row_context
from .discovery import Discoverer, SearchCache, WebSearch
from .fetcher import TieredFetcher, quiet_scrapling_logs
from .matching import core_name, fold
from .models import DomainRecord
from .normalization import normalize_domain, registered_domain
from .pipeline import investigate_domain
from .ratelimit import SharedRateLimiter
from .registers import RegisterClient

LOG = logging.getLogger("master_resolver")
PRIORITY = {"UNVERIFIED_INACTIVE": 1, "NOT_FOUND": 2, "REPLACED_INACTIVE": 3, "PRE_EXISTING_ACTIVE": 4,
            "VERIFIED_ACTIVE": 5}
# Supplied domains that are readable but unproven also get a search for a proven alternative.
SEARCH_WHEN_UNPROVEN = {"INSUFFICIENT_PROOF", "NO_PROOF", "REDIRECT_UNCONFIRMED", "UNREADABLE"}
DEAD_OUTCOMES = {"DOMAIN_NOT_EXIST", "PARKED", "SITE_GONE", "INVALID_DOMAIN", "NO_DOMAIN_SUPPLIED"}
OUTPUT_COLUMNS = [
    "new_domain_url", "domain_changed", "decision", "outcome", "reason", "answer_type", "found_via",
    "proof_level", "proof_summary", "evidence_url", "supplied_domain_outcome", "supplied_domain_reason",
    "candidates_tried", "successor", "accept_rule", "register_name", "register_status", "register_url",
    "tls_valid", "checked_at", "code_version",
]


def code_version() -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                             cwd=Path(__file__).parent, timeout=10).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "."], capture_output=True, text=True,
                               cwd=Path(__file__).parent, timeout=10).stdout.strip()
        return (sha or "unknown") + ("-modified" if dirty else "")
    except Exception:
        return "unknown"


def host_of(value: str) -> str:
    return normalize_domain(value).normalized_domain if (value or "").strip() else ""


def worker_of(source_row_number: int, num_workers: int) -> int:
    """Owner of a row: a scrambled (multiplicative hash) row number modulo the worker count.
    Plain `% num_workers` would leave workers idle when the input is itself a shard made with a
    modulo (e.g. shard 1 = multiples of 4 -> only even remainders mod 10)."""
    return ((source_row_number * 2654435761) % 4294967296 >> 7) % num_workers


def partition(queue: list[dict], num_workers: int, worker_id: int) -> list[dict]:
    """Rows owned by one worker. Every row has exactly one owner, ownership never depends on run
    order or progress, workers get near-equal shares even for sharded input, and each worker's
    rows keep the queue's priority order."""
    if not 0 <= worker_id < num_workers:
        raise ValueError(f"worker_id must be 0..{num_workers - 1}")
    return [r for r in queue if worker_of(int(r["source_row_number"]), num_workers) == worker_id]


class StateStore:
    """Per-row results keyed by source_row_number (one saved row = one finished row), shared by
    all worker processes, plus a per-worker progress table. A restarted worker skips its saved
    rows and continues from its first unfinished row."""

    def __init__(self, path: Path):
        for attempt in range(10):  # several workers may open the database at the same moment
            try:
                self._open(path)
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) or attempt == 9:
                    raise
                time.sleep(1 + attempt)

    def _open(self, path: Path) -> None:
        self.db = sqlite3.connect(path, timeout=120.0)
        self.db.execute("PRAGMA busy_timeout=120000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS results (source_row_number INTEGER PRIMARY KEY, payload TEXT, finished_at TEXT)")
        if "worker_id" not in {row[1] for row in self.db.execute("PRAGMA table_info(results)")}:
            self.db.execute("ALTER TABLE results ADD COLUMN worker_id INTEGER")
        self.db.execute("""CREATE TABLE IF NOT EXISTS progress (worker_id INTEGER PRIMARY KEY, num_workers INTEGER,
                           assigned INTEGER, done INTEGER, last_row INTEGER, status TEXT, pid INTEGER,
                           started_at TEXT, updated_at TEXT)""")
        self.db.commit()

    def done(self) -> set[int]:
        return {r[0] for r in self.db.execute("SELECT source_row_number FROM results")}

    def put(self, row_number: int, payload: dict, worker_id: int | None = None) -> None:
        self.db.execute("INSERT OR REPLACE INTO results (source_row_number, payload, finished_at, worker_id) VALUES (?,?,?,?)",
                        (row_number, json.dumps(payload, ensure_ascii=False, default=str),
                         datetime.now(timezone.utc).isoformat(), worker_id))
        self.db.commit()

    def all(self) -> dict[int, dict]:
        return {r[0]: json.loads(r[1]) for r in self.db.execute("SELECT source_row_number, payload FROM results")}

    def set_progress(self, worker_id: int, num_workers: int, assigned: int, done: int, last_row: int | None,
                     status: str, started_at: str = "") -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute("""INSERT INTO progress VALUES (?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(worker_id) DO UPDATE SET num_workers=excluded.num_workers,
                           assigned=excluded.assigned, done=excluded.done, last_row=excluded.last_row,
                           status=excluded.status, pid=excluded.pid, updated_at=excluded.updated_at""",
                        (worker_id, num_workers, assigned, done, last_row, status, os.getpid(), started_at or now, now))
        self.db.commit()

    def progress(self) -> list[dict]:
        cols = ["worker_id", "num_workers", "assigned", "done", "last_row", "status", "pid", "started_at", "updated_at"]
        return [dict(zip(cols, r)) for r in self.db.execute(f"SELECT {', '.join(cols)} FROM progress ORDER BY worker_id")]

    def close(self) -> None:
        self.db.close()


class Inspector:
    """Fetch each host once (in-flight requests shared) with a persistent domain cache."""

    def __init__(self, config: Config, cache: SQLiteCache):
        self.config = config
        self.cache = cache
        self.fetcher = TieredFetcher(config)
        self._inflight: dict[str, asyncio.Future] = {}

    async def __call__(self, host: str) -> DomainRecord:
        host = host_of(host) or host
        cached = await self.cache.get(host)
        if cached:
            return cached
        if host in self._inflight:
            return await self._inflight[host]
        future = asyncio.get_running_loop().create_future()
        self._inflight[host] = future
        try:
            record = await investigate_domain(host, self.config, self.fetcher)
            # Temporary failures (DNS or a resolved host that gave no response) are not cached,
            # so a later run retries them.
            transient = record.dns_status == "TEMPORARY" or (record.dns_status == "RESOLVED" and not record.http_status)
            if not transient:
                await self.cache.put(record)
            future.set_result(record)
            return record
        except Exception as exc:
            future.set_exception(exc)
            raise
        finally:
            self._inflight.pop(host, None)


def _summarise(result: dict) -> tuple[str, str]:
    proofs = result.get("proofs") or []
    summary = "; ".join(f"{p['type']}[{p['level'][0]}]: {p['text'][:80]}" for p in proofs if p["level"] != "HINT")
    url = next((p["url"] for p in proofs if p.get("url") and p["level"] != "HINT"), "")
    return summary[:1000], url


def same_site(host: str, supplied: str) -> bool:
    """True when one host is the other or a subdomain of it (www.x.com, usa.x.com vs x.com).
    Sibling subdomains (a.wixsite.com vs b.wixsite.com) are different sites."""
    host, supplied = host.lower().removeprefix("www."), supplied.lower().removeprefix("www.")
    return bool(host and supplied) and (host == supplied or host.endswith("." + supplied) or supplied.endswith("." + host))


async def resolve_row(row: dict, inspect: Inspector, discoverer: Discoverer, version: str,
                      registers: RegisterClient | None = None) -> dict:
    register = None
    if registers is not None and not row.get("_tax_id_untrusted"):
        try:
            register = await registers.lookup(row.get("country code", ""), row.get("tax_id", ""))
        except Exception as exc:
            LOG.warning("row %s: register lookup failed: %s", row.get("source_row_number"), exc)
    row = {**row, "_register": register or {}}
    supplied = (row.get("Domain URL") or "").strip()
    legacy = (row.get("Original Legacy Domain") or "").strip()
    normalized = normalize_domain(supplied)
    record = None
    if normalized.normalized_domain:
        try:
            record = await inspect(normalized.normalized_domain)
        except Exception as exc:
            LOG.warning("row %s: inspecting %s failed: %s", row.get("source_row_number"), supplied, exc)
    via = "SUPPLIED"
    if record and record.live_host.startswith("www.") and not normalized.normalized_domain.startswith("www."):
        via = "WWW_VARIANT"  # the original domain works with www.; it still has to prove ownership
    verdict = decide(row, normalized, record, found_via=via)
    final = dict(verdict)
    final["supplied_domain_outcome"] = verdict["outcome"]
    final["supplied_domain_reason"] = verdict["reason"]
    final["new_domain"] = host_of(verdict.get("current_website", "")) if verdict["decision"] == ACCEPT else ""

    needs_search = verdict["outcome"] in NEEDS_DISCOVERY or (
        verdict["outcome"] in SEARCH_WHEN_UNPROVEN and verdict["decision"] == REVIEW)
    if needs_search and verdict["outcome"] != "NO_NAME":
        dead = verdict["outcome"] in DEAD_OUTCOMES
        exclude = tuple(h for h in (normalized.normalized_domain if verdict["decision"] == REJECT else "",) if h)
        registry_status = "CLOSED" if (register or {}).get("status") == "CLOSED" else row.get("registry_status_normalised", "")
        found = await discoverer.discover(row, legacy_domain=legacy or supplied, registry_status=registry_status,
                                          exclude_hosts=exclude, wayback=dead)
        final["candidates_tried"] = found.get("candidates_judged", [])
        final["successor"] = found.get("successor", "")
        if found.get("suggested"):
            final["suggested_domain"] = found["suggested"]  # closest unproven candidate, for a reviewer
        if found["decision"] == ACCEPT:
            same_as_supplied = host_of(found["new_domain"]) == normalized.normalized_domain
            if verdict["decision"] == REVIEW and not same_as_supplied:
                # The supplied site is live and was not disproven (often it names the company
                # only in the local language), so a proven alternative is suggested for a
                # reviewer instead of replacing it automatically.
                final.update(decision=REVIEW, outcome="REPLACEMENT_SUGGESTED",
                             reason=f"Supplied site unproven ({verdict['outcome']}); suggested {found['new_domain']}: {found['reason']}",
                             suggested_domain=found["new_domain"], proofs=found.get("proofs", []),
                             proof_level=found.get("proof_level"), found_via=found.get("found_via"))
            else:
                final.update({k: found.get(k) for k in ("decision", "outcome", "reason", "answer_type", "found_via",
                                                        "proof_level", "proofs", "checks", "tls_valid", "checked_at",
                                                        "accept_rule")})
                final["new_domain"] = found["new_domain"]
                if verdict["decision"] != ACCEPT and not same_as_supplied and found["outcome"] == "VERIFIED":
                    final["outcome"] = "FOUND" if verdict["outcome"] == "NO_DOMAIN_SUPPLIED" else "REPLACED"
        elif found["outcome"] == "COMPANY_CLOSED" or verdict["decision"] != REVIEW:
            final.update(decision=found["decision"], outcome=found["outcome"],
                         reason=f"{verdict['reason']} {found['reason']}".strip(), found_via=found.get("found_via"))
        else:
            final["reason"] = f"{verdict['reason']} Search: {found['reason']}"
    summary, url = _summarise(final)
    if final.get("new_domain") and same_site(host_of(final["new_domain"]), normalized.normalized_domain):
        # e.g. mizuno.com answering from usa.mizuno.com (a geo-redirect): the supplied domain stands
        final["new_domain"] = normalized.normalized_domain
    new_url = f"https://{final['new_domain']}" if final.get("new_domain") else ""
    return {
        "source_row_number": int(row["source_row_number"]),
        "new_domain_url": new_url,
        "domain_changed": bool(new_url) and host_of(final["new_domain"]) != normalized.normalized_domain
                          and registered_domain(final["new_domain"]) != (normalized.registered_domain if final.get("found_via") == "REDIRECT" else ""),
        "decision": final["decision"], "outcome": final["outcome"], "reason": final["reason"],
        "answer_type": final.get("answer_type", ""), "found_via": final.get("found_via", ""),
        "proof_level": final.get("proof_level", "NONE"), "proof_summary": summary, "evidence_url": url,
        "supplied_domain_outcome": final["supplied_domain_outcome"],
        "supplied_domain_reason": final["supplied_domain_reason"],
        "candidates_tried": "; ".join(final.get("candidates_tried", []) or []),
        "successor": final.get("successor", ""), "suggested_domain": final.get("suggested_domain", ""),
        "tls_valid": final.get("tls_valid"), "checked_at": final.get("checked_at") or datetime.now(timezone.utc).isoformat(),
        "code_version": version, "accept_rule": final.get("accept_rule", "") if final["decision"] == ACCEPT else "",
        "register_name": (register or {}).get("legal_name") or (register or {}).get("local_name", ""),
        "register_status": (register or {}).get("status", ""), "register_url": (register or {}).get("register_url", ""),
    }


# --------------------------------------------------------------------------- outputs
def load_overrides(path: str | None) -> dict[int, dict]:
    if not path:
        return {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        return {int(r["source_row_number"]): r for r in csv.DictReader(f) if (r.get("source_row_number") or "").strip()}


def apply_override(result: dict, override: dict) -> dict:
    decision = (override.get("decision") or "").strip().upper()
    domain = host_of(override.get("domain") or "")
    if decision not in (ACCEPT, REJECT, REVIEW):
        return result
    return {**result, "decision": decision, "outcome": "REVIEWER_OVERRIDE",
            "new_domain_url": f"https://{domain}" if domain and decision == ACCEPT else "",
            "reason": f"Reviewer: {override.get('note', '').strip()} (was {result.get('outcome')})",
            "found_via": "REVIEWER"}


def consistency_report(rows: list[dict]) -> list[dict]:
    """Flag the same company given different websites, and one website given to many
    unrelated companies (unless they share the sales-territory group)."""
    flags: list[dict] = []
    by_company: dict[tuple, set] = defaultdict(set)
    by_site: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r.get("decision") != ACCEPT or not r.get("new_domain_url"):
            continue
        site = registered_domain(host_of(r["new_domain_url"]))
        key = (fold(core_name(row_context(r)["name"])), r.get("country code", ""))
        by_company[key].add(site)
        by_site[site].append(r)
    for (company, country), sites in by_company.items():
        if len(sites) > 1:
            flags.append({"issue": "SAME_COMPANY_DIFFERENT_SITES", "company": company, "country": country,
                          "sites": "; ".join(sorted(sites)), "rows": ""})
    for site, members in by_site.items():
        companies = {fold(core_name(row_context(r)["name"])) for r in members}
        groups = {fold(core_name(r.get("Sales Territory Name", ""))) for r in members}
        own = {fold(core_name(row_context(r)["name"])) for r in members if r.get("answer_type") == "OWN_SITE"}
        if len(own) >= 2:
            flags.append({"issue": "ONE_SITE_OWN_SITE_FOR_SEVERAL_COMPANIES", "company": " | ".join(sorted(own))[:300],
                          "country": "", "sites": site,
                          "rows": "; ".join(str(r["source_row_number"]) for r in members[:50])})
        if len(companies) >= 5 and len(groups) > 1:
            flags.append({"issue": "ONE_SITE_MANY_COMPANIES", "company": f"{len(companies)} companies",
                          "country": "", "sites": site,
                          "rows": "; ".join(str(r["source_row_number"]) for r in members[:50])})
    return flags


def updated_status(result: dict | None) -> str:
    """Plain status for the sheet's `Updated Domain Status` column."""
    if not result:
        return "PENDING"
    decision, outcome = result.get("decision"), result.get("outcome")
    if outcome == "REVIEWER_OVERRIDE":
        return {"ACCEPT": "REVIEWER_VERIFIED", "REJECT": "REVIEWER_REJECTED"}.get(decision, "NEEDS_REVIEW")
    if decision == ACCEPT:
        if outcome == "FOUND":
            return "FOUND_VERIFIED"
        if outcome == "MERGED_INTO":
            return "MERGED_SUCCESSOR_SITE"
        return "REPLACED_VERIFIED" if result.get("domain_changed") else "VERIFIED_ACTIVE"
    if outcome == "COMPANY_CLOSED":
        return "COMPANY_CLOSED"
    if outcome in ("REPLACEMENT_SUGGESTED", "UNCALIBRATED_ACCEPT"):
        return "NEEDS_REVIEW_SUGGESTION"
    if outcome in ("TEMP_UNREACHABLE", "BLOCKED", "UNREADABLE"):
        return "RETRY_UNREACHABLE"
    if outcome == "NOT_FOUND":
        return "NOT_FOUND"
    return "NEEDS_REVIEW"


def _file_sha256(path: Path | None) -> str:
    if not path or not path.exists():
        return ""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


_EXCEL_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def excel_cell(value):
    """A value Excel accepts: lists/dicts as text, control characters (from scraped pages) removed."""
    if isinstance(value, (list, dict)):
        value = str(value)
    return _EXCEL_ILLEGAL.sub("", value) if isinstance(value, str) else value


def _save_workbook(book, target: Path) -> Path:
    try:
        book.save(target)
        return target
    except PermissionError:  # usually the file is open in Excel
        fallback = target.with_name(f"{target.stem}_{datetime.now():%Y%m%d_%H%M%S}{target.suffix}")
        book.save(fallback)
        LOG.warning("%s is open/locked; saved %s instead", target.name, fallback.name)
        return fallback


def load_calibration(path: str | None) -> set[str] | None:
    """Accept-rules that met the 95% bar on the hand-checked sample (verifier.accuracy score)."""
    if not path:
        return None
    return set(json.loads(Path(path).read_text(encoding="utf-8")).get("auto_accept_rules", []))


def apply_calibration(result: dict, allowed: set[str] | None) -> dict:
    """Framework review 2.2: accept automatically only with rules proven >=95% right on the
    hand-checked sample; other acceptances go to review with the domain as a suggestion."""
    if allowed is None or result.get("decision") != ACCEPT or result.get("outcome") == "REVIEWER_OVERRIDE":
        return result
    if result.get("accept_rule") in allowed:
        return result
    return {**result, "decision": REVIEW, "outcome": "UNCALIBRATED_ACCEPT",
            "suggested_domain": host_of(result.get("new_domain_url", "")), "new_domain_url": "",
            "reason": f"Accept rule '{result.get('accept_rule')}' has not met the 95% bar on the hand-checked sample. "
                      + str(result.get("reason", ""))}


def write_outputs(queue: list[dict], results: dict[int, dict], overrides: dict[int, dict], out: Path,
                  input_path: Path | None = None, excel: bool = True, calibration: set[str] | None = None,
                  not_processed: set[str] | None = None) -> dict:
    """Write the live sheet (every queue row; unprocessed rows are PENDING), the review queue,
    the consistency report and the summary. Safe to call while workers are running."""
    extra = ["Updated Domain URL", "Updated Domain Status"] + OUTPUT_COLUMNS + ["suggested_domain"]
    columns = (list(queue[0].keys()) if queue else []) + extra
    sheet: list[dict] = []
    merged: list[dict] = []
    for row in queue:
        number = int(row["source_row_number"])
        result = results.get(number)
        if result is not None:
            result = apply_calibration(result, calibration)
        if result is not None and number in overrides:
            result = apply_override(result, overrides[number])
        values = {k: (result or {}).get(k, "") for k in OUTPUT_COLUMNS + ["suggested_domain"]}
        accepted = bool(result) and result.get("decision") == ACCEPT
        status = updated_status(result)
        if result is None and not_processed and row.get("Domain Status") in not_processed:
            status = "NOT_PROCESSED"  # group deliberately left out of this run; domain unchanged
        full = {**row, **values, "Updated Domain Status": status,
                "Updated Domain URL": result.get("new_domain_url", "") if accepted else ""}
        sheet.append(full)
        if result is not None:
            merged.append(full)
    with open(out / "resolved_master.csv.tmp", "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(sheet)
    try:
        os.replace(out / "resolved_master.csv.tmp", out / "resolved_master.csv")
    except PermissionError:
        LOG.warning("resolved_master.csv is open/locked; latest sheet left in resolved_master.csv.tmp")
    # Riskiest first: domain changes, then rejections, then reviews.
    risk = lambda r: (0 if r["decision"] == ACCEPT and r["domain_changed"] else 1 if r["decision"] == REJECT else 2,
                      int(r.get("priority") or 9), int(r["source_row_number"]))
    review = sorted((r for r in merged if r["decision"] != ACCEPT or r["domain_changed"]), key=risk)
    with open(out / "review_queue.csv", "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(review)
    flags = consistency_report(merged)
    with open(out / "consistency_report.csv", "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["issue", "company", "country", "sites", "rows"])
        writer.writeheader()
        writer.writerows(flags)
    if excel:
        try:
            import openpyxl
            book = openpyxl.Workbook(write_only=True)
            for title, data in (("resolved", sheet), ("review_queue", review)):
                ws = book.create_sheet(title)
                ws.append(columns)
                for r in data:
                    ws.append([excel_cell(r.get(c, "")) for c in columns])
            _save_workbook(book, out / "resolved_master.xlsx")
        except Exception as exc:
            LOG.warning("Excel export skipped: %s", exc)
    by_status = defaultdict(Counter)
    for r in merged:
        by_status[r.get("Domain Status", "")][f"{r['decision']}:{r['outcome']}"] += 1
    total = len(merged)
    proven = sum(r["decision"] == ACCEPT and r["proof_level"] in (STRONG, "SUPPORTING") for r in merged)
    summary = {
        "rows_resolved": total, "rows_in_queue": len(queue), "rows_pending": len(queue) - total,
        "updated_domain_status": dict(Counter(r["Updated Domain Status"] for r in sheet)),
        "decisions": dict(Counter(r["decision"] for r in merged)),
        "outcomes": dict(Counter(r["outcome"] for r in merged)),
        "found_via": dict(Counter(r["found_via"] for r in merged if r["decision"] == ACCEPT)),
        "answer_types": dict(Counter(r["answer_type"] for r in merged if r["decision"] == ACCEPT)),
        "domains_changed": sum(bool(r["domain_changed"]) for r in merged),
        "accepted_with_proof": proven,
        "accepted_with_proof_rate": round(proven / total, 4) if total else 0.0,
        "strong_proof_accepts": sum(r["decision"] == ACCEPT and r["proof_level"] == STRONG for r in merged),
        "by_original_domain_status": {k: dict(v) for k, v in by_status.items()},
        "consistency_flags": len(flags), "review_queue_rows": len(review),
        "reviewer_overrides_applied": sum(int(r["source_row_number"]) in overrides for r in merged),
        "code_version": code_version(), "input_file": str(input_path or ""),
        "input_sha256": _file_sha256(input_path), "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


# --------------------------------------------------------------------------- main
def untrusted_ids(rows: list[dict]) -> set[tuple[str, str, str]]:
    """IDs that cannot identify one company: placeholders (0000000000, 1111...) and IDs the
    data attaches to differently named companies (e.g. a parent's corporate number on its
    subsidiaries). Such IDs are not used for register lookups or as proof."""
    names: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for r in rows:
        for field in ("tax_id", "registration_number"):
            digits = "".join(c for c in str(r.get(field) or "") if c.isalnum())
            if len(digits) >= 6:
                names[(field, r.get("country code", ""), digits)].add(fold(core_name(r.get("original_company_name", ""))))
    bad = {k for k, v in names.items() if len(v) > 1}
    bad |= {k for k in names if len(set(k[2])) <= 2}  # 0000000000, 1212121212...
    return bad


def mark_untrusted_ids(rows: list[dict], bad: set[tuple[str, str, str]]) -> int:
    marked = 0
    for r in rows:
        for field in ("tax_id", "registration_number"):
            digits = "".join(c for c in str(r.get(field) or "") if c.isalnum())
            if (field, r.get("country code", ""), digits) in bad:
                r[f"_{field}_untrusted"] = "1"
                marked += field == "tax_id"
    return marked


def load_queue(path: Path, statuses: set[str] | None) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if statuses:
        rows = [r for r in rows if r.get("Domain Status") in statuses]
    rows.sort(key=lambda r: (PRIORITY.get(r.get("Domain Status", ""), 9), int(r["source_row_number"])))
    return rows


async def run(args) -> None:
    """Resolve rows. With --num-workers N --worker-id k this process owns only the rows with
    source_row_number % N == k (see partition); verifier.run_parallel starts all N workers."""
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    statuses = set(args.statuses.split(",")) if args.statuses else None
    queue = load_queue(Path(args.input), statuses)
    state = StateStore(out / "resolver_state.sqlite")
    overrides = load_overrides(args.overrides)
    worker_id, num_workers = args.worker_id, args.num_workers
    try:
        if not args.export_only:
            # IDs shared by different company names (in the whole queue) are not trusted.
            bad_ids = untrusted_ids(load_queue(Path(getattr(args, "id_reference", None) or args.input), None))
            LOG.info("worker %d: %d rows have an untrusted (shared/placeholder) tax ID",
                     worker_id, mark_untrusted_ids(queue, bad_ids))
            mine = partition(queue, num_workers, worker_id)
            done = state.done()
            if args.retry_unreachable:
                retry = {n for n, r in state.all().items() if r.get("outcome") in ("TEMP_UNREACHABLE", "BLOCKED", "UNREADABLE")}
                LOG.info("Retrying %d previously unreachable/blocked rows", len(retry))
                done -= retry
            todo = [r for r in mine if int(r["source_row_number"]) not in done]
            if args.limit:
                todo = todo[:args.limit]
            already = len(mine) - len([r for r in mine if int(r["source_row_number"]) not in done])
            LOG.info("worker %d/%d owns %d rows | already saved %d | to resolve now %d | resuming at row %s",
                     worker_id, num_workers, len(mine), already, len(todo),
                     todo[0]["source_row_number"] if todo else "-")
            started_at = datetime.now(timezone.utc).isoformat()
            state.set_progress(worker_id, num_workers, len(mine), already, None, "RUNNING", started_at)
            config = Config(concurrency=args.fetch_concurrency, per_host_delay_seconds=0.5,
                            timeout_seconds=args.timeout, max_evidence_pages=args.max_pages)
            domain_cache = SQLiteCache(out / "domain_cache.sqlite", config.cache_ttl_seconds)
            search_cache = SearchCache(str(out / "search_cache.sqlite"))
            search_limiter = SharedRateLimiter(str(out / "rate_limits.sqlite"), "search", args.max_searches_per_minute)
            register_limiter = SharedRateLimiter(str(out / "rate_limits.sqlite"), "register", args.max_register_lookups_per_minute)
            search = WebSearch(search_cache, concurrency=args.search_concurrency, limiter=search_limiter)
            inspect = Inspector(config, domain_cache)
            discoverer = Discoverer(search, inspect, use_wayback=not args.no_wayback)
            registers = None if args.no_registers else RegisterClient(str(out / "registers.sqlite"), limiter=register_limiter)
            version = code_version()
            rows: asyncio.Queue = asyncio.Queue()
            for row in todo:
                rows.put_nowait(row)
            started, finished, saved_total = time.time(), 0, already
            status = "STOPPED"

            async def consumer() -> None:
                # Rows are taken strictly in the worker's order; at most row_concurrency are in flight.
                nonlocal finished, saved_total
                while True:
                    try:
                        row = rows.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    number = int(row["source_row_number"])
                    try:
                        result = await resolve_row(row, inspect, discoverer, version, registers)
                    except Exception as exc:
                        LOG.exception("row %s failed (left unsaved, retried on next run): %s", number, exc)
                        continue
                    result["worker_id"] = worker_id
                    state.put(number, result, worker_id)  # saved before moving on
                    finished += 1
                    saved_total += 1
                    state.set_progress(worker_id, num_workers, len(mine), saved_total, number, "RUNNING", started_at)
                    if finished % 25 == 0 or finished == len(todo):
                        rate = finished / max(1.0, time.time() - started)
                        LOG.info("worker %d: %d/%d rows this run, %d/%d saved overall (%.2f rows/s, engines %s)",
                                 worker_id, finished, len(todo), saved_total, len(mine), rate, search.engine_counts)

            try:
                await asyncio.gather(*(consumer() for _ in range(max(1, args.row_concurrency))))
                status = "FINISHED" if saved_total >= len(mine) else "PARTIAL"
            except BaseException:
                status = "STOPPED"
                raise
            finally:
                state.set_progress(worker_id, num_workers, len(mine), saved_total, None, status, started_at)
                await search.close()
                search_cache.close()
                domain_cache.close()
                if registers is not None:
                    await registers.close()
                search_limiter.close()
                register_limiter.close()
        if num_workers == 1 or args.export_only:
            summary = write_outputs(queue, state.all(), overrides, out, input_path=Path(args.input),
                                    calibration=load_calibration(args.calibration))
            print(json.dumps(summary, indent=2))
    finally:
        state.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify and resolve domains for the work-queue master")
    parser.add_argument("--input", default="work_queue_master.csv")
    parser.add_argument("--output-dir", default="resolver_output")
    parser.add_argument("--statuses", help="comma-separated Domain Status values to include (default: all)")
    parser.add_argument("--limit", type=int, help="resolve at most N new rows this run (pilots)")
    parser.add_argument("--overrides", help="CSV of reviewer decisions: source_row_number,decision,domain,note")
    parser.add_argument("--export-only", action="store_true", help="only rebuild outputs from saved results")
    parser.add_argument("--retry-unreachable", action="store_true",
                        help="re-resolve rows whose site was temporarily unreachable, blocked or unreadable")
    parser.add_argument("--row-concurrency", type=int, default=12)
    parser.add_argument("--fetch-concurrency", type=int, default=40)
    parser.add_argument("--search-concurrency", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--max-pages", type=int, default=4)
    parser.add_argument("--no-wayback", action="store_true", help="skip Internet Archive lookups")
    parser.add_argument("--no-registers", action="store_true", help="skip official register lookups (JP/AU)")
    parser.add_argument("--max-searches-per-minute", type=float, default=20,
                        help="search-engine requests per minute for the whole machine, all workers together (0 = no limit)")
    parser.add_argument("--max-register-lookups-per-minute", type=float, default=60,
                        help="register page requests per minute for the whole machine (0 = no limit)")
    parser.add_argument("--id-reference", help="full queue file used to detect shared tax IDs (default: --input); "
                                               "needed when --input is one shard of the queue")
    parser.add_argument("--calibration", help="accuracy_report.json: only its auto_accept_rules are accepted automatically")
    parser.add_argument("--num-workers", type=int, default=1, help="total number of worker processes")
    parser.add_argument("--worker-id", type=int, default=0, help="this worker's id, 0..num-workers-1")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format=f"%(asctime)s %(levelname)s w{args.worker_id} %(name)s %(message)s")
    quiet_scrapling_logs()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        LOG.warning("worker %d interrupted; finished rows are saved and the next run resumes", args.worker_id)


if __name__ == "__main__":
    main()
