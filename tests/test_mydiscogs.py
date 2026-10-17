"""Your wantlist + collection: syncing, album grouping and album-level pricing."""

import pytest

from cd_screener import mydiscogs


def want(release_id, master_id, title="Mr Beast", fmt="CD"):
    return {"id": release_id, "date_added": "2026-01-01T00:00:00-08:00",
            "basic_information": {"id": release_id, "master_id": master_id, "title": title,
                                  "year": 2006, "artists": [{"name": "Mogwai (2)"}],
                                  "formats": [{"name": fmt}],
                                  "labels": [{"name": "PIAS", "catno": f"CAT{release_id}"}]}}


class FakeClient:
    """Paged wantlist/collection plus album prices; records every request."""

    def __init__(self, wants, collection=(), master_prices=None):
        self.wants, self.collection = list(wants), list(collection)
        self.master_prices = master_prices or {}
        self.calls = []

    def get_json(self, path, params=None):
        self.calls.append(path)
        if path == "/oauth/identity":
            return {"username": "example_collector"}
        if path == "/users/example_collector":
            return {"curr_abbr": "GBP"}
        if path.endswith("/collection/value"):
            return {"minimum": "£18.55", "median": "£1,055.06", "maximum": "£169.94"}
        items, key = (self.wants, "wants") if path.endswith("/wants") else (self.collection, "releases")
        page, per_page = params["page"], params["per_page"]
        pages = max(1, -(-len(items) // per_page))
        return {"pagination": {"pages": pages}, key: items[(page - 1) * per_page: page * per_page]}

    def get_master(self, master_id):
        self.calls.append(f"/masters/{master_id}")
        return {"lowest_price": self.master_prices.get(master_id, 5.0), "num_for_sale": 12}

    def get_marketplace_stats(self, release_id):
        self.calls.append(f"/marketplace/stats/{release_id}")
        return {"num_for_sale": 1, "lowest_price": {"value": 9.99, "currency": "GBP"}}


def test_money_parsing():
    assert mydiscogs.parse_money("£1,055.06") == (1055.06, "GBP")
    assert mydiscogs.parse_money("$12.50") == (12.5, "USD")
    assert mydiscogs.parse_money(None) == (None, None)


def test_variants_are_priced_once_per_album(conn):
    # 150 variants of one album (more than one page) + 2 of another + 1 with no album group.
    wants = [want(1000 + i, 18032) for i in range(150)] + [want(1, 77, "Young Team"), want(2, 77, "Young Team")]
    wants.append(want(5, None, "Demo"))
    client = FakeClient(wants, collection=[want(9, 18032)], master_prices={18032: 0.79})
    result = mydiscogs.sync_me(conn, client)

    assert (result["wants"], result["albums"], result["priced"]) == (153, 3, 3)
    album_calls = [c for c in client.calls if c.startswith(("/masters/", "/marketplace/"))]
    assert sorted(album_calls) == ["/marketplace/stats/5", "/masters/18032", "/masters/77"]

    albums = {a["album_key"]: a for a in mydiscogs.wanted_albums(conn)}
    beast = albums["m18032"]
    assert (beast["variants"], beast["artist"], beast["lowest_price"], beast["currency"]) == (150, "Mogwai", 0.79, "GBP")
    assert beast["cd_listings_url"] == "https://www.discogs.com/sell/list?master_id=18032&format=CD"
    assert albums["r5"]["album_url"] == "https://www.discogs.com/release/5"
    assert result["collection_value"]["median"] == 1055.06


def test_sync_is_skipped_when_done_today_and_tracks_removals(conn):
    client = FakeClient([want(1, 77), want(2, 77)])
    mydiscogs.sync_me(conn, client)
    assert mydiscogs.sync_me(conn, FakeClient([])) == {}  # already synced today

    client = FakeClient([want(1, 77)])  # variant 2 removed on Discogs
    mydiscogs.sync_lists(conn, client, "example_collector")
    assert mydiscogs.wanted_albums(conn)[0]["variants"] == 1


def test_album_deal_needs_a_usual_price(conn):
    mydiscogs.sync_lists(conn, FakeClient([want(1, 77)]), "example_collector")
    for day, price in enumerate([10.0, 10.0, 10.0, 7.0]):
        conn.execute("INSERT INTO user_album_prices (album_key, fetched_at, lowest_price, currency, "
                     "num_for_sale) VALUES ('m77', ?, ?, 'GBP', 3)", (f"2026-10-1{day}T12:00:00Z", price))
    conn.commit()
    album = mydiscogs.wanted_albums(conn)[0]
    assert (album["usual_price"], album["below_usual_pct"], album["deal"]) == (10.0, 30.0, True)
    assert album["change_pct"] == pytest.approx(-30.0)
    assert album["first_checked_at"] == "2026-10-10T12:00:00Z"  # start of "change since first check"
