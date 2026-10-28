"""Proof-ladder decision engine: does this website belong to this company?

Every row ends as ACCEPT, REJECT or REVIEW with an outcome code and a short reason.

Proof ladder (logical framework review, section 2.1):
  STRONG      full legal name in structured data, footer, copyright line or a legal/about/contact
              page; the company's tax/registration number on the site; the subsidiary named
              on its group's site.                                    -> enough on its own
  SUPPORTING  company or brand name in the site identity (title, heading, site name);
              matching country (full name, city, phone code or ccTLD).   -> two together, one a name
  HINT        domain label looks like the name; contact email on the site's own domain;
              sales-territory group matches the domain.                  -> never enough

Nothing is accepted or rejected without readable page content. A mismatch needs the site to
clearly name a different owner. Government sites go to review. Search-found and archive-found
candidates need a higher bar: a name proof AND a country proof (or a strong proof).
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from .entity_resolution import relationship_mention
from .matching import (
    address_evidence, cctld_country, has_local_script, core_name, country_evidence, distinctive_tokens, fold, has_legal_suffix,
    is_government_host, is_government_name, label_looks_like_name, legal_name_in, name_in,
    strip_territory_tag, word_in,
)
from .models import DomainRecord
from .normalization import NormalizedDomain, registered_domain

ACCEPT, REJECT, REVIEW = "ACCEPT", "REJECT", "REVIEW"
STRONG, SUPPORTING, HINT = "STRONG", "SUPPORTING", "HINT"
# Outcomes whose supplied domain cannot be the answer: the row needs discovery.
NEEDS_DISCOVERY = {"NO_DOMAIN_SUPPLIED", "INVALID_DOMAIN", "DOMAIN_NOT_EXIST", "PARKED", "SITE_GONE", "MISMATCH"}
LEGAL_PAGE_HINTS = ("about", "company", "corporate", "contact", "legal", "privacy", "terms", "imprint",
                    "profile", "overview", "outline", "gaiyo", "会社", "概要")
MIN_READABLE_CHARS = 200
SEARCH_LIKE = ("SEARCH", "WAYBACK")


def row_context(row: dict) -> dict[str, str]:
    """Identity fields from either the work-queue master or the original domain list.
    Name priority: original company name, then organization name, then sales territory."""
    def get(*keys: str) -> str:
        for key in keys:
            value = str(row.get(key) or "").strip()
            if value and value.lower() not in ("none", "nan"):
                return value
        return ""
    territory = get("Sales Territory Name")
    name = get("original_company_name", "Organization Name") or strip_territory_tag(territory)
    return {
        "name": name,
        "territory": territory,
        "group": strip_territory_tag(territory),
        "country": get("country code", "Country", "﻿Country", "Country (Group)").upper()[:2],
        # IDs shared by different companies in the data are not used (master_resolver.untrusted_ids).
        "tax_id": "" if row.get("_tax_id_untrusted") else get("tax_id"),
        "registration_number": "" if row.get("_registration_number_untrusted") else get("registration_number"),
        # Official register record (verifier.registers), attached by the resolver when available.
        "register": row.get("_register") or {},
        "addresses": [a for a in (get("registry_address", "registered_address"),
                                  (row.get("_register") or {}).get("address", "")) if a],
    }


def _id_pattern(value: str) -> re.Pattern | None:
    chars = re.sub(r"[^0-9A-Za-z]", "", value or "")
    if len(chars) < 8 or not any(c.isdigit() for c in chars):
        return None
    return re.compile(r"(?<![0-9A-Za-z])" + r"[\s\-./]?".join(re.escape(c) for c in chars) + r"(?![0-9A-Za-z])", re.I)


def _is_legal_page(url: str) -> bool:
    path = fold(urlsplit(url).path)
    return any(k in path for k in LEGAL_PAGE_HINTS)


def _copyright_owner(line: str) -> str:
    owner = re.sub(r"(?i)©|\(c\)|copyright|all rights reserved|\d{4}(\s*[-–]\s*\d{4})?", " ", line)
    return re.sub(r"\s+", " ", owner).strip(" .,|-")


_ENTITY_RE = re.compile(
    r"((?:[A-Za-z0-9&.'\-]+\s+){1,3})(?:co\.?,?\s*ltd|limited|ltd|inc|corporation|corp|pte\.?\s*ltd|pty\.?\s*ltd|"
    r"sdn\.?\s*bhd|berhad|plc|llc|k\.k)\b", re.I)


_LOCAL_ENTITY_RE = re.compile(
    r"([぀-ヿ㐀-鿿A-Za-z0-9・ー]{1,20}?)(株式会社|有限会社|合同会社|労働金庫|信用金庫|信用組合|協同組合|銀行|証券|生命保険|損害保険)")


def _legal_entity_count(text: str) -> int:
    """Rough count of distinct companies named with a legal ending in a text (English endings
    such as Ltd/Inc and Japanese forms such as 株式会社/労働金庫)."""
    names = {m.group(0) for m in _LOCAL_ENTITY_RE.finditer(text or "")}
    for match in _ENTITY_RE.finditer(text or ""):
        words = match.group(1).lower().split()
        cut = max((i + 1 for i, w in enumerate(words) if w.endswith((".", ",", ":", "|"))), default=0)
        words = words[cut:] or words[-1:]
        names.add(" ".join(words))
    return len(names)


def _core_key(name: str) -> str:
    return " ".join(t for t in re.split(r"[^\w]+", fold(core_name(name))) if t)


def _shares_token(a: str, b: str) -> bool:
    return bool(set(distinctive_tokens(a)) & set(distinctive_tokens(b)))


def _proof(kind: str, level: str, text: str, url: str, is_name: bool = False, is_country: bool = False) -> dict:
    return {"type": kind, "level": level, "text": str(text)[:300], "url": url,
            "is_name": is_name, "is_country": is_country}


def collect_proofs(ctx: dict, domain: DomainRecord) -> tuple[list[dict], dict[str, Any]]:
    """Gather proofs and identity facts from the fetched pages of one domain."""
    name, group, country = ctx["name"], ctx["group"], ctx["country"]
    pages = domain.pages or []
    home = pages[0] if pages else {}
    host = (urlsplit(domain.final_url or domain.requested_url).hostname or "").lower()
    proofs: list[dict] = []
    jsonld = [s for p in pages for s in p.get("jsonld", [])]
    org_schemas = [s for s in jsonld if "WebSite" not in str(s.get("@type", ""))]
    identity_parts = [home.get("title", ""), " ".join(home.get("h1", [])), home.get("site_name", "")]
    identity_parts += [str(s.get("name") or "") for s in org_schemas] + [str(s.get("alternateName") or "") for s in org_schemas]
    identity = " | ".join(x for x in identity_parts if x)
    specific_name = has_legal_suffix(name) or len(distinctive_tokens(name)) >= 2
    # Every known legal name of the company: the data's name, the official register's legal
    # name and its local-script (e.g. Japanese) name. (label, name, specific enough to be strong)
    legal_names = [("", name, specific_name)]
    register = ctx.get("register") or {}
    if register.get("legal_name") and fold(register["legal_name"]) != fold(name):
        legal_names.append(("register", register["legal_name"], True))
    if register.get("local_name"):
        legal_names.append(("local", register["local_name"], True))

    def add(kind: str, level: str, text: str, url: str, label: str) -> None:
        proofs.append(_proof(kind, level, f"{label + ': ' if label else ''}{text}", url, is_name=True))

    for label, legal, specific in legal_names:
        # STRONG: legal name in structured data.
        for schema in org_schemas:
            if any(isinstance(schema.get(k), str) and legal_name_in(schema[k], legal) and (k == "legalName" or label or has_legal_suffix(legal))
                   for k in ("legalName", "name")):
                add("legal_name_structured_data", STRONG, legal, home.get("url", ""), label)
                break
        # STRONG: legal name in footer / copyright line / legal-about-contact page.
        for page in pages:
            url = page.get("url", "")
            places = [("footer", page.get("footer", "")), ("copyright", " ".join(page.get("copyright", [])))]
            if _is_legal_page(url):
                places.append(("legal_page", page.get("visible_text", "")))
            hit = next((where for where, text in places if text and legal_name_in(text, legal)), "")
            if hit:
                text = dict(places)[hit]
                # A footer or page listing several companies (member/group directories) is not an
                # owner statement for any one of them: being listed there is only a hint.
                if _legal_entity_count(text) > (1 if hit == "footer" else 2):
                    proofs.append(_proof(f"listed_among_companies_{hit}", HINT, legal, url))
                else:
                    add(f"legal_name_{hit}", STRONG if specific else SUPPORTING, legal, url, label)
                break
    # Owner statement with the same core name but another legal ending (e.g. 株式会社 rendered as
    # "Co., Ltd." in the data and "Corporation" on the site).
    if not any(p["type"].startswith("legal_name_") for p in proofs):
        owners = [_copyright_owner(line) for page in pages[:3] for line in page.get("copyright", [])]
        owners += [str(s.get("legalName")) for s in org_schemas if isinstance(s.get("legalName"), str)]
        for label, legal, _ in legal_names:
            wanted = _core_key(legal)
            owner = next((o for o in owners if wanted and has_legal_suffix(o) and _core_key(o) == wanted), "")
            if owner:
                level = STRONG if len(distinctive_tokens(legal)) >= 2 else SUPPORTING
                add("legal_name_owner_variant", level, owner, home.get("url", ""), label)
                break
    # STRONG: tax / registration number on the site.
    for field in ("tax_id", "registration_number"):
        pattern = _id_pattern(ctx.get(field, ""))
        if pattern:
            for page in pages:
                haystack = page.get("visible_text", "") + " " + " ".join(str(s.get("taxID") or s.get("vatID") or "") for s in page.get("jsonld", []))
                if pattern.search(haystack):
                    proofs.append(_proof(f"{field}_on_site", STRONG, ctx[field], page.get("url", ""), is_name=True))
                    break
    # Group site: the site's identity is the sales-territory group.
    group_identity = bool(group) and fold(core_name(group)) != fold(core_name(name)) and (
        bool(name_in(identity, group)) or any(word_in(identity, t) for t in distinctive_tokens(group)[:1]))
    # Subsidiary named on its group's site, or named with relationship wording anywhere.
    for page in pages:
        text = page.get("visible_text", "")
        relation = relationship_mention(text, strip_territory_tag(name))
        named = any(legal_name_in(text, legal) for _, legal, _ in legal_names)
        if (group_identity and named) or relation:
            proofs.append(_proof("subsidiary_named_on_group_site" if group_identity else "relationship_text",
                                 STRONG if (group_identity and specific_name) else SUPPORTING,
                                 relation or strip_territory_tag(name), page.get("url", ""), is_name=True))
            break
    # SUPPORTING: company name or brand in the site identity. A brand needs every distinctive
    # word of a one-word name, or at least two distinctive words of a longer name
    # ("American Express", not just "American"). Register names (incl. the local-script name
    # without its company form, and registered business/trading names) count too.
    identity_names = [name] + [legal for label, legal, _ in legal_names if label]
    identity_names += [register.get("local_core_name", "")] + list(register.get("other_names", []))
    matched = next((m for m in (name_in(identity, n) for n in identity_names if n)
                    if m and len(fold(m)) >= (2 if has_local_script(m) else 3)), "")
    if matched:
        proofs.append(_proof("name_in_site_identity", SUPPORTING, identity, home.get("url", ""), is_name=True))
    else:
        tokens = distinctive_tokens(name)
        hits = [t for t in tokens if word_in(identity, t)]
        if (len(tokens) == 1 and hits) or len(hits) >= 2:
            proofs.append(_proof("brand_in_site_identity", SUPPORTING, f"{' '.join(hits)}: {identity}", home.get("url", ""), is_name=True))
    # SUPPORTING: full legal name in body text (not footer/legal page).
    if not any(p["is_name"] and p["level"] == STRONG for p in proofs):
        for page in pages:
            footer = page.get("footer", "")
            body = page.get("visible_text", "").replace(footer, " ") if footer else page.get("visible_text", "")  # footer judged above
            if any(specific and legal_name_in(body, legal) for _, legal, specific in legal_names):
                proofs.append(_proof("legal_name_in_body", SUPPORTING, strip_territory_tag(name), page.get("url", ""), is_name=True))
                break
    # SUPPORTING: country (full name / city / phone code in text, structured address, or ccTLD).
    all_text = " ".join(p.get("visible_text", "")[:20000] + " " + p.get("footer", "") for p in pages)
    term = country_evidence(all_text, country)
    if not term:
        for schema in jsonld:
            address = schema.get("address")
            found = address.get("addressCountry") if isinstance(address, dict) else None
            if isinstance(found, dict):
                found = found.get("name")
            if isinstance(found, str) and (found.upper() == country or country_evidence(found, country)):
                term = f"structured address: {found}"
                break
    if not term and country and cctld_country(host) == country:
        term = f"ccTLD .{host.rsplit('.', 1)[-1]}"
    if term:
        proofs.append(_proof("country_match", SUPPORTING, term, home.get("url", ""), is_country=True))
    # SUPPORTING: registered address (postcode, state/prefecture, city) found on the site.
    address_hit = address_evidence(all_text, ctx.get("addresses", []))
    if address_hit:
        proofs.append(_proof("address_match", SUPPORTING, address_hit, home.get("url", ""), is_country=True))
    # HINTS (never enough on their own).
    reg = registered_domain(host) or host
    if label_looks_like_name(reg, name):
        proofs.append(_proof("domain_looks_like_name", HINT, reg, ""))
    if any(e.lower().rsplit("@", 1)[-1].endswith(reg) for p in pages for e in p.get("emails", [])):
        proofs.append(_proof("contact_email_on_own_domain", HINT, reg, ""))
    if group and label_looks_like_name(reg, group):
        proofs.append(_proof("territory_group_matches_domain", HINT, group, ""))
    proofs = list({p["type"]: p for p in reversed(proofs)}.values())[::-1]  # one proof per type, first kept
    facts = {"identity": identity, "group_identity": group_identity, "host": host,
             "registered": reg, "org_schemas": org_schemas, "pages": pages}
    return proofs, facts


def _stated_other_owner(ctx: dict, facts: dict) -> str:
    """An owner the site states clearly (structured legal name or copyright holder with a
    legal ending) that shares no word with the company or its group."""
    candidates = [str(s.get("legalName")) for s in facts["org_schemas"] if isinstance(s.get("legalName"), str)]
    for page in facts["pages"][:3]:
        candidates += [_copyright_owner(line) for line in page.get("copyright", [])]
    for owner in candidates:
        if not owner or not has_legal_suffix(owner) or len(distinctive_tokens(owner)) == 0:
            continue
        if _shares_token(owner, ctx["name"]) or (ctx["group"] and _shares_token(owner, ctx["group"])):
            continue
        return owner
    return ""


def _base(ctx: dict, normalized: NormalizedDomain, domain: DomainRecord | None, found_via: str) -> dict:
    home = (domain.pages[0] if domain and domain.pages else {})
    return {
        "company_name_used": ctx["name"], "found_via": found_via,
        "original_domain": normalized.original_domain, "normalized_domain": normalized.normalized_domain,
        "registered_domain": normalized.registered_domain, "requested_url": domain.requested_url if domain else normalized.requested_url,
        "final_url": domain.final_url if domain else "", "final_registered_domain": domain.final_registered_domain if domain else "",
        "live_host": domain.live_host if domain else "", "http_status": domain.http_status if domain else 0,
        "dns_status": domain.dns_status if domain else "NOT_CHECKED", "tls_valid": domain.tls_valid if domain else None,
        "fetch_method": domain.fetch_method if domain else "NONE", "fetch_attempts": domain.fetch_attempts if domain else 0,
        "checked_at": domain.checked_at if domain else "", "website_title": home.get("title", ""),
        "answer_type": "", "current_website": "", "proof_level": "NONE", "proofs": [], "checks": {},
        "accept_rule": "", "register_name": "", "register_status": "",
    }


def _finish(result: dict, decision: str, outcome: str, reason: str) -> dict:
    result.update(decision=decision, outcome=outcome, reason=reason,
                  classification=decision, verification_status=outcome, decision_reason=reason,
                  confidence={ACCEPT: "HIGH" if result.get("proof_level") == STRONG else "MEDIUM"}.get(decision, "LOW"))
    return result


def decide(row: dict, normalized: NormalizedDomain, domain: DomainRecord | None, *,
           found_via: str = "SUPPLIED", network_healthy: bool = True, external: list | None = None) -> dict:
    """external: reviewed Evidence records (e.g. --external-evidence JSONL). Strong exact/group
    evidence counts as a strong proof; strong 'unrelated' evidence counts as a stated other owner."""
    ctx = row_context(row)
    result = _base(ctx, normalized, domain, found_via)
    register = ctx["register"]
    result["register_name"] = register.get("legal_name") or register.get("local_name", "")
    result["register_status"] = register.get("status", "")
    not_checked = {k: "NOT_CHECKED" for k in ("legal_name", "registration_number", "name_in_identity", "country",
                                              "group_relationship", "address", "register_name", "different_owner")}
    result["checks"] = dict(not_checked)
    if not ctx["name"]:
        return _finish(result, REVIEW, "NO_NAME", "Row has no company or territory name to verify against.")
    if not normalized.original_domain.strip():
        return _finish(result, REJECT, "NO_DOMAIN_SUPPLIED", "No domain supplied for this row.")
    if normalized.error:
        return _finish(result, REJECT, "INVALID_DOMAIN", f"Supplied domain is not usable: {normalized.error}.")
    if domain is None:
        return _finish(result, REVIEW, "NOT_CHECKED", "Domain has not been fetched yet.")
    # Unreadable sites are never judged valid or mismatched.
    if domain.dns_status == "FAILED":
        if network_healthy:
            return _finish(result, REJECT, "DOMAIN_NOT_EXIST", "Domain does not exist in DNS (bare and www.).")
        return _finish(result, REVIEW, "TEMP_UNREACHABLE", "DNS failed while the network itself was unhealthy.")
    if domain.dns_status in ("TEMPORARY", "UNKNOWN"):
        return _finish(result, REVIEW, "TEMP_UNREACHABLE", "DNS lookup did not complete; retry later.")
    if domain.fetch_error_type in ("ROBOTS", "UNSAFE_ADDRESS"):
        return _finish(result, REVIEW, "BLOCKED", f"Site could not be inspected: {domain.blocked_reason or domain.fetch_error_type}.")
    if domain.parked:
        return _finish(result, REJECT, "PARKED", "Site is a parked, for-sale or placeholder page.")
    if domain.http_status == 410:
        return _finish(result, REJECT, "SITE_GONE", "Site returned HTTP 410 Gone.")
    if domain.http_status in (401, 403, 429):
        return _finish(result, REVIEW, "BLOCKED", f"Access denied or rate-limited (HTTP {domain.http_status}).")
    if domain.http_status == 0 or domain.http_status >= 400:
        return _finish(result, REVIEW, "TEMP_UNREACHABLE",
                       f"Site did not return a readable page (HTTP {domain.http_status or 'none'}, {domain.fetch_error_type or 'no error'}).")
    if sum(p.get("text_length", len(p.get("visible_text", ""))) for p in domain.pages) < MIN_READABLE_CHARS:
        return _finish(result, REVIEW, "UNREADABLE", "Page loaded but has almost no readable content.")

    proofs, facts = collect_proofs(ctx, domain)
    external_owner = ""
    for item in external or []:
        if getattr(item, "strength", "") not in ("STRONG", "VERY_STRONG"):
            continue
        if item.relationship in ("exact_entity", "group_entity"):
            proofs.append(_proof(f"external_{item.relationship}", STRONG, item.text, item.url, is_name=True))
        elif item.relationship == "unrelated":
            external_owner = item.text
    strong = [p for p in proofs if p["level"] == STRONG]
    supporting = [p for p in proofs if p["level"] == SUPPORTING]
    name_proof = any(p["is_name"] for p in strong + supporting)
    country_proof = any(p["is_country"] for p in supporting)
    result["proofs"] = proofs
    result["proof_level"] = STRONG if strong else SUPPORTING if supporting else HINT if proofs else "NONE"
    types = {p["type"] for p in proofs}
    result["checks"] = {
        "legal_name": "YES" if types & {"legal_name_structured_data", "legal_name_footer", "legal_name_copyright",
                                        "legal_name_legal_page", "legal_name_owner_variant"} else "NO",
        "registration_number": ("YES" if types & {"tax_id_on_site", "registration_number_on_site"} else "NO")
        if (_id_pattern(ctx["tax_id"]) or _id_pattern(ctx["registration_number"])) else "NOT_CHECKED",
        "name_in_identity": "YES" if types & {"name_in_site_identity", "brand_in_site_identity"} else "NO",
        "country": ("YES" if country_proof else "NO") if ctx["country"] else "NOT_CHECKED",
        "group_relationship": ("YES" if types & {"subsidiary_named_on_group_site", "relationship_text"} else "NO") if ctx["group"] else "NOT_CHECKED",
        "address": ("YES" if "address_match" in types else "NO") if ctx["addresses"] else "NOT_CHECKED",
        "register_name": ("YES" if any(p["text"].startswith(("register:", "local:")) for p in proofs) else "NO")
        if (register.get("legal_name") or register.get("local_name")) else "NOT_CHECKED",
        "different_owner": "NO",
    }
    redirected = bool(domain.final_registered_domain and normalized.registered_domain
                      and domain.final_registered_domain != normalized.registered_domain)
    if redirected and found_via == "SUPPLIED":
        result["found_via"] = "REDIRECT"
    search_like = found_via in SEARCH_LIKE

    # Same-name companies (framework #9): a common name (at most one distinctive word) needs a
    # second match on address or an ID number; the country alone is not enough.
    # A long one-word brand on its own exact domain (wesfarmers.com.au, autoliv.com) is not "common".
    words = distinctive_tokens(ctx["name"])
    own_brand_domain = len(words) == 1 and len(words[0]) >= 6 and facts["registered"].split(".")[0] == words[0]
    common_name = len(words) <= 1 and not strong and not own_brand_domain
    if common_name and "address_match" not in types:
        supporting = [p for p in supporting if p["type"] != "country_match"]
        country_proof = False
    if search_like:
        hint_domain = "domain_looks_like_name" in types
        accepted = bool(strong) and (country_proof or any(p["type"].endswith("_on_site") for p in strong)) or (
            name_proof and country_proof and (hint_domain or "name_in_site_identity" in types))
    else:
        accepted = bool(strong) or (name_proof and len({p["type"] for p in supporting}) >= 2)
    # Which rule accepted the row (used by the accuracy gate, verifier.accuracy).
    result["accept_rule"] = (strong[0]["type"] if strong else
                             "+".join(sorted({p["type"] for p in supporting}))) if accepted else ""

    if is_government_host(facts["host"]) and not is_government_name(ctx["name"]) and not strong:
        return _finish(result, REVIEW, "GOVERNMENT_SITE",
                       "Government website for a non-government company name; needs a person to confirm.")
    if accepted:
        # Own site: the site names this company (full name or legal name). Group site: it is the
        # group's / brand's site and only the brand, group or a relationship links it to the company.
        own = types & {"name_in_site_identity", "legal_name_structured_data", "legal_name_footer",
                       "legal_name_copyright", "legal_name_legal_page", "legal_name_owner_variant",
                       "tax_id_on_site", "registration_number_on_site", "external_exact_entity"}
        result["answer_type"] = "OWN_SITE" if own else "GROUP_SITE"
        if result["answer_type"] == "GROUP_SITE" and ctx["country"] and cctld_country(facts["host"]) == ctx["country"]:
            result["answer_type"] = "REGIONAL_SITE"
        result["current_website"] = f"https://{facts['host']}" if facts["host"] else ""
        how = "; ".join(f"{p['type']} ({p['level'].lower()})" for p in strong + supporting)
        return _finish(result, ACCEPT, "VERIFIED", f"Site proves the company: {how}.")
    other = external_owner or _stated_other_owner(ctx, facts)
    if other and not name_proof:
        result["checks"]["different_owner"] = "YES"
        return _finish(result, REJECT, "MISMATCH", f"Site clearly names a different owner: '{other}'.")
    if redirected:
        return _finish(result, REVIEW, "REDIRECT_UNCONFIRMED",
                       f"Domain forwards to {domain.final_url}, which does not prove the company.")
    if name_proof or supporting:
        return _finish(result, REVIEW, "INSUFFICIENT_PROOF",
                       "Some supporting signals found but not enough proof to accept: " + ", ".join(sorted(types)) + ".")
    return _finish(result, REVIEW, "NO_PROOF", "Readable site, but nothing on it links it to the company.")
