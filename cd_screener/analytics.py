"""Stage 3 - time-series analytics with pandas.

For every release with price history (one currency per release - the latest one):

  current_price     latest observation
  baseline_price    earliest observation (the historical baseline)
  abs_change        current - baseline
  pct_appreciation  (current - baseline) / baseline * 100
  growth_30d_pct    % change vs. the last observation made at least 30 days before the
                    latest one (NULL until there is ~30 days of history)
  velocity_30d_pct  trend speed: least-squares slope of ln(price) over the trailing 90 days,
                    expressed as % change per 30 days. A fitted line uses every point, so
                    it is far less noisy than comparing two single observations.
  Appreciation, growth and velocity are measured on the *adjusted* price: Discogs converts
  each suggestion into the account currency daily, so exchange-rate moves shift many CDs by
  the same % at once. remove_shared_moves() takes those shared moves out; the raw_* columns
  keep the unadjusted figures.
  scarcity_index    0-100 score: weighted blend of two percentile ranks within the screened
                    universe - demand (want/have ratio) and momentum (velocity). Releases
                    without enough history for a velocity count as neutral (50th pct).

Results are written to `release_metrics`, which the dashboard reads.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3

import numpy as np
import pandas as pd

from . import config, db

log = logging.getLogger(__name__)

METRIC_COLUMNS = [
    "release_id", "rank", "currency", "current_price", "current_at", "baseline_price",
    "baseline_at", "abs_change", "pct_appreciation", "growth_30d_pct", "velocity_30d_pct",
    "demand_pctile", "momentum_pctile", "scarcity_index", "n_snapshots", "sparkline",
    "raw_pct_appreciation", "raw_growth_30d_pct",
    "listed_price", "listed_count", "usual_listed_price", "discount_vs_usual_pct",
    "discount_vs_estimate_pct", "deal", "deal_reason", "reissue_risk", "reissue_reason",
    "computed_at",
]


def load_price_history(conn: sqlite3.Connection) -> pd.DataFrame:
    df = pd.read_sql_query(
        "SELECT release_id, fetched_at, price, currency FROM price_history", conn)
    df["fetched_at"] = pd.to_datetime(df["fetched_at"], utc=True, format="ISO8601")
    return df


def load_releases(conn: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql_query("SELECT release_id, want_have_ratio FROM releases", conn)


def _poisson_tail(k: int, lam: float) -> float:
    """P(X >= k) for X ~ Poisson(lam)."""
    term, cdf = math.exp(-lam), 0.0
    for i in range(k):
        cdf += term
        term *= lam / (i + 1)
    return max(0.0, 1.0 - cdf)


def _shared_moves(returns: pd.Series, prices: pd.Series, *, tolerance: float,
                  min_members: int, min_share: float, neighbourhood: int = 10,
                  alpha: float = 1e-3) -> list[float]:
    """Find % moves that many releases made identically on the same day.

    Discogs computes each suggestion in some underlying currency and converts it to the
    account currency daily, so releases sharing a currency move in lockstep. Only prices of
    at least 20 are used to locate the clusters, where 1-cent rounding is below 0.05%.

    A shared move must be a statistically significant *spike*: given how many releases
    moved by similar amounts in the surrounding +/- (neighbourhood x tolerance) range,
    seeing this many inside the +/- tolerance window by chance must have probability
    below `alpha`. On days when many releases move for their own reasons, chance
    groupings in that spread are therefore not mistaken for currency effects.
    """
    precise = returns[prices >= 20].sort_values().to_numpy()
    threshold = max(min_members, min_share * len(returns))
    candidates = precise  # windows still to examine
    reference = precise  # background for the significance test (minus accepted spikes)
    moves = []
    while len(candidates):
        counts = [np.sum(np.abs(candidates - r) <= tolerance) for r in candidates]
        best = int(np.argmax(counts))
        if counts[best] < threshold:
            break
        centre = candidates[best]
        in_window = int(np.sum(np.abs(reference - centre) <= tolerance))
        nearby = np.sum(np.abs(reference - centre) <= tolerance * neighbourhood)
        background = max((nearby - in_window) / (neighbourhood - 1), 0.5)  # expected by chance
        members = np.abs(candidates - centre) <= tolerance
        if _poisson_tail(in_window, background) < alpha:
            moves.append(float(np.median(candidates[members])))
            reference = reference[np.abs(reference - centre) > tolerance]
        candidates = candidates[~members]
    return moves


def remove_shared_moves(
    history: pd.DataFrame,
    *,
    tolerance_pct: float = config.SHARED_MOVE_TOLERANCE_PCT,
    min_members: int = config.SHARED_MOVE_MIN_MEMBERS,
    min_share: float = config.SHARED_MOVE_MIN_SHARE,
    min_explained: float = config.SHARED_MOVE_MIN_EXPLAINED,
) -> pd.DataFrame:
    """Add `adj_price`: the price path with market-wide (currency) moves taken out.

    For each pair of consecutive observation dates (oldest first):
    * releases whose change matches a shared move (within rounding) count as unchanged,
      and remember who they moved with - their currency group;
    * releases that changed for their own reasons keep their change, minus that day's move
      of *their own* currency group (the shared move most of their previous co-movers
      made). If their group is unknown, nothing is subtracted.
    The adjusted path starts at the first real price.
    """
    h = history.sort_values(["release_id", "fetched_at"], kind="mergesort").copy()
    grouped = h.groupby("release_id")
    h["prev_price"] = grouped["price"].shift()
    h["prev_at"] = grouped["fetched_at"].shift()
    h["adj_ret"] = 0.0
    steps = h["prev_price"].notna()
    if steps.any():
        s = h.loc[steps]
        ret = np.log(s["price"] / s["prev_price"]) * 100
        pair = s["prev_at"].dt.strftime("%Y-%m-%d") + ">" + s["fetched_at"].dt.strftime("%Y-%m-%d")
        adjusted = pd.Series(0.0, index=s.index)
        co_movers: dict[int, set] = {}  # release -> releases it last moved in lockstep with
        for _, idx in sorted(pair.groupby(pair).groups.items()):  # chronological
            r = ret[idx].to_numpy()
            price = s.loc[idx, "price"].to_numpy()
            prev = s.loc[idx, "prev_price"].to_numpy()
            rids = s.loc[idx, "release_id"].to_numpy()
            moves = _shared_moves(ret[idx], s.loc[idx, "price"], tolerance=tolerance_pct,
                                  min_members=min_members, min_share=min_share)

            def matches(move: float) -> np.ndarray:
                # "Unchanged" means the same price to the penny (an estimate that did not
                # move is identical day to day); a shared move allows a cent of rounding
                # plus its tolerance, since conversions round in two currencies.
                slack = 0.005 if move == 0.0 else np.maximum(0.011, price * tolerance_pct / 100)
                return np.abs(price - prev * np.exp(move / 100)) <= slack

            unchanged = matches(0.0)
            explained = {m: matches(m) & ~unchanged for m in moves}
            # Currency days look like a few groups explaining most CDs. If the candidate
            # groups explain only a small share, the CDs moved for their own reasons and
            # any "groups" are chance groupings: keep every move.
            share = (unchanged | np.logical_or.reduce(list(explained.values()) or [unchanged])).mean()
            if moves and share < min_explained:
                moves, explained = [], {}
            groups = {m: set(rids[mask]) for m, mask in explained.items()}
            out = np.zeros(len(r))
            for i, rid in enumerate(rids):
                if unchanged[i]:
                    continue
                hit = next((m for m in moves if explained[m][i]), None)
                if hit is not None:
                    co_movers[rid] = groups[hit] - {rid}
                    continue
                # A genuine move: subtract the move its own currency group made today.
                group_move, overlap = 0.0, 0
                for m, members in groups.items():
                    shared = len(co_movers.get(rid, set()) & members)
                    if shared > overlap:
                        group_move, overlap = m, shared
                out[i] = r[i] - (group_move if overlap >= 3 else 0.0)
            adjusted[idx] = out
        h.loc[steps, "adj_ret"] = adjusted
    first_price = h.groupby("release_id")["price"].transform("first")
    h["adj_price"] = first_price * np.exp(h.groupby("release_id")["adj_ret"].cumsum() / 100)
    return h.drop(columns=["prev_price", "prev_at", "adj_ret"])


def adjusted_history(history: pd.DataFrame) -> pd.DataFrame:
    """Clean price history and add `adj_price` (shared currency moves removed).

    Never mixes currencies: only observations in each release's latest currency are kept.
    """
    h = history.dropna(subset=["price", "currency"])
    h = h[h["price"] > 0].sort_values(["release_id", "fetched_at"], kind="mergesort")
    if h.empty:
        return h.assign(adj_price=pd.Series(dtype=float))
    latest_ccy = h.groupby("release_id")["currency"].transform("last")
    return remove_shared_moves(h[h["currency"] == latest_ccy])


def market_index(adjusted: pd.DataFrame, release_ids=None) -> list[dict]:
    """Chain-linked market index (100 = first day): median daily change across CDs.

    `index` uses adjusted prices (each CD's own moves); `raw_index` uses the raw prices,
    so the gap between them is the currency effect.
    """
    h = adjusted if release_ids is None else adjusted[adjusted["release_id"].isin(release_ids)]
    if h.empty:
        return []
    h = h.assign(day=h["fetched_at"].dt.strftime("%Y-%m-%d"))
    daily = h.groupby(["release_id", "day"], as_index=False)[["price", "adj_price"]].last()
    daily = daily.sort_values(["release_id", "day"])
    grouped = daily.groupby("release_id")
    daily["raw_ret"] = np.log(daily["price"] / grouped["price"].shift())
    daily["adj_ret"] = np.log(daily["adj_price"] / grouped["adj_price"].shift())
    per_day = daily.groupby("day").agg(
        adj=("adj_ret", "median"), raw=("raw_ret", "median"), n=("release_id", "size"))
    per_day[["adj", "raw"]] = per_day[["adj", "raw"]].fillna(0.0)  # first day = base
    per_day["index"] = 100 * np.exp(per_day["adj"].cumsum())
    per_day["raw_index"] = 100 * np.exp(per_day["raw"].cumsum())
    return [{"date": day, "index": round(float(r["index"]), 3),
             "raw_index": round(float(r["raw_index"]), 3), "cds": int(r["n"])}
            for day, r in per_day.iterrows()]


def compute_metrics(
    history: pd.DataFrame,
    releases: pd.DataFrame,
    *,
    growth_days: int = config.GROWTH_WINDOW_DAYS,
    growth_tolerance_days: int = config.GROWTH_TOLERANCE_DAYS,
    velocity_days: int = config.VELOCITY_WINDOW_DAYS,
    velocity_min_points: int = config.VELOCITY_MIN_POINTS,
    velocity_min_span_days: float = config.VELOCITY_MIN_SPAN_DAYS,
    weight_demand: float = config.SCORE_WEIGHT_DEMAND,
    weight_momentum: float = config.SCORE_WEIGHT_MOMENTUM,
    sparkline_points: int = config.SPARKLINE_POINTS,
) -> pd.DataFrame:
    """Return one row per release (indexed by release_id), sorted by rank."""
    h = adjusted_history(history)
    if h.empty:
        return pd.DataFrame(columns=METRIC_COLUMNS[1:-1]).rename_axis("release_id")

    # --- current vs. baseline ----------------------------------------------------
    m = h.groupby("release_id").agg(
        currency=("currency", "last"),
        current_price=("price", "last"),
        current_adj=("adj_price", "last"),
        current_at=("fetched_at", "last"),
        baseline_price=("price", "first"),
        baseline_at=("fetched_at", "first"),
        n_snapshots=("price", "size"),
    )
    m["abs_change"] = m["current_price"] - m["baseline_price"]
    m["raw_pct_appreciation"] = m["abs_change"] / m["baseline_price"] * 100
    m["pct_appreciation"] = (m["current_adj"] / m["baseline_price"] - 1) * 100

    # --- 30-day growth: as-of join to the price ~30 days before the latest one ----
    targets = (
        (m["current_at"] - pd.Timedelta(days=growth_days))
        .rename("target_at").reset_index().sort_values("target_at")
    )
    past = pd.merge_asof(
        targets,
        h[["release_id", "fetched_at", "price", "adj_price"]].sort_values("fetched_at"),
        left_on="target_at",
        right_on="fetched_at",
        by="release_id",
        direction="backward",  # latest observation at or before the target date ...
        tolerance=pd.Timedelta(days=growth_tolerance_days),  # ... but not a stale one
    )
    past = past.set_index("release_id")
    m["raw_growth_30d_pct"] = (m["current_price"] / past["price"] - 1) * 100
    m["growth_30d_pct"] = (m["current_adj"] / past["adj_price"] - 1) * 100

    # --- velocity: OLS slope of ln(adjusted price) vs. days over the trailing window -
    window_start = h["release_id"].map(m["current_at"]) - pd.Timedelta(days=velocity_days)
    w = h.loc[h["fetched_at"] >= window_start,
              ["release_id", "fetched_at", "price", "adj_price"]].copy()
    w["x"] = (w["fetched_at"] - w["fetched_at"].min()).dt.total_seconds() / 86400.0
    w["y"] = np.log(w["adj_price"])
    grouped = w.groupby("release_id")
    dx = w["x"] - grouped["x"].transform("mean")
    dy = w["y"] - grouped["y"].transform("mean")
    sxx = (dx * dx).groupby(w["release_id"]).sum()
    sxy = (dx * dy).groupby(w["release_id"]).sum()
    n_points = grouped.size()
    span_days = grouped["x"].max() - grouped["x"].min()
    enough = (n_points >= velocity_min_points) & (span_days >= velocity_min_span_days) & (sxx > 0)
    slope_per_day = (sxy / sxx).where(enough)
    m["velocity_30d_pct"] = (np.exp(slope_per_day * 30) - 1) * 100

    # --- scarcity index -----------------------------------------------------------
    before = len(m)
    m = m.join(releases.set_index("release_id")["want_have_ratio"], how="inner")
    if len(m) < before:
        log.warning("%d releases have prices but no metadata row - left out", before - len(m))
    m["demand_pctile"] = m["want_have_ratio"].rank(pct=True)
    m["momentum_pctile"] = m["velocity_30d_pct"].rank(pct=True).fillna(0.5)
    total_weight = weight_demand + weight_momentum
    m["scarcity_index"] = (
        100 * (weight_demand * m["demand_pctile"] + weight_momentum * m["momentum_pctile"])
        / total_weight
    )

    # --- sparkline: up to N evenly spaced prices from the trailing window ---------
    sparks = {}
    for rid, grp in w.groupby("release_id"):
        prices = grp["price"].to_numpy()
        idx = np.unique(np.linspace(0, len(prices) - 1, min(len(prices), sparkline_points))
                        .round().astype(int))
        sparks[rid] = json.dumps([round(float(p), 2) for p in prices[idx]])
    m["sparkline"] = pd.Series(sparks, dtype=object).reindex(m.index).fillna("[]")

    # --- rank & tidy --------------------------------------------------------------
    for col in ("current_price", "baseline_price", "abs_change"):
        m[col] = m[col].round(2)
    for col in ("pct_appreciation", "growth_30d_pct", "velocity_30d_pct",
                "raw_pct_appreciation", "raw_growth_30d_pct"):
        m[col] = m[col].round(2)
    for col in ("demand_pctile", "momentum_pctile"):
        m[col] = m[col].round(4)
    m["scarcity_index"] = m["scarcity_index"].round(1)
    m = m.sort_values(["scarcity_index", "growth_30d_pct", "want_have_ratio"],
                      ascending=False, na_position="last", kind="mergesort")
    m["rank"] = np.arange(1, len(m) + 1)
    return m.drop(columns=["want_have_ratio", "current_adj"])


def market_signals(
    metrics: pd.DataFrame,
    listings: pd.DataFrame,
    albums: pd.DataFrame,
    release_masters: pd.Series,
    *,
    today_year: int | None = None,
) -> pd.DataFrame:
    """Add deal-finder and reissue-monitor columns to `metrics` (indexed by release_id).

    listings: release_id, fetched_at, lowest_price, currency, num_for_sale (listing_history)
    albums:   master_id, fetched_at, cd_versions, latest_cd_year, latest_cd_release_id
    release_masters: master_id per release_id
    """
    m = metrics.copy()
    for col in ("listed_price", "listed_count", "usual_listed_price",
                "discount_vs_usual_pct", "discount_vs_estimate_pct"):
        m[col] = np.nan
    m["deal"] = 0
    m["deal_reason"] = None
    m["reissue_risk"] = 0
    m["reissue_reason"] = None
    today_year = today_year or pd.Timestamp.now(tz="UTC").year

    # --- deal finder ----------------------------------------------------------------
    if not listings.empty:
        lst = listings.sort_values(["release_id", "fetched_at"], kind="mergesort")
        latest = lst.groupby("release_id").last()
        m["listed_price"] = latest["lowest_price"]
        m["listed_count"] = latest["num_for_sale"]
        listed_ccy = latest["currency"].reindex(m.index)

        # "Usual" price: median of earlier listings (with copies) in the trailing window.
        latest_at = lst["release_id"].map(latest["fetched_at"])
        prior = lst[(lst["fetched_at"] < latest_at)
                    & (lst["fetched_at"] >= latest_at - pd.Timedelta(days=config.USUAL_LISTING_DAYS))
                    & lst["lowest_price"].notna()]
        stats = prior.groupby("release_id")["lowest_price"].agg(["median", "size"])
        usual = stats["median"].where(stats["size"] >= config.USUAL_LISTING_MIN_OBS)
        m["usual_listed_price"] = usual
        m["discount_vs_usual_pct"] = (1 - m["listed_price"] / m["usual_listed_price"]) * 100
        same_ccy = listed_ccy.isna() | (listed_ccy == m["currency"])
        m["discount_vs_estimate_pct"] = ((1 - m["listed_price"] / m["current_price"]) * 100).where(same_ccy)

        for rid, row in m.iterrows():
            if not row["listed_count"] or pd.isna(row["listed_price"]):
                continue
            if row["discount_vs_usual_pct"] >= config.DEAL_BELOW_USUAL_PCT:
                m.at[rid, "deal"] = 1
                m.at[rid, "deal_reason"] = (
                    f"Listed {row['discount_vs_usual_pct']:.0f}% below its usual listing price")
            elif row["discount_vs_estimate_pct"] >= config.DEAL_BELOW_ESTIMATE_PCT:
                m.at[rid, "deal"] = 1
                m.at[rid, "deal_reason"] = (
                    f"Listed {row['discount_vs_estimate_pct']:.0f}% below the NM estimate "
                    "(check the copy's condition)")

    # --- reissue monitor -----------------------------------------------------------
    if not albums.empty:
        alb = albums.dropna(subset=["cd_versions"]).sort_values(["master_id", "fetched_at"])
        if not alb.empty:
            grouped = alb.groupby("master_id")
            first, last = grouped.first(), grouped.last()
            for rid in m.index:
                master = release_masters.get(rid)
                if pd.isna(master) or master not in last.index:
                    continue
                now, then = last.loc[master], first.loc[master]
                if now["cd_versions"] > then["cd_versions"]:
                    added = int(now["cd_versions"] - then["cd_versions"])
                    reason = (f"{added} new CD pressing{'s' if added > 1 else ''} added since "
                              f"{then['fetched_at']:%d %b %Y}")
                elif (pd.notna(now["latest_cd_year"]) and now["latest_cd_release_id"] != rid
                      and now["latest_cd_year"] >= today_year - config.REISSUE_RECENT_YEARS):
                    reason = f"Reissued on CD in {int(now['latest_cd_year'])}"
                else:
                    continue
                m.at[rid, "reissue_risk"] = 1
                m.at[rid, "reissue_reason"] = reason

    for col in ("discount_vs_usual_pct", "discount_vs_estimate_pct"):
        m[col] = m[col].round(1)
    return m


def load_market_inputs(conn: sqlite3.Connection):
    listings = pd.read_sql_query(
        "SELECT release_id, fetched_at, lowest_price, currency, num_for_sale FROM listing_history",
        conn)
    listings["fetched_at"] = pd.to_datetime(listings["fetched_at"], utc=True, format="ISO8601")
    albums = pd.read_sql_query(
        "SELECT master_id, fetched_at, cd_versions, latest_cd_year, latest_cd_release_id "
        "FROM master_snapshots", conn)
    albums["fetched_at"] = pd.to_datetime(albums["fetched_at"], utc=True, format="ISO8601")
    masters = pd.read_sql_query("SELECT release_id, master_id FROM releases", conn)
    return listings, albums, masters.set_index("release_id")["master_id"]


def save_metrics(conn: sqlite3.Connection, metrics: pd.DataFrame) -> int:
    """Replace the release_metrics snapshot in a single transaction."""
    rows: list[tuple] = []
    if not metrics.empty:
        out = metrics.reset_index()
        for col in ("current_at", "baseline_at"):
            out[col] = out[col].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        out["computed_at"] = db.utc_now_iso()
        rows = [
            tuple(None if isinstance(v, float) and math.isnan(v) else v for v in record.values())
            for record in out[METRIC_COLUMNS].to_dict("records")  # native Python types
        ]
    marks = ", ".join("?" for _ in METRIC_COLUMNS)
    with conn:
        conn.execute("DELETE FROM release_metrics")
        conn.executemany(
            f"INSERT INTO release_metrics ({', '.join(METRIC_COLUMNS)}) VALUES ({marks})", rows)
    return len(rows)


def run_analytics(conn: sqlite3.Connection, *, show: int = 15) -> pd.DataFrame:
    db.ensure_releases_loaded(conn)
    history = load_price_history(conn)
    if history.empty:
        raise db.ScreenerError(
            "No price history yet. Run `python -m cd_screener fetch-prices` first.")
    metrics = compute_metrics(history, load_releases(conn))
    metrics = market_signals(metrics, *load_market_inputs(conn))
    saved = save_metrics(conn, metrics)
    if saved:
        print(f"Signals: {int(metrics['deal'].sum())} deals, "
              f"{int(metrics['reissue_risk'].sum())} reissue risks")
    print(f"\nAnalytics: {saved} releases scored from {len(history):,} price observations")

    if show and saved:
        meta = pd.read_sql_query("SELECT release_id, artist, title, want_have_ratio FROM releases",
                                 conn).set_index("release_id")
        top = metrics.head(show).join(meta)
        top["want_have_ratio"] = top["want_have_ratio"].round(2)
        top["artist_title"] = (top["artist"] + " - " + top["title"]).str.slice(0, 42)
        cols = ["rank", "artist_title", "want_have_ratio", "current_price", "pct_appreciation",
                "growth_30d_pct", "velocity_30d_pct", "scarcity_index"]
        print(f"\nCD Appreciation Leaderboard - top {len(top)}")
        print(top[cols].to_string(index=False))
    return metrics
