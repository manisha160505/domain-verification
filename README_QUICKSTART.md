# Quick start

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows   (macOS/Linux: source .venv/bin/activate)
pip install -r verifier/requirements.txt

python -m unittest discover -s verifier/tests -t .      # all tests should pass
python -m verifier.master_resolver --limit 200          # pilot on the first 200 queue rows
```

Results land in `resolver_output/`:
- `resolved_master.csv` / `.xlsx`
- `review_queue.csv`
- `consistency_report.csv`
- `summary.json`

See `README.md` for how decisions are made and for all options.
