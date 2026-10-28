"""Serializable records shared by local and Actor execution."""
from __future__ import annotations
from dataclasses import asdict, dataclass, field
from typing import Any


DECISIONS = ("ACCEPT", "REJECT", "REVIEW")
# Outcome codes written by decision.py / discovery.py / master_resolver.py.
OUTCOMES = (
    "VERIFIED", "FOUND", "REPLACED", "MERGED_INTO", "REVIEWER_OVERRIDE",              # ACCEPT
    "NO_DOMAIN_SUPPLIED", "INVALID_DOMAIN", "DOMAIN_NOT_EXIST", "PARKED", "SITE_GONE",  # REJECT
    "MISMATCH", "COMPANY_CLOSED",
    "TEMP_UNREACHABLE", "BLOCKED", "UNREADABLE", "GOVERNMENT_SITE", "REDIRECT_UNCONFIRMED",  # REVIEW
    "INSUFFICIENT_PROOF", "NO_PROOF", "REPLACEMENT_SUGGESTED", "UNCALIBRATED_ACCEPT", "NOT_FOUND", "NOT_CHECKED", "NO_NAME",
)


@dataclass
class Evidence:
    url: str
    evidence_type: str
    text: str
    relationship: str
    strength: str
    title: str = ""
    source_type: str = "website"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FetchRecord:
    requested_url: str
    final_url: str = ""
    status: int = 0
    html: str = ""
    redirect_chain: list[str] = field(default_factory=list)
    method: str = "HTTP"
    attempts: int = 0
    error: str = ""
    error_type: str = ""
    checked_at: str = ""
    tls_valid: bool | None = None


@dataclass
class DomainRecord:
    domain: str
    registered_domain: str
    requested_url: str
    final_url: str = ""
    final_registered_domain: str = ""
    http_status: int = 0
    dns_status: str = "UNKNOWN"
    https_available: bool | None = None
    redirect_chain: list[str] = field(default_factory=list)
    domain_active: bool | None = None
    parked: bool = False
    blocked_reason: str = ""
    fetch_method: str = "NONE"
    fetch_attempts: int = 0
    checked_at: str = ""
    pages: list[dict[str, Any]] = field(default_factory=list)
    fetch_error: str = ""
    fetch_error_type: str = ""
    live_host: str = ""
    tls_valid: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
