# Domain Verification Tool: Code-Level Review

2026-10-01

The code has bugs that quietly change results, not just style problems. Some parts fail outright, and several rules accept a website with little or no proof. Most fixes are small: a missing line, a stricter rule, or using evidence the tool already collects.

## 1. Code-level gaps and fixes

The most serious gaps are 1, 2, 11, 12 and 19, where websites are accepted or counted as resolved with little or no proof, and 13 and 18, where part of the tool fails outright.

### Checking whether a domain belongs to the company

| # | Gap | Fix |
| --- | --- | --- |
| 1 | **The tool collects the best evidence, then ignores it.** It finds legal company names, legal-page mentions and “subsidiary of” wording, but the final decision never looks at them, so the evidence file is always empty. (`verification.py`) | Feed this evidence into the decision and make a legal-name match the main proof. Write it to the evidence file so every result can be traced. |
| 2 | **Websites that never loaded can be marked valid.** Timeouts, “page not found”, server errors and refused connections skip the dead-or-blocked checks, so a look-alike domain name alone is enough for “valid”. (`elimination_engine.py`) | Catch these cases first and label them “unreachable”. Never mark a row valid without readable page content. |
| 3 | **Matching works on fragments of words.** “abc” matches inside “Fabcorp”, “star” inside “Starlight”, and country codes like “in”, “us” and “th” inside almost any English text. (`elimination_engine.py`, `v2_human_decision.py`) | Match whole words only. Check countries by full name, city or phone code, never by two-letter codes. |
| 4 | **The mismatch rules misfire.** The site's owner is taken to be the first word of the page title (“Welcome”, “Home”, “Loading…”), and the government rule matches word fragments, so “Keyboard Ltd” counts as a government body because it contains “board”. (`elimination_engine.py`) | Drop the first-word guess and use only an owner the site states clearly, such as the legal name in the footer. Match government words as whole words, and send government sites to review. |
| 5 | **Several of the nine checks are not real.** The search check never runs but shows “YES” on every valid row, the address and phone checks never run, the business check passes whenever a page has a description, and the group check almost never triggers. (`elimination_engine.py`) | Implement each check properly or remove it. Show “not checked” for anything that did not run. |
| 6 | **A domain is called dead too quickly.** Only the address without “www.” is looked up, one temporary lookup failure counts as dead, and parked or for-sale pages are recognised by only a handful of phrases. (`fetcher.py`, `pipeline.py`, `extractor.py`) | Try the address with and without “www.”, and over both https and http, before calling it dead. Separate “does not exist” from “temporary failure”, and widen the list of parked-page signs. |
| 7 | **Blank domain cells are labelled “inactive”.** A row with no domain gets “inactive, high confidence” and is described as a parked domain. (`elimination_engine.py`) | Give these rows their own outcome, “no domain supplied”, and send them to the search step. |
| 8 | **The README's scoring engine lets the spreadsheet confirm itself.** It gives “verified group, high confidence” when the sales-territory column matches the domain, even if the site never mentions the company, and it reads two page fields that are never filled in. (`v2_human_decision.py`) | Use the territory only as a lead, and require the site to mention the company or its country office. Remove the unused fields or fill them in. |
| 9 | **Redirects hide good answers.** Any forward to a different domain is labelled “redirect” before the identity checks run, even when the new site clearly belongs to the company. (`elimination_engine.py`) | Check the destination with the normal rules. If it matches, record it as the current website. |
| 10 | **The page reader misses where company details usually are.** It ignores emails in email links, mistakes years and prices for phone numbers, misses most footers and non-Latin names, and never reads sister sites such as “corporate.” addresses. (`extractor.py`) | Read emails from links, tighten the phone pattern, look for the © copyright line, and support local-language names. Prefer “about”, “contact” and legal-notice pages, including those on sister sites. |

### Finding the current website when a domain is dead

| # | Gap | Fix |
| --- | --- | --- |
| 11 | **The main replacement step can use a search result unchecked.** When no verified match is found by name, it takes the first saved search result as the company's new website. (`replace_and_populate_super_merged.py`) | Run every search result through the ownership check before it is written to the master file. |
| 12 | **Any site that loads counts as recovered.** Three scripts mark a dead domain “replaced” as soon as it loads with or without “www.” (one even accepts an “access denied” page), with no check of who owns it; the saved summaries show nearly 1,400 rows handled this way. (`max_parallel_resolver.py`, `deep_targeted_recovery.py`, `recover_remaining_unresolved.py`) | Treat a working “www.” version as “the original domain is live”, then run the ownership check. Never accept an access-denied page. |
| 13 | **One search script fails every new search without saying so.** It calls a random-delay helper it never loads, the error is hidden, and every search not already saved returns nothing; its own summary shows just 3 rows found by search. (`max_parallel_resolver.py`) | Add the missing `import random`, and log errors where they can be seen so silent failures can't hide. |
| 14 | **The skip list removes real company websites.** It matches parts of names, so “x.com” blocks fedex.com, netflix.com and rolex.com, and “apple.com” blocks pineapple.com.au. (`recover_inactive.py`) | Match the full site name, or the end of it after a dot. |
| 15 | **One script's main check can never pass, and its shortcut accepts anything.** It looks for labels the decision engine never produces, so only the “same old domain name” shortcut works, and that shortcut accepts even a mismatch. (`replace_remaining_inactive.py`) | Use the labels the engine actually returns, and require a valid result for the shortcut too. |
| 16 | **Search results are picked and judged too loosely.** Only the first result is used, one row's details decide for every row with the same search, and the lenient “valid unless contradicted” rules meant for supplied domains are applied; earlier runs had to undo news sites accepted this way. (all search scripts) | Check the top several results, and judge each row on its own details (such as tax ID). Require the site to name the company and match its country. |
| 17 | **Saved and outside results are handled carelessly.** Failed searches are saved as “no results” and never retried or refreshed, and pages from the Apify unblocking service are matched back by partial domain name, keeping only the home page. (`recover_inactive.py`, `open_source_resolver.py`, `pipeline.py`) | Don't save failed searches, and give saved results an expiry date. Match Apify pages by exact address and keep every page fetched. |

### Across the whole tool

| # | Gap | Fix |
| --- | --- | --- |
| 18 | **The main command crashes on start.** `python -m verifier` stops at once because the code uses a module it never loads. (`cli.py`) | Add the missing `import os`. |
| 19 | **Rows judged wrong or unclear are counted as active.** When the master file is built, any domain not on the dead list is labelled “pre-existing active”, including mismatches, unclear and blocked rows, and rows never checked. (`replace_and_populate_super_merged.py`) | Carry each row's actual result into the master file, and send mismatch rows to the search step. |
| 20 | **The tests don't test the code in use.** Only 8 of 20 tests pass: 10 check an older decision engine that is no longer called, one fails on Windows because a file is left open, and one shows the group rule never triggers. (`verifier/tests/`) | Rewrite the tests for the engine actually used, add tests for the gaps in this list, and run them before every change. |
| 21 | **Output files lose basic facts, so the summary is wrong.** Results leave blank whether the site answered, where it ended up and whether its address exists, so the summary reports 0 sites fetched even when they were. (`elimination_engine.py`, `export.py`) | Copy these facts into every result row, and check the summary against a small run with known answers. |
| 22 | **The scripts are fragile and overwrite their inputs.** File names, sheet names and column positions are fixed in the code, about 12 scripts hold near-copies of the same logic, and most save over the original files. (recovery scripts) | Merge them into one tool with settings for files and columns. Always write to a new file or keep a backup. |
| 23 | **The crawler presents itself inconsistently.** It announces itself as a bot but copies Chrome's connection signature and adds a fake “came from Google” header, a mix that security systems flag easily. (`fetcher.py`) | Use one honest identity, a named crawler with a contact address, and send sites that block it to review. |
| 24 | **Results are hard to reproduce and the code is hard to maintain.** Saved summaries (such as 1,230 “valid group” rows) can't be reproduced with the current code, package versions aren't fixed, security certificate checks are switched off, and about 870 files of outside libraries are copied into the repository. (whole repository) | Save the code version and inputs with every run, and pin package versions. Turn certificate checks back on, and install outside libraries as packages instead of copies. |

## 2. Framework-Level Notes

Fixing the code alone won't make results reliable, because the method behind it has its own weaknesses. These are covered in full, with fixes, in `logical_framework_reveiw.md`; the main ones are below.

- **Accuracy is unmeasured.** Results have never been compared with a hand-checked sample, so the true error rate is unknown.
- **"Valid unless contradicted" is the default.** Combined with weak checks, almost any live site with a similar name gets accepted.
- **The website is the only witness.** The tool relies on what a site says about itself, and doesn't use company registers, domain ownership records or the old site's archived history.
- **A brand is not a legal entity.** A group website shows a company belongs to a brand family, not that this subsidiary is represented there, and there is no agreed rule on when a group site is acceptable.
- **Loading is not owning.** Recovery treats a site that answers as the company's site, though lapsed domains are often bought by someone else.
- **Discovery is narrow.** Each company gets one search wording and one result, with no ranking of several candidates.
- **Rows are decided one at a time.** Nothing checks for the same company getting different websites, or one website being given to many unrelated companies.
