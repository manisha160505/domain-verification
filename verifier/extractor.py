"""Extract compact, traceable identity signals from bounded pages."""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlsplit

KEYWORDS = ("about", "company", "corporate", "contact", "legal", "privacy", "terms", "imprint",
            "subsidiar", "profile", "overview", "outline", "kaisha", "gaiyo", "会社", "概要")
PARKED = re.compile(
    r"domain (?:is |name )?(?:for sale|expired|parked|has expired)|this domain (?:may be|is) for sale|"
    r"buy this domain|make an offer on this domain|sedo(?:parking| domain parking)|afternic|dan\.com|"
    r"hugedomains|parkingcrew|bodis|above\.com|domain parking|parked free|godaddy (?:domain|parking)|"
    r"this site can.t be reached|account (?:has been )?suspended|website (?:is )?(?:expired|suspended)|"
    r"web hosting account|future home of|default web site page|welcome to nginx|apache2? (?:ubuntu )?default page|"
    r"it works!|index of /|coming soon|under construction",
    re.I,
)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[a-zA-Z]{2,}")
# A phone number needs a country code or a phone label; bare digit runs (years, prices) are ignored.
PHONE = re.compile(r"(?:(?:tel|phone|ph|call|fax|電話|전화|โทร)\.?\s*[:：]?\s*|(?<![\w])\+\s?)\(?\d[\d\s().\-]{6,18}\d", re.I)
REGISTRATION = re.compile(r"(?:registration|company|business|vat|uen|abn|acn|corporate|reg\.?)\s*(?:no\.?|number|id|#)\s*[:.]?\s*[A-Z0-9-]{5,25}", re.I)
COPYRIGHT = re.compile(r"(?:©|\(c\)|copyright)\s*(?:\d{4}(?:\s*[-–]\s*\d{4})?)?[\s,]*([^|\n©]{2,160}?)(?=\s*(?:all rights reserved|\||©|copyright|$))", re.I)
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: list[str] = []
        self.h1: list[str] = []
        self.h2: list[str] = []
        self.text: list[str] = []
        self.footer: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.meta_description = ""
        self.og_site_name = ""
        self.canonical = ""
        self.jsonld_raw: list[str] = []
        self._jsonld_current: list[str] = []
        self._tags: list[tuple[str, bool]] = []  # (tag, opens a footer region)
        self._skip = 0
        self._jsonld = False
        self._anchor: list[str] = []

    def _in(self, name: str) -> bool:
        return any(tag == name for tag, _ in self._tags)

    def _in_footer(self) -> bool:
        return any(flag for _, flag in self._tags)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        if tag not in VOID_TAGS:
            marker = " ".join((attr.get("class") or "", attr.get("id") or "", attr.get("role") or "")).lower()
            self._tags.append((tag, tag == "footer" or "footer" in marker or "contentinfo" in marker or "copyright" in marker))
        if tag in ("style", "noscript"):
            self._skip += 1
        if tag == "script":
            self._jsonld = (attr.get("type") or "").lower() == "application/ld+json"
            if not self._jsonld:
                self._skip += 1
        if tag == "meta":
            key = (attr.get("name") or attr.get("property") or "").lower()
            if key in ("description", "og:description") and not self.meta_description:
                self.meta_description = (attr.get("content") or "")[:500]
            elif key == "og:site_name":
                self.og_site_name = (attr.get("content") or "")[:200]
        if tag == "link" and "canonical" in (attr.get("rel") or "").lower():
            self.canonical = attr.get("href") or ""
        if tag == "a":
            self._anchor = []
            self.links.append((attr.get("href") or "", ""))

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.links:
            href, _ = self.links[-1]
            self.links[-1] = (href, " ".join(self._anchor)[:120])
            self._anchor = []
        if tag == "script":
            if self._jsonld:
                self.jsonld_raw.append("".join(self._jsonld_current))
                self._jsonld_current = []
                self._jsonld = False
            elif self._skip:
                self._skip -= 1
        elif tag in ("style", "noscript") and self._skip:
            self._skip -= 1
        for index in range(len(self._tags) - 1, -1, -1):
            if self._tags[index][0] == tag:
                del self._tags[index:]
                break

    def handle_data(self, data: str) -> None:
        if self._jsonld:
            self._jsonld_current.append(data)
            return
        if self._skip:
            return
        clean = " ".join(data.split())
        if not clean:
            return
        self.text.append(clean)
        if self._in("title"):
            self.title.append(clean)
        if self._in("h1"):
            self.h1.append(clean)
        if self._in("h2"):
            self.h2.append(clean)
        if self._in_footer():
            self.footer.append(clean)
        if self._in("a"):
            self._anchor.append(clean)


def _schemas(raw: list[str]) -> list[dict]:
    result: list[dict] = []
    for item in raw:
        try:
            data = json.loads(item)
        except (json.JSONDecodeError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                if "@graph" in node:
                    graph = node["@graph"]
                    stack.extend(graph if isinstance(graph, list) else [graph])
                if any(t in str(node.get("@type", "")) for t in ("Organization", "Corporation", "LocalBusiness", "WebSite")):
                    result.append({k: node.get(k) for k in ("@type", "name", "legalName", "alternateName", "url", "parentOrganization", "subOrganization", "address", "telephone", "email", "taxID", "vatID", "sameAs", "brand") if k in node})
    return result[:30]


def extract_page(url: str, html: str, status: int) -> dict:
    parser = _Parser()
    try:
        parser.feed(html)
    except Exception:
        pass
    text = " ".join(parser.text)[:50_000]
    footer = " ".join(parser.footer)[:8000]
    links = []
    emails = set(EMAIL.findall(text))
    host = (urlsplit(url).hostname or "").lower()
    for href, anchor in parser.links[:1500]:
        if href.lower().startswith("mailto:"):
            address = unquote(href[7:].split("?", 1)[0]).strip()
            if EMAIL.fullmatch(address):
                emails.add(address)
            continue
        full = urljoin(url, href).split("#", 1)[0]
        if urlsplit(full).scheme not in ("http", "https"):
            continue
        links.append({"url": full, "anchor": anchor,
                      "internal": (urlsplit(full).hostname or "").lower() == host})
    social_hosts = ("linkedin.com", "facebook.com", "instagram.com", "x.com", "twitter.com", "youtube.com")
    social_links = [link["url"] for link in links
                    if any((urlsplit(link["url"]).hostname or "").lower() in (h, "www." + h) for h in social_hosts)]
    schemas = _schemas(parser.jsonld_raw)
    addresses = [schema["address"] for schema in schemas if schema.get("address")]
    copyright_lines = [" ".join(m.group(0).split()) for source in (footer, text[-4000:])
                       for m in COPYRIGHT.finditer(source)]
    head = " ".join(parser.title) + " " + text[:1500]
    return {
        "url": url, "status": status, "title": " ".join(parser.title)[:300],
        "description": parser.meta_description, "site_name": parser.og_site_name,
        "h1": parser.h1[:10], "h2": parser.h2[:20],
        "visible_text": text, "text_length": len(text), "footer": footer,
        "copyright": list(dict.fromkeys(copyright_lines))[:5],
        "canonical_url": urljoin(url, parser.canonical) if parser.canonical else "",
        "jsonld": schemas, "emails": sorted(emails)[:30],
        "phones": list(dict.fromkeys(" ".join(p.split()) for p in PHONE.findall(text)))[:20],
        "addresses": addresses[:20], "social_links": list(dict.fromkeys(social_links))[:30],
        "registrations": REGISTRATION.findall(text)[:15],
        "links": links[:500],
        "parked": bool(PARKED.search(head)) and len(text) < 3000,
        "js_shell": len(text) < 250 and ("__NEXT_DATA__" in html or "id=\"root\"" in html or "id=\"app\"" in html
                                          or "enable javascript" in html.lower()),
    }


def relevant_links(page: dict, max_pages: int, sister_domain: str = "") -> list[str]:
    """Pick about/contact/legal pages; links to sister hosts of the same registered domain
    (e.g. corporate.example.com) count as internal."""
    scored: list[tuple[int, str]] = []
    for link in page.get("links", []):
        link_host = (urlsplit(link["url"]).hostname or "").lower()
        sister = bool(sister_domain) and (link_host == sister_domain or link_host.endswith("." + sister_domain))
        if not (link["internal"] or sister):
            continue
        path = unquote(urlsplit(link["url"]).path).lower()
        label = link["anchor"].lower()
        score = sum(3 if key in path else 1 if key in label else 0 for key in KEYWORDS)
        if score:
            scored.append((score, link["url"]))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return list(dict.fromkeys(url for _, url in scored))[:max_pages]


def latin_share(text: str) -> float:
    """Share of letters that are Latin; low for Japanese/Korean/Thai/Chinese pages."""
    letters = [c for c in text[:20000] if c.isalpha()]
    if not letters:
        return 1.0
    return sum(c.isascii() for c in letters) / len(letters)


_ENGLISH_ANCHOR = re.compile(r"^\s*(english|en|eng|english site|english page|global \(english\))\s*$", re.I)
_ENGLISH_PATH = re.compile(r"(^|/)(en|eng|english|en-us|en-gb|en_us|global/en)(/|$|\.html?$)|[?&](lang|hl|locale)=en", re.I)


def english_link(page: dict, sister_domain: str) -> str:
    """The site's own link to its English version (same registered domain), if any."""
    for link in page.get("links", []):
        url = link["url"]
        host = (urlsplit(url).hostname or "").lower()
        if sister_domain and not (host == sister_domain or host.endswith("." + sister_domain)):
            continue
        if _ENGLISH_ANCHOR.match(link.get("anchor", "")) or _ENGLISH_PATH.search(urlsplit(url).path + "?" + urlsplit(url).query) \
                or host.startswith(("en.", "english.")):
            if url.rstrip("/") != page.get("url", "").rstrip("/"):
                return url
    return ""
