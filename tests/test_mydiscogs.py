"""Your wantlist + collection: syncing, album grouping and album-level pricing."""

import pytest

from cd_screener import mydiscogs


def want(release_id, master_id, title="Mr Beast", fmt="CD", added="2026-01-01T00:00:00-08:00"):
    return {"id": release_id, "date_added": added,
            "basic_information": {"id": release_id, "master_id": master_id, "title": title,
                                  "year": 2006, "artists": [{"name": "Mogwai (2)"}],
                                  "formats": [{"name": fmt}],
                                  "labels": [{"name": "PIAS", "catno": f"CAT{release_id}"}]}}


class FakeClient:
    """Paged wantlist/collection plus album and pressing prices; records every request."""

    def __init__(self, wants, collection=(), master_prices=None, suggestions=None):
        self.wants, self.collection = list(wants), list(collection)
        self.master_prices = master_prices or {}
        self.suggestions = suggestions or {}  # release_id -> NM value (None: no suggestion)
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

    def get_price_suggestions(self, release_id):
        self.calls.append(f"/marketplace/price_suggestions/{release_id}")
        value = self.suggestions.get(release_id, 15.0)
        return {} if value is None else {"Near Mint (NM or M-)": {"currency": "GBP", "value": value}}


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
    price_calls = sorted(c for c in client.calls if c.startswith(("/masters/", "/marketplace/")))
    assert price_calls == [  # one per wanted album, two for the collection item
        "/marketplace/price_suggestions/9", "/marketplace/stats/5", "/marketplace/stats/9",
        "/masters/18032", "/masters/77"]

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


def add_check(conn, table, key, fetched_at, **values):
    key_column = "album_key" if table == "user_album_prices" else "release_id"
    columns = ", ".join([key_column, "fetched_at", *values])
    marks = ", ".join("?" for _ in range(len(values) + 2))
    conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})", (key, fetched_at, *values.values()))
    conn.commit()


def test_wantlist_change_is_day_on_day(conn):
    mydiscogs.sync_lists(conn, FakeClient([want(1, 77)]), "example_collector")
    for fetched_at, price in [("2026-10-10T12:00:00Z", 10.0), ("2026-10-11T10:00:00Z", 9.0),
                              ("2026-10-11T12:00:00Z", 8.0),  # a forced re-check: last one counts
                              ("2026-10-13T12:00:00Z", 6.0)]:  # the PC was off on the 12th
        add_check(conn, "user_album_prices", "m77", fetched_at, lowest_price=price,
                  currency="GBP", num_for_sale=3)
    album = mydiscogs.wanted_albums(conn)[0]
    assert album["day_change_pct"] == pytest.approx(-25.0)  # 6 vs 8 on the previous check day
    assert (album["previous_checked_at"], album["days_since_previous"]) == ("2026-10-11T12:00:00Z", 2)
    assert album["change_pct"] == pytest.approx(-40.0) and album["days_tracked"] == 3
    assert len(mydiscogs.wanted_album_detail(conn, "m77")["history"]) == 3  # one point per day


def test_collection_items_are_priced_once_a_day(conn):
    collection = [want(9, 18032), want(10, None, "Demo")]
    client = FakeClient([], collection, suggestions={9: 12.0, 10: None})
    assert mydiscogs.sync_me(conn, client)["collection_priced"] == 2
    items = {i["release_id"]: i for i in mydiscogs.collection_items(conn)}
    assert (items[9]["nm_price"], items[9]["lowest_price"], items[9]["num_for_sale"]) == (12.0, 9.99, 1)
    assert items[10]["nm_price"] is None and items[10]["lowest_price"] == 9.99  # no NM suggestion
    assert items[9]["day_change_pct"] is None and items[9]["previous_checked_at"] is None

    again = FakeClient([], collection)  # same day: nothing is priced twice
    assert mydiscogs.sync_me(conn, again, force=True)["collection_priced"] == 0
    assert not any("price_suggestions" in c for c in again.calls)

    bought = FakeClient([], [*collection, want(11, None, "New CD")])  # added on Discogs today
    assert mydiscogs.sync_me(conn, bought, force=True)["collection_priced"] == 1
    sold = FakeClient([], [want(9, 18032), want(11, None, "New CD")])  # 10 removed
    mydiscogs.sync_lists(conn, sold, "example_collector")
    assert sorted(i["release_id"] for i in mydiscogs.collection_items(conn)) == [9, 11]


def test_collection_compares_with_yesterday_what_you_paid_and_when_added(conn):
    collection = [want(9, 18032, added="2026-10-10T08:00:00-07:00"),  # bought during tracking
                  want(10, None, "Demo")]  # added long before tracking began
    mydiscogs.sync_lists(conn, FakeClient([], collection), "example_collector")
    for fetched_at, nm, low in [("2026-10-10T18:00:00Z", 10.0, 4.0), ("2026-10-11T12:00:00Z", 12.0, 5.0)]:
        add_check(conn, "user_item_prices", 9, fetched_at, nm_price=nm, nm_currency="GBP",
                  lowest_price=low, lowest_currency="GBP", num_for_sale=2)
    add_check(conn, "user_item_prices", 10, "2026-10-11T12:00:00Z", nm_price=20.0, nm_currency="GBP")
    assert mydiscogs.set_purchase(conn, 9, price_paid=8.0, shipping_paid=2.5, currency="GBP",
                                  purchased_on="2026-10-09")
    assert not mydiscogs.set_purchase(conn, 999, price_paid=1.0, currency="GBP")  # not yours

    items = {i["release_id"]: i for i in mydiscogs.collection_items(conn)}
    cd = items[9]
    assert (cd["day_change_pct"], cd["listed_day_change_pct"], cd["days_since_previous"]) == (20.0, 25.0, 1)
    assert (cd["added_nm_price"], cd["added_checked_at"]) == (10.0, "2026-10-10T18:00:00Z")
    assert (cd["vs_paid_pct"], cd["listed_vs_paid_pct"]) == (50.0, -37.5)  # 12 and 5 vs 8 paid
    assert items[10]["added_nm_price"] is None
    paid = mydiscogs.collection_summary(conn)["paid"]
    assert (paid["items"], paid["total"], paid["shipping"], paid["change_pct"]) == (1, 10.5, 2.5, 50.0)

    mydiscogs.set_purchase(conn, 9, price_paid=8.0, currency="EUR")  # no exchange rates yet
    assert mydiscogs.collection_item_detail(conn, 9)["vs_paid_pct"] is None
    assert mydiscogs.collection_summary(conn)["paid"]["other_currencies"] == 1
    assert mydiscogs.clear_purchase(conn, 9)
    assert mydiscogs.collection_item_detail(conn, 9)["price_paid"] is None
