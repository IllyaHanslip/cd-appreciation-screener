"""Offline stand-in for the Discogs marketplace, used when no DISCOGS_TOKEN is configured.

`SimulatedDiscogsSession` replaces requests.Session: its .get() returns real
`requests.Response` objects shaped like the price-suggestions endpoint, including random
429s, empty "no data" answers and 404s. The real DiscogsClient, rate limiter and retry
logic therefore run unchanged in simulation.

Prices follow a deterministic per-release random walk (geometric Brownian motion with a
drift that rises with the want/have ratio, plus an occasional jump such as a reissue
announcement). `backfill_history` and later simulated fetches read the same path, so the
history and the "live" observations line up.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta, timezone

import numpy as np
import requests

EPOCH = date(2020, 1, 1)
HORIZON_DAYS = 6000  # price paths are defined from EPOCH until mid-2036
ANCHOR_INDEX = (date(2026, 1, 1) - EPOCH).days  # the path passes through `base` on this day
NM_KEY = "Near Mint (NM or M-)"
CONDITION_MULTIPLIERS = {
    "Mint (M)": 1.18,
    NM_KEY: 1.0,
    "Very Good Plus (VG+)": 0.74,
    "Very Good (VG)": 0.52,
    "Good Plus (G+)": 0.34,
    "Good (G)": 0.24,
    "Fair (F)": 0.14,
    "Poor (P)": 0.08,
}
_PRICE_PATH = re.compile(r"/marketplace/price_suggestions/(\d+)$")
_STATS_PATH = re.compile(r"/marketplace/stats/(\d+)$")


def market_status(release_id: int) -> str:
    """Deterministic per-release outcome: ~4% have no price data, ~1% return 404."""
    digest = hashlib.sha256(f"market:{release_id}".encode()).digest()
    u = int.from_bytes(digest[:8], "big") / 2**64
    if u < 0.01:
        return "not_found"
    if u < 0.05:
        return "no_data"
    return "ok"


class PriceModel:
    def __init__(self, seed: int = 42):
        self.seed = seed

    def daily_path(self, release_id: int, want_have_ratio: float) -> np.ndarray:
        """Near Mint price for every day from EPOCH, in USD."""
        rng = np.random.default_rng([self.seed, release_id])
        scarcity = math.log2(max(want_have_ratio, 1.0))  # 1.0 at ratio 2, 2.0 at ratio 4 ...
        base = math.exp(rng.normal(math.log(14.0), 0.45)) * (1 + 0.45 * scarcity)
        annual_drift = rng.normal(0.06 + 0.09 * scarcity, 0.20)  # scarcer -> faster on average
        daily_vol = rng.uniform(0.004, 0.016)
        log_returns = rng.normal(annual_drift / 365 - 0.5 * daily_vol**2, daily_vol, HORIZON_DAYS)
        jump_day = int(rng.integers(2000, 2750))  # mid-2025 .. mid-2027
        u = rng.random()
        if u < 0.18:
            log_returns[jump_day] += rng.normal(-0.22, 0.06)  # reissue announced
        elif u < 0.30:
            log_returns[jump_day] += rng.normal(0.20, 0.06)  # hype spike
        log_path = np.cumsum(log_returns)
        return base * np.exp(log_path - log_path[ANCHOR_INDEX])

    def price_on(self, release_id: int, want_have_ratio: float, day: date) -> float:
        return round(float(self.daily_path(release_id, want_have_ratio)[_day_index(day)]), 2)


def _day_index(day: date) -> int:
    idx = (day - EPOCH).days
    if not 0 <= idx < HORIZON_DAYS:
        raise ValueError(f"{day} is outside the simulated price horizon")
    return idx


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


def backfill_history(
    conn: sqlite3.Connection,
    releases: Iterable[Mapping],
    *,
    days: int,
    model: PriceModel,
    today: date | None = None,
    currency: str = "USD",
) -> int:
    """Write one simulated Near Mint observation per day for the `days` days before today."""
    today = today or utc_today()
    rows = []
    for rel in releases:
        rid = rel["release_id"]
        if market_status(rid) != "ok":
            continue  # these never have a price, in history or live
        path = model.daily_path(rid, rel["want_have_ratio"])
        for offset in range(days, 0, -1):
            day = today - timedelta(days=offset)
            price = round(float(path[_day_index(day)]), 2)
            rows.append((rid, f"{day.isoformat()}T06:00:00Z", NM_KEY, price, currency, "simulated"))
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO price_history "
            "(release_id, fetched_at, condition, price, currency, source) VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
    return len(rows)


class SimulatedDiscogsSession:
    """Duck-types requests.Session.get() for the price-suggestions endpoint."""

    def __init__(
        self,
        ratios: Mapping[int, float],
        *,
        model: PriceModel | None = None,
        p_rate_limited: float = 0.03,
        currency: str = "USD",
        today: date | None = None,
        seed: int = 42,
    ):
        self.ratios = dict(ratios)
        self.model = model or PriceModel(seed)
        self.p_rate_limited = p_rate_limited
        self.currency = currency
        self.today = today
        self._rng = random.Random(seed)
        self.calls = 0

    def get(self, url, params=None, headers=None, timeout=None) -> requests.Response:
        self.calls += 1
        if self._rng.random() < self.p_rate_limited:
            # Real Discogs 429s may or may not carry Retry-After; exercise both code paths.
            extra = {"Retry-After": "1"} if self._rng.random() < 0.5 else {}
            return _response(429, {"message": "You are making requests too quickly."}, url, extra)

        path = url.split("?", 1)[0]
        match = _PRICE_PATH.search(path) or _STATS_PATH.search(path)
        if not match:
            return _response(404, {"message": "The requested resource was not found."}, url)
        release_id = int(match.group(1))
        status = market_status(release_id)
        if release_id not in self.ratios or status == "not_found":
            return _response(404, {"message": "Release not found."}, url)

        if match.re is _STATS_PATH:
            # Listings scatter around the suggestion; a few pressings have nothing for sale.
            rng = random.Random(release_id)
            copies = 0 if status == "no_data" else rng.choice([0, 1, 1, 2, 3, 5, 8, 13])
            if not copies:
                return _response(200, {"num_for_sale": 0, "lowest_price": None}, url)
            nm = self.model.price_on(release_id, self.ratios[release_id], self.today or utc_today())
            lowest = round(nm * rng.uniform(0.55, 1.15), 2)
            return _response(200, {"num_for_sale": copies, "blocked_from_sale": False,
                                   "lowest_price": {"value": lowest, "currency": self.currency}}, url)

        if status == "no_data":
            return _response(200, {}, url)
        nm = self.model.price_on(release_id, self.ratios[release_id], self.today or utc_today())
        payload = {
            condition: {"currency": self.currency, "value": round(nm * mult, 2)}
            for condition, mult in CONDITION_MULTIPLIERS.items()
        }
        return _response(200, payload, url)


def _response(status: int, payload, url: str, headers: Mapping[str, str] | None = None):
    resp = requests.Response()
    resp.status_code = status
    resp.reason = {200: "OK", 404: "Not Found", 429: "Too Many Requests"}.get(status, "")
    resp._content = json.dumps(payload).encode("utf-8")
    resp.encoding = "utf-8"
    resp.url = url
    resp.headers.update({"Content-Type": "application/json", **(headers or {})})
    return resp
