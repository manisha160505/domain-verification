"""Bounded Scrapling HTTP -> dynamic -> stealth fetching."""
from __future__ import annotations

import asyncio
import logging
import random
import socket
from datetime import datetime, timezone
from ipaddress import ip_address
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from .config import Config
from .models import FetchRecord

LOG = logging.getLogger(__name__)
# One consistent identity: a Chrome user-agent that matches the impersonated Chrome TLS
# fingerprint. No fake referer is sent (stealthy_headers=False).
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}

# DNS outcomes: RESOLVED, FAILED (name does not exist), NO_ADDRESS (exists, no web address),
# TEMPORARY (timeout / server failure, retry later), UNSAFE_ADDRESS, UNKNOWN.
DNS_DEAD = ("FAILED", "NO_ADDRESS")


def quiet_scrapling_logs(level: int = logging.CRITICAL) -> None:
    """Scrapling configures its own INFO logger on import; per-row fetch errors are recorded
    in our results instead, so silence it (import first, then set the level)."""
    try:
        import scrapling.core.utils  # noqa: F401  (creates the "scrapling" logger)
    except ImportError:
        return
    logger = logging.getLogger("scrapling")
    logger.setLevel(level)
    logger.propagate = False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _error_type(exc: Exception) -> str:
    message = str(exc).lower()
    if any(x in message for x in ("certificate", "ssl", "tls")):
        return "TLS"
    if any(x in message for x in ("timeout", "timed out")):
        return "TIMEOUT"
    if any(x in message for x in ("resolve", "dns", "name or service", "nodename")):
        return "DNS"
    if any(x in message for x in ("refused", "reset", "connection")):
        return "CONNECTION"
    return type(exc).__name__.upper()


def _unsafe(addresses: list[str]) -> bool:
    return any(not ip_address(a).is_global for a in addresses)


async def _dns_once(host: str) -> str:
    try:
        import dns.asyncresolver
        import dns.exception
        import dns.resolver
    except ImportError:  # fall back to the OS resolver (cannot tell NXDOMAIN from timeouts)
        try:
            infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
            return "UNSAFE_ADDRESS" if _unsafe([i[4][0] for i in infos]) else "RESOLVED"
        except socket.gaierror as exc:
            return "FAILED" if exc.errno in (socket.EAI_NONAME, getattr(socket, "EAI_NODATA", -5)) else "TEMPORARY"
    resolver = dns.asyncresolver.Resolver()
    resolver.lifetime = 4.0
    addresses: list[str] = []
    no_answer = 0
    for record_type in ("A", "AAAA"):
        try:
            answer = await resolver.resolve(host, record_type)
            addresses.extend(r.address for r in answer)
        except dns.resolver.NXDOMAIN:
            return "FAILED"
        except dns.resolver.NoAnswer:
            no_answer += 1
        except (dns.resolver.NoNameservers, dns.exception.Timeout):
            return "TEMPORARY"
        except Exception:
            return "UNKNOWN"
    if addresses:
        return "UNSAFE_ADDRESS" if _unsafe(addresses) else "RESOLVED"
    return "NO_ADDRESS" if no_answer == 2 else "UNKNOWN"


async def dns_status(host: str) -> str:
    """Resolve host, retrying once after a temporary failure before giving up."""
    status = await _dns_once(host)
    if status in ("TEMPORARY", "UNKNOWN"):
        await asyncio.sleep(1.0)
        status = await _dns_once(host)
    return status


class TieredFetcher:
    def __init__(self, config: Config, proxy_url: str | None = None):
        self.config = config
        self.proxy_url = proxy_url if config.use_proxy else None
        self._last_request: dict[str, float] = {}
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self._semaphore = asyncio.Semaphore(config.concurrency)

    async def _pace(self, host: str) -> None:
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            now = asyncio.get_running_loop().time()
            wait = self.config.per_host_delay_seconds - (now - self._last_request.get(host, 0))
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request[host] = asyncio.get_running_loop().time()

    def _kwargs(self, verify: bool, max_redirects: int | None = None, timeout: float | None = None) -> dict:
        return dict(timeout=timeout or self.config.timeout_seconds, retries=0,
                    follow_redirects="safe", max_redirects=max_redirects or self.config.max_redirects,
                    headers=dict(HEADERS), impersonate="chrome", stealthy_headers=False, verify=verify)

    async def _http(self, url: str, verify: bool = True) -> FetchRecord:
        from scrapling.fetchers import AsyncFetcher
        host = urlsplit(url).hostname or ""
        result = FetchRecord(requested_url=url, checked_at=utc_now())
        for attempt in range(1, self.config.max_http_attempts + 1):
            result.attempts = attempt
            try:
                await self._pace(host)
                async with self._semaphore:
                    response = await AsyncFetcher.get(url, **self._kwargs(verify))
                result.status = int(response.status)
                result.final_url = str(response.url)
                result.redirect_chain = [str(x.url) for x in response.history] + [result.final_url]
                result.html = response.body[:self.config.max_body_bytes].decode(response.encoding or "utf-8", "replace")
                result.error = result.error_type = ""
                result.tls_valid = verify if url.startswith("https://") else None
                if result.status in (429, 500, 502, 503, 504) and attempt < self.config.max_http_attempts:
                    await asyncio.sleep(min(5, 2 ** (attempt - 1)) + random.random())
                    continue
                return result
            except Exception as exc:
                result.error = str(exc)[:400]
                result.error_type = _error_type(exc)
                LOG.debug("HTTP %s attempt %s: %s", host, attempt, result.error)
                if result.error_type in ("TIMEOUT", "DNS", "TLS"):
                    break
                if attempt < self.config.max_http_attempts:
                    await asyncio.sleep(min(5, 2 ** (attempt - 1)) + random.random())
        return result

    async def robots_allowed(self, url: str) -> bool | None:
        if not self.config.respect_robots:
            return True
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if host not in self._robots:
            robots_url = f"{parsed.scheme}://{host}/robots.txt"
            try:
                from scrapling.fetchers import AsyncFetcher
                async with self._semaphore:
                    resp = await AsyncFetcher.get(robots_url, **self._kwargs(False, 3, min(4, self.config.timeout_seconds)))
                if int(resp.status) == 200:
                    robot = RobotFileParser()
                    body = resp.body[:100_000].decode(resp.encoding or "utf-8", "replace")
                    robot.parse(body.splitlines())
                    self._robots[host] = robot
                else:
                    self._robots[host] = None
            except Exception:
                self._robots[host] = None
        robot = self._robots[host]
        return robot.can_fetch(USER_AGENT, url) if robot else None

    async def fetch(self, url: str, *, allow_browser: bool = False,
                    use_stealth: bool = False, force_browser: bool = False) -> FetchRecord:
        permission = await self.robots_allowed(url)
        if permission is False:
            return FetchRecord(requested_url=url, error="robots.txt disallows fetch",
                               error_type="ROBOTS", checked_at=utc_now())
        result = await self._http(url)
        if result.status == 0 and result.error_type == "TLS" and url.startswith("https://"):
            # Certificate problem: read the page anyway but record that TLS is not valid.
            insecure = await self._http(url, verify=False)
            insecure.attempts += result.attempts
            if insecure.status:
                insecure.tls_valid = False
                result = insecure
            else:
                fallback = await self._http("http://" + url.removeprefix("https://"))
                fallback.attempts += result.attempts + insecure.attempts
                if fallback.status:
                    fallback.requested_url = url
                    result = fallback
                else:  # keep the most informative failure (often a timeout, not the TLS error)
                    insecure.attempts = fallback.attempts
                    result = insecure
        if not allow_browser or not (self.config.use_dynamic or self.config.use_stealth):
            return result
        if not force_browser and result.status not in (0, 403, 429) and len(result.html) > 1200:
            return result
        if result.status in (403, 429) and not use_stealth:
            return result
        method = "STEALTH" if use_stealth and self.config.use_stealth else "DYNAMIC"
        if method == "DYNAMIC" and not self.config.use_dynamic:
            return result
        try:
            from scrapling.fetchers import DynamicFetcher, StealthyFetcher
            fetcher = StealthyFetcher if method == "STEALTH" else DynamicFetcher
            kwargs = {"timeout": self.config.timeout_seconds * 1000, "disable_resources": True}
            if self.proxy_url:
                kwargs["proxy"] = self.proxy_url
            async with self._semaphore:
                page = await fetcher.async_fetch(url, **kwargs)
            return FetchRecord(requested_url=url, final_url=str(page.url), status=int(page.status),
                               html=page.body[:self.config.max_body_bytes].decode(page.encoding or "utf-8", "replace"),
                               redirect_chain=[str(x.url) for x in page.history] + [str(page.url)],
                               method=method, attempts=result.attempts + 1, checked_at=utc_now())
        except Exception as exc:
            result.error = (result.error + "; " + str(exc))[:400]
            result.error_type = _error_type(exc)
            result.attempts += 1
            return result
