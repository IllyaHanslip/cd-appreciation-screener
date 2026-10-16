"""Your own Discogs wantlist and collection, synced daily.

    python -m cd_screener sync-me        # also part of the scheduled `daily` job

* The wantlist is grouped by album (Discogs "master"): variants of the same album are
  priced together with ONE request per album per day - never per variant. The price is
  Discogs' album-level cheapest copy (any pressing or format); the dashboard links to the
  album's CD-only marketplace listings for the exact CD offers.
* The collection is valued with Discogs' own collection valuation (one request).
* Items added on the Discogs website appear at the next sync; removed ones are hidden.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Iterator, Mapping
from datetime import datetime, timezone
from typing import Any

from . import config, db
from .discogs_client import (
    DiscogsAuthError,
    DiscogsClient,
    DiscogsError,
    extract_listing,
    extract_master_market,
)
from .seed import clean_artist_name

log = logging.getLogger(__name__)

_SYMBOLS = {"£": "GBP", "$": "USD", "€": "EUR", "¥": "JPY"}


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
    today = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    done = set() if force else {r[0] for r in conn.execute(
        "SELECT DISTINCT album_key FROM user_album_prices WHERE fetched_at >= ?", (today,))}
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
        latest = history[-1] if history else None
        earlier = [h["lowest_price"] for h in history[:-1][-config.USUAL_LISTING_DAYS:]
                   if h["lowest_price"] is not None]
        usual = sorted(earlier)[len(earlier) // 2] if len(earlier) >= config.USUAL_LISTING_MIN_OBS else None
        first = next((h["lowest_price"] for h in history if h["lowest_price"] is not None), None)
        now = latest["lowest_price"] if latest else None
        below_usual = round((1 - now / usual) * 100, 1) if now and usual else None
        out.append({
            **a, "artist": artist, "title": title,
            "lowest_price": now,
            "currency": latest["currency"] if latest else None,
            "num_for_sale": latest["num_for_sale"] if latest else None,
            "checked_at": latest["fetched_at"] if latest else None,
            "usual_price": usual,
            "below_usual_pct": below_usual,
            "change_pct": round((now / first - 1) * 100, 1) if now and first else None,
            "deal": bool(below_usual is not None and below_usual >= config.DEAL_BELOW_USUAL_PCT),
            "days_tracked": len(history),
            **_album_links(key),
        })
    return out


def wanted_album_detail(conn: sqlite3.Connection, album_key: str) -> dict | None:
    album = next((a for a in wanted_albums(conn) if a["album_key"] == album_key), None)
    if album is None:
        return None
    album["history"] = [dict(r) for r in conn.execute(
        "SELECT fetched_at, lowest_price, currency, num_for_sale FROM user_album_prices "
        "WHERE album_key = ? ORDER BY id", (album_key,))]
    album["variants_list"] = [dict(r) for r in conn.execute(
        "SELECT release_id, title, year, format, label, catno FROM user_items "
        "WHERE list = 'want' AND active = 1 AND album_key = ? ORDER BY year, catno", (album_key,))]
    return album


def collection_summary(conn: sqlite3.Connection) -> dict:
    items = [dict(r) for r in conn.execute(
        "SELECT release_id, artist, title, year, format, label, catno, date_added "
        "FROM user_items WHERE list = 'collection' AND active = 1 ORDER BY date_added DESC")]
    values = [dict(r) for r in conn.execute(
        "SELECT fetched_at, minimum, median, maximum, currency, items "
        "FROM collection_value_history ORDER BY id")]
    return {"items": items, "value_history": values, "latest_value": values[-1] if values else None}


def synced_today(conn: sqlite3.Connection) -> bool:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    return conn.execute("SELECT 1 FROM collection_value_history WHERE fetched_at >= ? LIMIT 1",
                        (today,)).fetchone() is not None


def sync_me(conn: sqlite3.Connection, client: DiscogsClient | None = None, *,
            force: bool = False) -> dict:
    """Sync the wantlist + collection and record today's album prices and collection value."""
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
    value = record_collection_value(conn, client, username)
    log.info("Priced %d wanted albums; collection valued at %s (median)", priced, value["median"])
    return {"username": username, "wants": counts["want"], "albums": albums, "priced": priced,
            "collection": counts["collection"], "collection_value": value}
