"""Stage 2: the pricing worker's bookkeeping and failure handling."""

import json
import re

import pytest
import requests

from cd_screener import config, db, price_worker, seed
from cd_screener.discogs_client import DiscogsAuthError, DiscogsClient, RateLimiter

NM = "Near Mint (NM or M-)"


def response(status, payload=None):
    resp = requests.Response()
    resp.status_code = status
    resp._content = json.dumps(payload if payload is not None else {}).encode()
    return resp


class RoutedSession:
    """Answers price-suggestion requests per release ID; listings and albums have defaults."""

    def __init__(self, routes, stats=None, masters=None, versions=None):
        self.routes = routes
        self.stats = stats or {}
        self.masters = masters or {}
        self.versions = versions or {}
        self.paths = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.paths.append(url.split("api.discogs.com", 1)[-1])
        if url.endswith("/versions"):
            master_id = int(re.search(r"/masters/(\d+)/versions$", url).group(1))
            return response(200, self.versions.get(master_id, {"pagination": {"items": 0}, "versions": []}))
        item_id = int(re.search(r"(\d+)$", url).group(1))
        if "/marketplace/stats/" in url:
            payload = self.stats.get(item_id, {"num_for_sale": 0, "lowest_price": None})
            return response(200, payload)
        if "/masters/" in url:
            return response(200, self.masters[item_id])
        return self.routes[item_id]()


def fast_client(routes, **session_kwargs):
    session = RoutedSession(routes, **session_kwargs)
    return DiscogsClient("t", session=session, rate_limiter=RateLimiter(1e6, 1.0),
                         max_retries=1, backoff_base=0.0)


def test_every_outcome_is_recorded(conn, release_row):
    db.upsert_releases(conn, [release_row(i, wants=300, haves=100) for i in (1, 2, 3, 4)])
    releases = db.get_flagged_releases(conn, limit=None)
    client = fast_client({
        1: lambda: response(200, {NM: {"currency": "EUR", "value": 18.5}}),
        2: lambda: response(200, {}),  # no price data
        3: lambda: response(404, {"message": "Release not found."}),
        4: lambda: response(500),  # still failing after the retry
    })
    summary = price_worker.fetch_prices(conn, client, releases, run_id="r1", source="discogs")

    assert (summary.ok, summary.no_data, summary.not_found, summary.error) == (1, 1, 1, 1)
    prices = conn.execute("SELECT release_id, price, currency, condition FROM price_history").fetchall()
    assert [tuple(p) for p in prices] == [(1, 18.5, "EUR", NM)]
    statuses = dict(conn.execute("SELECT release_id, status FROM fetch_log WHERE run_id = 'r1'").fetchall())
    assert statuses == {1: "ok", 2: "no_data", 3: "not_found", 4: "error"}


def test_listings_and_album_prices_are_recorded(conn, release_row):
    db.upsert_releases(conn, [release_row(1, wants=300, haves=100, master_id=77),
                              release_row(2, wants=300, haves=100, master_id=77)])
    releases = db.get_flagged_releases(conn, limit=None)
    client = fast_client(
        {1: lambda: response(200, {NM: {"currency": "GBP", "value": 45.57}}),
         2: lambda: response(200, {NM: {"currency": "GBP", "value": 3.0}})},
        stats={1: {"num_for_sale": 2, "lowest_price": {"value": 22.37, "currency": "GBP"}}},
        masters={77: {"lowest_price": 0.79, "num_for_sale": 293}},
        versions={77: {"pagination": {"items": 18},
                       "versions": [{"id": 1, "released": "2008-05-00", "major_formats": ["CD"]}]}})
    summary = price_worker.fetch_prices(conn, client, releases, run_id="r", source="discogs")
    price_worker.refresh_albums(conn, client, releases, summary)
    pressing = conn.execute(
        "SELECT cd_versions, latest_cd_year, latest_cd_release_id FROM master_snapshots").fetchone()
    assert tuple(pressing) == (18, 2008, 1)

    listings = {r["release_id"]: tuple(r)[1:] for r in conn.execute(
        "SELECT release_id, lowest_price, currency, num_for_sale FROM listing_history")}
    assert listings == {1: (22.37, "GBP", 2), 2: (None, None, 0)}  # 2 has nothing for sale
    album = conn.execute("SELECT master_id, lowest_price, num_for_sale FROM master_snapshots").fetchall()
    assert any("/masters/77/versions" in p for p in client.session.paths)
    assert [tuple(a) for a in album] == [(77, 0.79, 293)]  # one lookup per album, not per pressing
    assert (summary.listings, summary.albums) == (2, 1)

    # Within a week the album is not fetched again.
    client.session.paths.clear()
    price_worker.refresh_albums(conn, client, releases, summary)
    assert client.session.paths == []


def test_rerun_fills_only_what_is_missing(seeded):
    first = price_worker.run_worker(seeded, limit=5, requests_per_minute=1e6, analyze=False)
    seeded.execute("DELETE FROM listing_history")  # pretend listings failed earlier today
    seeded.commit()
    again = price_worker.run_worker(seeded, limit=5, requests_per_minute=1e6, analyze=False)
    assert again.queued == 5 and again.listings + again.listing_errors == 5
    assert again.already_priced == first.processed - first.error  # no duplicate prices
    n_prices = seeded.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
    assert n_prices == first.ok


def test_auth_error_stops_the_run(conn, release_row):
    db.upsert_releases(conn, [release_row(1, wants=300, haves=100)])
    client = fast_client({1: lambda: response(401)})
    with pytest.raises(DiscogsAuthError):
        price_worker.fetch_prices(conn, client, db.get_flagged_releases(conn), run_id="r", source="discogs")


def test_circuit_breaker_after_consecutive_errors(conn, release_row):
    db.upsert_releases(conn, [release_row(i, wants=300, haves=100) for i in range(1, 8)])
    client = fast_client({i: (lambda: response(503)) for i in range(1, 8)})
    summary = price_worker.fetch_prices(conn, client, db.get_flagged_releases(conn), run_id="r",
                                        source="discogs", max_consecutive_errors=3)
    assert summary.processed == 3 and summary.aborted


@pytest.fixture
def seeded(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DISCOGS_TOKEN", None)  # force simulated mode
    seed.run_seed(conn, count=400, seed=3, dump_path=tmp_path / "dump.xml")
    return conn


def test_simulated_run_end_to_end_and_resume(seeded):
    summary = price_worker.run_worker(seeded, limit=15, backfill_days=45,
                                      requests_per_minute=1e6, analyze=True)
    assert summary.processed == 15 and summary.aborted is None
    n_metrics = seeded.execute("SELECT COUNT(*) FROM release_metrics").fetchone()[0]
    assert n_metrics == summary.ok
    assert seeded.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] >= 45 * summary.ok

    again = price_worker.run_worker(seeded, limit=15, requests_per_minute=1e6, analyze=False)
    assert again.queued == summary.error  # only transient failures are retried today


def test_unknown_ids_are_skipped_with_a_warning(seeded, caplog):
    valid = db.get_flagged_releases(seeded, limit=1)[0]["release_id"]
    summary = price_worker.run_worker(seeded, release_ids=[valid, 123], requests_per_minute=1e6,
                                      analyze=False)
    assert summary.queued == 1
    assert "Release 123 is not in the database" in caplog.text
    with pytest.raises(db.ScreenerError):
        price_worker.run_worker(seeded, release_ids=[123], analyze=False)


def test_live_mode_never_sends_synthetic_ids(seeded, monkeypatch):
    monkeypatch.setattr(config, "DISCOGS_TOKEN", "real-token")
    with pytest.raises(db.ScreenerError, match="ingest"):
        price_worker.run_worker(seeded, analyze=False)


def test_backfill_is_rejected_in_live_mode(seeded, monkeypatch):
    monkeypatch.setattr(config, "DISCOGS_TOKEN", "real-token")
    with pytest.raises(db.ScreenerError, match="simulated mode"):
        price_worker.run_worker(seeded, backfill_days=30, analyze=False)
