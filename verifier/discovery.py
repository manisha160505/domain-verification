"""Find a company's current website when the supplied domain is missing, dead or wrong.

Free tools only: Yahoo / DuckDuckGo / Bing web search through curl_cffi and the Internet Archive (web.archive.org).
Every candidate is fetched and judged by the proof-ladder engine with the higher bar for
search-found sites (decision.py); nothing is accepted just because it loads.
"""
from __future__ import annotations

import asyncio
import base64
import html as html_lib
import json
import logging
import random
import re
import sqlite3
import time
from datetime import datetime, timezone
from urllib.parse import quote_plus, unquote, urlsplit

from .decision import ACCEPT, STRONG, decide, row_context
from .matching import COUNTRIES, core_name, distinctive_tokens, fold, host_matches, strip_territory_tag
from .normalization import normalize_domain, registered_domain

LOG = logging.getLogger(__name__)

# Directories, news, jobs, social, encyclopedias, registries and other non-company sites.
# Matched as the whole host or a parent domain (host == d or host.endswith("." + d)).
EXCLUDE_DOMAINS = frozenset({
    "linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com", "youtube.com", "tiktok.com",
    "pinterest.com", "reddit.com", "quora.com", "zhihu.com", "medium.com", "wikipedia.org", "wikimedia.org",
    "wikiwand.com", "britannica.com", "fandom.com", "bloomberg.com", "reuters.com", "ft.com", "wsj.com",
    "forbes.com", "cnbc.com", "nikkei.com", "japantimes.co.jp", "straitstimes.com", "thestar.com.my",
    "bangkokpost.com", "smh.com.au", "afr.com", "abc.net.au", "timesofindia.com", "indiatimes.com",
    "hindustantimes.com", "ndtv.com", "moneycontrol.com", "laodong.vn", "vietnamnet.vn", "yahoo.com",
    "msn.com", "bing.com", "google.com", "baidu.com", "naver.com", "dnb.com", "zoominfo.com",
    "crunchbase.com", "pitchbook.com", "cbinsights.com", "owler.com", "craft.co", "emis.com",
    "kompass.com", "opencorporates.com", "yellowpages.com", "yellowpages.com.au", "yelp.com",
    "tripadvisor.com", "trustpilot.com", "glassdoor.com", "indeed.com", "seek.com.au", "jobstreet.com",
    "jobsdb.com", "justdial.com", "mapquest.com", "bbb.org", "6sense.com", "rocketreach.co",
    "apollo.io", "signalhire.com", "lusha.com", "datanyze.com", "companiesmarketcap.com",
    "marketscreener.com", "investing.com", "morningstar.com", "globaldata.com", "ditchcarbon.com",
    "databasesets.com", "bizdirlib.com", "cybo.com", "findglocal.com", "companiesdb.net",
    "dataforthai.com", "auscompanies.com", "aubiz.net", "bizly.com.au", "verif.com", "infobel.com",
    "sgpbusiness.com", "recordowl.com", "opengovsg.com", "abr.business.gov.au", "abn.business.gov.au",
    "asic.gov.au", "acnc.gov.au", "houjin-bangou.nta.go.jp", "ebsco.com", "sciencedirect.com",
    "researchgate.net", "springer.com", "jstor.org", "wipo.int", "sec.gov", "marklines.com",
    "play.google.com", "apps.apple.com", "amazon.com", "ebay.com", "alibaba.com", "made-in-china.com",
    "indiamart.com", "tradeindia.com", "europages.com", "github.com", "blogspot.com", "wordpress.com",
    "info-clipper.com", "infobelpro.com", "wiktionary.org", "b2bhint.com", "bizprofile.net",
    "baseconnect.in", "salesnow.jp", "nikkei.co.jp", "musubu.in", "alarmbox.jp", "en-hyouban.com",
    "openwork.jp", "mynavi.jp", "rikunabi.com", "doda.jp", "indeed.jp", "townwork.net", "bizmaps.or.jp",
})
GENERIC_SLUGS = {"auto", "air", "sports", "tech", "med", "news", "group", "holdings", "services", "online",
                 "shop", "global", "japan", "thai", "malaysia", "china", "korea", "school", "college",
                 "bank", "credit", "energy", "trade", "india", "home", "info", "corp", "company"}
SUCCESSOR_RE = re.compile(
    r"(?:acquired by|now part of|became part of|merged (?:into|with)|amalgamated with|"
    r"has been renamed(?: to)?|changed (?:its|our) name to|is now(?: known as)?)\s+"
    r"(?:the\s+)?([A-Z][\w&.,'\- ]{2,70}?)(?=[.,;()]|\s+(?:in|on|and|which|as|from)\s|$)")
CLOSED_STATUSES = {"CEASED", "SUCCEEDED", "IN_LIQUIDATION", "PENDING_CESSATION", "CLOSED"}


def country_name(code: str) -> str:
    names = COUNTRIES.get((code or "").upper(), {}).get("names", ())
    return names[0].title() if names else (code or "")


def is_excluded(host: str) -> bool:
    return host_matches(host, EXCLUDE_DOMAINS)


# --------------------------------------------------------------------------- search cache
class SearchCache:
    """SQLite cache of search results with an expiry. Failed searches are never stored, and
    results from fallback engines expire after a day so the best engine is tried again."""

    PRIMARY_ENGINE = "yahoo"
    FALLBACK_TTL_SECONDS = 86400

    def __init__(self, path: str, ttl_days: int = 60):
        self.db = sqlite3.connect(path, timeout=60.0)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS searches (query TEXT PRIMARY KEY, results_json TEXT, created_at TEXT)")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(searches)")}
        if "engine" not in columns:
            self.db.execute("ALTER TABLE searches ADD COLUMN engine TEXT DEFAULT ''")
        # Shared by all worker processes: one worker being blocked makes every worker back off.
        self.db.execute("CREATE TABLE IF NOT EXISTS engine_cooldown (engine TEXT PRIMARY KEY, until REAL)")
        self.db.commit()
        self.ttl_seconds = ttl_days * 86400

    def get(self, query: str) -> list[dict] | None:
        row = self.db.execute("SELECT results_json, created_at, engine FROM searches WHERE query = ?", (query,)).fetchone()
        if not row:
            return None
        try:
            age = time.time() - datetime.fromisoformat(row[1]).timestamp()
        except (TypeError, ValueError):
            return None
        ttl = self.ttl_seconds if (row[2] or "") == self.PRIMARY_ENGINE else self.FALLBACK_TTL_SECONDS
        return json.loads(row[0]) if age <= ttl else None

    def put(self, query: str, results: list[dict], engine: str = "") -> None:
        self.db.execute("INSERT OR REPLACE INTO searches (query, results_json, created_at, engine) VALUES (?, ?, ?, ?)",
                        (query, json.dumps(results, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), engine))
        self.db.commit()

    def cooldown_until(self, engine: str) -> float:
        row = self.db.execute("SELECT until FROM engine_cooldown WHERE engine = ?", (engine,)).fetchone()
        return float(row[0]) if row else 0.0

    def set_cooldown(self, engine: str, until: float) -> None:
        self.db.execute("INSERT OR REPLACE INTO engine_cooldown VALUES (?, ?)", (engine, until))
        self.db.commit()

    def close(self) -> None:
        self.db.close()


# --------------------------------------------------------------------------- Bing search
def decode_bing_url(href: str) -> str:
    """Unwrap Bing's /ck/a?...&u=a1<base64> redirect links without a network request."""
    href = html_lib.unescape(href or "")
    if "bing.com/ck/a" in href or href.startswith("/ck/a"):
        match = re.search(r"[?&]u=a1([^&]+)", href)
        if match:
            raw = match.group(1)
            try:
                return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "ignore")
            except Exception:
                return ""
    return href


def parse_bing_serp(page: str) -> tuple[list[dict], bool]:
    """Return (results, parsed_ok). parsed_ok is False for captcha/blocked pages."""
    results: list[dict] = []
    blocks = re.findall(r'<li class="b_algo"[^>]*>(.*?)</li>', page, re.S)
    for block in blocks:
        link = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not link:
            continue
        url = decode_bing_url(link.group(1))
        if not url.startswith("http") or "bing.com" in (urlsplit(url).hostname or ""):
            continue
        snippet = re.search(r'<p[^>]*>(.*?)</p>', block, re.S)
        clean = lambda s: html_lib.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()
        results.append({"url": url, "title": " ".join(clean(link.group(2)).split()),
                        "snippet": " ".join(clean(snippet.group(1) if snippet else "").split())[:400]})
    blocked = bool(re.search(r"captcha|verify you are a human|unusual traffic", page, re.I)) and not blocks
    parsed_ok = bool(blocks) or ("b_results" in page and not blocked) or "b_no" in page
    return results, parsed_ok


def _clean_html(fragment: str) -> str:
    return " ".join(html_lib.unescape(re.sub(r"<[^>]+>", " ", fragment or "")).split())


def parse_yahoo_serp(page: str) -> tuple[list[dict], bool]:
    results: list[dict] = []
    blocks = page.split('class="compTitle')[1:]
    for block in blocks:
        link = re.search(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not link:
            continue
        href = html_lib.unescape(link.group(1))
        target = re.search(r"/RU=([^/]+)/R[KS]", href)
        url = unquote(target.group(1)) if target else href
        host = urlsplit(url).hostname or ""
        if not url.startswith("http") or host.endswith(("yahoo.com", "yahoo.co.jp", "bing.com")):
            continue
        snippet = re.search(r'class="compText[^"]*"[^>]*>(.*?)</div>', block, re.S)
        results.append({"url": url, "title": _clean_html(link.group(2))[:200],
                        "snippet": _clean_html(snippet.group(1) if snippet else "")[:400]})
    ok = bool(results) or "compTitle" in page or "We did not find results" in page
    return list({r["url"]: r for r in results}.values()), ok


def parse_ddg_html(page: str) -> tuple[list[dict], bool]:
    results: list[dict] = []
    for match in re.finditer(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>(.*?)(?=class="result__a"|$)', page, re.S):
        href = html_lib.unescape(match.group(1))
        target = re.search(r"uddg=([^&]+)", href)
        url = unquote(target.group(1)) if target else ("https:" + href if href.startswith("//") else href)
        snippet = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', match.group(3), re.S)
        if url.startswith("http"):
            results.append({"url": url, "title": _clean_html(match.group(2))[:200],
                            "snippet": _clean_html(snippet.group(1) if snippet else "")[:400]})
    blocked = "anomaly" in page.lower() and not results
    return results, (bool(results) or "No results." in page) and not blocked


class WebSearch:
    """Free web search: Yahoo, then DuckDuckGo HTML, then Bing. An engine that blocks or
    rate-limits is cooled down and the next one is used. Only successful searches are cached."""

    ENGINES = (
        ("yahoo", "GET", "https://search.yahoo.com/search?p={q}&n=10", parse_yahoo_serp),
        ("duckduckgo", "POST", "https://html.duckduckgo.com/html/", parse_ddg_html),
        ("bing", "GET", "https://www.bing.com/search?q={q}&count=10&mkt=en-US", parse_bing_serp),
    )

    PRIMARY_MAX_WAIT = 120  # seconds worth waiting for the best engine
    DELAY = (1.0, 2.5)      # polite pause before each request

    def __init__(self, cache: SearchCache, concurrency: int = 2, max_results: int = 5, limiter=None):
        self.cache = cache
        self.limiter = limiter  # SharedRateLimiter: searches per minute for the whole machine
        self.max_results = max_results
        self._semaphore = asyncio.Semaphore(concurrency)
        self._session = None
        self._cooldown: dict[str, float] = {}
        self._strikes: dict[str, int] = {}
        self.failures = 0
        self.engine_counts: dict[str, int] = {}

    async def _get_session(self):
        if self._session is None:
            from curl_cffi.requests import AsyncSession
            self._session = AsyncSession(impersonate="chrome", timeout=15)
        return self._session

    async def _try(self, engine: tuple, query: str) -> list[dict] | None:
        name, method, template, parse = engine
        session = await self._get_session()
        if self.limiter is not None:
            await self.limiter.acquire()  # every request to a search engine counts, across all workers
        try:
            if method == "POST":
                response = await session.post(template, data={"q": query})
            else:
                response = await session.get(template.format(q=quote_plus(query)))
        except Exception as exc:
            LOG.warning("%s search error for %r: %s", name, query, exc)
            return None
        if response.status_code == 200:
            results, ok = parse(response.text)
            if ok:
                self._strikes[name] = 0
                return results
        strikes = self._strikes.get(name, 0) + 1
        self._strikes[name] = strikes
        self._cooldown[name] = time.time() + min(600, 30 * 2 ** (strikes - 1))
        self.cache.set_cooldown(name, self._cooldown[name])
        if self.limiter is not None:
            self.limiter.slow_down()  # every worker on this machine searches more slowly for a while
        LOG.warning("%s search blocked/failed (HTTP %s); cooling down %ss", name, response.status_code,
                    int(self._cooldown[name] - time.time()))
        return None

    async def search(self, query: str) -> list[dict]:
        cached = self.cache.get(query)
        if cached is not None:
            return cached[:self.max_results]
        async with self._semaphore:
            for _ in range(3):
                # The first engine gives the best results: wait for a short cool-down rather
                # than falling back to an engine with poorer results.
                for name, *_ in self.ENGINES:  # pick up cool-downs set by other workers
                    self._cooldown[name] = max(self._cooldown.get(name, 0), self.cache.cooldown_until(name))
                primary_wait = self._cooldown.get(self.ENGINES[0][0], 0) - time.time()
                if 0 < primary_wait <= self.PRIMARY_MAX_WAIT:
                    await asyncio.sleep(primary_wait)
                for engine in self.ENGINES:
                    if self._cooldown.get(engine[0], 0) > time.time():
                        continue
                    await asyncio.sleep(random.uniform(*self.DELAY))
                    results = await self._try(engine, query)
                    if results is not None:
                        self.cache.put(query, results, engine[0])
                        self.engine_counts[engine[0]] = self.engine_counts.get(engine[0], 0) + 1
                        return results[:self.max_results]
                wait = min(self._cooldown.values(), default=time.time()) - time.time()
                if wait > 0:
                    LOG.warning("All search engines cooling down; waiting %ss", int(wait))
                    await asyncio.sleep(wait)
        self.failures += 1
        return []

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()


# --------------------------------------------------------------------------- Internet Archive
async def wayback_candidates(domain: str, timeout: float = 15.0) -> tuple[list[str], str]:
    """Look at the last archived copy of a dead domain. Returns (candidate hosts, archived text)."""
    from curl_cffi.requests import AsyncSession
    from .extractor import extract_page
    try:
        async with AsyncSession(impersonate="chrome", timeout=timeout) as session:
            meta = await session.get(f"https://archive.org/wayback/available?url={quote_plus(domain)}")
            snapshot = (meta.json().get("archived_snapshots") or {}).get("closest") or {}
            if not snapshot.get("available") or not snapshot.get("url"):
                return [], ""
            archived = await session.get(snapshot["url"].replace("http://", "https://", 1))
            if archived.status_code != 200:
                return [], ""
            body = archived.text[:800_000]
    except Exception as exc:
        LOG.debug("Wayback lookup failed for %s: %s", domain, exc)
        return [], ""
    page = extract_page(f"https://{domain}/", body, 200)
    old_reg = registered_domain(domain)
    hosts: list[str] = []
    refresh = re.search(r'http-equiv=["\']?refresh["\']?[^>]*url=([^"\'>\s]+)', body, re.I)
    raw_links = [refresh.group(1)] if refresh else []
    raw_links += [link["url"] for link in page.get("links", [])
                  if re.search(r"move|new (?:web)?site|visit|relocat|renew|official|移転|新しい", link.get("anchor", ""), re.I)]
    if page.get("canonical_url"):
        raw_links.append(page["canonical_url"])
    for link in raw_links:
        target = unquote(link)
        archived_target = re.search(r"/web/\d+[a-z_]*/(https?://[^\s]+)", target)
        if archived_target:
            target = archived_target.group(1)
        host = (urlsplit(target).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if host and registered_domain(host) != old_reg and "archive.org" not in host and not is_excluded(host):
            hosts.append(host)
    return list(dict.fromkeys(hosts))[:3], page.get("visible_text", "")[:20000]


# --------------------------------------------------------------------------- discovery
def build_queries(ctx: dict, legacy_domain: str = "") -> list[str]:
    """Up to four wordings: register legal name / local-script name (exact), the data's full
    name in quotes + country, the core name + "official website", and the old domain's slug."""
    name = strip_territory_tag(ctx["name"])
    core = core_name(ctx["name"]) or name
    where = country_name(ctx["country"])
    register = ctx.get("register") or {}
    queries = []
    if register.get("local_name"):
        queries.append(f'"{register["local_name"]}"')
    if register.get("legal_name") and fold(register["legal_name"]) != fold(name):
        queries.append(f'"{register["legal_name"]}" {where}'.strip())
    queries += [f'"{name}" {where}'.strip(), f"{core} {where} official website".strip()]
    if legacy_domain:
        label = (registered_domain(normalize_domain(legacy_domain).normalized_domain) or "").split(".")[0]
        if len(label) >= 4 and label not in GENERIC_SLUGS and not label.isdigit():
            queries.append(f"{label} {core} {where}".strip())
    return list(dict.fromkeys(q for q in queries if q.strip('" ')))[:4]


def successor_from_text(texts: list[str], name: str) -> str:
    own = set(distinctive_tokens(name))
    for text in texts:
        for match in SUCCESSOR_RE.finditer(text or ""):
            successor = match.group(1).strip(" .,'")
            tokens = set(distinctive_tokens(successor))
            if tokens and tokens != own and len(successor) >= 3:
                return successor
    return ""


def relevance_terms(ctx: dict, legacy_domain: str = "") -> tuple[list[str], list[str]]:
    """Words that a relevant search result must mention: (text terms, host terms)."""
    tokens = distinctive_tokens(ctx["name"])
    core_words = [w for w in re.split(r"[^\w]+", fold(core_name(ctx["name"]))) if w]
    host_terms = [t for t in tokens if len(t) >= 4 or any(c.isdigit() for c in t)]
    if not tokens and core_words:  # short names such as "A S N": use the whole core name
        tokens = [" ".join(core_words)]
        host_terms = ["".join(core_words)] if len("".join(core_words)) >= 3 else []
    label = (registered_domain(normalize_domain(legacy_domain).normalized_domain) or "").split(".")[0] if legacy_domain else ""
    if len(label) >= 4 and label not in GENERIC_SLUGS:
        host_terms.append(label)
    register = ctx.get("register") or {}
    if register.get("local_core_name"):
        tokens = tokens + [register["local_core_name"]]  # Japanese name in result titles
    if register.get("legal_name"):
        extra = distinctive_tokens(register["legal_name"])
        tokens = tokens + extra
        host_terms = host_terms + [t for t in extra if len(t) >= 4]
    return list(dict.fromkeys(tokens)), list(dict.fromkeys(host_terms))


def is_relevant(item: dict, host: str, terms: tuple[list[str], list[str]]) -> bool:
    from .matching import word_in
    text_terms, host_terms = terms
    text = f"{item.get('title', '')} {item.get('snippet', '')}"
    compact_host = re.sub(r"[^a-z0-9]", "", host.lower())
    return any(word_in(text, t) for t in text_terms) or any(t in compact_host for t in host_terms)


def candidate_hosts(results: list[dict], exclude: set[str], terms: tuple[list[str], list[str]] | None = None) -> list[str]:
    """Usable candidate hosts in result order: not excluded, not a document, and (when terms
    are given) mentioning the company in the title, snippet or address."""
    hosts: list[str] = []
    for item in results:
        url = item.get("url", "")
        host = (urlsplit(url).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if not host or is_excluded(host) or host in exclude or url.lower().endswith(".pdf"):
            continue
        if terms is not None and not is_relevant(item, host, terms):
            continue
        hosts.append(host)
    return list(dict.fromkeys(hosts))


def _score(result: dict) -> tuple:
    """Rank accepted candidates: strong proof, name in the site identity, domain resembling
    the name, country match, then earlier search position."""
    types = {p["type"] for p in result.get("proofs", [])}
    return (result.get("proof_level") == STRONG,
            bool(types & {"name_in_site_identity", "brand_in_site_identity"}),
            "domain_looks_like_name" in types, "country_match" in types,
            -result.get("candidate_rank", 99))


class Discoverer:
    """Per-row discovery. Search results and domain inspections are cached, so rows that
    share a query cost nothing extra, but every row is judged on its own details."""

    def __init__(self, search: WebSearch, inspect, max_candidates: int = 6, use_wayback: bool = True):
        self.search = search
        self.inspect = inspect  # async (host) -> DomainRecord, cached by the caller
        self.max_candidates = max_candidates
        self.use_wayback = use_wayback

    async def _judge(self, row: dict, host: str, found_via: str, rank: int = 0) -> dict | None:
        try:
            record = await self.inspect(host)
        except Exception as exc:
            LOG.debug("Candidate %s failed: %s", host, exc)
            return None
        result = decide(row, normalize_domain(host), record, found_via=found_via)
        result["candidate_host"] = host
        result["candidate_rank"] = rank
        return result

    async def discover(self, row: dict, legacy_domain: str = "", registry_status: str = "",
                       exclude_hosts: tuple[str, ...] = (), wayback: bool = True) -> dict:
        """legacy_domain: the old/dead domain (used for the archive lookup and a slug query).
        exclude_hosts: hosts already judged wrong for this row, never re-proposed."""
        ctx = row_context(row)
        tried: list[str] = []
        judged: list[dict] = []
        texts: list[str] = []
        legacy_host = normalize_domain(legacy_domain).normalized_domain if legacy_domain else ""
        exclude = {h for h in exclude_hosts if h}
        # 1. The old site's archived copy often says where the company moved.
        if self.use_wayback and wayback and legacy_host:
            hosts, archived_text = await wayback_candidates(legacy_host)
            texts.append(archived_text)
            for host in hosts:
                tried.append(host)
                result = await self._judge(row, host, "WAYBACK", len(tried))
                if result:
                    judged.append(result)
        # 2. Search with up to three wordings; top results of each.
        if not any(r["decision"] == ACCEPT and r["proof_level"] == STRONG for r in judged):
            terms = relevance_terms(ctx, legacy_domain)
            for query in build_queries(ctx, legacy_domain):
                results = await self.search.search(query)
                texts += [f"{r.get('title', '')}. {r.get('snippet', '')}" for r in results]
                for host in candidate_hosts(results, exclude | set(tried), terms):
                    if len(tried) >= self.max_candidates:
                        break
                    tried.append(host)
                    result = await self._judge(row, host, "SEARCH", len(tried))
                    if result:
                        judged.append(result)
                        if result["decision"] == ACCEPT and result["proof_level"] == STRONG:
                            break
                if any(r["decision"] == ACCEPT and r["proof_level"] == STRONG for r in judged):
                    break
        accepted = sorted((r for r in judged if r["decision"] == ACCEPT), key=_score, reverse=True)
        summary = {"candidates_tried": tried,
                   "candidates_judged": [f"{r['candidate_host']}:{r['outcome']}" for r in judged]}
        if accepted:
            best = accepted[0]
            return {**best, **summary, "new_domain": best["candidate_host"]}
        # 3. Closed or merged companies: the official register names the successor when known.
        register = ctx.get("register") or {}
        if register.get("status") == "CLOSED":
            registry_status = registry_status if (registry_status or "").upper() in CLOSED_STATUSES else "CLOSED"
        successor = register.get("successor_name") or successor_from_text(texts, ctx["name"])
        if successor:
            successor_row = {**row, "original_company_name": successor, "Organization Name": successor, "_register": {}}
            successor_queries = build_queries({**ctx, "name": successor, "register": {}})[:1]
            for query in successor_queries:
                successor_terms = relevance_terms({**ctx, "name": successor, "register": {}})
                for host in candidate_hosts(await self.search.search(query), exclude, successor_terms)[:3]:
                    result = await self._judge(successor_row, host, "SEARCH", 0)
                    if result and result["decision"] == ACCEPT and result.get("proof_level") == STRONG:
                        result.update(outcome="MERGED_INTO", answer_type="SUCCESSOR_SITE",
                                      reason=f"Company appears to be merged into / acquired by '{successor}'; "
                                             f"successor's site verified: {result['reason']}")
                        return {**result, **summary, "successor": successor, "new_domain": host,
                                "verification_status": "MERGED_INTO"}
        if (registry_status or "").upper() in CLOSED_STATUSES:
            return {**summary, "decision": "REJECT", "outcome": "COMPANY_CLOSED", "successor": successor,
                    "reason": f"Registry status is {registry_status} and no current website was found"
                              + (f"; possible successor '{successor}'." if successor else "."),
                    "new_domain": "", "proof_level": "NONE", "proofs": [], "found_via": "REGISTRY"}
        best_review = sorted((r for r in judged if r["decision"] == "REVIEW"), key=_score, reverse=True)
        closest = next((r["candidate_host"] for r in best_review if r["outcome"] == "INSUFFICIENT_PROOF"
                        and any(p["is_name"] or p["type"] == "domain_looks_like_name" for p in r.get("proofs", []))), "")
        return {**summary, "decision": "REVIEW", "outcome": "NOT_FOUND", "successor": successor, "suggested": closest,
                "reason": ("No candidate website proved the company." if tried else "Search returned no usable candidates.")
                          + (f" Closest: {best_review[0]['candidate_host']} ({best_review[0]['reason']})" if best_review else ""),
                "new_domain": "", "proof_level": "NONE", "proofs": [], "found_via": "SEARCH"}
