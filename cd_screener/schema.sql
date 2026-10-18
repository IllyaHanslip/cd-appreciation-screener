-- CD Market Appreciation Screener - SQLite schema
-- Applied idempotently by cd_screener.db.init_schema().

PRAGMA foreign_keys = ON;

-- Stage 1: release metadata (one row per Discogs release, any format).
CREATE TABLE IF NOT EXISTS releases (
    release_id      INTEGER PRIMARY KEY,               -- Discogs release ID
    title           TEXT    NOT NULL,
    artist          TEXT    NOT NULL,
    year            INTEGER,                           -- NULL when unknown
    genre           TEXT,                              -- primary Discogs genre
    style           TEXT,                              -- primary Discogs style
    format          TEXT    NOT NULL,                  -- primary format name: 'CD', 'Vinyl', ...
    country         TEXT,
    label           TEXT,                              -- first label credited on this pressing
    catno           TEXT,                              -- catalogue number of this pressing
    master_id       INTEGER,                           -- the album this pressing belongs to
    wants           INTEGER NOT NULL DEFAULT 0 CHECK (wants >= 0),
    haves           INTEGER NOT NULL DEFAULT 0 CHECK (haves >= 0),
    -- Computed by SQLite so it can never drift from wants/haves.
    -- haves is floored at 1 so "wanted by many, owned by nobody" ranks as maximally scarce.
    want_have_ratio REAL GENERATED ALWAYS AS (CAST(wants AS REAL) / MAX(haves, 1)) STORED,
    source          TEXT    NOT NULL DEFAULT 'simulated'
                            CHECK (source IN ('simulated', 'discogs')),
    updated_at      TEXT    NOT NULL
);

-- Serves the funnel query: WHERE format = 'CD' AND want_have_ratio > 2.0 ORDER BY ratio DESC
CREATE INDEX IF NOT EXISTS idx_releases_funnel ON releases (format, want_have_ratio DESC);
CREATE INDEX IF NOT EXISTS idx_releases_genre ON releases (genre);

-- Stage 2: one row per successful price observation (append-only time series).
CREATE TABLE IF NOT EXISTS price_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    release_id  INTEGER NOT NULL REFERENCES releases (release_id) ON DELETE CASCADE,
    fetched_at  TEXT    NOT NULL,                      -- ISO-8601 UTC, e.g. 2026-10-15T09:30:00Z
    condition   TEXT    NOT NULL,                      -- e.g. 'Near Mint (NM or M-)'
    price       REAL    NOT NULL CHECK (price > 0),
    currency    TEXT    NOT NULL,                      -- ISO code, e.g. 'USD'
    source      TEXT    NOT NULL CHECK (source IN ('simulated', 'discogs')),
    UNIQUE (release_id, condition, fetched_at)
);

CREATE INDEX IF NOT EXISTS idx_price_history_release_time
    ON price_history (release_id, fetched_at);

-- Stage 2: what copies of this pressing are actually listed for (marketplace stats).
-- A sanity check on the suggestion: a suggestion far above the cheapest listing is suspect.
CREATE TABLE IF NOT EXISTS listing_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    release_id    INTEGER NOT NULL REFERENCES releases (release_id) ON DELETE CASCADE,
    fetched_at    TEXT    NOT NULL,
    lowest_price  REAL,                                -- NULL when no copies are for sale
    currency      TEXT,
    num_for_sale  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_listing_history_release ON listing_history (release_id, id);

-- Stage 2: the album (Discogs "master") across all its pressings and formats.
-- Shows when a pressing is pricey only because it's a rare variant of a cheap album.
CREATE TABLE IF NOT EXISTS master_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    master_id     INTEGER NOT NULL,
    fetched_at    TEXT    NOT NULL,
    lowest_price  REAL,                                -- cheapest copy of any pressing/format
    num_for_sale  INTEGER NOT NULL,
    cd_versions   INTEGER,                             -- number of CD pressings of the album
    latest_cd_year       INTEGER,                      -- release year of the newest CD pressing
    latest_cd_release_id INTEGER
);

CREATE INDEX IF NOT EXISTS idx_master_snapshots_master ON master_snapshots (master_id, id);

-- Your Discogs wantlist and collection (synced daily). Wantlist variants are grouped by
-- album (album_key = 'm<master_id>', or 'r<release_id>' for releases without an album).
CREATE TABLE IF NOT EXISTS user_items (
    list          TEXT    NOT NULL CHECK (list IN ('want', 'collection')),
    release_id    INTEGER NOT NULL,
    album_key     TEXT    NOT NULL,
    master_id     INTEGER,
    artist        TEXT    NOT NULL,
    title         TEXT    NOT NULL,
    year          INTEGER,
    format        TEXT,
    label         TEXT,
    catno         TEXT,
    date_added    TEXT,
    active        INTEGER NOT NULL DEFAULT 1,          -- 0 once removed on Discogs
    first_seen    TEXT    NOT NULL,
    last_seen     TEXT    NOT NULL,
    PRIMARY KEY (list, release_id)
);

CREATE INDEX IF NOT EXISTS idx_user_items_album ON user_items (list, album_key);

-- One price check per wanted album per day: cheapest copy of any pressing/format.
CREATE TABLE IF NOT EXISTS user_album_prices (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    album_key     TEXT    NOT NULL,
    fetched_at    TEXT    NOT NULL,
    lowest_price  REAL,                                -- NULL when nothing is for sale
    currency      TEXT,
    num_for_sale  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_user_album_prices ON user_album_prices (album_key, id);

-- Discogs' own valuation of your whole collection, once a day.
CREATE TABLE IF NOT EXISTS collection_value_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at    TEXT    NOT NULL,
    minimum       REAL,
    median        REAL,
    maximum       REAL,
    currency      TEXT,
    items         INTEGER NOT NULL
);

-- One price check per collection item per day: Discogs' Near Mint suggestion for your
-- exact pressing, and the cheapest copy of it listed for sale (any condition).
CREATE TABLE IF NOT EXISTS user_item_prices (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    release_id       INTEGER NOT NULL,
    fetched_at       TEXT    NOT NULL,
    nm_price         REAL,                             -- NULL when Discogs has no suggestion
    nm_currency      TEXT,
    lowest_price     REAL,                             -- NULL when no copies are for sale
    lowest_currency  TEXT,
    num_for_sale     INTEGER
);

CREATE INDEX IF NOT EXISTS idx_user_item_prices ON user_item_prices (release_id, id);

-- What you paid for items in your collection (typed in on the dashboard, never from Discogs).
CREATE TABLE IF NOT EXISTS user_purchases (
    release_id    INTEGER PRIMARY KEY,
    price_paid    REAL    NOT NULL CHECK (price_paid >= 0),  -- the CD itself
    shipping_paid REAL    CHECK (shipping_paid >= 0),        -- postage, when known
    currency      TEXT    NOT NULL,
    purchased_on  TEXT,                                -- YYYY-MM-DD, optional
    updated_at    TEXT    NOT NULL
);

-- Weekly market reports, frozen when published (the dashboard shows the latest one).
CREATE TABLE IF NOT EXISTS reports (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    period_start  TEXT    NOT NULL,                    -- ISO-8601 UTC
    period_end    TEXT    NOT NULL,
    published_at  TEXT    NOT NULL,
    data          TEXT    NOT NULL                     -- JSON snapshot of the report
);

-- Stage 2: operational log of every fetch attempt, including failures.
-- Kept separate so price_history only ever contains real prices.
CREATE TABLE IF NOT EXISTS fetch_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT    NOT NULL,
    release_id    INTEGER NOT NULL,
    attempted_at  TEXT    NOT NULL,
    status        TEXT    NOT NULL CHECK (status IN ('ok', 'no_data', 'not_found', 'error')),
    http_status   INTEGER,
    message       TEXT
);

CREATE INDEX IF NOT EXISTS idx_fetch_log_release ON fetch_log (release_id, id);

-- Stage 3: latest analytics snapshot (fully rebuilt by every `analyze` run).
CREATE TABLE IF NOT EXISTS release_metrics (
    release_id        INTEGER PRIMARY KEY REFERENCES releases (release_id) ON DELETE CASCADE,
    rank              INTEGER NOT NULL,
    currency          TEXT    NOT NULL,
    current_price     REAL    NOT NULL,
    current_at        TEXT    NOT NULL,
    baseline_price    REAL    NOT NULL,
    baseline_at       TEXT    NOT NULL,
    abs_change        REAL    NOT NULL,
    pct_appreciation  REAL,
    growth_30d_pct    REAL,                            -- NULL until ~30 days of history exist
    velocity_30d_pct  REAL,                            -- NULL until the trend line has enough points
    demand_pctile     REAL    NOT NULL,
    momentum_pctile   REAL    NOT NULL,
    scarcity_index    REAL    NOT NULL,
    n_snapshots       INTEGER NOT NULL,
    sparkline         TEXT    NOT NULL,                -- JSON array of recent prices
    raw_pct_appreciation REAL,                         -- before removing shared currency moves
    raw_growth_30d_pct   REAL,
    listed_price         REAL,                         -- latest cheapest listing (this pressing)
    listed_count         INTEGER,
    usual_listed_price   REAL,                         -- median listing over the prior 30 days
    discount_vs_usual_pct    REAL,
    discount_vs_estimate_pct REAL,
    deal                 INTEGER NOT NULL DEFAULT 0,
    deal_reason          TEXT,
    reissue_risk         INTEGER NOT NULL DEFAULT 0,
    reissue_reason       TEXT,
    computed_at       TEXT    NOT NULL
);
