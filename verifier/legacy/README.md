# Legacy scripts (superseded — do not use)

These scripts produced the earlier `SUPER_MERGED_MASTER_FINAL*` files. They are kept for reference only, are not maintained, and nothing in `verifier/` imports them.

They were retired because the code-level and logical-framework reviews (`code_level_review.md`, `logical_framework_reveiw.md`) found that they:
- accepted search results or sites that merely loaded without checking who owns them (review #11, #12)
- used the lenient "valid unless contradicted" elimination engine (`elimination_engine.py`) and the self-confirming V2 scorer (`v2_human_decision.py`)
- duplicated the same logic across about 12 files, with fixed file names and column positions (#22)
- overwrote their input files
- depended on paid Apify actors

Use these instead:
- `python -m verifier.master_resolver` verifies and resolves the work queue (`work_queue_master.csv`).
- `verifier/decision.py` is the proof-ladder decision engine.
- `verifier/discovery.py` finds current websites with free search plus the Internet Archive.
