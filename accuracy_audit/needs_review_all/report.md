# Accuracy of mapped domains, all NEEDS_REVIEW rows (suggested domain if any, else the old provided URL)

**Headline (country-reweighted): 54.4%**  
Observed raw accuracy: 54.9% (95% CI 50.0%–59.8%, Wilson with finite-population correction)
n = 384 judged (211 MATCH, 173 MISMATCH) of 1000 drawn from 41,142 rows
Replacement rate: 61.6% {'NO_DOMAIN_FOUND': 349, 'LOW_CONFIDENCE': 246, 'MEDIUM_NOT_CONFIRMED': 14, 'TECHNICAL_FAILURE': 7}
Bounds (replaced rows counted as MISMATCH / MATCH): 21.1% – 82.7%
Cluster bootstrap by suggested domain: 49.2%–60.5%
**Reliability flag:** more than 40% of drawn rows were replaced; the headline applies only to verifiable companies and must be read with the bounds.

| Domain tested | Drawn | Judged | Match | Accuracy | Replacement rate |
|---|---|---|---|---|---|
| Old URL provided (no suggestion) | 599 | 191 | 67 | 35.1% | 68.1% |
| Suggested domain | 401 | 193 | 144 | 74.6% | 51.9% |
(Split figures are indicative: each part has fewer judged rows than the ±5% target.)

| Country | Judged | Match | Accuracy | Replaced | Replacement rate |
|---|---|---|---|---|---|
| JP | 120 | 68 | 56.7% | 117 | 49.4% |
| AU | 77 | 36 | 46.8% | 164 | 68.0% |
| MY | 36 | 21 | 58.3% | 65 | 64.4% |
| TH | 32 | 17 | 53.1% | 54 | 62.8% |
| SG | 20 | 13 | 65.0% | 57 | 74.0% |
| KR | 23 | 9 | 39.1% | 52 | 69.3% |
| PH | 24 | 12 | 50.0% | 32 | 57.1% |
| NZ | 19 | 13 | 68.4% | 28 | 59.6% |
| VN | 16 | 11 | 68.8% | 25 | 61.0% |
| ID | 17 | 11 | 64.7% | 22 | 56.4% |

Country figures are indicative (each has its own, wider uncertainty); the ±5% applies overall.
Seed 20261006, model gemini-2.5-flash, temperature 0, run 2026-10-06T09:33:04+00:00.
No agent-error correction: canary calibration and the inter-run agreement check were not run.
Trade-off: a company with several valid domains (country and global site) can be MISMATCH when the suggested domain is a valid one the agent did not pick.
