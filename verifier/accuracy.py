"""Measure accuracy against hand-checked rows (logical framework review #16 and 2.2).

    python -m verifier.accuracy sample --resolved resolver_output/resolved_master.csv --size 300
        -> hand_check_sample.csv: fill `true_domain` for every row with the company's real
           website, or NONE if it has none / is closed. Rows left blank are not scored.
    python -m verifier.accuracy score --labelled hand_check_sample.csv
        -> accuracy_report.json: precision per outcome, answer type, discovery route and proof
           type, and which rules reach the 95% bar for automatic acceptance.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
from collections import defaultdict
from pathlib import Path

from .normalization import normalize_domain, registered_domain

LABEL_COLUMNS = ["true_domain", "notes"]
SAMPLE_COLUMNS = ["source_row_number", "original_company_name", "Sales Territory Name", "country code", "Domain Status",
                  "Domain URL", "new_domain_url", "suggested_domain", "decision", "outcome", "answer_type", "found_via",
                  "accept_rule", "proof_level", "proof_summary", "register_name", "reason"]
AUTO_ACCEPT_BAR = 0.95


def _site(value: str) -> str:
    host = normalize_domain(value).normalized_domain if (value or "").strip() else ""
    return registered_domain(host) or host


def sample(resolved: Path, size: int, out: Path, seed: int = 42) -> None:
    with open(resolved, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    strata: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        strata[(row.get("country code", ""), row.get("decision", ""), row.get("outcome", ""))].append(row)
    rng = random.Random(seed)
    picked: list[dict] = []
    # Spread across every country x outcome group first, then fill in proportion to size.
    for members in strata.values():
        rng.shuffle(members)
        picked.append(members[0])
    remaining = [r for members in strata.values() for r in members[1:]]
    rng.shuffle(remaining)
    picked += remaining[:max(0, size - len(picked))]
    picked = sorted(picked[:size], key=lambda r: int(r["source_row_number"]))
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=SAMPLE_COLUMNS + LABEL_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows({**r, "true_domain": "", "notes": ""} for r in picked)
    print(f"Wrote {len(picked)} rows from {len(strata)} country/outcome groups to {out}")


def score(labelled: Path, out: Path) -> dict:
    with open(labelled, encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.DictReader(f) if (r.get("true_domain") or "").strip()]  # blank = not labelled yet
    groups: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    missed = no_answer_correct = labelled_count = 0
    for row in rows:
        labelled_count += 1
        label = row["true_domain"].strip()
        truth = "" if label.upper() == "NONE" else _site(label)
        if row["decision"] == "ACCEPT":
            correct = int(bool(truth) and truth == _site(row.get("new_domain_url", "")))
            proof_types = {part.split("[")[0].strip() for part in row.get("proof_summary", "").split(";") if part.strip()}
            keys = [("accept_rule", row.get("accept_rule", "")), ("outcome", row["outcome"]), ("answer_type", row.get("answer_type", "")),
                    ("found_via", row.get("found_via", "")), ("proof_level", row.get("proof_level", ""))]
            keys += [("proof_type", t) for t in proof_types]
            for kind, value in keys:
                stats = groups[kind][value or "(blank)"]
                stats[0] += correct
                stats[1] += 1
        elif truth:
            missed += 1  # a website existed but the row was not accepted
        else:
            no_answer_correct += 1
    report = {"labelled_rows": labelled_count,
              "accepted_rows": sum(r["decision"] == "ACCEPT" for r in rows),
              "not_accepted_but_site_exists": missed,
              "not_accepted_and_no_site": no_answer_correct,
              "precision": {}, "rules_meeting_auto_accept_bar": [], "auto_accept_rules": []}
    for kind, values in groups.items():
        report["precision"][kind] = {v: {"correct": c, "total": t, "precision": round(c / t, 3)} for v, (c, t) in values.items()}
        for value, (c, t) in values.items():
            if t >= 10 and c / t >= AUTO_ACCEPT_BAR:
                report["rules_meeting_auto_accept_bar"].append(f"{kind}={value} ({c}/{t})")
                if kind == "accept_rule":
                    report["auto_accept_rules"].append(value)  # read by master_resolver --calibration
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Hand-check sampling and accuracy scoring")
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("sample")
    s.add_argument("--resolved", default="resolver_output/resolved_master.csv")
    s.add_argument("--size", type=int, default=300)
    s.add_argument("--out", default="resolver_output/hand_check_sample.csv")
    s.add_argument("--seed", type=int, default=42)
    c = sub.add_parser("score")
    c.add_argument("--labelled", default="resolver_output/hand_check_sample.csv")
    c.add_argument("--out", default="resolver_output/accuracy_report.json")
    args = parser.parse_args()
    if args.command == "sample":
        sample(Path(args.resolved), args.size, Path(args.out), args.seed)
    else:
        score(Path(args.labelled), Path(args.out))


if __name__ == "__main__":
    main()
