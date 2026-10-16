"""Stage 2 - the rate-limited pricing worker.

Takes the top-N releases flagged by the funnel, asks Discogs for each one's marketplace
price suggestions, extracts the Near Mint value and currency, and appends a time-stamped
row to `price_history`. Every attempt, successful or not, is recorded in `fetch_log`.

Run it on a schedule (e.g. daily with Windows Task Scheduler or cron): each run adds one
observation per release, which builds the time series that analytics.py works on.
The worker commits after every release, so an interrupted run loses nothing, and a re-run
on the same day skips releases that already have a conclusive result.
"""

from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import analytics, config, db, report
from .discogs_client import (
    DiscogsAuthError,
    DiscogsClient,
    DiscogsError,
    DiscogsNotFound,
    RateLimiter,
    extract_condition_price,
    extract_cd_versions,
    extract_listing,
    extract_master_market,
)
from .simulated_market import PriceModel, SimulatedDiscogsSession, backfill_history

log = logging.getLogger(__name__)

SIMULATED_REQUESTS_PER_MINUTE = 1200  # the fake API has no real limit; keep the demo quick


@dataclass
class RunSummary:
    run_id: str
    mode: str
    queued: int = 0
    ok: int = 0
    no_data: int = 0
    not_found: int = 0
    error: int = 0
    already_priced: int = 0  # suggestion fetched earlier today; only the listing was missing
    listings: int = 0
    listing_errors: int = 0
    albums: int = 0
    report_period: str | None = None  # set when this run published the weekly report
    aborted: str | None = None
    started: float = field(default_factory=time.monotonic)

    @property
    def processed(self) -> int:
        return self.ok + self.no_data + self.not_found + self.error + self.already_priced

    def report(self) -> str:
        elapsed = time.monotonic() - self.started
        lines = [
            f"\nPrice run {self.run_id} ({self.mode}) finished in {elapsed:.1f}s",
            f"  processed        {self.processed:>5} / {self.queued}",
            f"  priced (NM)      {self.ok:>5}",
            f"  no price data    {self.no_data:>5}",
            f"  not on Discogs   {self.not_found:>5}",
            f"  errors           {self.error:>5}",
            f"  listings saved   {self.listings:>5}  (cheapest copy + copies for sale)",
            f"  album prices     {self.albums:>5}  (cheapest copy of any pressing)",
        ]
        if self.already_priced:
            lines.append(f"  already priced   {self.already_priced:>5}  (today; listing added)")
        if self.listing_errors:
            lines.append(f"  listing errors   {self.listing_errors:>5}")
        if self.aborted:
            lines.append(f"  STOPPED EARLY: {self.aborted}")
        return "\n".join(lines)


def new_run_id() -> str:
    return f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"


def fetch_prices(
    conn: sqlite3.Connection,
    client: DiscogsClient,
    releases: Sequence[sqlite3.Row | dict],
    *,
    run_id: str,
    source: str,
    condition_prefix: str = config.NM_CONDITION_PREFIX,
    skip_prices: set[int] | frozenset[int] = frozenset(),
    skip_listings: set[int] | frozenset[int] = frozenset(),
    max_consecutive_errors: int = 10,
    progress_every: int = 25,
) -> RunSummary:
    """Fetch the NM suggestion and the current listing for each release.

    Raises DiscogsAuthError (bad token / no seller settings); everything else is logged.
    """
    summary = RunSummary(run_id=run_id, mode=source, queued=len(releases))
    consecutive_errors = 0
    try:
        for i, rel in enumerate(releases, 1):
            rid = rel["release_id"]
            if rid not in skip_listings:
                _fetch_listing(conn, client, rid, summary)
            if rid in skip_prices:
                summary.already_priced += 1
                conn.commit()
                continue
            status, http_status, message = "ok", 200, None
            try:
                payload = client.get_price_suggestions(rid)
                point = extract_condition_price(payload, condition_prefix)
                if point is None:
                    status, message = "no_data", "No Near Mint price suggestion available"
                else:
                    db.insert_price(conn, release_id=rid, fetched_at=db.utc_now_iso(),
                                    price=point.value, currency=point.currency,
                                    condition=point.condition, source=source)
            except DiscogsAuthError:
                raise
            except DiscogsNotFound as exc:
                status, http_status, message = "not_found", exc.status, str(exc)
            except DiscogsError as exc:  # includes "still 429 after all retries"
                status, http_status, message = "error", exc.status, str(exc)
            except sqlite3.IntegrityError as exc:  # e.g. release deleted mid-run
                status, http_status, message = "error", None, f"Database error: {exc}"

            db.log_fetch(conn, run_id=run_id, release_id=rid, status=status,
                         http_status=http_status, message=message)
            conn.commit()  # per-release commit: an interrupted run keeps its progress
            setattr(summary, status, getattr(summary, status) + 1)

            if status == "error":
                log.warning("Release %s: %s", rid, message)
                consecutive_errors += 1
                if consecutive_errors >= max_consecutive_errors:
                    summary.aborted = (f"{consecutive_errors} consecutive errors, last one: "
                                       f"{message}")
                    log.error("Stopping: %s", summary.aborted)
                    break
            else:
                consecutive_errors = 0
                if status != "ok":
                    log.info("Release %s: %s", rid, message)

            if i % progress_every == 0 or i == len(releases):
                log.info("[%d/%d] ok=%d no_data=%d not_found=%d errors=%d", i, len(releases),
                         summary.ok, summary.no_data, summary.not_found, summary.error)
    except KeyboardInterrupt:
        conn.commit()
        summary.aborted = "interrupted with Ctrl+C - everything fetched so far is saved"
    return summary


def _fetch_listing(conn: sqlite3.Connection, client: DiscogsClient, release_id: int,
                   summary: RunSummary) -> None:
    """Record what this pressing is listed for. A failure here never blocks the price."""
    try:
        snapshot = extract_listing(client.get_marketplace_stats(release_id))
    except DiscogsAuthError:
        raise
    except DiscogsError as exc:
        summary.listing_errors += 1
        log.warning("Release %s: listing lookup failed: %s", release_id, exc)
        return
    if snapshot is not None:
        db.insert_listing(conn, release_id=release_id, lowest_price=snapshot.lowest_price,
                          currency=snapshot.currency, num_for_sale=snapshot.num_for_sale)
        summary.listings += 1


def refresh_albums(conn: sqlite3.Connection, client: DiscogsClient,
                   releases: Sequence[sqlite3.Row | dict], summary: RunSummary) -> None:
    """Fetch each album's cheapest copy (any pressing), at most once a week per album."""
    week_ago = (datetime.now(timezone.utc) - timedelta(days=config.ALBUM_REFRESH_DAYS)
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh = db.master_ids_fetched_since(conn, week_ago)
    master_ids = sorted({r["master_id"] for r in releases
                         if "master_id" in r.keys() and r["master_id"]} - fresh)
    if not master_ids:
        return
    log.info("Fetching album-level prices for %d albums", len(master_ids))
    for master_id in master_ids:
        try:
            snapshot = extract_master_market(client.get_master(master_id))
        except DiscogsAuthError:
            raise
        except DiscogsError as exc:
            log.warning("Album %s: lookup failed: %s", master_id, exc)
            continue
        versions = None
        try:  # CD pressings, for the reissue monitor; optional
            versions = extract_cd_versions(client.get_cd_versions(master_id))
        except DiscogsAuthError:
            raise
        except DiscogsError as exc:
            log.warning("Album %s: pressing list failed: %s", master_id, exc)
        if snapshot is not None:
            db.insert_master_snapshot(
                conn, master_id=master_id, lowest_price=snapshot.lowest_price,
                num_for_sale=snapshot.num_for_sale,
                cd_versions=versions.count if versions else None,
                latest_cd_year=versions.latest_year if versions else None,
                latest_cd_release_id=versions.latest_release_id if versions else None)
            conn.commit()
            summary.albums += 1


def run_worker(
    conn: sqlite3.Connection,
    *,
    limit: int = config.TOP_N_RELEASES,
    release_ids: Sequence[int] | None = None,
    simulate: bool = False,
    backfill_days: int = 0,
    requests_per_minute: float | None = None,
    min_ratio: float = config.MIN_WANT_HAVE_RATIO,
    min_wants: int = config.MIN_WANTS,
    force: bool = False,
    seed: int = 42,
    analyze: bool = True,
    on_start: Callable[[int, float], None] | None = None,
) -> RunSummary:
    """Price the funnel's releases. `on_start(n_releases, eta_minutes)` is called only when
    there is real work to do (used by the scheduled job to show a notification)."""
    db.ensure_releases_loaded(conn)
    live = bool(config.DISCOGS_TOKEN) and not simulate
    if not live and not simulate:
        log.info("DISCOGS_TOKEN is not set - using the simulated Discogs market "
                 "(add a token to .env for live prices)")
    if live and backfill_days:
        raise db.ScreenerError("--backfill-days only works in simulated mode: the Discogs API "
                               "returns today's suggestion, not historical prices.")

    # 1. Work list: the funnel's top N, or explicit IDs.
    if release_ids:
        releases, missing = db.get_releases_by_ids(conn, release_ids)
        for rid in missing:
            log.warning("Release %s is not in the database - skipped (load it with seed/ingest)",
                        rid)
    else:
        releases = db.get_flagged_releases(conn, min_ratio=min_ratio, limit=limit,
                                           min_wants=min_wants)
    if not releases:
        raise db.ScreenerError(
            f"No releases to price: nothing with format={config.TARGET_FORMAT!r}, want/have "
            f"> {min_ratio} and at least {min_wants} wants. Load more candidates with "
            "`python -m cd_screener ingest` (or `seed`), or lower --min-wants.")

    if live:
        synthetic = [r for r in releases if r["source"] == "simulated"]
        if synthetic:
            log.warning("Skipping %d simulated releases: their IDs are synthetic and don't exist "
                        "on Discogs. Load real releases with `python -m cd_screener ingest`.",
                        len(synthetic))
            releases = [r for r in releases if r["source"] != "simulated"]
        if not releases:
            raise db.ScreenerError("No real Discogs releases to price. Run "
                                   "`python -m cd_screener ingest --genre Rock` first.")

    # 2. Resumability: only fetch what is still missing today (suggestion and/or listing).
    all_releases = list(releases)
    done_prices: set[int] = set()
    done_listings: set[int] = set()
    if not force:
        today_start = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
        done_prices = db.release_ids_resolved_since(conn, today_start)
        done_listings = db.release_ids_with_listing_since(conn, today_start)
        before = len(releases)
        releases = [r for r in releases
                    if r["release_id"] not in done_prices or r["release_id"] not in done_listings]
        if before != len(releases):
            log.info("%d releases are already fully fetched today - skipping them "
                     "(--force refetches)", before - len(releases))
        if not releases:
            log.info("Nothing left to fetch today.")

    # 3. Build the client: real HTTP for live mode, the simulated session otherwise.
    run_id = new_run_id()
    if live:
        rpm = min(requests_per_minute or config.DISCOGS_MAX_REQUESTS_PER_MINUTE,
                  config.DISCOGS_MAX_REQUESTS_PER_MINUTE)
        limiter = RateLimiter(rpm, config.RATE_LIMIT_SAFETY)
        client = DiscogsClient(config.DISCOGS_TOKEN, rate_limiter=limiter)
        source = "discogs"
    else:
        model = PriceModel(seed)
        if backfill_days:
            n = backfill_history(conn, releases, days=backfill_days, model=model)
            log.info("Backfilled %d simulated daily prices (%d days of history)", n, backfill_days)
        session = SimulatedDiscogsSession(
            {r["release_id"]: r["want_have_ratio"] for r in releases}, model=model, seed=seed)
        limiter = RateLimiter(requests_per_minute or SIMULATED_REQUESTS_PER_MINUTE, safety=1.0)
        client = DiscogsClient(None, session=session, rate_limiter=limiter,
                               backoff_base=0.5, backoff_cap=2.0)
        source = "simulated"

    if releases:
        calls = sum((r["release_id"] not in done_prices) + (r["release_id"] not in done_listings)
                    for r in releases)
        eta_minutes = calls * limiter.min_interval / 60
        log.info("Fetching prices and listings for %d releases (%s mode, ~%.1f min at "
                 "%.0f req/min)", len(releases), source, eta_minutes, 60 / limiter.min_interval)
        if on_start:
            on_start(len(releases), eta_minutes)
    summary = fetch_prices(conn, client, releases, run_id=run_id, source=source,
                           skip_prices=done_prices, skip_listings=done_listings)
    if not summary.aborted:
        refresh_albums(conn, client, all_releases, summary)
    print(summary.report())
    if client.retries:
        print(f"  retries          {client.retries:>5}  (429 / 5xx / network backoffs)")

    if analyze:
        analytics.run_analytics(conn)
        published = report.publish_if_due(conn)  # weekly: replaces the dashboard's report
        if published:
            summary.report_period = f"{published['period_start'][:10]} to {published['period_end'][:10]}"
    return summary
