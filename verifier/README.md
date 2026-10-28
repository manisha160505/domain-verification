# Domain-to-company verification pipeline

This pipeline tests whether a domain is officially related to a legal entity or corporate group. Reachability and name similarity are diagnostic signals, not verification.

## Input assessment

`Domains List_V2.xlsx` has 93,918 data rows and the columns `Country`, `Sales Territory ID`, `Sales Territory Name`, `Organization ID`, `Organization Name`, and `Domain Name`. It contains about 40,196 distinct hosts, 4,453 blank domains, and 5,576 values with URL paths. `harveynorman.com.au` appears 814 times. The code normalizes hosts for a shared fetch cache and retains the original value and requested path per row.

## Install and run locally

Use Python 3.12. Install `verifier/requirements.txt` from the repository root. Browser fallback also needs Chromium (`python -m playwright install chromium` and `python -m patchright install chromium`).

```bash
python -m verifier --input 'Domains List_V2.xlsx' --sample-size 75 --output-dir verifier/dry_run_output
python -m verifier --input 'Domains List_V2.xlsx' --full-run --output-dir verifier/full_output
```

The default command is a 75-row dry run. The full dataset requires `--full-run`; inspect dry-run evidence and results first. `--dynamic` and `--stealth` enable browser fallbacks. `--proxy-url` supplies a proxy for the stealth tier. `--search-ambiguous` uses free web search; `--max-external-searches` caps it. `--offline` reads cached evidence without making requests, useful for output/schema checks; it does **not** verify uncached domains.

Local outputs:

- `results.csv`: every selected input row once, source columns preserved, appended decision and provenance fields.
- `evidence.jsonl`: one structured record per evidence item.
- `review.csv`: prioritized `MISMATCH`, `PROBABLE`, `UNVERIFIED`, `BLOCKED`, and `REDIRECT` rows.
- `summary.json`: counts and rates; ground-truth accuracy stays null without labels.

The SQLite cache keeps domain pages, input path pages, and entity-level evidence separately for 30 days. Duplicate domains reuse the crawl, while each organization/domain pair receives its own decision. Browser fetches are opt-in and only used after HTTP indicates a JS shell or access protection. The crawler fetches the homepage and up to seven high-priority identity/legal pages; it does not crawl an entire site. Requests use a global concurrency bound, per-host pacing, robots checks, safe redirects, timeout, and finite retry/backoff.

## Decision rules

Decisions come from `decision.py`, the proof-ladder engine (see the root `README.md`). Every row ends as `ACCEPT`, `REJECT` or `REVIEW`, with an outcome code.

- `ACCEPT / VERIFIED` needs one strong proof, or two supporting proofs where one is the company name. Strong proofs are: the legal name in structured data, the footer, the copyright line or a legal/about page; a tax or registration number on the site; the subsidiary named on its group's site. A look-alike domain or a contact email on the site's own domain is only a hint.
- `REJECT` covers `NO_DOMAIN_SUPPLIED`, `INVALID_DOMAIN`, `DOMAIN_NOT_EXIST` (DNS says the name does not exist for both the bare domain and www.), `PARKED`, `SITE_GONE` (HTTP 410) and `MISMATCH` (the site clearly names a different owner).
- `REVIEW` covers `TEMP_UNREACHABLE`, `BLOCKED`, `UNREADABLE`, `GOVERNMENT_SITE`, `REDIRECT_UNCONFIRMED`, `INSUFFICIENT_PROOF` and `NO_PROOF`. Nothing is judged valid or mismatched without readable content.
- Redirects are judged at the destination. If the destination proves the company, it is accepted and recorded as the current website.

External search is discovery only. A search snippet is never evidence: the returned source URL is fetched and the relevant text retained. A previously reviewed external-evidence JSONL can be passed with `--external-evidence`; each entry needs `organization`, `domain`, `source_url`, `source_type`, `source_title`, `evidence_text`, `relationship`, `strength`, and `fetched_at`.

## Apify Actor (optional hosting wrapper; not used by the free resolver)

The Actor definition is [`.actor/actor.json`](../.actor/actor.json); its image uses the supplied Scrapling repository. Supply an HTTPS CSV/XLSX URL as `input_file`, or a path inside the image. `dry_run` defaults to true. Set `dry_run: false` only after reviewing the sample. The Actor writes separate named result, evidence, and review datasets, plus `PROGRESS` and `SUMMARY` records in a named key-value store. The dataset and cache names are derived from input-file contents and `run_key`. On restart it reads existing row/evidence IDs and skips published items; persisted domain and pair caches avoid recrawling. Change `run_key` to start a fresh verification run.

Example input:

```json
{
  "input_file": "https://your-storage.example/data/company-master.xlsx",
  "organization_column": "Organization Name",
  "domain_column": "Domain Name",
  "dry_run": true,
  "sample_size": 75,
  "concurrency": 20,
  "use_dynamic": false,
  "use_stealth": false,
  "use_proxy": false
}
```

If `use_proxy` is enabled, configure Apify Proxy in the Actor account; the proxy is reserved for stealth escalation.

## Validation and present environment

Run `python -m unittest discover -s verifier/tests -v`. Tests cover exact identity, subsidiary/group evidence, weak mentions, mismatch restraint, redirects, parking, blocked versus inactive DNS, duplicate-domain reuse, row preservation, and export counts.

The local host could not resolve external domains or install missing Scrapling/Apify dependencies from PyPI. Therefore the saved 75-row `dry_run_output` is an **offline schema/sample check**, with all rows unverified and zero fetched domains. It is not a live classification validation. A network-enabled Actor/local environment must run the live dry run and inspect evidence before the full dataset is processed. No full run was started here.
