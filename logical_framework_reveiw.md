# Company–Domain Verification: Logical Framework Review

2026-10-01

The current framework accepts too much on weak evidence. Supplied domains count as right unless the site contradicts them, and replacements for dead domains are often barely checked or not checked at all. Most fixes come down to one rule: accept nothing without clear proof linking the site to the company.

## 1. Gaps and fixes in the current logical framework

The most serious gaps are 2, 11, 12 and 15: in each, an answer is accepted, or counted as resolved, with little or no proof.

### Checking whether a domain belongs to the company

| # | Gap | Fix |
| --- | --- | --- |
| 1 | **There is no agreed definition of a correct website.** Nothing says whether the company's own site, its group's site, a regional site, a distributor's page or a social media page counts, so similar rows can get different outcomes. | Agree a short list of acceptable answer types with the people who use the data. Label every accepted row with its type (own site, group site, regional site). |
| 2 | **Weak evidence is enough to mark a row valid.** Supplied domains count as right unless the site contradicts them, so a look-alike domain (for example “abc.com” for “Fabcorp Industries”) can pass without the site ever naming the company. | Require at least one clear link between the site and this specific company (see the proof ladder in section 2). Treat a look-alike domain name as a hint only, and ignore clues that only show the site agrees with itself, such as a contact email on its own domain. |
| 3 | **Words are matched as fragments.** “Star” matches “Starlight”, and short country codes such as “in”, “us” or “th” are found inside ordinary words, so most English pages look consistent with India, the US or Thailand. | Match whole words only. Check the country by its full name, a city, the address or the phone country code, never by two-letter codes. |
| 4 | **The strongest proof barely counts.** The decision rests mainly on the page title, main heading and description, while the legal name in the footer, copyright line or contact, “about us” and legal pages only counts as a weak partial match. | Make the full legal name (or registration number) on these pages the main proof of ownership, ranked above the title and the domain name. |
| 5 | **Sites that can't be read are still judged.** Timeouts and error pages can be marked valid if the domain name looks right, near-empty pages (such as “Loading…”) can be marked as a mismatch, and blocked sites get no next step. | Never decide “valid” or “mismatch” without readable content. Label these rows “unreachable”, retry once later, and send any that still fail to review. |
| 6 | **Mismatches are decided on weak clues.** A site is rejected when the first word of its title (such as “Welcome”) doesn't match and the name isn't on the page, or simply for being a government site. | Call a mismatch only when the site clearly names a different company or organisation as its owner. Send government sites to review, since state-owned companies and public bodies use them legitimately. |
| 7 | **Redirects are set aside instead of judged.** A domain that forwards to another website is labelled “redirect” even when the new site clearly belongs to the same company, such as after a rebrand or a move to the group site. | Check the destination with the same rules. If it belongs to the company, mark the row valid and record the destination as the current website. |
| 8 | **Group websites are accepted too easily.** A parent group's site can be accepted for a subsidiary just because the domain matches the group's brand or the spreadsheet's own “sales territory” column, so the spreadsheet ends up confirming itself. | Use territory and parent data only as a lead. Accept a group site only if it shows the subsidiary or its country office, label it “group site”, and prefer the subsidiary's own site when one exists. |
| 9 | **Companies with the same name are not told apart.** Name matching cannot separate, say, “Apex Pty Ltd” (Australia) from “Apex Pte Ltd” (Singapore), and websites are copied between rows in the same country whose names match once the legal ending is removed. | For common names, require a second match on address, phone number or country, and use registration numbers where the spreadsheet has them. Copy a website to other rows only when the full legal name (or company ID) and country match. |
| 10 | **“Inactive” mixes different situations.** It covers blank domain cells, addresses that don't exist, temporary lookup failures and sites that only work with “www.” in front, while many parked or for-sale pages are missed and treated as live company sites. | Use separate outcomes: “no domain supplied”, “domain does not exist”, “parked or for sale” and “temporarily unreachable”. Re-check the last group, with and without “www.”, before calling a domain dead. |

### Finding the current website when a domain is dead

| # | Gap | Fix |
| --- | --- | --- |
| 11 | **Search results are accepted with little or no checking.** The main replacement step uses the first search result unchecked when no match is found by name, and later steps apply the lenient rules meant for supplied domains. | Check every search-found site before use, with a higher bar than for supplied domains: the site must show the company's name (ideally its legal name) and the country must match. Otherwise, send the row to review. |
| 12 | **A site that loads is treated as the company's site.** A dead domain is marked “replaced” as soon as it loads with or without “www.”, even if the page says access denied, with no check that it still belongs to the company. | If the address works with or without “www.”, record the original domain as live, not replaced, and run the normal ownership check, since lapsed domains are often bought by others. Never accept an access-denied page as proof. |
| 13 | **The search is too narrow.** Each company is usually searched with one wording and only the first usable result is kept, so the official site is missed when a directory, news site or distributor ranks higher. | Try two or three wordings (full legal name in quotes; brand name plus city or country; the old domain name) and look at the top five results. Skip known directory, news, job and social media sites, and pick the result with the strongest proof. |
| 14 | **The search assumes the company still exists.** A dead domain is taken to mean the company moved, so closures, mergers and acquisitions are never considered and the process either forces a match or gives up. | Allow “company closed” and “merged into or acquired by X” as outcomes, with the new owner's site as the answer where acceptable. Look for these signs (such as “acquired by” or “now part of”) in search results before looking for a replacement. |

### Across the whole process

| # | Gap | Fix |
| --- | --- | --- |
| 15 | **Rows judged wrong or unclear are counted as active.** In the master file, any domain not on the dead list is labelled “pre-existing active”, even if it was judged a mismatch, unclear or blocked, or never checked. | Carry each row's verification result into the master file. Send mismatch rows, and review rows that stay unresolved, through the same search for a current website. |
| 16 | **Accuracy is never measured.** Apart from a single spot check, no results have been compared with hand-checked answers, so the real error rate is unknown and the confidence labels are set by rule, not by how often each rule is right. | Hand-check about 300 rows spread across countries and outcomes. Set the confidence labels from the error rates found, and repeat the check whenever the rules change. |
| 17 | **Results overstate the proof behind them.** Several of the nine listed checks (search, address, phone) are never done, some are shown as passed by default, and coverage counts every “active” row, proven or not. | Mark checks that were not done as “not checked”. For each row, record how the answer was found (supplied, “www.” version, redirect, search) and what proved it, and count coverage only for rows with proof. |

## 2. Additional recommendations for the logical framework

Eight practical changes would make results more accurate and easier to trust. The proof ladder (2.1) matters most, because most gaps above come from accepting weak proof.

**2.1 Use a simple proof ladder.** Agree in advance what proof a decision needs. Accept a row only with one strong proof, or with two supporting proofs when one of them is the company name.

| Proof level | Examples | Enough to accept? |
| --- | --- | --- |
| Strong | Full legal name or registration number on the site; the subsidiary named on its group's site; an official company register listing the website | Yes, on its own |
| Supporting | Company or brand name in the page title or heading; matching address or phone number; matching country | Two together, one of them the company name |
| Hint only | Domain name looks like the company name; contact email on the site's own domain | Never |

**2.2 End every row in one of three decisions.** Each row ends as “accept”, “reject” or “needs review”, with a short reason such as “group site”, “parked” or “company closed”. Accept automatically only where the hand-checked sample shows a rule is right at least 95% of the time.

**2.3 Use the old website's history for dead domains.** The free Internet Archive (web.archive.org) usually keeps copies of old websites. The last copy often shows a “we have moved” notice, a link to the new site or the company's exact name, so check it before searching.

**2.4 Check official registers for high-risk rows.** Where a domain would be replaced or rejected, look the company up in its country's free official register (for example ABN Lookup in Australia or ACRA in Singapore). This confirms the exact legal name, separates same-name companies and shows whether the company has closed or merged.

**2.5 Check rows against each other.** After each run, flag the same company given different websites, and one website given to many unrelated companies (often a directory, distributor or group site). Review each flagged group once, not row by row.

**2.6 Review the riskiest rows first.** Put rows where a domain would be replaced or rejected at the top of the review list, since these changes do the most harm if wrong.

**2.7 Reuse reviewers' decisions.** Keep a running list of review decisions and apply it in every run, so no row is reviewed twice. Where reviewers keep overturning the same kind of decision, change that rule.

**2.8 Accept company names in the local language.** For companies in Japan, Korea, China, Thailand and Vietnam, count the company name written in the local language on the site as a name match. Use the local-language name in searches when the spreadsheet has it.
