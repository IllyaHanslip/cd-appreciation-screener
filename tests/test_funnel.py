"""Stage 1: schema, funnel query and the simulated-dump parser."""

import xml.etree.ElementTree as ET
from collections import Counter

import pytest

from cd_screener import db, ingest, seed


def test_ratio_is_computed_by_sqlite_and_funnel_filters(conn, release_row):
    db.upsert_releases(conn, [
        release_row(1, wants=300, haves=100),               # CD, ratio 3.0  -> flagged
        release_row(2, wants=150, haves=100),               # CD, ratio 1.5  -> too common
        release_row(3, wants=900, haves=100, fmt="Vinyl"),  # ratio 9 but not a CD
        release_row(4, wants=200, haves=100),               # exactly 2.0 is not > 2.0
        release_row(5, wants=50, haves=0),                  # haves floored to 1 -> ratio 50
        release_row(6, wants=500, haves=50),                # ratio 10
    ])
    flagged = db.get_flagged_releases(conn, min_ratio=2.0, fmt="CD", limit=None)
    assert [r["release_id"] for r in flagged] == [5, 6, 1]  # scarcest first
    assert flagged[2]["want_have_ratio"] == pytest.approx(3.0)


def test_funnel_limit_and_min_wants(conn, release_row):
    db.upsert_releases(conn, [release_row(i, wants=i * 30, haves=10) for i in range(1, 11)])
    assert len(db.get_flagged_releases(conn, limit=3)) == 3
    noisy_filtered = db.get_flagged_releases(conn, limit=None, min_wants=200)
    assert all(r["wants"] >= 200 for r in noisy_filtered)


def test_upsert_updates_and_ratio_follows(conn, release_row):
    db.upsert_releases(conn, [release_row(1, wants=100, haves=100)])
    db.upsert_releases(conn, [release_row(1, wants=400, haves=100, title="Renamed")])
    row = conn.execute("SELECT title, want_have_ratio FROM releases").fetchone()
    assert (row["title"], row["want_have_ratio"]) == ("Renamed", 4.0)


def test_get_releases_by_ids_reports_missing(conn, release_row):
    db.upsert_releases(conn, [release_row(1, wants=1, haves=1), release_row(2, wants=1, haves=1)])
    found, missing = db.get_releases_by_ids(conn, [2, 99, 1])
    assert [r["release_id"] for r in found] == [2, 1]
    assert missing == [99]


def test_ensure_releases_loaded_explains_what_to_run(tmp_path):
    conn = db.connect(tmp_path / "empty.db")
    with pytest.raises(db.DatabaseNotReady, match="seed"):
        db.ensure_releases_loaded(conn)  # no tables yet
    db.init_schema(conn)
    with pytest.raises(db.DatabaseNotReady, match="empty"):
        db.ensure_releases_loaded(conn)  # tables but no rows


def test_simulated_dump_round_trip_is_deterministic(tmp_path):
    stats = Counter()
    path = seed.generate_simulated_dump(tmp_path / "a.xml", count=300, seed=7)
    rows = list(seed.iter_dump_releases(path, stats))
    assert stats["parsed"] + stats["skipped"] == 300
    assert stats["parsed"] == len(rows) > 250
    assert {"CD", "Vinyl"} <= {r["format"] for r in rows}
    assert all(r["source"] == "simulated" and r["haves"] >= 1 for r in rows)
    assert not any(r["artist"].endswith(")") for r in rows)  # "(2)" suffixes stripped

    again = seed.generate_simulated_dump(tmp_path / "b.xml", count=300, seed=7)
    assert list(seed.iter_dump_releases(again)) == rows


def test_parser_handles_real_dump_shapes():
    xml = """
    <release id="42" status="Accepted">
      <artists>
        <artist><id>1</id><name>Nirvana (2)</name></artist>
        <artist><id>2</id><name>Guest</name></artist>
      </artists>
      <title>Album</title>
      <formats><format name="CD" qty="1" text=""/></formats>
      <released>1997-05-00</released>
    </release>"""
    row = seed._parse_release(ET.fromstring(xml))
    assert row["artist"] == "Nirvana & Guest"
    assert row["year"] == 1997
    assert (row["wants"], row["haves"]) == (0, 0)  # real dumps carry no community stats

    draft = xml.replace('status="Accepted"', 'status="Draft"')
    assert seed._parse_release(ET.fromstring(draft)) is None
    no_format = xml.replace('<formats><format name="CD" qty="1" text=""/></formats>', "")
    assert seed._parse_release(ET.fromstring(no_format)) is None


def test_seed_refuses_to_wipe_live_data(conn, release_row, tmp_path):
    db.upsert_releases(conn, [release_row(1, wants=1, haves=1, source="discogs")])
    with pytest.raises(db.ScreenerError, match="--force"):
        seed.run_seed(conn, count=10, dump_path=tmp_path / "d.xml")
    assert conn.execute("SELECT COUNT(*) FROM releases").fetchone()[0] == 1


def test_search_result_mapping():
    item = {
        "id": 123, "type": "release", "title": "Nirvana (2) - Nevermind",
        "format": ["CD", "Album"], "year": "1991", "genre": ["Rock"], "style": ["Grunge"],
        "country": "US", "community": {"want": 400, "have": 100},
    }
    assert ingest.search_result_to_row(item) == {
        "release_id": 123, "title": "Nevermind", "artist": "Nirvana", "year": 1991,
        "genre": "Rock", "style": "Grunge", "format": "CD", "country": "US",
        "label": None, "catno": None, "master_id": None,
        "wants": 400, "haves": 100, "source": "discogs",
    }
    pressing = ingest.search_result_to_row(
        {**item, "label": ["[PIAS] Recordings", "Selecta"], "catno": "MBB20036", "master_id": 18032})
    assert (pressing["label"], pressing["catno"], pressing["master_id"]) == ("[PIAS] Recordings", "MBB20036", 18032)
    assert ingest.search_result_to_row({**item, "type": "master"}) is None
    assert ingest.search_result_to_row({**item, "title": "Smashing Pumpkins* - Siamese Dream"})["artist"] == "Smashing Pumpkins"
    assert ingest.search_result_to_row({**item, "format": ["CDr"]})["format"] == "CDr"


def test_ingest_paginates_and_replaces_simulated_rows(conn, release_row):
    db.upsert_releases(conn, [release_row(90_000_001, wants=5, haves=1)])
    pages_requested = []

    class FakeClient:
        def search_releases(self, *, page, per_page, **filters):
            pages_requested.append(page)
            assert filters["format"] == "CD" and filters["genre"] == "Jazz"
            result = {"id": page, "type": "release", "title": f"A - T{page}",
                      "format": ["CD"], "community": {"want": 30, "have": 10}}
            return {"pagination": {"pages": 2}, "results": [result]}

    written = ingest.run_ingest(conn, pages=5, filters={"genre": "Jazz"}, client=FakeClient())
    assert written == 2 and pages_requested == [1, 2]  # stops at the last page
    assert db.count_releases_by_source(conn) == {"discogs": 2}
