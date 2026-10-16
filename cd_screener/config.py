"""Central configuration.

Everything tunable lives here. Secrets and machine-specific values (Discogs token,
database path) come from environment variables or a `.env` file in the project root.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

# --- Storage -----------------------------------------------------------------
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = Path(os.getenv("CD_SCREENER_DB", str(DATA_DIR / "cd_screener.db")))
SIMULATED_DUMP_PATH = DATA_DIR / "simulated_discogs_releases.xml"

# --- Funnel (stage 1) --------------------------------------------------------
TARGET_FORMAT = "CD"
MIN_WANT_HAVE_RATIO = 2.0  # strictly greater than
MIN_WANTS = 25  # ignore tiny samples like "4 wants / 1 have" (ratio 4.0 means little)
TOP_N_RELEASES = 300  # how many flagged releases the price worker processes

# --- Discogs API (stage 2) ---------------------------------------------------
DISCOGS_API_BASE = "https://api.discogs.com"
DISCOGS_TOKEN = (os.getenv("DISCOGS_TOKEN") or "").strip() or None
# Discogs requires a descriptive, unique User-Agent. Put your contact in .env.
DISCOGS_USER_AGENT = os.getenv(
    "DISCOGS_USER_AGENT", "CDAppreciationScreener/1.0 (+https://localhost)"
)
DISCOGS_MAX_REQUESTS_PER_MINUTE = 60  # authenticated limit, per source IP
RATE_LIMIT_SAFETY = 0.9  # use 90% of the allowance (=54 req/min) to leave headroom
NM_CONDITION_PREFIX = "Near Mint"  # Discogs key is "Near Mint (NM or M-)"
ALBUM_REFRESH_DAYS = 3  # album prices + pressing count (reissue monitor), every 3 days

# --- Deal finder & reissue monitor -------------------------------------------
DEAL_BELOW_USUAL_PCT = 20  # cheapest listing this far below its own usual listing price
DEAL_BELOW_ESTIMATE_PCT = 30  # ...or this far below the NM estimate (any condition!)
USUAL_LISTING_DAYS = 30  # "usual" = median listing over this many prior days
USUAL_LISTING_MIN_OBS = 3  # ...from at least this many observations
REISSUE_RECENT_YEARS = 2  # a CD pressing released this recently counts as a reissue risk

# --- Pressing flags (dashboard) ------------------------------------------------
RARE_PRESSING_PREMIUM = 3.0  # pressing NM estimate >= 3x the album's cheapest copy
ESTIMATE_HIGH_RATIO = 1.5  # NM estimate >= 1.5x this pressing's cheapest listing

# --- Analytics (stage 3) -----------------------------------------------------
GROWTH_WINDOW_DAYS = 30
GROWTH_TOLERANCE_DAYS = 10  # how stale the "30 days ago" snapshot may be
VELOCITY_WINDOW_DAYS = 90  # trailing window for the log-price trend line
VELOCITY_MIN_POINTS = 5
VELOCITY_MIN_SPAN_DAYS = 14
SCORE_WEIGHT_DEMAND = 0.5  # want/have percentile
SCORE_WEIGHT_MOMENTUM = 0.5  # price-velocity percentile
SPARKLINE_POINTS = 30
# Shared-move (currency) removal: a move counts as "shared" when at least this many CDs
# (and this share of the day's CDs) changed by the same % within the tolerance.
SHARED_MOVE_TOLERANCE_PCT = 0.05
SHARED_MOVE_MIN_MEMBERS = 4
SHARED_MOVE_MIN_SHARE = 0.02
SHARED_MOVE_MIN_EXPLAINED = 0.30  # groups must explain >= 30% of the day's CDs to count
