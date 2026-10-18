"""Stage 4 - Flask app: a small JSON API plus the static dashboard.

    GET /                          dashboard (static/index.html)
    GET /api/summary               funnel counts, data mode, last fetch time
    GET /api/genres                genres present on the leaderboard
    GET /api/leaderboard           ?q= &genre= &min_growth= &max_growth= &sort= &order= &limit=
    GET /api/releases/<id>         one release: metadata, metrics, full price history
    GET /api/me/wantlist           your wantlist by album (+ /<album_key> for one album)
    GET /api/me/collection         your collection with daily prices (+ /<release_id>)
    PUT/DELETE /api/me/collection/<release_id>/purchase   what you paid for an item
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import statistics
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, Response, g, jsonify, request
from werkzeug.exceptions import HTTPException

from . import analytics, config, db, mydiscogs, report

log = logging.getLogger(__name__)

# Whitelisted sort keys -> (SQL expression, collation). Never interpolate user input.
SORT_COLUMNS = {
    "rank": ("m.rank", ""),
    "title": ("r.title", " COLLATE NOCASE"),
    "artist": ("r.artist", " COLLATE NOCASE"),
    "year": ("r.year", ""),
    "ratio": ("r.want_have_ratio", ""),
    "price": ("m.current_price", ""),
    "growth": ("m.growth_30d_pct", ""),
    "velocity": ("m.velocity_30d_pct", ""),
    "appreciation": ("m.pct_appreciation", ""),
    "score": ("m.scarcity_index", ""),
    "listed": ("l.listed_price", ""),
    "discount": ("COALESCE(m.discount_vs_usual_pct, m.discount_vs_estimate_pct)", ""),
    "premium": ("m.current_price / a.album_lowest_price", ""),
}

FLAG_FILTERS = {"deal": "m.deal = 1", "reissue": "m.reissue_risk = 1"}

# Latest listing per pressing and latest album snapshot (SQLite returns the MAX(id) row).
LATEST_LISTING = """(SELECT release_id, lowest_price AS listed_price, currency AS listed_currency,
                            num_for_sale AS listed_count, fetched_at AS listed_at, MAX(id)
                     FROM listing_history GROUP BY release_id)"""
LATEST_ALBUM = """(SELECT master_id, lowest_price AS album_lowest_price,
                          num_for_sale AS album_for_sale, MAX(id)
                   FROM master_snapshots GROUP BY master_id)"""

LEADERBOARD_SELECT = f"""
    SELECT m.rank, r.release_id, r.title, r.artist, r.year, r.genre, r.style, r.country,
           r.label, r.catno, r.master_id,
           r.wants, r.haves, r.want_have_ratio, r.source,
           m.currency, m.current_price, m.current_at, m.baseline_price, m.baseline_at,
           m.abs_change, m.pct_appreciation, m.growth_30d_pct, m.velocity_30d_pct,
           m.demand_pctile, m.momentum_pctile, m.scarcity_index, m.n_snapshots, m.sparkline,
           m.usual_listed_price, m.discount_vs_usual_pct, m.discount_vs_estimate_pct,
           m.deal, m.deal_reason, m.reissue_risk, m.reissue_reason,
           l.listed_price, l.listed_currency, l.listed_count, l.listed_at,
           a.album_lowest_price, a.album_for_sale
    FROM release_metrics AS m
    JOIN releases AS r USING (release_id)
    LEFT JOIN {LATEST_LISTING} AS l USING (release_id)
    LEFT JOIN {LATEST_ALBUM} AS a ON a.master_id = r.master_id
"""


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _float_arg(name: str) -> float | None:
    raw = request.args.get(name, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        raise ApiError(f"'{name}' must be a number, got {raw!r}") from None
    if not math.isfinite(value):
        raise ApiError(f"'{name}' must be a finite number")
    return value


def _filter_clauses() -> tuple[str, dict]:
    """WHERE clause for the shared filters (search, genre, growth range) on m/r aliases."""
    q = request.args.get("q", "").strip()
    genre = request.args.get("genre", "").strip()
    min_growth = _float_arg("min_growth")
    max_growth = _float_arg("max_growth")
    clauses, params = [], {}
    if q:
        clauses.append("(r.title LIKE :q ESCAPE '\\' OR r.artist LIKE :q ESCAPE '\\')")
        params["q"] = f"%{_escape_like(q)}%"
    if genre:
        clauses.append("r.genre = :genre")
        params["genre"] = genre
    if min_growth is not None:
        clauses.append("m.growth_30d_pct >= :min_growth")
        params["min_growth"] = min_growth
    if max_growth is not None:
        clauses.append("m.growth_30d_pct <= :max_growth")
        params["max_growth"] = max_growth
    flag = request.args.get("flag", "").strip()
    if flag:
        if flag not in FLAG_FILTERS:
            raise ApiError(f"'flag' must be one of: {', '.join(FLAG_FILTERS)}")
        clauses.append(FLAG_FILTERS[flag])
    return (f" WHERE {' AND '.join(clauses)}" if clauses else ""), params


_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _require_local_request() -> None:
    """Changes are accepted only from the dashboard itself, on this PC.

    Blocks other websites (and DNS-rebinding tricks) from editing your data through the
    local server: the Host must be local, and so must the Origin when the browser sends one.
    """
    host = urlsplit(f"//{request.host}").hostname
    origin = request.headers.get("Origin")
    if host not in _LOCAL_HOSTS or (origin and urlsplit(origin).hostname not in _LOCAL_HOSTS):
        raise ApiError("Changes are only accepted from the dashboard on this computer", 403)


def _money_field(body: dict, name: str, *, required: bool = False) -> float | None:
    raw = body.get(name)
    if raw is None or raw == "":
        if required:
            raise ApiError(f"'{name}' is required")
        return None
    if isinstance(raw, bool):
        raise ApiError(f"'{name}' must be a number")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ApiError(f"'{name}' must be a number, got {raw!r}") from None
    if not math.isfinite(value) or not 0 <= value <= 1_000_000:
        raise ApiError(f"'{name}' must be between 0 and 1,000,000")
    return round(value, 2)


def _date_field(body: dict, name: str) -> str | None:
    raw = body.get(name)
    if raw is None or raw == "":
        return None
    try:
        day = date.fromisoformat(str(raw))
    except ValueError:
        raise ApiError(f"'{name}' must be a date like 2026-10-18") from None
    if day > date.today() + timedelta(days=1):
        raise ApiError(f"'{name}' can't be in the future")
    return day.isoformat()


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _discogs_url(row) -> str | None:
    return f"https://www.discogs.com/release/{row['release_id']}" if row["source"] == "discogs" else None


def _ratio(numerator, denominator) -> float | None:
    if numerator is None or not denominator:
        return None
    return round(numerator / denominator, 2)


def _market_checks(item: dict) -> dict:
    """Cross-check the NM estimate against real listings for this pressing and its album."""
    price = item.get("current_price")
    same_currency = item.get("listed_currency") in (None, item.get("currency"))
    item["estimate_vs_listing"] = (
        _ratio(price, item.get("listed_price")) if same_currency else None)
    # Album prices come back in the account currency, which is the suggestion currency.
    item["pressing_premium"] = _ratio(price, item.get("album_lowest_price"))
    item["estimate_high"] = (item["estimate_vs_listing"] or 0) >= config.ESTIMATE_HIGH_RATIO
    item["rare_pressing"] = (item["pressing_premium"] or 0) >= config.RARE_PRESSING_PREMIUM
    return item


def _leaderboard_item(row: sqlite3.Row) -> dict:
    item = dict(row)
    item.pop("MAX(id)", None)
    item["want_have_ratio"] = round(item["want_have_ratio"], 2)
    item["sparkline"] = json.loads(item["sparkline"] or "[]")
    item["discogs_url"] = _discogs_url(row)
    return _market_checks(item)


def create_app(db_path: Path | str | None = None) -> Flask:
    app = Flask(__name__, static_folder="static", static_url_path="/static")
    app.json.sort_keys = False
    app.config["DB_PATH"] = Path(db_path) if db_path else config.DB_PATH
    if app.config["DB_PATH"].exists():  # upgrade an older database before serving it
        with closing(db.connect(app.config["DB_PATH"])) as conn:
            db.init_schema(conn)

    def get_db() -> sqlite3.Connection:
        if "db" not in g:
            g.db = db.connect(app.config["DB_PATH"], must_exist=True)
        return g.db

    @app.teardown_appcontext
    def close_db(_exc):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    # --- pages -------------------------------------------------------------------

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    # --- API ---------------------------------------------------------------------

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/summary")
    def summary():
        conn = get_db()
        counts = conn.execute(
            "SELECT COUNT(*) AS releases, "
            "       COALESCE(SUM(format = :fmt), 0) AS cds, "
            "       COALESCE(SUM(format = :fmt AND want_have_ratio > :ratio "
            "                    AND wants >= :wants), 0) AS flagged, "
            "       COALESCE(SUM(source = 'simulated'), 0) AS simulated "
            "FROM releases",
            {"fmt": config.TARGET_FORMAT, "ratio": config.MIN_WANT_HAVE_RATIO,
             "wants": config.MIN_WANTS},
        ).fetchone()
        growth = [r[0] for r in conn.execute(
            "SELECT growth_30d_pct FROM release_metrics WHERE growth_30d_pct IS NOT NULL")]
        # Latest fetch outcome per release (SQLite returns the row holding MAX(id)).
        outcomes = dict(conn.execute(
            "SELECT status, COUNT(*) FROM ("
            "  SELECT release_id, status, MAX(id) FROM fetch_log GROUP BY release_id"
            ") GROUP BY status").fetchall())
        priced, computed_at, deals, reissues = conn.execute(
            "SELECT COUNT(*), MAX(computed_at), COALESCE(SUM(deal), 0), "
            "COALESCE(SUM(reissue_risk), 0) FROM release_metrics").fetchone()
        (last_price_at,) = conn.execute("SELECT MAX(fetched_at) FROM price_history").fetchone()
        return {
            "mode": "simulated" if counts["simulated"] else "live",
            "format": config.TARGET_FORMAT,
            "min_ratio": config.MIN_WANT_HAVE_RATIO,
            "min_wants": config.MIN_WANTS,
            "releases": counts["releases"],
            "cds": counts["cds"],
            "flagged": counts["flagged"],
            "priced": priced,
            "missing_price_data": outcomes.get("no_data", 0),
            "not_found": outcomes.get("not_found", 0),
            "fetch_errors": outcomes.get("error", 0),
            "median_growth_30d": round(statistics.median(growth), 2) if growth else None,
            "rising_30d": sum(1 for x in growth if x > 0),
            "with_growth_30d": len(growth),
            "deals": deals,
            "reissue_risks": reissues,
            "last_price_at": last_price_at,
            "computed_at": computed_at,
        }

    @app.get("/api/genres")
    def genres():
        rows = get_db().execute(
            "SELECT r.genre AS genre, COUNT(*) AS count FROM release_metrics m "
            "JOIN releases r USING (release_id) WHERE r.genre IS NOT NULL "
            "GROUP BY r.genre ORDER BY count DESC, r.genre").fetchall()
        return jsonify([dict(r) for r in rows])

    @app.get("/api/me/wantlist")
    def my_wantlist():
        albums = mydiscogs.wanted_albums(get_db())
        albums.sort(key=lambda a: (not a["deal"], -(a["below_usual_pct"] or -1e9),
                                   a["artist"].lower(), a["title"].lower()))
        (items,) = get_db().execute(
            "SELECT COUNT(*) FROM user_items WHERE list = 'want' AND active = 1").fetchone()
        return {"albums": albums, "items": items}

    @app.get("/api/me/wantlist/<album_key>")
    def my_wanted_album(album_key: str):
        album = mydiscogs.wanted_album_detail(get_db(), album_key)
        if album is None:
            raise ApiError(f"Album {album_key} is not on your wantlist", 404)
        return album

    @app.get("/api/me/collection")
    def my_collection():
        return mydiscogs.collection_summary(get_db())

    def collection_item(release_id: int) -> dict:
        item = mydiscogs.collection_item_detail(get_db(), release_id)
        if item is None:
            raise ApiError(f"Release {release_id} is not in your collection", 404)
        return item

    @app.get("/api/me/collection/<int:release_id>")
    def my_collection_item(release_id: int):
        return collection_item(release_id)

    @app.put("/api/me/collection/<int:release_id>/purchase")
    def set_purchase(release_id: int):
        _require_local_request()
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise ApiError("Send the purchase as a JSON object")
        currency = str(body.get("currency") or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise ApiError("'currency' must be a 3-letter code such as GBP")
        saved = mydiscogs.set_purchase(
            get_db(), release_id, price_paid=_money_field(body, "price_paid", required=True),
            currency=currency, purchased_on=_date_field(body, "purchased_on"))
        if not saved:
            raise ApiError(f"Release {release_id} is not in your collection", 404)
        return collection_item(release_id)

    @app.delete("/api/me/collection/<int:release_id>/purchase")
    def clear_purchase(release_id: int):
        _require_local_request()
        mydiscogs.clear_purchase(get_db(), release_id)
        return collection_item(release_id)

    @app.get("/api/reports/latest")
    def latest_report():
        data = report.latest(get_db())
        if data is None:
            raise ApiError("No weekly report has been published yet. It appears after the "
                           "first `python -m cd_screener fetch-prices` run (or run `report`).", 404)
        return data

    @app.get("/api/report")
    def market_report():
        data = report.latest(get_db())
        if data is None:
            raise ApiError("No weekly report has been published yet.", 404)
        filename = f"cd-market-report-{data['period_end'][:10]}.md"
        return Response(report.to_markdown(data), mimetype="text/markdown",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.get("/api/market-index")
    def market_index():
        where, params = _filter_clauses()
        conn = get_db()
        ids = [r[0] for r in conn.execute(
            f"SELECT m.release_id FROM release_metrics m JOIN releases r USING (release_id){where}",
            params)]
        adjusted = analytics.adjusted_history(analytics.load_price_history(conn))
        return {"points": analytics.market_index(adjusted, ids), "cds": len(ids)}

    @app.get("/api/leaderboard")
    def leaderboard():
        sort = request.args.get("sort", "rank")
        order = request.args.get("order", "asc").lower()
        if sort not in SORT_COLUMNS:
            raise ApiError(f"'sort' must be one of: {', '.join(SORT_COLUMNS)}")
        if order not in ("asc", "desc"):
            raise ApiError("'order' must be 'asc' or 'desc'")
        try:
            limit = min(max(int(request.args.get("limit", 500)), 1), 1000)
        except ValueError:
            raise ApiError("'limit' must be an integer") from None

        where, params = _filter_clauses()
        params["limit"] = limit

        expr, collation = SORT_COLUMNS[sort]
        order_by = f" ORDER BY {expr} IS NULL, {expr}{collation} {order.upper()}, m.rank"
        conn = get_db()
        rows = conn.execute(f"{LEADERBOARD_SELECT}{where}{order_by} LIMIT :limit", params)
        items = [_leaderboard_item(r) for r in rows]
        (matched,) = conn.execute(
            f"SELECT COUNT(*) FROM release_metrics m JOIN releases r USING (release_id){where}",
            params).fetchone()
        (total,) = conn.execute("SELECT COUNT(*) FROM release_metrics").fetchone()
        return {"total": total, "matched": matched, "items": items}

    @app.get("/api/releases/<int:release_id>")
    def release_detail(release_id: int):
        conn = get_db()
        release = conn.execute(
            "SELECT * FROM releases WHERE release_id = ?", (release_id,)).fetchone()
        if release is None:
            raise ApiError(f"Release {release_id} is not in the database", 404)
        metrics = conn.execute(
            "SELECT * FROM release_metrics WHERE release_id = ?", (release_id,)).fetchone()
        history = conn.execute(
            "SELECT fetched_at, price, currency, source FROM price_history "
            "WHERE release_id = ? ORDER BY fetched_at", (release_id,)).fetchall()
        fetches = conn.execute(
            "SELECT attempted_at, status, http_status, message FROM fetch_log "
            "WHERE release_id = ? ORDER BY id DESC LIMIT 5", (release_id,)).fetchall()
        listings = conn.execute(
            "SELECT lowest_price, currency, num_for_sale, fetched_at FROM listing_history "
            "WHERE release_id = ? ORDER BY id", (release_id,)).fetchall()
        listing = listings[-1] if listings else None
        album = None
        if release["master_id"]:
            album = conn.execute(
                "SELECT lowest_price, num_for_sale, fetched_at FROM master_snapshots "
                "WHERE master_id = ? ORDER BY id DESC LIMIT 1", (release["master_id"],)).fetchone()
        rel = dict(release)
        rel["want_have_ratio"] = round(rel["want_have_ratio"], 2)
        rel["discogs_url"] = _discogs_url(release)
        rel["album_url"] = (f"https://www.discogs.com/master/{release['master_id']}"
                            if release["master_id"] and release["source"] == "discogs" else None)
        met = dict(metrics) if metrics else None
        market = None
        if met:
            met["sparkline"] = json.loads(met["sparkline"] or "[]")
            market = _market_checks({
                "currency": met["currency"],
                "current_price": met["current_price"],
                "listed_price": listing["lowest_price"] if listing else None,
                "listed_currency": listing["currency"] if listing else None,
                "album_lowest_price": album["lowest_price"] if album else None,
            })
        return {
            "release": rel,
            "metrics": met,
            "listing": dict(listing) if listing else None,
            "listing_history": [dict(x) for x in listings],
            "album": dict(album) if album else None,
            "checks": market,
            "history": [dict(h) for h in history],
            "recent_fetches": [dict(f) for f in fetches],
        }

    # --- errors ------------------------------------------------------------------

    @app.errorhandler(ApiError)
    def handle_api_error(exc: ApiError):
        return jsonify(error=str(exc)), exc.status

    @app.errorhandler(db.DatabaseNotReady)
    def handle_db_missing(exc):
        return jsonify(error=str(exc)), 503

    @app.errorhandler(sqlite3.OperationalError)
    def handle_db_error(exc):
        log.warning("Database error: %s", exc)
        return jsonify(error="The database is not initialised yet. Run "
                             "`python -m cd_screener demo` first.", detail=str(exc)), 503

    @app.errorhandler(Exception)
    def handle_unexpected(exc):
        if isinstance(exc, HTTPException):
            if request.path.startswith("/api/"):
                return jsonify(error=exc.description), exc.code
            return exc
        log.exception("Unhandled error on %s", request.path)
        return jsonify(error="Internal server error"), 500

    return app
