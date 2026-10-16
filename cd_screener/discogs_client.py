"""Discogs HTTP client with client-side rate limiting and resilient retries.

Discogs allows 60 authenticated requests per minute per source IP (25 unauthenticated),
measured over a moving 60-second window, and answers `429 Too Many Requests` beyond that.
Responses carry X-Discogs-Ratelimit, -Used and -Remaining headers.

Three layers keep us inside the limit:
  * proactive - RateLimiter spaces requests evenly at 90% of the limit (~1.1 s apart);
  * adaptive  - when the server reports only a few requests left, the limiter slows down;
  * reactive  - 429, 5xx and network errors are retried with exponential backoff and
                jitter, honouring a Retry-After header when the server sends one.
"""

from __future__ import annotations

import logging
import math
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any

import requests

from . import config

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class DiscogsError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class DiscogsAuthError(DiscogsError):
    """401: the token is missing or invalid. Fatal for the whole run."""


class DiscogsNotFound(DiscogsError):
    """404: the release does not exist (or was removed)."""


class DiscogsRateLimited(DiscogsError):
    """Still receiving 429 after every retry."""


class RateLimiter:
    """Evenly spaces calls so at most `max_per_minute * safety` happen per minute."""

    def __init__(
        self,
        max_per_minute: float = config.DISCOGS_MAX_REQUESTS_PER_MINUTE,
        safety: float = config.RATE_LIMIT_SAFETY,
        *,
        low_water: int = 5,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if max_per_minute <= 0 or not 0 < safety <= 1:
            raise ValueError("max_per_minute must be > 0 and safety in (0, 1]")
        self.min_interval = 60.0 / (max_per_minute * safety)
        self.low_water = low_water
        self._clock = clock
        self._sleep = sleep
        self._next_slot = float("-inf")
        self.total_slept = 0.0

    def wait(self) -> float:
        """Block until the next request may be sent; returns the seconds slept."""
        now = self._clock()
        start = max(now, self._next_slot)
        delay = start - now
        if delay > 0:
            self._sleep(delay)
            self.total_slept += delay
        self._next_slot = start + self.min_interval
        return delay

    def defer(self, seconds: float) -> None:
        """Hold the next request back until at least `seconds` from now (used for backoff)."""
        self._next_slot = max(self._next_slot, self._clock() + seconds)

    def observe(self, headers: Mapping[str, str]) -> None:
        """Adapt to the server's count of requests left in the current window."""
        remaining = _int_header(headers, "X-Discogs-Ratelimit-Remaining")
        if remaining is None or remaining >= self.low_water:
            return
        # Spread what is left of this 60 s window evenly over the window.
        pause = 60.0 / max(remaining, 1)
        log.info("Discogs reports %d requests left in this window - pausing %.0fs", remaining, pause)
        self.defer(pause)


@dataclass(frozen=True)
class PricePoint:
    condition: str
    value: float
    currency: str


def extract_condition_price(
    payload: Any, condition_prefix: str = config.NM_CONDITION_PREFIX
) -> PricePoint | None:
    """Pick one condition out of a price-suggestions payload.

    Payload shape: {"Near Mint (NM or M-)": {"currency": "USD", "value": 21.5}, ...}.
    Discogs returns {} when it has no suggestion; that (and malformed values) gives None.
    """
    if not isinstance(payload, Mapping):
        return None
    for condition, entry in payload.items():
        if not str(condition).lower().startswith(condition_prefix.lower()):
            continue
        if not isinstance(entry, Mapping):
            return None
        try:
            value = float(entry.get("value"))
        except (TypeError, ValueError):
            return None
        currency = entry.get("currency")
        if not math.isfinite(value) or value <= 0 or not currency:
            return None
        return PricePoint(condition=str(condition), value=round(value, 2), currency=str(currency))
    return None


@dataclass(frozen=True)
class MarketSnapshot:
    lowest_price: float | None  # None when nothing is for sale
    currency: str | None
    num_for_sale: int


def _positive_price(value: Any) -> float | None:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return round(price, 2) if math.isfinite(price) and price > 0 else None


def extract_listing(payload: Any) -> MarketSnapshot | None:
    """Parse /marketplace/stats: {"num_for_sale": 2, "lowest_price": {"value": 22.37, "currency": "GBP"}}."""
    if not isinstance(payload, Mapping):
        return None
    lowest = payload.get("lowest_price")
    lowest = lowest if isinstance(lowest, Mapping) else {}
    return MarketSnapshot(
        lowest_price=_positive_price(lowest.get("value")),
        currency=lowest.get("currency"),
        num_for_sale=max(0, int(payload.get("num_for_sale") or 0)),
    )


def extract_master_market(payload: Any) -> MarketSnapshot | None:
    """Parse /masters/{id}: {"lowest_price": 0.79, "num_for_sale": 293} (account currency)."""
    if not isinstance(payload, Mapping):
        return None
    return MarketSnapshot(
        lowest_price=_positive_price(payload.get("lowest_price")),
        currency=None,
        num_for_sale=max(0, int(payload.get("num_for_sale") or 0)),
    )


@dataclass(frozen=True)
class CdVersions:
    count: int  # how many CD pressings the album has
    latest_year: int | None  # release year of the newest one
    latest_release_id: int | None


def extract_cd_versions(payload: Any) -> CdVersions | None:
    """Parse /masters/{id}/versions?format=CD&sort=released&sort_order=desc&per_page=1."""
    if not isinstance(payload, Mapping):
        return None
    count = int((payload.get("pagination") or {}).get("items") or 0)
    versions = payload.get("versions") or []
    newest = versions[0] if versions and isinstance(versions[0], Mapping) else {}
    released = str(newest.get("released") or "")
    year = int(released[:4]) if released[:4].isdigit() else None
    return CdVersions(count=count, latest_year=year, latest_release_id=newest.get("id"))


class DiscogsClient:
    def __init__(
        self,
        token: str | None = None,
        *,
        user_agent: str = config.DISCOGS_USER_AGENT,
        base_url: str = config.DISCOGS_API_BASE,
        session: Any = None,
        rate_limiter: RateLimiter | None = None,
        max_retries: int = 5,
        backoff_base: float = 5.0,
        backoff_cap: float = 60.0,
        timeout: float = 20.0,
        rng: random.Random | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.session = session if session is not None else requests.Session()
        per_minute = config.DISCOGS_MAX_REQUESTS_PER_MINUTE if token else 25
        self.rate_limiter = rate_limiter or RateLimiter(per_minute)
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.timeout = timeout
        self._rng = rng or random.Random()
        self.headers = {
            "User-Agent": user_agent,
            "Accept": "application/vnd.discogs.v2.discogs+json",
        }
        if token:
            self.headers["Authorization"] = f"Discogs token={token}"
        self.requests_sent = 0
        self.retries = 0

    # --- endpoints ---------------------------------------------------------------

    def get_price_suggestions(self, release_id: int) -> Any:
        """GET /marketplace/price_suggestions/{release_id} (auth + seller settings required)."""
        return self.get_json(f"/marketplace/price_suggestions/{int(release_id)}")

    def get_marketplace_stats(self, release_id: int) -> Any:
        """GET /marketplace/stats/{release_id}: cheapest listing + copies for sale."""
        return self.get_json(f"/marketplace/stats/{int(release_id)}")

    def get_master(self, master_id: int) -> Any:
        """GET /masters/{id}: the album across all pressings, incl. its cheapest copy."""
        return self.get_json(f"/masters/{int(master_id)}")

    def get_cd_versions(self, master_id: int) -> Any:
        """GET /masters/{id}/versions, CD pressings only, newest first (1 item + total count)."""
        return self.get_json(f"/masters/{int(master_id)}/versions",
                             {"format": "CD", "sort": "released", "sort_order": "desc",
                              "per_page": 1})

    def search_releases(self, *, page: int = 1, per_page: int = 100, **filters: Any) -> Any:
        """GET /database/search?type=release&... (auth required)."""
        params = {k: v for k, v in filters.items() if v not in (None, "")}
        params.update(type="release", page=page, per_page=per_page)
        return self.get_json("/database/search", params)

    # --- transport ---------------------------------------------------------------

    def get_json(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        for attempt in range(self.max_retries + 1):
            self.rate_limiter.wait()
            self.requests_sent += 1
            try:
                resp = self.session.get(url, params=params, headers=self.headers,
                                        timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                self._retry_or_raise(attempt, f"Network error ({type(exc).__name__})", None)
                continue

            self.rate_limiter.observe(resp.headers)
            status = resp.status_code
            if status in RETRYABLE_STATUS:
                label = "429 Too Many Requests" if status == 429 else f"HTTP {status}"
                self._retry_or_raise(attempt, label, status, _retry_after_seconds(resp.headers))
                continue
            if status == 401:
                raise DiscogsAuthError("401 Unauthorized - check DISCOGS_TOKEN in .env", status)
            if status == 404:
                message = _error_message(resp)
                if "seller settings" in message.lower():  # account-level, not per-release
                    raise DiscogsAuthError(
                        f"{message} Fill them in at https://www.discogs.com/settings/seller",
                        status)
                raise DiscogsNotFound(f"404 Not Found: {message}", status)
            if status >= 400:  # e.g. 403 when seller settings are missing; the message says why
                raise DiscogsError(f"HTTP {status}: {_error_message(resp)}", status)
            try:
                return resp.json()
            except ValueError as exc:
                raise DiscogsError(f"Invalid JSON in response to {path}", status) from exc
        raise AssertionError("unreachable")  # the loop always returns or raises

    def _retry_or_raise(
        self, attempt: int, reason: str, status: int | None, retry_after: float | None = None
    ) -> None:
        if attempt >= self.max_retries:
            if status == 429:
                raise DiscogsRateLimited(
                    f"Still rate-limited after {self.max_retries} retries", status)
            raise DiscogsError(f"{reason} - gave up after {self.max_retries} retries", status)
        if retry_after is not None:
            delay = min(retry_after, 600.0)
        else:
            delay = min(self.backoff_cap, self.backoff_base * 2 ** attempt)
            delay *= self._rng.uniform(0.8, 1.2)  # jitter so parallel clients don't sync up
        self.retries += 1
        log.warning("%s - backing off %.1fs (retry %d/%d)", reason, delay, attempt + 1,
                    self.max_retries)
        self.rate_limiter.defer(delay)  # the sleep happens in the next wait()


def _int_header(headers: Mapping[str, str], name: str) -> int | None:
    try:
        return int(headers.get(name))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    """Retry-After may be a number of seconds or an HTTP date."""
    raw = headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        return max(0.0, parsedate_to_datetime(raw).timestamp() - time.time())
    except (TypeError, ValueError):
        return None


def _error_message(resp: requests.Response) -> str:
    try:
        return str(resp.json().get("message") or resp.reason)
    except (ValueError, AttributeError):
        return (resp.text or resp.reason or "").strip()[:200]
