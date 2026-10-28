# Accuracy of suggested domains (NEEDS_REVIEW rows with a suggestion)

**Headline (country-reweighted): 50.0%**  
Observed raw accuracy: 50.0% (95% CI 9.4%–90.5%, Wilson with finite-population correction)
n = 2 judged (1 MATCH, 1 MISMATCH) of 3 drawn from 16,509 rows
Replacement rate: 33.3% {'LOW_CONFIDENCE': 1}
Bounds (replaced rows counted as MISMATCH / MATCH): 33.3% – 66.7%

| Country | Judged | Match | Accuracy | Replaced | Replacement rate |
|---|---|---|---|---|---|
| AU | 1 | 1 | 100.0% | 0 | 0.0% |
| MY | 1 | 0 | 0.0% | 0 | 0.0% |
| TH | 0 | 0 | — | 1 | 100.0% |

Country figures are indicative (each has its own, wider uncertainty); the ±5% applies overall.
Seed 20261006, model gemini-2.5-flash, temperature 0, run 2026-10-06T06:57:07+00:00.
No agent-error correction: canary calibration and the inter-run agreement check were not run.
Trade-off: a company with several valid domains (country and global site) can be MISMATCH when the suggested domain is a valid one the agent did not pick.
