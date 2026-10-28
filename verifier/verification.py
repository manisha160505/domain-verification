"""Row-level verification entry point used by the CLI and the Apify Actor pipeline."""
from __future__ import annotations

from .decision import decide
from .models import DomainRecord, Evidence
from .normalization import NormalizedDomain


def verify_row(row: dict, normalized: NormalizedDomain, domain: DomainRecord | None,
               evidence: list[Evidence], *, network_healthy: bool = True) -> dict:
    """Decide one organization/domain pair with the proof-ladder engine.

    Website proofs are read from the fetched pages; `evidence` adds reviewed outside
    evidence (external-evidence file or fetched search sources)."""
    external = [e for e in evidence if e.source_type == "external"]
    result = decide(row, normalized, domain, network_healthy=network_healthy, external=external)
    result["evidence_json"] = [
        {"url": p["url"], "evidence_type": p["type"], "text": p["text"], "relationship": "proof",
         "strength": p["level"], "title": result.get("website_title", ""), "source_type": "website"}
        for p in result["proofs"]
    ] + [e.to_dict() for e in external]
    return result
