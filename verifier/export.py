"""Local result, evidence, review, and quality-summary exports."""
from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path


APPENDED_COLUMNS = [
    "input_row_id", "organization_for_verification", "company_name_used", "decision", "outcome", "reason",
    "answer_type", "found_via", "proof_level", "current_website", "confidence",
    "original_domain", "normalized_domain", "registered_domain", "requested_url", "final_url",
    "final_registered_domain", "live_host", "http_status", "dns_status", "tls_valid", "website_title",
    "checks", "proofs", "evidence_json", "fetch_method", "fetch_attempts", "checked_at",
]
# Riskiest first: rejections would change or drop a domain; reviews need a person.
REVIEW_PRIORITY = {"MISMATCH": 0, "GOVERNMENT_SITE": 1, "REDIRECT_UNCONFIRMED": 2, "INSUFFICIENT_PROOF": 3,
                   "NO_PROOF": 4, "BLOCKED": 5, "TEMP_UNREACHABLE": 6, "UNREADABLE": 7}


def _csv_value(value: object) -> object:
    return json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value


INTERNAL_KEYS = {"classification", "verification_status", "decision_reason"}


def write_outputs(rows: list[dict], output: str | Path, review: str | Path,
                  evidence_path: str | Path, summary_path: str | Path,
                  unique_domains: int) -> dict:
    appended = set(APPENDED_COLUMNS) | INTERNAL_KEYS
    original_columns = [k for k in rows[0] if k not in appended] if rows else []
    columns = original_columns + APPENDED_COLUMNS
    for path in (output, review):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows({k: _csv_value(r.get(k)) for k in columns} for r in rows)
    review_rows = [r for r in rows if r.get("decision") in ("REVIEW", "REJECT")]
    review_rows.sort(key=lambda r: (0 if r.get("decision") == "REJECT" else 1,
                                    REVIEW_PRIORITY.get(r.get("outcome"), 99), r.get("input_row_id", 0)))
    with open(review, "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows({k: _csv_value(r.get(k)) for k in columns} for r in review_rows)
    with open(evidence_path, "w", encoding="utf-8") as file:
        for row in rows:
            for index, item in enumerate(row.get("evidence_json", [])):
                file.write(json.dumps({
                    "input_row_id": row.get("input_row_id"),
                    "organization": row.get("company_name_used", ""),
                    "original_domain": row.get("original_domain", ""),
                    "final_url": row.get("final_url", ""),
                    "decision": row.get("decision", ""), "outcome": row.get("outcome", ""),
                    "evidence_index": index, **item,
                    "checked_timestamp": row.get("checked_at", ""),
                }, ensure_ascii=False) + "\n")
    total = len(rows)
    decisions = Counter(r.get("decision") for r in rows)
    outcomes = Counter(r.get("outcome") for r in rows)
    domains = {r.get("normalized_domain") for r in rows if r.get("normalized_domain")}
    fetched = {r["normalized_domain"] for r in rows if r.get("normalized_domain") and r.get("http_status")}
    attempted = {r["normalized_domain"] for r in rows if r.get("normalized_domain")
                 and (r.get("http_status") or r.get("fetch_attempts") or r.get("dns_status") not in (None, "", "NOT_CHECKED"))}
    with_proof = sum(r.get("decision") == "ACCEPT" and r.get("proof_level") in ("STRONG", "SUPPORTING") for r in rows)
    summary = {
        "total_rows": total,
        "unique_domains": unique_domains,
        "domains_attempted": len(attempted),
        "domains_successfully_fetched": len(fetched),
        "domains_not_attempted": len(domains - attempted),
        "fetch_success_rate": round(len(fetched) / len(attempted), 4) if attempted else None,
        "decisions": dict(decisions),
        "outcomes": dict(outcomes),
        "answer_types": dict(Counter(r.get("answer_type") for r in rows if r.get("answer_type"))),
        "accepted_with_proof_rate": round(with_proof / total, 4) if total else 0.0,
        "strong_proof_rate": round(sum(r.get("proof_level") == "STRONG" and r.get("decision") == "ACCEPT" for r in rows) / total, 4) if total else 0.0,
        "review_or_reject_rate": round(len(review_rows) / total, 4) if total else 0.0,
        "ground_truth_accuracy": None,
    }
    Path(summary_path).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary
