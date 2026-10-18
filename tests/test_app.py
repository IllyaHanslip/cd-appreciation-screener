"""Stage 4: Flask API behaviour, filters and error handling."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from cd_screener import analytics, db
from cd_screener.__main__ import main
from cd_screener.app import create_app

START = (datetime.now(timezone.utc) - timedelta(days=60)).replace(hour=6, minute=0, second=0, microsecond=0)


@pytest.fixture
def db_path(tmp_path, release_row):
    path = tmp_path / "app.db"
    conn = db.connect(path)
    db.init_schema(conn)
    db.upsert_releases(conn, [
        release_row(1, wants=300, haves=100, genre="Rock", title="Golden Rivers", source="discogs",
                    country="Brazil", label="[PIAS] Recordings", catno="MBB20036", master_id=18032),
        release_row(2, wants=500, haves=100, genre="Jazz", title="Blue_Train 100%"),
        release_row(3, wants=50, haves=100, title="Never Priced"),
    ])
    for day in range(61):
        ts = (START + timedelta(days=day)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for release_id, price in ((1, 20 + day * 0.2), (2, 40 - day * 0.1)):  # 1 rises, 2 falls
            db.insert_price(conn, release_id=release_id, fetched_at=ts, price=round(price, 2),
                            currency="USD", condition="Near Mint (NM or M-)", source="simulated")
    # Release 1 ends at an NM estimate of 32.00; listings and the album are far cheaper.
    db.insert_listing(conn, release_id=1, lowest_price=15.0, currency="USD", num_for_sale=2)
    db.insert_listing(conn, release_id=2, lowest_price=None, currency=None, num_for_sale=0)
    db.insert_master_snapshot(conn, master_id=18032, lowest_price=0.79, num_for_sale=293)
    conn.commit()
    analytics.run_analytics(conn, show=0)
    conn.close()
    return path


@pytest.fixture
def client(db_path):
    app = create_app(db_path)
    app.testing = True
    return app.test_client()


def ids(resp):
    return [item["release_id"] for item in resp.get_json()["items"]]


def test_leaderboard_default(client):
    body = client.get("/api/leaderboard").get_json()
    assert body["total"] == body["matched"] == 2
    first = body["items"][0]
    assert first["rank"] == 1
    for field in ("title", "artist", "year", "want_have_ratio", "current_price", "growth_30d_pct",
                  "scarcity_index", "sparkline"):
        assert field in first
    assert isinstance(first["sparkline"], list)


def test_pressing_and_market_checks(client):
    items = {i["release_id"]: i for i in client.get("/api/leaderboard").get_json()["items"]}
    brazil = items[1]
    assert (brazil["country"], brazil["catno"], brazil["label"]) == ("Brazil", "MBB20036", "[PIAS] Recordings")
    assert brazil["discogs_url"] == "https://www.discogs.com/release/1"
    assert (brazil["listed_price"], brazil["listed_count"]) == (15.0, 2)
    assert brazil["estimate_vs_listing"] == pytest.approx(32.0 / 15.0, abs=0.01)
    assert brazil["estimate_high"] and brazil["rare_pressing"]  # 32 vs 0.79 for the album
    nothing_listed = items[2]
    assert nothing_listed["listed_count"] == 0 and not nothing_listed["estimate_high"]
    assert nothing_listed["pressing_premium"] is None  # no album group

    detail = client.get("/api/releases/1").get_json()
    assert detail["album"]["lowest_price"] == 0.79 and detail["checks"]["rare_pressing"]
    assert detail["release"]["album_url"] == "https://www.discogs.com/master/18032"
    assert ids(client.get("/api/leaderboard?sort=listed&order=desc")) == [1, 2]


def test_filters(client):
    assert ids(client.get("/api/leaderboard?genre=Jazz")) == [2]
    assert ids(client.get("/api/leaderboard?min_growth=0")) == [1]
    assert ids(client.get("/api/leaderboard?max_growth=0")) == [2]
    assert ids(client.get("/api/leaderboard?q=rivers")) == [1]  # case-insensitive
    assert ids(client.get("/api/leaderboard?q=_")) == [2]  # LIKE wildcards are escaped
    assert ids(client.get("/api/leaderboard?q=%25")) == [2]  # a literal "%"
    assert client.get("/api/leaderboard?genre=Polka").get_json()["matched"] == 0


def test_sorting_and_validation(client):
    assert ids(client.get("/api/leaderboard?sort=price&order=desc")) == [2, 1]
    assert ids(client.get("/api/leaderboard?sort=growth&order=desc")) == [1, 2]
    assert ids(client.get("/api/leaderboard?sort=velocity&order=desc")) == [1, 2]  # 90-day trend
    assert ids(client.get("/api/leaderboard?sort=velocity&order=asc")) == [2, 1]
    assert client.get("/api/leaderboard?sort=price;DROP TABLE releases").status_code == 400
    assert client.get("/api/leaderboard?order=sideways").status_code == 400
    bad = client.get("/api/leaderboard?min_growth=lots")
    assert bad.status_code == 400 and "min_growth" in bad.get_json()["error"]


def test_market_index_follows_filters(client):
    everything = client.get("/api/market-index").get_json()
    assert everything["cds"] == 2 and len(everything["points"]) == 61
    assert everything["points"][0]["index"] == 100.0
    jazz = client.get("/api/market-index?genre=Jazz").get_json()
    assert jazz["cds"] == 1 and jazz["points"][-1]["index"] < 100  # release 2 falls
    assert client.get("/api/market-index?min_growth=x").status_code == 400


def test_deal_filter_and_report(client, db_path):
    # Release 1's £15 listing is >30% under its £32 NM estimate -> a deal.
    deals = client.get("/api/leaderboard?flag=deal").get_json()
    assert ids(client.get("/api/leaderboard?flag=deal")) == [1]
    assert "below the NM estimate" in deals["items"][0]["deal_reason"]
    assert client.get("/api/leaderboard?flag=reissue").get_json()["matched"] == 0
    assert client.get("/api/leaderboard?flag=bogus").status_code == 400
    assert client.get("/api/summary").get_json()["deals"] == 1

    detail = client.get("/api/releases/1").get_json()
    assert len(detail["listing_history"]) == 1

    # No report until one is published.
    assert client.get("/api/reports/latest").status_code == 404
    assert client.get("/api/report").status_code == 404


def test_weekly_report_is_published_and_frozen(client, db_path, tmp_path):
    from cd_screener import report

    conn = db.connect(db_path)
    now = datetime.now(timezone.utc)
    assert report.publish_if_due(conn, now=now, out_dir=tmp_path) is not None  # first one
    assert report.publish_if_due(conn, now=now + timedelta(days=6), out_dir=tmp_path) is None
    conn.close()
    assert list(tmp_path.glob("report-*.md"))

    latest = client.get("/api/reports/latest").get_json()
    assert latest["deals_total"] == 1 and latest["deals"][0]["title"] == "Golden Rivers"
    assert [i["release_id"] for i in latest["rising"]] == [1]  # release 1 rose over the week
    assert latest["period_start"] < latest["period_end"]

    md = client.get("/api/report")
    assert md.status_code == 200 and md.mimetype == "text/markdown"
    text = md.get_data(as_text=True)
    assert text.startswith("# CD Market Report") and "## Deals" in text and "Golden Rivers" in text

    conn = db.connect(db_path)  # a week after the last report ended: a new one replaces it
    assert report.publish_if_due(conn, now=now + timedelta(days=7, minutes=1), out_dir=tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 2
    conn.close()


def test_release_detail_and_missing_entries(client):
    detail = client.get("/api/releases/1").get_json()
    assert detail["release"]["title"] == "Golden Rivers"
    assert len(detail["history"]) == 61 and detail["metrics"]["rank"] in (1, 2)

    unpriced = client.get("/api/releases/3").get_json()
    assert unpriced["metrics"] is None and unpriced["history"] == []

    missing = client.get("/api/releases/999")
    assert missing.status_code == 404 and "not in the database" in missing.get_json()["error"]
    assert client.get("/api/nope").status_code == 404


def test_summary_and_genres(client):
    summary = client.get("/api/summary").get_json()
    assert (summary["releases"], summary["cds"], summary["flagged"], summary["priced"]) == (3, 3, 2, 2)
    assert summary["mode"] == "simulated" and summary["rising_30d"] == 1
    assert {g["genre"] for g in client.get("/api/genres").get_json()} == {"Rock", "Jazz"}


def test_dashboard_is_served(client):
    page = client.get("/")
    assert page.status_code == 200 and b"CD Appreciation Leaderboard" in page.data
    assert b"not affiliated with, sponsored or endorsed by Discogs" in page.data  # API terms notice
    assert b"trademark of Zink Media, LLC" in page.data
    assert client.get("/static/app.js").status_code == 200


def test_missing_database_returns_503_without_creating_it(tmp_path):
    path = tmp_path / "missing.db"
    resp = create_app(path).test_client().get("/api/leaderboard")
    assert resp.status_code == 503 and "demo" in resp.get_json()["error"]
    assert not path.exists()


def test_old_or_blank_database_is_upgraded_on_startup(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)  # a first-version releases table without pressing columns
    old.execute("CREATE TABLE releases (release_id INTEGER PRIMARY KEY, title TEXT NOT NULL, "
                "artist TEXT NOT NULL, year INTEGER, genre TEXT, style TEXT, format TEXT NOT NULL, "
                "country TEXT, wants INTEGER NOT NULL DEFAULT 0, haves INTEGER NOT NULL DEFAULT 0, "
                "want_have_ratio REAL GENERATED ALWAYS AS (CAST(wants AS REAL) / MAX(haves, 1)) STORED, "
                "source TEXT NOT NULL DEFAULT 'simulated', updated_at TEXT NOT NULL)")
    old.close()
    client = create_app(path).test_client()
    assert client.get("/api/summary").status_code == 200
    assert client.get("/api/leaderboard").get_json()["total"] == 0
    columns = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(releases)")}
    assert {"label", "catno", "master_id"} <= columns


def test_cli_reports_missing_steps_without_a_traceback(tmp_path, capsys):
    path = str(tmp_path / "cli.db")
    assert main(["--db", path, "analyze"]) == 1  # nothing seeded yet
    assert main(["--db", path, "seed", "--count", "200"]) == 0
    assert main(["--db", path, "analyze"]) == 1  # seeded but no prices yet
    assert "Funnel" in capsys.readouterr().out


def add_collection_item(path, release_id=42):
    conn = db.connect(path)
    conn.execute(
        "INSERT INTO user_items (list, release_id, album_key, artist, title, year, format, label, "
        "catno, date_added, first_seen, last_seen) VALUES ('collection', ?, ?, 'Mogwai', "
        "'Mr Beast', 2006, 'CD', 'PIAS', 'PIASR60CD', '2026-06-03T06:18:15-07:00', "
        "'2026-10-16T00:00:00Z', '2026-10-16T00:00:00Z')", (release_id, f"r{release_id}"))
    for fetched_at, nm, low in [("2026-10-16T11:00:00Z", 10.0, 3.0), ("2026-10-17T11:00:00Z", 11.0, 2.5)]:
        conn.execute("INSERT INTO user_item_prices (release_id, fetched_at, nm_price, nm_currency, "
                     "lowest_price, lowest_currency, num_for_sale) VALUES (?, ?, ?, 'GBP', ?, 'GBP', 4)",
                     (release_id, fetched_at, nm, low))
    conn.commit()
    conn.close()


def test_collection_prices_and_purchase_editing(client, db_path):
    add_collection_item(db_path)
    body = client.get("/api/me/collection").get_json()
    item = body["items"][0]
    assert (item["nm_price"], item["lowest_price"], item["week_change_pct"]) == (11.0, 2.5, 10.0)
    assert item["price_paid"] is None and body["paid"]["items"] == 0
    assert len(client.get("/api/me/collection/42").get_json()["history"]) == 2
    assert client.get("/api/me/collection/999").status_code == 404

    url = "/api/me/collection/42/purchase"
    saved = client.put(url, json={"price_paid": "8.80", "currency": "gbp", "purchased_on": "2026-06-01"})
    assert saved.status_code == 200
    data = saved.get_json()
    assert (data["price_paid"], data["paid_currency"], data["purchased_on"]) == (8.8, "GBP", "2026-06-01")
    assert data["vs_paid_pct"] == 25.0  # 11 now vs 8.80
    paid = client.get("/api/me/collection").get_json()["paid"]
    assert (paid["items"], paid["total"], paid["change_pct"]) == (1, 8.8, 25.0)

    for bad in ({"currency": "GBP"}, {"price_paid": -1, "currency": "GBP"},
                {"price_paid": "lots", "currency": "GBP"}, {"price_paid": True, "currency": "GBP"},
                {"price_paid": 5, "currency": "pounds"},
                {"price_paid": 5, "currency": "GBP", "purchased_on": "yesterday"},
                {"price_paid": 5, "currency": "GBP", "purchased_on": "2999-01-01"}):
        assert client.put(url, json=bad).status_code == 400, bad
    assert client.put(url, data="price_paid=5").status_code == 400  # not JSON
    missing = client.put("/api/me/collection/999/purchase", json={"price_paid": 5, "currency": "GBP"})
    assert missing.status_code == 404

    # Other websites can't change your data through the local server.
    evil = {"Origin": "https://evil.example"}
    assert client.put(url, json={"price_paid": 1, "currency": "GBP"}, headers=evil).status_code == 403
    assert client.delete(url, headers=evil).status_code == 403
    rebound = client.put(url, json={"price_paid": 1, "currency": "GBP"}, base_url="http://evil.example")
    assert rebound.status_code == 403
    assert client.get("/api/me/collection/42").get_json()["price_paid"] == 8.8

    cleared = client.delete(url, headers={"Origin": "http://127.0.0.1:5000"})
    assert cleared.status_code == 200 and cleared.get_json()["price_paid"] is None
