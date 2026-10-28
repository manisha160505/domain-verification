# Accuracy of suggested domains (NEEDS_REVIEW rows with a suggestion)

**Headline (country-reweighted): 78.0%**  
Observed raw accuracy: 78.0% (95% CI 64.8%–87.2%, Wilson with finite-population correction)
n = 50 judged (39 MATCH, 11 MISMATCH) of 100 drawn from 16,509 rows
Replacement rate: 50.0% {'NO_DOMAIN_FOUND': 33, 'LOW_CONFIDENCE': 16, 'MEDIUM_NOT_CONFIRMED': 1}
Bounds (replaced rows counted as MISMATCH / MATCH): 39.0% – 89.0%
**Reliability flag:** more than 40% of drawn rows were replaced; the headline applies only to verifiable companies and must be read with the bounds.

| Country | Judged | Match | Accuracy | Replaced | Replacement rate |
|---|---|---|---|---|---|
| JP | 24 | 18 | 75.0% | 14 | 36.8% |
| AU | 7 | 6 | 85.7% | 11 | 61.1% |
| SG | 7 | 6 | 85.7% | 3 | 30.0% |
| TH | 3 | 3 | 100.0% | 6 | 66.7% |
| MY | 2 | 1 | 50.0% | 7 | 77.8% |
| ID | 4 | 2 | 50.0% | 2 | 33.3% |
| KR | 2 | 2 | 100.0% | 2 | 50.0% |
| NZ | 1 | 1 | 100.0% | 2 | 66.7% |
| PH | 0 | 0 | — | 2 | 100.0% |
| VN | 0 | 0 | — | 1 | 100.0% |

Country figures are indicative (each has its own, wider uncertainty); the ±5% applies overall.
Seed 20261006, model gemini-2.5-flash, temperature 0, run 2026-10-06T07:03:11+00:00.
No agent-error correction: canary calibration and the inter-run agreement check were not run.
Trade-off: a company with several valid domains (country and global site) can be MISMATCH when the suggested domain is a valid one the agent did not pick.
