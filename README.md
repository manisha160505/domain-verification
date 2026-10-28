# Company Domain Verification & Recovery

Checks whether each company's listed website really belongs to it, and finds the current website where the listed one is missing, dead or wrong. It uses free, open-source tools only, with no paid APIs.

The approach follows the two reviews in this folder, `code_level_review.md` and `logical_framework_reveiw.md`. **Nothing is accepted without proof that links the site to the specific company.**

## How it works

```
work_queue_master.csv  (built by verifier/build_work_queue.py; VERIFIED_ACTIVE rows set aside)
        │   rows in priority order: UNVERIFIED_INACTIVE → NOT_FOUND → REPLACED_INACTIVE → PRE_EXISTING_ACTIVE
        ▼
 1. Inspect the supplied domain           verifier/pipeline.py, fetcher.py, extractor.py
    • DNS: "does not exist" vs "temporary failure" (dnspython); bare domain and www., https then http
    • homepage + up to 4 about/contact/legal pages (incl. sister hosts such as corporate.example.com)
    • records certificate problems (tls_valid) instead of hiding them
        ▼
 1b. Official register lookup (free)     verifier/registers.py
    • Japan: NTA corporate-number site (13-digit tax_id) -> Japanese legal name, address, closed/merged + successor
    • Australia: ABN Lookup (11-digit ABN) -> legal name, active/cancelled, state + postcode, business names
    • local-language home pages: the site's own English page is read as well
        ▼
 2. Decide with the proof ladder          verifier/decision.py
    STRONG      legal name in structured data / footer / copyright / legal page; tax or registration
                number on the site; subsidiary named on its group's site        → accept on its own
    SUPPORTING  company or brand name in the site title/heading/site name; country by full name,
                city, phone code or ccTLD; registered address (postcode/state/city) → two, one a name
                (register legal names and Japanese names count as the company's name;
                 a common one-word name needs the address or an ID, not just the country)
    HINT        domain looks like the name; email on own domain; territory = domain → never enough
    • unreadable / blocked / near-empty pages are never judged valid or mismatched
    • mismatch only when the site clearly names a different owner; government sites → review
    • redirects are judged at the destination
        ▼
 3. Find the current website if needed    verifier/discovery.py
    • Internet Archive copy of a dead domain ("we have moved", links, canonical)
    • free web search (Yahoo → DuckDuckGo → Bing), three wordings, top 5 results each,
      directories/news/social skipped, results must mention the company
    • every candidate must pass the decision engine with a HIGHER bar (name AND country)
    • closed / merged companies: registry status + "acquired by / now part of" wording
        ▼
 4. Write new files only                  verifier/master_resolver.py
    resolver_output/resolved_master.csv/.xlsx, review_queue.csv (riskiest first),
    consistency_report.csv, summary.json — inputs are never modified
```

Every row ends as **ACCEPT**, **REJECT** or **REVIEW**. Each row also carries:
- an outcome code, such as `VERIFIED`, `FOUND` (no domain before), `REPLACED`, `DOMAIN_NOT_EXIST`, `INSUFFICIENT_PROOF` or `REPLACEMENT_SUGGESTED`
- the answer type (`OWN_SITE`, `GROUP_SITE`, `REGIONAL_SITE`, `SUCCESSOR_SITE`)
- how the answer was found (`SUPPLIED`, `WWW_VARIANT`, `REDIRECT`, `SEARCH`, `WAYBACK`)
- the proofs behind it

**Replacement rules:**
- **Automatic:** the supplied domain is replaced only if it is missing, does not exist, is parked, or names a different owner.
- **Suggestion only:** a live supplied site that simply couldn't be proven (often because it names the company only in Japanese/Thai/Korean) keeps its domain. Any proven alternative is offered as `REPLACEMENT_SUGGESTED` for a reviewer.

## Setup (Windows / macOS / Linux)

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -r verifier/requirements.txt
```

## Usage

```bash
# 1. Build the work queue from the final super merged master (already done once)
python -m verifier.build_work_queue

# 2. Pilot: resolve the first 200 rows in priority order, then inspect resolver_output/
python -m verifier.master_resolver --limit 200

# 3. Full run with N parallel worker processes (resumable)
python -m verifier.run_parallel --workers 2 --limit-per-worker 2   # tiny test first (4 rows)
python -m verifier.run_parallel --workers 6                        # whole queue
python -m verifier.run_parallel --status                           # progress per worker
#    Worker k owns rows with source_row_number % N == k (no overlap); every finished row is saved
#    immediately, so a stopped/crashed worker resumes at its first unsaved row. The sheet
#    resolver_output/resolved_master.csv is refreshed every 5 min with `Updated Domain Status`.
python -m verifier.master_resolver                               # single process alternative
python -m verifier.master_resolver --statuses NOT_FOUND          # one group only
python -m verifier.master_resolver --retry-unreachable           # re-check temporarily unreachable rows
python -m verifier.master_resolver --overrides reviewer_decisions.csv --export-only   # apply reviewer decisions

# 4. Measure accuracy: hand-check a stratified sample, then score it
python -m verifier.accuracy sample --size 300      # fill true_domain (or NONE) in the CSV
python -m verifier.accuracy score                  # -> resolver_output/accuracy_report.json
#    Accuracy gate: only accept-rules proven >=95% right stay automatic; the rest go to review
python -m verifier.master_resolver --export-only --calibration resolver_output/accuracy_report.json

# Tests
python -m unittest discover -s verifier/tests -t . -v
```

The reviewer decisions file has the columns `source_row_number,decision,domain,note`. It is applied on every run, so no row is reviewed twice.

The original domain-list verifier (`python -m verifier --input "Domains List_V2.xlsx"`) uses the same decision engine.

## Repository layout

| Path | Purpose |
| --- | --- |
| `verifier/master_resolver.py` | Work-queue resolver: the main entry point |
| `verifier/decision.py` | Proof-ladder decision engine |
| `verifier/discovery.py` | Free search, Internet Archive lookups, candidate judging |
| `verifier/matching.py` | Whole-word name, country and host matching |
| `verifier/fetcher.py`, `pipeline.py`, `extractor.py` | Fetching, DNS, page extraction |
| `verifier/accuracy.py` | Hand-check sampling and accuracy scoring |
| `verifier/build_work_queue.py` | Builds `work_queue_master.*` from the final master |
| `verifier/legacy/` | Retired scripts (kept for reference only, see its README) |
| `verifier/registers.py` | Free Japan (NTA) and Australia (ABN Lookup) register lookups |
| `verifier/run_parallel.py` | Parallel worker launcher (unique row split, resume, live sheet) |

## Known limits

- **Registers:** only Japan and Australia have free register pages. Singapore, Korea, Thailand, Vietnam, Malaysia and the Philippines rely on the site's English page, address and ID matches.
- **Untuned confidence:** accuracy is not measured until someone hand-checks a sample (`verifier.accuracy`). Until then the accuracy gate is off.
- **Rate limits:** free search engines rate-limit heavy use. The resolver cools down a blocked engine and switches to the next one, so very large runs are slow, not broken.
