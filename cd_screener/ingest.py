"""Stage 1, live alternative: load real CD releases with community want/have counts.

The simulated seed is great for a demo, but its release IDs are synthetic. For real
screening, this pulls candidates from the Discogs database search endpoint, whose results
include `community.want` and `community.have` - exactly what the funnel needs:

    python -m cd_screener ingest --genre Rock --style Shoegaze --pages 5

Requires DISCOGS_TOKEN. Uses the same rate-limited client as the price worker.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping
from typing import Any

from . import config, db
from .discogs_client import DiscogsClient
from .seed import clean_artist_name, funnel_report

log = logging.getLogger(__name__)


def _first(values: Any) -> str | None:
    return values[0] if isinstance(values, list) and values else None


def search_result_to_row(item: Mapping[str, Any]) -> dict | None:
    """Map one /database/search result to a releases row (None if unusable)."""
    release_id = item.get("id")
    if item.get("type", "release") != "release" or not isinstance(release_id, int):
        return None
    artist, sep, title = str(item.get("title") or "").partition(" - ")  # "Artist - Title"
    if not sep:
        artist, title = "Unknown Artist", artist
    formats = item.get("format") or []
    fmt = "CD" if "CD" in formats else _first(formats)
    year = str(item.get("year") or "")
    community = item.get("community") or {}
    if not title.strip() or not fmt:
        return None
    return {
        "release_id": release_id,
        "title": title.strip(),
        "artist": clean_artist_name(artist) or "Unknown Artist",
        "year": int(year) if year.isdigit() and int(year) > 1800 else None,
        "genre": _first(item.get("genre")),
        "style": _first(item.get("style")),
        "format": fmt,
        "country": item.get("country"),
        "label": _first(item.get("label")),
        "catno": item.get("catno") or None,
        "master_id": item.get("master_id") or None,  # 0 means "not part of an album group"
        "wants": max(0, int(community.get("want") or 0)),
        "haves": max(0, int(community.get("have") or 0)),
        "source": "discogs",
    }


def run_ingest(
    conn: sqlite3.Connection,
    *,
    pages: int = 2,
    per_page: int = 100,
    filters: Mapping[str, Any] | None = None,
    client: DiscogsClient | None = None,
) -> int:
    if client is None:
        if not config.DISCOGS_TOKEN:
            raise db.ScreenerError("`ingest` calls the live Discogs API and needs DISCOGS_TOKEN "
                                   "in .env. For an offline demo use `seed` instead.")
        client = DiscogsClient(config.DISCOGS_TOKEN)
    db.init_schema(conn)

    rows: list[dict] = []
    for page in range(1, pages + 1):
        payload = client.search_releases(page=page, per_page=per_page,
                                         format=config.TARGET_FORMAT, **(filters or {}))
        results = payload.get("results") or []
        rows.extend(row for row in map(search_result_to_row, results) if row)
        total_pages = (payload.get("pagination") or {}).get("pages") or 0
        log.info("Search page %d of %d: %d results", page, min(pages, total_pages), len(results))
        if page >= total_pages:
            break
    if not rows:
        raise db.ScreenerError("Discogs search returned no CD releases for those filters.")

    purged = db.purge_simulated_releases(conn)
    if purged:
        log.warning("Removed %d simulated releases so synthetic and real data don't mix", purged)
    written = db.upsert_releases(conn, rows)
    print(f"Loaded {written} real Discogs releases")
    funnel_report(conn, min_ratio=config.MIN_WANT_HAVE_RATIO, fmt=config.TARGET_FORMAT,
                  top=config.TOP_N_RELEASES)
    return written
