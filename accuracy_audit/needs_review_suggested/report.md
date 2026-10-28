# Accuracy of suggested domains (NEEDS_REVIEW rows with a suggestion)

**Headline (country-reweighted): 74.6%**  
Observed raw accuracy: 74.9% (95% CI 70.3%–78.9%, Wilson with finite-population correction)
n = 382 judged (286 MATCH, 96 MISMATCH) of 800 drawn from 16,509 rows
Replacement rate: 52.2% {'LOW_CONFIDENCE': 195, 'NO_DOMAIN_FOUND': 194, 'MEDIUM_NOT_CONFIRMED': 20, 'TECHNICAL_FAILURE': 9}
Bounds (replaced rows counted as MISMATCH / MATCH): 35.8% – 88.0%
Cluster bootstrap by suggested domain: 70.5%–79.2%
**Reliability flag:** more than 40% of drawn rows were replaced; the headline applies only to verifiable companies and must be read with the bounds.

| Country | Judged | Match | Accuracy | Replaced | Replacement rate |
|---|---|---|---|---|---|
| JP | 182 | 137 | 75.3% | 124 | 40.5% |
| AU | 51 | 38 | 74.5% | 88 | 63.3% |
| TH | 37 | 25 | 67.6% | 41 | 52.6% |
| SG | 30 | 27 | 90.0% | 36 | 54.5% |
| MY | 21 | 16 | 76.2% | 47 | 69.1% |
| KR | 13 | 10 | 76.9% | 30 | 69.8% |
| ID | 22 | 12 | 54.5% | 10 | 31.2% |
| NZ | 12 | 11 | 91.7% | 15 | 55.6% |
| PH | 5 | 5 | 100.0% | 18 | 78.3% |
| VN | 9 | 5 | 55.6% | 9 | 50.0% |

Country figures are indicative (each has its own, wider uncertainty); the ±5% applies overall.
Seed 20261006, model gemini-2.5-flash, temperature 0, run 2026-10-06T08:12:53+00:00.
No agent-error correction: canary calibration and the inter-run agreement check were not run.
Trade-off: a company with several valid domains (country and global site) can be MISMATCH when the suggested domain is a valid one the agent did not pick.
