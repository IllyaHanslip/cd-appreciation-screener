"""SQLite access layer: connections, schema bootstrap, the funnel query and write helpers.

All timestamps are stored as ISO-8601 UTC strings ("2026-10-15T09:30:00Z"), which sort
lexicographically in chronological order, so plain string comparisons work in SQL.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

from . import config

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

RELEASE_COLUMNS = (
    "release_id", "title", "artist", "year", "genre", "style",
    "format", "country", "label", "catno", "master_id", "wants", "haves", "source",
)

# Columns added after the first version; init_schema() adds them to older databases.
_ADDED_COLUMNS = {
    "releases": {"label": "TEXT", "catno": "TEXT", "master_id": "INTEGER"},
    "master_snapshots": {"cd_versions": "INTEGER", "latest_cd_year": "INTEGER",
                         "latest_cd_release_id": "INTEGER"},
    "user_items": {"format_details": "TEXT", "media_condition": "TEXT", "sleeve_condition": "TEXT",
                   "notes": "TEXT", "country": "TEXT", "barcode": "TEXT"},
}
_NEWEST_METRIC_COLUMN = "reissue_reason"

_RELEASE_SELECT = """
    SELECT release_id, title, artist, year, genre, style, format, country,
           label, catno, master_id, wants, haves, want_have_ratio, source
    FROM releases"""

FUNNEL_SQL = _RELEASE_SELECT + """
    WHERE format = :format
      AND want_have_ratio > :min_ratio
      AND wants >= :min_wants
    ORDER BY want_have_ratio DESC, wants DESC, release_id
    LIMIT :limit
"""


class ScreenerError(RuntimeError):
    """An expected, user-fixable problem. The CLI prints the message without a traceback."""


class DatabaseNotReady(ScreenerError):
    """The database file, its tables or its data are missing. The message says what to run."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: Path | str | None = None, *, must_exist: bool = False) -> sqlite3.Connection:
    """Open the database with Row access and foreign keys enabled.

    With must_exist=True a missing file raises DatabaseNotReady instead of silently
    creating an empty database (used by the read-only parts of the app).
    """
    path = Path(db_path or config.DB_PATH)
    if must_exist and not path.exists():
        raise DatabaseNotReady(
            f"Database not found at {path}. Run `python -m cd_screener demo` (or `seed`) first."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)  # wait up to 10s if another process is writing
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create missing tables and upgrade older databases in place (idempotent)."""
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    for table, columns in _ADDED_COLUMNS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, sql_type in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")
    # release_metrics is a derived snapshot rebuilt by every `analyze`: recreate if outdated.
    metric_columns = {row[1] for row in conn.execute("PRAGMA table_info(release_metrics)")}
    if _NEWEST_METRIC_COLUMN not in metric_columns:
        conn.execute("DROP TABLE release_metrics")
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


def ensure_releases_loaded(conn: sqlite3.Connection) -> int:
    """Return the number of releases, or raise DatabaseNotReady with a hint."""
    try:
        (count,) = conn.execute("SELECT COUNT(*) FROM releases").fetchone()
    except sqlite3.OperationalError as exc:  # "no such table"
        raise DatabaseNotReady(
            "Database has no schema yet. Run `python -m cd_screener seed` first."
        ) from exc
    if count == 0:
        raise DatabaseNotReady(
            "The releases table is empty. Run `python -m cd_screener seed` (or `ingest`) first."
        )
    return count


# --- Releases ------------------------------------------------------------------

def upsert_releases(
    conn: sqlite3.Connection, rows: Iterable[Mapping], *, batch_size: int = 1000
) -> int:
    """Insert or update releases in batches; returns the number of rows written."""
    columns = ", ".join(RELEASE_COLUMNS)
    placeholders = ", ".join("?" for _ in RELEASE_COLUMNS)
    updates = ", ".join(f"{c} = excluded.{c}" for c in RELEASE_COLUMNS[1:])
    sql = (
        f"INSERT INTO releases ({columns}, updated_at) VALUES ({placeholders}, ?) "
        f"ON CONFLICT (release_id) DO UPDATE SET {updates}, updated_at = excluded.updated_at"
    )
    now = utc_now_iso()
    written = 0
    batch: list[tuple] = []
    with conn:  # one transaction for the whole load
        for row in rows:
            batch.append(tuple(row[c] if c in row else None for c in RELEASE_COLUMNS) + (now,))
            if len(batch) >= batch_size:
                conn.executemany(sql, batch)
                written += len(batch)
                batch.clear()
        if batch:
            conn.executemany(sql, batch)
            written += len(batch)
    return written


def get_flagged_releases(
    conn: sqlite3.Connection,
    *,
    min_ratio: float = config.MIN_WANT_HAVE_RATIO,
    fmt: str = config.TARGET_FORMAT,
    limit: int | None = config.TOP_N_RELEASES,
    min_wants: int = config.MIN_WANTS,
) -> list[sqlite3.Row]:
    """The funnel: releases of `fmt` whose want/have ratio exceeds `min_ratio`, scarcest first."""
    params = {
        "format": fmt,
        "min_ratio": min_ratio,
        "min_wants": min_wants,
        "limit": -1 if limit is None else limit,  # LIMIT -1 means "no limit" in SQLite
    }
    return conn.execute(FUNNEL_SQL, params).fetchall()


def get_releases_by_ids(
    conn: sqlite3.Connection, release_ids: Sequence[int]
) -> tuple[list[sqlite3.Row], list[int]]:
    """Look up specific releases. Returns (found rows in input order, ids missing from the DB)."""
    found: dict[int, sqlite3.Row] = {}
    for start in range(0, len(release_ids), 500):  # stay under SQLite's variable limit
        chunk = list(release_ids[start:start + 500])
        marks = ", ".join("?" for _ in chunk)
        for row in conn.execute(f"{_RELEASE_SELECT} WHERE release_id IN ({marks})", chunk):
            found[row["release_id"]] = row
    ordered = [found[i] for i in release_ids if i in found]
    missing = [i for i in release_ids if i not in found]
    return ordered, missing


def count_releases_by_source(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT source, COUNT(*) AS n FROM releases GROUP BY source")
    return {r["source"]: r["n"] for r in rows}


def reset_all_data(conn: sqlite3.Connection) -> None:
    with conn:
        for table in ("release_metrics", "fetch_log", "price_history", "releases"):
            conn.execute(f"DELETE FROM {table}")


def purge_simulated_releases(conn: sqlite3.Connection) -> int:
    """Remove synthetic releases (their prices and metrics cascade)."""
    with conn:
        deleted = conn.execute("DELETE FROM releases WHERE source = 'simulated'").rowcount
        conn.execute(
            "DELETE FROM fetch_log WHERE release_id NOT IN (SELECT release_id FROM releases)"
        )
    return deleted


# --- Prices --------------------------------------------------------------------

def insert_price(
    conn: sqlite3.Connection,
    *,
    release_id: int,
    fetched_at: str,
    price: float,
    currency: str,
    condition: str,
    source: str,
) -> None:
    # OR IGNORE: re-running a backfill for the same timestamps is a no-op, not an error.
    conn.execute(
        "INSERT OR IGNORE INTO price_history "
        "(release_id, fetched_at, condition, price, currency, source) VALUES (?, ?, ?, ?, ?, ?)",
        (release_id, fetched_at, condition, price, currency, source),
    )


def log_fetch(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    release_id: int,
    status: str,
    http_status: int | None = None,
    message: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO fetch_log (run_id, release_id, attempted_at, status, http_status, message) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, release_id, utc_now_iso(), status, http_status, message),
    )


def insert_listing(
    conn: sqlite3.Connection,
    *,
    release_id: int,
    lowest_price: float | None,
    currency: str | None,
    num_for_sale: int,
) -> None:
    conn.execute(
        "INSERT INTO listing_history (release_id, fetched_at, lowest_price, currency, num_for_sale) "
        "VALUES (?, ?, ?, ?, ?)",
        (release_id, utc_now_iso(), lowest_price, currency, num_for_sale),
    )


def insert_master_snapshot(
    conn: sqlite3.Connection,
    *,
    master_id: int,
    lowest_price: float | None,
    num_for_sale: int,
    cd_versions: int | None = None,
    latest_cd_year: int | None = None,
    latest_cd_release_id: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO master_snapshots (master_id, fetched_at, lowest_price, num_for_sale, "
        "cd_versions, latest_cd_year, latest_cd_release_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (master_id, utc_now_iso(), lowest_price, num_for_sale, cd_versions, latest_cd_year,
         latest_cd_release_id),
    )


def release_ids_with_listing_since(conn: sqlite3.Connection, since_iso: str) -> set[int]:
    rows = conn.execute(
        "SELECT DISTINCT release_id FROM listing_history WHERE fetched_at >= ?", (since_iso,))
    return {r[0] for r in rows}


def master_ids_fetched_since(conn: sqlite3.Connection, since_iso: str) -> set[int]:
    # Snapshots without a pressing count predate the reissue monitor: treat them as stale.
    rows = conn.execute(
        "SELECT DISTINCT master_id FROM master_snapshots "
        "WHERE fetched_at >= ? AND cd_versions IS NOT NULL", (since_iso,))
    return {r[0] for r in rows}


def release_ids_resolved_since(conn: sqlite3.Connection, since_iso: str) -> set[int]:
    """Releases with a conclusive fetch (ok / no data / not found) since `since_iso`.

    Used to make the worker resumable: re-running it the same day skips finished work
    but retries anything that ended in a transient error.
    """
    rows = conn.execute(
        "SELECT DISTINCT release_id FROM fetch_log "
        "WHERE attempted_at >= ? AND status IN ('ok', 'no_data', 'not_found')",
        (since_iso,),
    )
    return {r[0] for r in rows}
