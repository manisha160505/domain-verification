"""Free official company-register lookups (logical framework review 2.4, #9, #14, 2.8).

  Japan      National Tax Agency corporate-number site (houjin-bangou.nta.go.jp), keyed by the
             13-digit corporate number: legal name in Japanese (and English when registered),
             head-office address, closure status and, for mergers, the successor company.
  Australia  ABN Lookup (abr.business.gov.au), keyed by the 11-digit ABN: legal entity name,
             ABN status (active / cancelled), state + postcode, business and trading names.

Both are public HTML pages that need no key. Results are cached in SQLite (shared by all
worker processes); failed lookups are not cached, so they are retried on the next run.
"""
from __future__ import annotations

import asyncio
import html as html_lib
import json
import logging
import random
import re
import sqlite3
import time
from datetime import datetime, timezone

LOG = logging.getLogger(__name__)
JP_URL = "https://www.houjin-bangou.nta.go.jp/henkorireki-johoto.html?selHouzinNo={}"
JP_EN_URL = "https://www.houjin-bangou.nta.go.jp/en/henkorireki-johoto.html?selHouzinNo={}"
AU_URL = "https://abr.business.gov.au/ABN/View?abn={}"
JP_COMPANY_FORMS = ("株式会社", "有限会社", "合同会社", "合資会社", "合名会社", "一般社団法人", "一般財団法人",
                    "公益社団法人", "公益財団法人", "独立行政法人", "国立大学法人", "学校法人", "医療法人",
                    "社会福祉法人", "特定非営利活動法人", "協同組合", "信用金庫", "労働金庫")


def register_key(country: str, tax_id: str) -> tuple[str, str] | None:
    digits = re.sub(r"\D", "", tax_id or "")
    country = (country or "").upper()
    if country == "JP" and len(digits) == 13:
        return "JP", digits
    if country == "AU" and len(digits) == 11:
        return "AU", digits
    return None


def _text(fragment: str) -> str:
    return " ".join(html_lib.unescape(re.sub(r"<[^>]+>", " ", fragment or "")).split())


def _dl(page: str) -> dict[str, str]:
    """dt/dd pairs of a definition list."""
    return {_text(k): _text(v) for k, v in re.findall(r"<dt[^>]*>(.*?)</dt>\s*<dd[^>]*>(.*?)</dd>", page, re.S)}


def local_core_name(name: str) -> str:
    """Japanese legal name without its company form (アディダスジャパン株式会社 -> アディダスジャパン)."""
    value = (name or "").replace("（株）", "").replace("(株)", "").replace("㈱", "")
    for form in JP_COMPANY_FORMS:
        value = value.replace(form, "")
    return value.strip(" 　・")


def parse_jp(page: str, english_page: str = "") -> dict | None:
    fields = _dl(page)
    name = fields.get("商号又は名称", "")
    if not name:
        return None
    state = fields.get("状態", "")
    successor_name, successor_number = "", ""
    match = re.search(r"([^\s　（(|]+?)[（(](\d{13})[）)]に(?:吸収)?合併", state)
    if match:
        # "<date><successor's address>…１号<successor name>（number）に合併": the name follows the address.
        successor_number = match.group(2)
        successor_name = re.split(r"[0-9０-９]+階?|号館|号|番地|番(?=[^\d０-９])|丁目|ビル|タワー|センター内", match.group(1))[-1].strip()
    english = ""
    if english_page:
        english_fields = _dl(english_page)
        english = english_fields.get("Name", "")
        if "No registration" in english:
            english = ""
    closed = bool(state) and ("閉鎖" in state or "解散" in state or "清算" in state)
    return {"country": "JP", "legal_name": english or "", "local_name": name, "local_core_name": local_core_name(name),
            "kana": fields.get("商号又は名称（フリガナ）", ""), "address": fields.get("本店又は主たる事務所の所在地", ""),
            "status": "CLOSED" if closed else "ACTIVE", "status_text": state,
            "successor_name": successor_name, "successor_number": successor_number, "other_names": []}


def parse_au(page: str) -> dict | None:
    text = _text(page)
    name = re.search(r"Entity name:\s*(.+?)\s+ABN status:", text)
    if not name:
        return None
    status = re.search(r"ABN status:\s*(Active|Cancelled)[^A-Z]*", text)
    location = re.search(r"Main business location:\s*([A-Z]{2,3}\s+\d{4})", text)
    other: list[str] = []
    for section in ("Business name", "Trading name"):
        block = re.search(section + r"\(s\)(.*?)(?:Business name|Trading name|ASIC registration|Disclaimer|$)", text, re.S)
        if block:
            other += [n.strip() for n in re.findall(r"([A-Z0-9][A-Z0-9&'.,\- ]{2,80}?)\s+\d{2} \w{3} \d{4}", block.group(1))]
    entity_type = re.search(r"Entity type:\s*(.+?)\s+Goods", text)
    return {"country": "AU", "legal_name": name.group(1).strip(), "local_name": "", "local_core_name": "",
            "kana": "", "address": location.group(1) if location else "",
            "status": "CLOSED" if status and status.group(1) == "Cancelled" else "ACTIVE",
            "status_text": status.group(0).strip() if status else "",
            "entity_type": entity_type.group(1) if entity_type else "",
            "successor_name": "", "successor_number": "", "other_names": list(dict.fromkeys(other))[:10]}


class RegisterClient:
    """Cached, polite register lookups. Returns None when the row has no usable ID or the
    lookup failed (failures are not cached)."""

    def __init__(self, path: str, ttl_days: int = 90, concurrency: int = 2, limiter=None):
        self.limiter = limiter  # SharedRateLimiter: register lookups per minute for the whole machine
        self.db = sqlite3.connect(path, timeout=120.0)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS registers (key TEXT PRIMARY KEY, payload TEXT, created_at REAL)")
        self.db.commit()
        self.ttl = ttl_days * 86400
        self._semaphore = asyncio.Semaphore(concurrency)
        self._session = None
        self._inflight: dict[str, asyncio.Future] = {}

    async def _get(self, url: str) -> str:
        if self._session is None:
            from curl_cffi.requests import AsyncSession
            self._session = AsyncSession(impersonate="chrome", timeout=20)
        for attempt in range(3):
            await asyncio.sleep(random.uniform(0.3, 0.9))
            if self.limiter is not None:
                await self.limiter.acquire()
            try:
                response = await self._session.get(url)
                if response.status_code == 200:
                    return response.text
                LOG.warning("register lookup HTTP %s for %s", response.status_code, url)
            except Exception as exc:
                LOG.warning("register lookup error for %s: %s", url, exc)
            await asyncio.sleep(3 * (attempt + 1))
        return ""

    async def lookup(self, country: str, tax_id: str) -> dict | None:
        key = register_key(country, tax_id)
        if not key:
            return None
        cache_key = ":".join(key)
        row = self.db.execute("SELECT payload, created_at FROM registers WHERE key = ?", (cache_key,)).fetchone()
        if row and time.time() - row[1] <= self.ttl:
            return json.loads(row[0])
        if cache_key in self._inflight:
            return await self._inflight[cache_key]
        future = asyncio.get_running_loop().create_future()
        self._inflight[cache_key] = future
        try:
            async with self._semaphore:
                result = await self._fetch(*key)
            if result:
                result.update(register_id=key[1], register_url=(JP_URL if key[0] == "JP" else AU_URL).format(key[1]),
                              checked_at=datetime.now(timezone.utc).isoformat())
                self.db.execute("INSERT OR REPLACE INTO registers VALUES (?, ?, ?)",
                                (cache_key, json.dumps(result, ensure_ascii=False), time.time()))
                self.db.commit()
            future.set_result(result)
            return result
        finally:
            self._inflight.pop(cache_key, None)

    async def _fetch(self, country: str, number: str) -> dict | None:
        if country == "JP":
            page = await self._get(JP_URL.format(number))
            if not page:
                return None
            english = await self._get(JP_EN_URL.format(number))
            return parse_jp(page, english)
        page = await self._get(AU_URL.format(number))
        return parse_au(page) if page else None

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
        self.db.close()
