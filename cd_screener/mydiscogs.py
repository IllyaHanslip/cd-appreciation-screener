"""Your own Discogs wantlist and collection, synced daily.

    python -m cd_screener sync-me        # also part of the scheduled `daily` job

* The wantlist is grouped by album (Discogs "master"): variants of the same album are
  priced together with ONE request per album per day - never per variant. The price is
  Discogs' album-level cheapest copy (any pressing or format); the dashboard links to the
  album's CD-only marketplace listings for the exact CD offers.
* The collection is valued with Discogs' own collection valuation (one request), and each
  item is priced once a day: the Near Mint suggestion for that exact pressing and the
  cheapest copy listed for sale (two requests per item).
* What you paid for an item is typed in on the dashboard and compared with today's prices.
* Items added on the Discogs website appear at the next sync (and are priced that day);
  removed ones are hidden.
* "Today" is the calendar day on this PC, so a late-night run never counts as tomorrow.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Iterator, Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import config, db
from .discogs_client import (
    DiscogsAuthError,
    DiscogsClient,
    DiscogsError,
    extract_condition_price,
    extract_listing,
    extract_master_market,
)
from .seed import clean_artist_name

log = logging.getLogger(__name__)

_SYMBOLS = {"£": "GBP", "$": "USD", "€": "EUR", "¥": "JPY"}
ADDED_WINDOW_DAYS = 3  # a check this soon after an item joins the collection = "value when added"


def _today_start() -> str:
    """Midnight today on this PC's clock, as a UTC timestamp."""
    midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local_day(iso: str) -> date:
    return datetime.fromisoformat(iso).astimezone().date()


def _last_per_day(history: list[dict]) -> list[dict]:
    """The last check of each day (a forced re-sync can check twice in one day)."""
    days: dict[date, dict] = {}
    for h in history:
        days[_local_day(h["fetched_at"])] = h
    return list(days.values())


def _pct_change(now: float | None, before: float | None) -> float | None:
    return round((now / before - 1) * 100, 1) if now and before else None


def _previous_check(latest: Mapping | None, previous: Mapping | None) -> dict:
    """When the comparison check was: normally yesterday, longer after days the PC was off."""
    if not latest or not previous:
        return {"previous_checked_at": None, "days_since_previous": None}
    gap = _local_day(latest["fetched_at"]) - _local_day(previous["fetched_at"])
    return {"previous_checked_at": previous["fetched_at"], "days_since_previous": gap.days}


def _pages(client: DiscogsClient, path: str, key: str) -> Iterator[Mapping[str, Any]]:
    page, pages = 1, 1
    while page <= pages:
        payload = client.get_json(path, {"per_page": 100, "page": page})
        pages = int((payload.get("pagination") or {}).get("pages") or 1)
        yield from payload.get(key) or []
        page += 1


def _item_row(entry: Mapping[str, Any], list_name: str, now: str) -> dict | None:
    info = entry.get("basic_information") or {}
    release_id = info.get("id") or entry.get("id")
    if not isinstance(release_id, int):
        return None
    master_id = info.get("master_id") or None
    artists = " & ".join(clean_artist_name(a.get("name") or "") for a in info.get("artists") or [])
    label = (info.get("labels") or [{}])[0]
    return {
        "list": list_name,
        "release_id": release_id,
        "album_key": f"m{master_id}" if master_id else f"r{release_id}",
        "master_id": master_id,
        "artist": artists or "Unknown Artist",
        "title": info.get("title") or "Untitled",
        "year": info.get("year") or None,
        "format": ((info.get("formats") or [{}])[0]).get("name"),
        "label": label.get("name"),
        "catno": label.get("catno"),
        "date_added": entry.get("date_added"),
        "first_seen": now,
        "last_seen": now,
    }


def parse_money(text: Any) -> tuple[float | None, str | None]:
    """'£1,234.56' -> (1234.56, 'GBP')."""
    if not isinstance(text, str) or not text.strip():
        return None, None
    currency = next((code for sym, code in _SYMBOLS.items() if sym in text), None)
    number = re.sub(r"[^\d.]", "", text)
    try:
        return float(number), currency
    except ValueError:
        return None, currency


def sync_lists(conn: sqlite3.Connection, client: DiscogsClient, username: str) -> dict:
    """Mirror the wantlist and collection into user_items. Returns counts per list."""
    now = db.utc_now_iso()
    counts = {}
    sources = {
        "want": (f"/users/{username}/wants", "wants"),
        "collection": (f"/users/{username}/collection/folders/0/releases", "releases"),
    }
    for list_name, (path, key) in sources.items():
        rows = [r for r in (_item_row(e, list_name, now) for e in _pages(client, path, key)) if r]
        with conn:
            conn.executemany(
                "INSERT INTO user_items (list, release_id, album_key, master_id, artist, title, "
                "year, format, label, catno, date_added, active, first_seen, last_seen) "
                "VALUES (:list, :release_id, :album_key, :master_id, :artist, :title, :year, "
                ":format, :label, :catno, :date_added, 1, :first_seen, :last_seen) "
                "ON CONFLICT (list, release_id) DO UPDATE SET album_key = excluded.album_key, "
                "master_id = excluded.master_id, artist = excluded.artist, title = excluded.title, "
                "year = excluded.year, format = excluded.format, label = excluded.label, "
                "catno = excluded.catno, date_added = excluded.date_added, active = 1, "
                "last_seen = excluded.last_seen", rows)
            # Anything not in this sync was removed on Discogs.
            seen = {r["release_id"] for r in rows}
            gone = [(list_name, rid) for (rid,) in conn.execute(
                "SELECT release_id FROM user_items WHERE list = ? AND active = 1", (list_name,))
                if rid not in seen]
            conn.executemany("UPDATE user_items SET active = 0 WHERE list = ? AND release_id = ?",
                             gone)
        counts[list_name] = len(rows)
    return counts


def price_wanted_albums(conn: sqlite3.Connection, client: DiscogsClient, currency: str | None,
                        *, force: bool = False) -> int:
    """One request per wanted album per day (skips albums already priced today)."""
    done = set() if force else {r[0] for r in conn.execute(
        "SELECT DISTINCT album_key FROM user_album_prices WHERE fetched_at >= ?",
        (_today_start(),))}
    albums = [r for r in conn.execute(
        "SELECT album_key, MIN(master_id) AS master_id, MIN(release_id) AS release_id "
        "FROM user_items WHERE list = 'want' AND active = 1 GROUP BY album_key") if r[0] not in done]
    priced = 0
    for album in albums:
        try:
            if album["master_id"]:
                snap = extract_master_market(client.get_master(album["master_id"]))
                snap_currency = currency
            else:  # no album group: price the single release
                snap = extract_listing(client.get_marketplace_stats(album["release_id"]))
                snap_currency = snap.currency if snap else None
        except DiscogsAuthError:
            raise
        except DiscogsError as exc:
            log.warning("Wanted album %s: price lookup failed: %s", album["album_key"], exc)
            continue
        if snap is None:
            continue
        conn.execute(
            "INSERT INTO user_album_prices (album_key, fetched_at, lowest_price, currency, "
            "num_for_sale) VALUES (?, ?, ?, ?, ?)",
            (album["album_key"], db.utc_now_iso(), snap.lowest_price,
             snap_currency or currency, snap.num_for_sale))
        conn.commit()
        priced += 1
    return priced


def price_collection_items(conn: sqlite3.Connection, client: DiscogsClient,
                           *, force: bool = False) -> int:
    """Two requests per collection item per day: its NM suggestion and its cheapest listing.

    Skips items already priced today, so a newly added item is priced at the next sync.
    """
    done = set() if force else {r[0] for r in conn.execute(
        "SELECT DISTINCT release_id FROM user_item_prices WHERE fetched_at >= ?",
        (_today_start(),))}
    release_ids = [r[0] for r in conn.execute(
        "SELECT release_id FROM user_items WHERE list = 'collection' AND active = 1 "
        "ORDER BY release_id") if r[0] not in done]
    priced = 0
    for rid in release_ids:
        nm = listing = None
        try:
            nm = extract_condition_price(client.get_price_suggestions(rid),
                                         config.NM_CONDITION_PREFIX)
        except DiscogsAuthError:
            raise
        except DiscogsError as exc:
            log.warning("Collection item %s: price suggestion failed: %s", rid, exc)
        try:
            listing = extract_listing(client.get_marketplace_stats(rid))
        except DiscogsAuthError:
            raise
        except DiscogsError as exc:
            log.warning("Collection item %s: listing lookup failed: %s", rid, exc)
        if nm is None and listing is None:
            continue
        conn.execute(
            "INSERT INTO user_item_prices (release_id, fetched_at, nm_price, nm_currency, "
            "lowest_price, lowest_currency, num_for_sale) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (rid, db.utc_now_iso(), nm.value if nm else None, nm.currency if nm else None,
             listing.lowest_price if listing else None, listing.currency if listing else None,
             listing.num_for_sale if listing else None))
        conn.commit()
        priced += 1
    return priced


def record_collection_value(conn: sqlite3.Connection, client: DiscogsClient, username: str) -> dict:
    payload = client.get_json(f"/users/{username}/collection/value")
    values = {k: parse_money(payload.get(k)) for k in ("minimum", "median", "maximum")}
    currency = next((c for _, c in values.values() if c), None)
    (items,) = conn.execute(
        "SELECT COUNT(*) FROM user_items WHERE list = 'collection' AND active = 1").fetchone()
    row = {k: v[0] for k, v in values.items()}
    with conn:
        conn.execute(
            "INSERT INTO collection_value_history (fetched_at, minimum, median, maximum, currency, "
            "items) VALUES (?, ?, ?, ?, ?, ?)",
            (db.utc_now_iso(), row["minimum"], row["median"], row["maximum"], currency, items))
    return {**row, "currency": currency, "items": items}


def _album_links(album_key: str) -> dict:
    kind, number = album_key[0], album_key[1:]
    if kind == "m":
        return {"album_url": f"https://www.discogs.com/master/{number}",
                "cd_listings_url": f"https://www.discogs.com/sell/list?master_id={number}&format=CD"}
    return {"album_url": f"https://www.discogs.com/release/{number}",
            "cd_listings_url": f"https://www.discogs.com/sell/release/{number}"}


def wanted_albums(conn: sqlite3.Connection) -> list[dict]:
    """One row per wanted album: variants, latest cheapest copy, usual price, deal flag."""
    items = conn.execute(
        "SELECT album_key, artist, title, format FROM user_items "
        "WHERE list = 'want' AND active = 1 ORDER BY album_key").fetchall()
    albums: dict[str, dict] = {}
    for it in items:
        a = albums.setdefault(it["album_key"], {"album_key": it["album_key"], "variants": 0,
                                                "cd_variants": 0, "_names": {}})
        a["variants"] += 1
        a["cd_variants"] += it["format"] == "CD"
        name = (it["artist"], it["title"])
        a["_names"][name] = a["_names"].get(name, 0) + 1
    prices: dict[str, list] = {}
    for p in conn.execute("SELECT album_key, fetched_at, lowest_price, currency, num_for_sale "
                          "FROM user_album_prices ORDER BY id"):
        prices.setdefault(p["album_key"], []).append(dict(p))

    out = []
    for key, a in albums.items():
        artist, title = max(a.pop("_names").items(), key=lambda kv: kv[1])[0]  # commonest name
        history = prices.get(key, [])
        daily = _last_per_day(history)
        latest = daily[-1] if daily else None
        previous = daily[-2] if len(daily) > 1 else None
        earlier = [h["lowest_price"] for h in daily[:-1][-config.USUAL_LISTING_DAYS:]
                   if h["lowest_price"] is not None]
        usual = sorted(earlier)[len(earlier) // 2] if len(earlier) >= config.USUAL_LISTING_MIN_OBS else None
        first = next((h["lowest_price"] for h in history if h["lowest_price"] is not None), None)
        now = latest["lowest_price"] if latest else None
        below_usual = round((1 - now / usual) * 100, 1) if now and usual else None
        same_currency = previous is not None and previous["currency"] == latest["currency"]
        out.append({
            **a, "artist": artist, "title": title,
            "lowest_price": now,
            "currency": latest["currency"] if latest else None,
            "num_for_sale": latest["num_for_sale"] if latest else None,
            "checked_at": latest["fetched_at"] if latest else None,
            "usual_price": usual,
            "below_usual_pct": below_usual,
            # Today's cheapest copy vs the previous day's check (normally yesterday).
            "day_change_pct": _pct_change(now, previous["lowest_price"]) if same_currency else None,
            **_previous_check(latest, previous),
            "change_pct": _pct_change(now, first),
            # When this album's price was first checked: the start of "change since first check".
            "first_checked_at": history[0]["fetched_at"] if history else None,
            "deal": bool(below_usual is not None and below_usual >= config.DEAL_BELOW_USUAL_PCT),
            "days_tracked": len(daily),
            **_album_links(key),
        })
    return out


def wanted_album_detail(conn: sqlite3.Connection, album_key: str) -> dict | None:
    album = next((a for a in wanted_albums(conn) if a["album_key"] == album_key), None)
    if album is None:
        return None
    album["history"] = _last_per_day([dict(r) for r in conn.execute(
        "SELECT fetched_at, lowest_price, currency, num_for_sale FROM user_album_prices "
        "WHERE album_key = ? ORDER BY id", (album_key,))])
    album["variants_list"] = [dict(r) for r in conn.execute(
        "SELECT release_id, title, year, format, label, catno FROM user_items "
        "WHERE list = 'want' AND active = 1 AND album_key = ? ORDER BY year, catno", (album_key,))]
    return album


def _value_when_added(date_added: str | None, daily: list[dict]) -> dict | None:
    """The first price check within a few days of the item joining your collection.

    None for items added before tracking began (Discogs has no price history to look up).
    """
    if not date_added:
        return None
    try:
        added = datetime.fromisoformat(date_added)
    except ValueError:
        return None
    if added.tzinfo is None:
        added = added.replace(tzinfo=timezone.utc)
    for h in daily:
        checked = datetime.fromisoformat(h["fetched_at"])
        if checked >= added:
            return h if checked - added <= timedelta(days=ADDED_WINDOW_DAYS) else None
    return None


def _with_prices(item: dict, history: list[dict]) -> dict:
    """Add an item's latest prices, its day-on-day change and the comparison with what you paid."""
    daily = _last_per_day(history)
    latest = daily[-1] if daily else {}
    previous = daily[-2] if len(daily) > 1 else {}
    added = _value_when_added(item["date_added"], daily) or {}
    nm, nm_currency = latest.get("nm_price"), latest.get("nm_currency")
    lowest, lowest_currency = latest.get("lowest_price"), latest.get("lowest_currency")
    paid, paid_currency = item["price_paid"], item["paid_currency"]
    item.update({
        "nm_price": nm,
        "nm_currency": nm_currency,
        "lowest_price": lowest,
        "lowest_currency": lowest_currency,
        "num_for_sale": latest.get("num_for_sale"),
        "checked_at": latest.get("fetched_at"),
        # Day-on-day, on the NM estimate (the value) and on the cheapest copy for sale.
        "day_change_pct": (_pct_change(nm, previous.get("nm_price"))
                           if previous.get("nm_currency") == nm_currency else None),
        "listed_day_change_pct": (_pct_change(lowest, previous.get("lowest_price"))
                                  if previous.get("lowest_currency") == lowest_currency else None),
        **_previous_check(latest, previous),
        "days_tracked": len(daily),
        "first_checked_at": daily[0]["fetched_at"] if daily else None,
        "added_nm_price": added.get("nm_price"),
        "added_lowest_price": added.get("lowest_price"),
        "added_currency": added.get("nm_currency") or added.get("lowest_currency"),
        "added_checked_at": added.get("fetched_at"),
        # Like for like: the CD's own price (postage excluded), in the same currency.
        "vs_paid_pct": _pct_change(nm, paid) if paid_currency == nm_currency else None,
        "listed_vs_paid_pct": _pct_change(lowest, paid) if paid_currency == lowest_currency else None,
        "discogs_url": f"https://www.discogs.com/release/{item['release_id']}",
        "listings_url": f"https://www.discogs.com/sell/release/{item['release_id']}",
    })
    return item


_COLLECTION_SELECT = """
    SELECT u.release_id, u.artist, u.title, u.year, u.format, u.label, u.catno, u.date_added,
           p.price_paid, p.shipping_paid, p.currency AS paid_currency, p.purchased_on
    FROM user_items AS u LEFT JOIN user_purchases AS p USING (release_id)
    WHERE u.list = 'collection' AND u.active = 1"""
_ITEM_PRICES_SELECT = ("SELECT release_id, fetched_at, nm_price, nm_currency, lowest_price, "
                       "lowest_currency, num_for_sale FROM user_item_prices")


def collection_items(conn: sqlite3.Connection) -> list[dict]:
    """Your collection, newest first, each item with its latest prices and what you paid."""
    history: dict[int, list] = {}
    for p in conn.execute(f"{_ITEM_PRICES_SELECT} ORDER BY id"):
        history.setdefault(p["release_id"], []).append(dict(p))
    return [_with_prices(dict(r), history.get(r["release_id"], [])) for r in conn.execute(
        f"{_COLLECTION_SELECT} ORDER BY u.date_added DESC, u.release_id")]


def collection_item_detail(conn: sqlite3.Connection, release_id: int) -> dict | None:
    row = conn.execute(f"{_COLLECTION_SELECT} AND u.release_id = ?", (release_id,)).fetchone()
    if row is None:
        return None
    history = [dict(p) for p in conn.execute(
        f"{_ITEM_PRICES_SELECT} WHERE release_id = ? ORDER BY id", (release_id,))]
    item = _with_prices(dict(row), history)
    item["history"] = _last_per_day(history)
    return item


def set_purchase(conn: sqlite3.Connection, release_id: int, *, price_paid: float, currency: str,
                 shipping_paid: float | None = None, purchased_on: str | None = None) -> bool:
    """Record what you paid for a collection item. False when it isn't in your collection."""
    if conn.execute("SELECT 1 FROM user_items WHERE list = 'collection' AND active = 1 "
                    "AND release_id = ?", (release_id,)).fetchone() is None:
        return False
    with conn:
        conn.execute(
            "INSERT INTO user_purchases (release_id, price_paid, shipping_paid, currency, "
            "purchased_on, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (release_id) DO UPDATE SET price_paid = excluded.price_paid, "
            "shipping_paid = excluded.shipping_paid, currency = excluded.currency, "
            "purchased_on = excluded.purchased_on, updated_at = excluded.updated_at",
            (release_id, price_paid, shipping_paid, currency, purchased_on, db.utc_now_iso()))
    return True


def clear_purchase(conn: sqlite3.Connection, release_id: int) -> bool:
    with conn:
        return conn.execute("DELETE FROM user_purchases WHERE release_id = ?",
                            (release_id,)).rowcount > 0


def _paid_totals(items: list[dict], currency: str | None) -> dict:
    """Totals for the items you've entered a price for, in your Discogs currency."""
    paid = [i for i in items if i["price_paid"] is not None]
    mine = [i for i in paid if i["paid_currency"] == currency]
    compared = [i for i in mine if i["nm_price"] is not None and i["nm_currency"] == currency]
    then = round(sum(i["price_paid"] for i in compared), 2)
    now = round(sum(i["nm_price"] for i in compared), 2)
    return {
        "items": len(paid),
        "currency": currency,
        "total": round(sum(i["price_paid"] + (i["shipping_paid"] or 0) for i in mine), 2),
        "shipping": round(sum(i["shipping_paid"] or 0 for i in mine), 2),
        "other_currencies": len(paid) - len(mine),
        "compared": len(compared),
        "compared_paid": then,
        "compared_now": now,
        "change_pct": _pct_change(now, then),
    }


def collection_summary(conn: sqlite3.Connection) -> dict:
    items = collection_items(conn)
    values = _last_per_day([dict(r) for r in conn.execute(
        "SELECT fetched_at, minimum, median, maximum, currency, items "
        "FROM collection_value_history ORDER BY id")])
    latest = values[-1] if values else None
    currency = (latest and latest["currency"]) or next(
        (i["nm_currency"] for i in items if i["nm_currency"]), None)
    return {"items": items, "value_history": values, "latest_value": latest,
            "currency": currency, "paid": _paid_totals(items, currency)}


def synced_today(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM collection_value_history WHERE fetched_at >= ? LIMIT 1",
                        (_today_start(),)).fetchone() is not None


def sync_me(conn: sqlite3.Connection, client: DiscogsClient | None = None, *,
            force: bool = False) -> dict:
    """Sync the wantlist + collection and record today's prices and collection value."""
    db.init_schema(conn)
    if not force and synced_today(conn):
        log.info("Wantlist and collection already synced today - skipping (--force to resync)")
        return {}
    if client is None:
        if not config.DISCOGS_TOKEN:
            raise db.ScreenerError("Syncing your wantlist needs DISCOGS_TOKEN in .env.")
        client = DiscogsClient(config.DISCOGS_TOKEN)
    username = client.get_json("/oauth/identity")["username"]
    currency = client.get_json(f"/users/{username}").get("curr_abbr")
    counts = sync_lists(conn, client, username)
    (albums,) = conn.execute(
        "SELECT COUNT(DISTINCT album_key) FROM user_items WHERE list = 'want' AND active = 1"
    ).fetchone()
    log.info("Synced %s's wantlist (%d items, %d albums) and collection (%d items)",
             username, counts["want"], albums, counts["collection"])
    priced = price_wanted_albums(conn, client, currency)
    items_priced = price_collection_items(conn, client)
    value = record_collection_value(conn, client, username)
    log.info("Priced %d wanted albums and %d collection items; collection valued at %s (median)",
             priced, items_priced, value["median"])
    return {"username": username, "wants": counts["want"], "albums": albums, "priced": priced,
            "collection": counts["collection"], "collection_priced": items_priced,
            "collection_value": value}
