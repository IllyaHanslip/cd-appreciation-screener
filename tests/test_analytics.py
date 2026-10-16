"""Stage 3: appreciation metrics and the Scarcity Index."""

import math

import numpy as np
import pandas as pd
import pytest

from cd_screener import analytics, db

T0 = pd.Timestamp("2026-01-01T06:00:00Z")


def history(rows):
    return pd.DataFrame(rows, columns=["release_id", "fetched_at", "price", "currency"])


def ratios(**by_id):
    return pd.DataFrame({"release_id": [int(k[1:]) for k in by_id], "want_have_ratio": list(by_id.values())})


def days(n):
    return pd.Timedelta(days=n)


def test_baseline_current_absolute_and_percentage_change():
    h = history([(1, T0, 100.0, "USD"), (1, T0 + days(60), 110.0, "USD"), (1, T0 + days(90), 121.0, "USD")])
    m = analytics.compute_metrics(h, ratios(r1=3.0)).loc[1]
    assert (m.baseline_price, m.current_price, m.abs_change) == (100.0, 121.0, 21.0)
    assert m.pct_appreciation == pytest.approx(21.0)
    assert m.growth_30d_pct == pytest.approx(10.0)  # vs. the day-60 price
    assert math.isnan(m.velocity_30d_pct)  # 3 points are too few for a trend line


def test_growth_needs_30_days_of_history():
    h = history([(1, T0 + days(d), 50.0 + d, "USD") for d in range(0, 20)])
    assert math.isnan(analytics.compute_metrics(h, ratios(r1=3.0)).loc[1, "growth_30d_pct"])


def test_stale_snapshot_is_not_used_for_30_day_growth():
    # Only observations at day 0 and day 100: "30 days ago" (day 70) is 70 days from any data.
    h = history([(1, T0, 10.0, "USD"), (1, T0 + days(100), 20.0, "USD")])
    assert math.isnan(analytics.compute_metrics(h, ratios(r1=3.0)).loc[1, "growth_30d_pct"])


def test_velocity_recovers_an_exponential_trend():
    rate = 0.002  # +0.2% per day
    h = history([(1, T0 + days(d), 50 * math.exp(rate * d), "USD") for d in range(0, 121)])
    m = analytics.compute_metrics(h, ratios(r1=3.0)).loc[1]
    assert m.velocity_30d_pct == pytest.approx((math.exp(rate * 30) - 1) * 100, abs=0.01)
    assert m.growth_30d_pct == pytest.approx(m.velocity_30d_pct, abs=0.01)


def test_only_the_latest_currency_is_used():
    h = history([(1, T0, 999.0, "EUR"), (1, T0 + days(10), 20.0, "USD"), (1, T0 + days(40), 25.0, "USD")])
    m = analytics.compute_metrics(h, ratios(r1=3.0)).loc[1]
    assert (m.currency, m.baseline_price, m.n_snapshots) == ("USD", 20.0, 2)


def test_scarcity_index_rewards_demand_and_momentum():
    rising = [100 * math.exp(0.003 * d) for d in range(0, 91)]
    flat = [100.0] * 91
    rows = []
    for rid, series in ((1, flat), (2, rising), (3, rising)):
        rows += [(rid, T0 + days(d), p, "USD") for d, p in enumerate(series)]
    m = analytics.compute_metrics(history(rows), ratios(r1=10.0, r2=2.5, r3=10.0))
    assert list(m.index) == [3, 1, 2] or list(m.index) == [3, 2, 1]
    assert m.loc[3, "rank"] == 1  # high demand AND rising
    assert m.loc[3, "scarcity_index"] > m.loc[1, "scarcity_index"]
    assert m.loc[3, "scarcity_index"] > m.loc[2, "scarcity_index"]
    assert m["scarcity_index"].between(0, 100).all()


def test_shared_currency_moves_are_removed():
    """Day 2 looks like today's real data: most CDs +1.33% (a currency move), some +0.22%."""
    rows = []
    day1, day2 = T0, T0 + days(1)
    for rid in range(1, 31):
        p1 = 20.0 + rid
        move = 1.0133 if rid <= 20 else 1.0022 if rid <= 28 else 1.0
        rows += [(rid, day1, p1, "GBP"), (rid, day2, round(p1 * move, 2), "GBP")]
    rows += [(31, day1, 3.87, "GBP"), (31, day2, 3.92, "GBP")]  # cheap: +1.29% from cent rounding
    rows += [(32, day1, 50.0, "GBP"), (32, day2, 55.0, "GBP")]  # a real +10% move
    adj = analytics.remove_shared_moves(history(rows))
    last = adj.groupby("release_id").last()
    first = adj.groupby("release_id").first()
    change = (last["adj_price"] / first["price"] - 1) * 100
    assert change.loc[1:31].abs().max() < 1e-9  # shared moves and rounding: no own change
    assert 8 < change.loc[32] < 10  # the real move survives (minus the nearest shared move)
    assert last.loc[1, "price"] == round(21 * 1.0133, 2)  # raw prices are untouched


def test_growth_metrics_ignore_currency_moves():
    rows = []
    for rid in range(1, 11):
        rows += [(rid, T0 + days(d), round((30.0 + rid) * 1.0133 ** d, 2), "GBP") for d in range(0, 41)]
    m = analytics.compute_metrics(history(rows), ratios(**{f"r{i}": 3.0 for i in range(1, 11)}))
    assert m["growth_30d_pct"].abs().max() < 1e-6  # every CD moved in lockstep: no own growth
    assert m["raw_growth_30d_pct"].min() > 40  # the raw figure still shows the currency drift


def test_market_index_separates_own_moves_from_currency():
    rows = []
    for rid in range(1, 11):
        rows += [(rid, T0 + days(d), round((30.0 + rid) * 1.0133 ** d, 2), "GBP") for d in range(3)]
    adjusted = analytics.adjusted_history(history(rows))
    points = analytics.market_index(adjusted)
    assert [p["cds"] for p in points] == [10, 10, 10]
    assert [p["index"] for p in points] == [100.0, 100.0, 100.0]  # no own moves
    assert points[-1]["raw_index"] == pytest.approx(100 * 1.0133 ** 2, abs=0.05)  # currency drift
    assert analytics.market_index(adjusted, release_ids=[]) == []


def _signals(listing_rows, album_rows=(), masters=None, metrics_rows=None):
    metrics = pd.DataFrame(metrics_rows or [{"release_id": 1, "current_price": 40.0, "currency": "GBP"}]
                           ).set_index("release_id")
    listings = pd.DataFrame(listing_rows, columns=["release_id", "fetched_at", "lowest_price",
                                                   "currency", "num_for_sale"])
    albums = pd.DataFrame(list(album_rows), columns=["master_id", "fetched_at", "cd_versions",
                                                     "latest_cd_year", "latest_cd_release_id"])
    return analytics.market_signals(metrics, listings, albums, pd.Series(masters or {}, dtype=float),
                                    today_year=2026)


def test_deal_when_listing_drops_below_its_usual_price():
    rows = [(1, T0 + days(d), 30.0, "GBP", 3) for d in range(4)] + [(1, T0 + days(4), 21.0, "GBP", 3)]
    s = _signals(rows).loc[1]
    assert s.usual_listed_price == 30.0 and s.discount_vs_usual_pct == pytest.approx(30.0)
    assert s.deal == 1 and "below its usual" in s.deal_reason


def test_deal_vs_estimate_needs_a_big_gap_and_copies_for_sale():
    assert _signals([(1, T0, 25.0, "GBP", 2)]).loc[1, "deal"] == 1  # 37.5% below the £40 estimate
    assert _signals([(1, T0, 32.0, "GBP", 2)]).loc[1, "deal"] == 0  # only 20% below
    assert _signals([(1, T0, None, None, 0)]).loc[1, "deal"] == 0  # nothing for sale
    assert _signals([(1, T0, 25.0, "USD", 2)]).loc[1, "deal"] == 0  # different currency: no comparison


def test_reissue_alerts():
    new_pressing = _signals([], [(9, T0, 18, 2008, 5), (9, T0 + days(3), 19, 2026, 6)], {1: 9})
    assert new_pressing.loc[1, "reissue_risk"] == 1
    assert new_pressing.loc[1, "reissue_reason"].startswith("1 new CD pressing added")

    recent = _signals([], [(9, T0, 12, 2025, 5)], {1: 9})
    assert recent.loc[1, "reissue_reason"] == "Reissued on CD in 2025"

    itself = _signals([], [(9, T0, 12, 2025, 1)], {1: 9})  # the newest pressing is this one
    old = _signals([], [(9, T0, 12, 2008, 5)], {1: 9})
    assert itself.loc[1, "reissue_risk"] == 0 and old.loc[1, "reissue_risk"] == 0


def test_releases_without_metadata_are_dropped():
    h = history([(1, T0, 10.0, "USD"), (2, T0, 10.0, "USD")])
    m = analytics.compute_metrics(h, ratios(r1=3.0))
    assert list(m.index) == [1]


def test_run_analytics_round_trip(conn, release_row):
    db.upsert_releases(conn, [release_row(1, wants=300, haves=100), release_row(2, wants=900, haves=100)])
    for d in range(0, 61):
        ts = (T0 + days(d)).strftime("%Y-%m-%dT%H:%M:%SZ")
        db.insert_price(conn, release_id=1, fetched_at=ts, price=float(np.round(20 + d * 0.1, 2)),
                        currency="USD", condition="Near Mint (NM or M-)", source="simulated")
    conn.commit()
    analytics.run_analytics(conn, show=0)
    row = conn.execute("SELECT * FROM release_metrics").fetchone()
    assert row["release_id"] == 1 and row["rank"] == 1
    assert row["current_at"] == "2026-03-02T06:00:00Z"
    assert row["growth_30d_pct"] > 0 and row["sparkline"].startswith("[")


def test_run_analytics_without_prices_explains_next_step(conn, release_row):
    db.upsert_releases(conn, [release_row(1, wants=300, haves=100)])
    with pytest.raises(db.ScreenerError, match="fetch-prices"):
        analytics.run_analytics(conn)
