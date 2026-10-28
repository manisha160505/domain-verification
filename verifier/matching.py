"""Whole-word name, country and host matching shared by the decision engine and discovery.

Fragment matching ("star" inside "Starlight", "in" inside "Singapore") is never used:
phrases match only between word boundaries, and countries match only by full name,
city, phone country code or ccTLD, never by two-letter codes in page text.
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

# Legal endings stripped to get a company's core name. Longest forms first.
LEGAL_SUFFIX_RE = re.compile(
    r"\b(?:"
    r"public\s+company\s+limited|public\s+limited\s+company|"
    r"pte\.?\s*ltd\.?|pty\.?\s*ltd\.?|sdn\.?\s*bhd\.?|co\.?,?\s*ltd\.?|"
    r"private\s+limited|proprietary\s+limited|company\s+limited|"
    r"sendirian\s+berhad|berhad|bhd\.?|"
    r"limited|ltd\.?|incorporated|inc\.?|corporation|corp\.?|"
    r"llc|l\.l\.c\.?|plc|p\.l\.c\.?|gmbh|b\.v\.?|bv|"
    r"k\.k\.?|kk|kabushiki\s+kaisha|g\.k\.?|godo\s+kaisha|"
    r"joint\s+stock\s+company|jsc|tbk|pt|pcl|"
    r"co\.?|company"
    r")(?=\W|$)",
    re.IGNORECASE,
)
# Legal endings cut off by a column width limit ("... COMPANY, INCO", "... CO., LIMI"). Only exact
# prefixes of legal words are removed, so real words such as "Corporate" are never cut.
_LEGAL_WORDS = ("incorporated", "limited", "corporation", "company", "proprietary", "berhad",
                "kabushiki", "kaisha", "sendirian", "private")
_PREFIXES = sorted({w[:n] for w in _LEGAL_WORDS for n in range(3, len(w) + 1)}, key=len, reverse=True)
TRUNCATED_LEGAL_RE = re.compile(r"[\s,.]+(?:" + "|".join(_PREFIXES) + r")\.?$", re.IGNORECASE)
# Trailing account tags such as " - JP", " - FED - AU", " (M)", " (EXPORT)".
TERRITORY_TAG_RE = re.compile(r"(?:\s*-\s*[A-Z]{2,4})+\s*$|\s*\([A-Z\s.]{1,20}\)\s*$")
LEADING_THE_RE = re.compile(r"^\s*the\s+|,\s*the\s*$", re.IGNORECASE)

GENERIC_TOKENS = {
    "and", "the", "of", "for", "in", "at", "by", "to", "co", "company", "group", "holdings",
    "holding", "services", "service", "international", "global", "national", "asia", "pacific",
    "enterprises", "enterprise", "industries", "industry", "technology", "technologies",
    "solutions", "management", "investments", "investment", "trading", "products", "systems",
    "corp", "corporation", "ltd", "limited", "inc", "pte", "pty", "sdn", "bhd", "japan",
    "australia", "malaysia", "singapore", "thailand", "korea", "vietnam", "indonesia",
    "philippines", "new", "zealand", "bank", "office", "branch", "dept", "department",
}

GOVERNMENT_TERMS = (
    "department", "dept", "ministry", "government", "court", "authority", "commission",
    "council", "parliament", "police", "customs", "bureau", "agency", "municipality",
    "state of", "city of", "office of", "federal", "army", "navy", "air force", "defence",
    "defense", "embassy", "prefecture", "university",
)

# Country evidence: full names (English + local), major cities, phone codes.
COUNTRIES: dict[str, dict[str, tuple[str, ...]]] = {
    "AU": {"names": ("australia", "australian"), "cities": ("sydney", "melbourne", "brisbane", "perth", "adelaide", "canberra", "hobart", "darwin", "nsw", "new south wales", "victoria", "queensland"), "phone": ("61",)},
    "JP": {"names": ("japan", "japanese", "日本"), "cities": ("tokyo", "osaka", "nagoya", "yokohama", "kyoto", "kobe", "fukuoka", "sapporo", "東京", "大阪"), "phone": ("81",)},
    "MY": {"names": ("malaysia", "malaysian"), "cities": ("kuala lumpur", "selangor", "penang", "johor", "petaling jaya", "shah alam", "putrajaya"), "phone": ("60",)},
    "TH": {"names": ("thailand", "thai", "ประเทศไทย"), "cities": ("bangkok", "chiang mai", "phuket", "nonthaburi", "กรุงเทพ"), "phone": ("66",)},
    "SG": {"names": ("singapore", "singaporean"), "cities": (), "phone": ("65",)},
    "KR": {"names": ("korea", "korean", "south korea", "republic of korea", "대한민국", "한국"), "cities": ("seoul", "busan", "incheon", "daegu", "서울"), "phone": ("82",)},
    "NZ": {"names": ("new zealand",), "cities": ("auckland", "wellington", "christchurch", "hamilton"), "phone": ("64",)},
    "ID": {"names": ("indonesia", "indonesian"), "cities": ("jakarta", "surabaya", "bandung", "medan"), "phone": ("62",)},
    "PH": {"names": ("philippines", "philippine", "filipino"), "cities": ("manila", "makati", "quezon city", "cebu", "taguig", "pasig"), "phone": ("63",)},
    "VN": {"names": ("vietnam", "viet nam", "vietnamese", "việt nam"), "cities": ("hanoi", "ha noi", "ho chi minh", "saigon", "da nang"), "phone": ("84",)},
    "IN": {"names": ("india", "indian"), "cities": ("mumbai", "delhi", "bengaluru", "bangalore", "chennai", "hyderabad", "pune"), "phone": ("91",)},
    "CN": {"names": ("china", "chinese", "中国"), "cities": ("beijing", "shanghai", "shenzhen", "guangzhou"), "phone": ("86",)},
    "HK": {"names": ("hong kong",), "cities": ("kowloon",), "phone": ("852",)},
    "TW": {"names": ("taiwan", "台灣", "台湾"), "cities": ("taipei",), "phone": ("886",)},
    "US": {"names": ("united states", "u.s.a.", "usa"), "cities": ("new york", "california", "texas"), "phone": ()},
    "GB": {"names": ("united kingdom", "england", "scotland"), "cities": ("london", "manchester"), "phone": ("44",)},
}
CCTLD_COUNTRY = {"au": "AU", "jp": "JP", "my": "MY", "th": "TH", "sg": "SG", "kr": "KR", "nz": "NZ",
                 "id": "ID", "ph": "PH", "vn": "VN", "in": "IN", "cn": "CN", "hk": "HK", "tw": "TW",
                 "uk": "GB", "us": "US"}

_CJK_RE = re.compile(r"[぀-ヿ㐀-鿿가-힯฀-๿]")


def fold(text: object) -> str:
    """Unicode-normalised, case-folded text with collapsed whitespace."""
    value = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return re.sub(r"\s+", " ", value).strip()


@lru_cache(maxsize=50_000)
def _phrase_pattern(phrase: str) -> re.Pattern | None:
    words = [re.escape(w) for w in phrase.split() if w]
    if not words:
        return None
    body = r"[\s\-–,.&/]*".join(words)
    if _CJK_RE.search(phrase):  # scripts without spaces between words
        return re.compile(body)
    return re.compile(r"(?<![\w])" + body + r"(?![\w])")


def word_in(text: str, phrase: str) -> bool:
    """True if phrase occurs in text as whole words (case/width insensitive)."""
    phrase = fold(phrase)
    if not phrase:
        return False
    pattern = _phrase_pattern(phrase)
    return bool(pattern and pattern.search(fold(text)))


def find_phrase(text: str, phrase: str, context: int = 140) -> str:
    """Return the text around a whole-word match of phrase, or ''."""
    folded_phrase = fold(phrase)
    pattern = _phrase_pattern(folded_phrase) if folded_phrase else None
    if not pattern:
        return ""
    folded = fold(text)
    match = pattern.search(folded)
    if not match:
        return ""
    return folded[max(0, match.start() - context):match.end() + context]


def strip_territory_tag(name: str) -> str:
    return TERRITORY_TAG_RE.sub("", str(name or "")).strip(" ,.-")


def untruncate(name: str) -> tuple[str, bool]:
    """Remove a cut-off legal ending ("... COMPANY, INCO" -> "... COMPANY"). Complete legal
    words are left alone. Returns (name, was_truncated)."""
    value = strip_territory_tag(name)
    match = TRUNCATED_LEGAL_RE.search(value)
    if match and match.group(0).strip(" ,.").lower() not in _LEGAL_WORDS:
        return value[:match.start()].strip(" ,.-"), True
    return value, False


def core_name(name: str) -> str:
    """Company name without territory tag, legal ending, leading 'The' or punctuation."""
    value, _ = untruncate(name)
    value = LEADING_THE_RE.sub(" ", value)
    previous = None
    while previous != value:  # strip stacked endings such as "Co., Ltd."
        previous = value
        value = LEGAL_SUFFIX_RE.sub(" ", value)
    value = re.sub(r"[^\w&'\s-]", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip(" -&'")


def distinctive_tokens(name: str) -> list[str]:
    tokens = [t for t in re.split(r"[^\w]+", fold(core_name(name))) if t]
    # Short brand tokens are kept when they mix letters and digits ("3m", "7eleven").
    return [t for t in tokens if t not in GENERIC_TOKENS and not t.isdigit()
            and (len(t) >= 3 or (len(t) == 2 and any(c.isdigit() for c in t)))]


def name_variants(name: str) -> list[str]:
    """Full name, core name, and the core name without a leading account number
    ("394-TEMASEK LABORATORIES" -> "TEMASEK LABORATORIES") when at least two distinctive
    words remain, so "77 BANK" is never reduced to "BANK"."""
    variants = [untruncate(name)[0], core_name(name)]
    without_number = re.sub(r"^\d+[\s\-_.]+", "", core_name(name))
    if without_number != core_name(name) and len(distinctive_tokens(without_number)) >= 2:
        variants.append(without_number)
    return list(dict.fromkeys(v for v in variants if v))


def name_in(text: str, name: str) -> str:
    """Return the matched form if the full name or its core name appears as whole words."""
    for candidate in name_variants(name):
        if len(fold(candidate)) >= 3 and word_in(text, candidate):
            return candidate
    return ""


_SUFFIX_SYNONYMS = {
    "limited": "ltd", "corporation": "corp", "incorporated": "inc", "company": "co",
    "proprietary": "pty", "private": "pte", "berhad": "bhd", "sendirian": "sdn",
    "kabushiki": "kk", "kaisha": "", "public": "pub",
}


def _legal_tokens(text: str) -> str:
    tokens = re.split(r"[^\w]+", fold(text))
    mapped = (_SUFFIX_SYNONYMS.get(t, t) for t in tokens if t)
    return " " + " ".join(t for t in mapped if t) + " "


def has_legal_suffix(name: str) -> bool:
    value, truncated = untruncate(name)
    return truncated or bool(LEGAL_SUFFIX_RE.search(value))


def legal_name_in(text: str, name: str) -> bool:
    """Full legal name (with its legal ending) on the page, ignoring punctuation and
    treating Ltd/Limited, Corp/Corporation, Co/Company etc. as the same."""
    if _CJK_RE.search(name or ""):  # local scripts have no spaces between words: substring match
        wanted = re.sub(r"\s+", "", fold(name))
        return len(wanted) >= 2 and wanted in re.sub(r"\s+", "", fold(text))
    # A cut-off name ("... COMPANY, INCO") matches the page's full legal name by its intact part.
    value, _ = untruncate(name)
    wanted = _legal_tokens(LEADING_THE_RE.sub(" ", value))
    if len(wanted.strip()) < 4:
        return False
    return wanted in _legal_tokens(text)


def is_government_name(name: str) -> bool:
    return any(word_in(name, term) for term in GOVERNMENT_TERMS)


def is_government_host(host: str) -> bool:
    labels = (host or "").lower().split(".")
    return any(label in ("gov", "go", "govt", "mil") for label in labels[1:]) or (host or "").endswith((".gov", ".mil"))


def country_evidence(text: str, country_code: str) -> str:
    """Return the term proving the country (full name, city or phone code), or ''."""
    info = COUNTRIES.get((country_code or "").upper())
    if not info:
        return ""
    for term in info["names"] + info["cities"]:
        if word_in(text, term):
            return term
    for code in info["phone"]:
        if re.search(r"(?:\+|00)\s?\(?" + code + r"\)?[\s\-.(]", text or ""):
            return "+" + code
    return ""


def cctld_country(host: str) -> str:
    tld = (host or "").lower().rstrip(".").rsplit(".", 1)[-1]
    return CCTLD_COUNTRY.get(tld, "")


def host_matches(host: str, domains) -> bool:
    """True if host equals a listed domain or is a subdomain of it (never a fragment)."""
    host = (host or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return any(host == d or host.endswith("." + d) for d in domains)


def label_looks_like_name(registered: str, name: str) -> bool:
    """Hint only: the domain's main label resembles the company name."""
    label = (registered or "").split(".")[0].lower()
    if len(label) < 3:
        return False
    tokens = distinctive_tokens(name)
    compact = "".join(tokens)
    initials = "".join(t[0] for t in re.split(r"[^\w]+", fold(core_name(name))) if t)
    return label in tokens or (len(label) >= 4 and compact.startswith(label)) or \
        (len(compact) >= 5 and compact in label) or (len(initials) >= 3 and label == initials)


def has_local_script(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


_AU_STATE_POSTCODE = re.compile(r"\b(NSW|VIC|QLD|WA|SA|TAS|ACT|NT)\s*,?\s*(\d{4})\b", re.I)
_JP_POSTCODE = re.compile(r"(?<!\d)(\d{3})-(\d{4})(?!\d)")
_LONG_POSTCODE = re.compile(r"(?<![\d-])(\d{5,6})(?![\d-])")
_JP_PLACE = re.compile(r"(東京都|北海道|(?:京都|大阪)府|.{2,3}?県)\s*,?\s*(.+?[市区町村郡])")


def address_evidence(text: str, addresses: list[str]) -> str:
    """Return what matched if a registered address appears on the site: an Australian state +
    postcode, a Japanese prefecture + city/ward or postcode, or a 5-6 digit postcode. A bare
    4-digit number is never used (it looks like a year)."""
    page = fold(text)
    compact = re.sub(r"\s+", "", page)
    for address in addresses or []:
        value = unicodedata.normalize("NFKC", address or "")
        for state, code in _AU_STATE_POSTCODE.findall(value):
            if re.search(rf"\b{state.lower()}\s*,?\s*{code}\b", page):
                return f"{state.upper()} {code}"
        for a, b in _JP_POSTCODE.findall(value):
            if re.search(rf"(?<!\d){a}\s?-\s?{b}(?!\d)", page):
                return f"〒{a}-{b}"
        place = _JP_PLACE.search(value.replace(", ", ""))
        if place:
            pref, city = place.group(1), place.group(2).split(",")[-1].strip()
            if pref + city in compact:
                return pref + city
        if not _AU_STATE_POSTCODE.search(value):
            for code in _LONG_POSTCODE.findall(value):
                if re.search(rf"(?<![\d-]){code}(?![\d-])", page):
                    return code
    return ""
