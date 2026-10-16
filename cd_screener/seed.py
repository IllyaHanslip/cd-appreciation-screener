"""Stage 1 - the funnel core: simulated Discogs metadata -> SQLite -> want/have filter.

1. `generate_simulated_dump` writes a synthetic file in the Discogs monthly data-dump XML
   format (https://data.discogs.com), so the pipeline runs without a 10+ GB download.
2. `iter_dump_releases` stream-parses it with ElementTree.iterparse - the same technique
   that works on the real dump - and the rows are bulk-loaded into SQLite.
3. `funnel_report` runs the filter query: format = 'CD' AND want/have ratio > 2.0.

Note: real dumps do not contain community want/have counts. The synthetic file carries
them in an extra <community> element; for real releases they come from the Discogs API
(see ingest.py). All artists, titles and labels generated here are fictional, and release
IDs start at 90,000,001 so they can't be mistaken for real Discogs releases.
"""

from __future__ import annotations

import math
import random
import re
import sqlite3
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

import pandas as pd

from . import config, db

SIMULATED_ID_START = 90_000_001

GENRE_STYLES = {
    "Rock": ["Alternative Rock", "Shoegaze", "Post-Rock", "Grunge", "Prog Rock",
             "Black Metal", "Doom Metal", "Math Rock"],
    "Electronic": ["IDM", "Ambient", "Techno", "Drum n Bass", "Trance", "Downtempo"],
    "Hip Hop": ["Boom Bap", "Horrorcore", "Conscious", "Instrumental"],
    "Jazz": ["Free Jazz", "Hard Bop", "Fusion", "Spiritual Jazz"],
    "Pop": ["J-pop", "City Pop", "Synth-pop", "Indie Pop"],
    "Funk / Soul": ["Soul", "Funk", "Neo Soul", "Rhythm & Blues"],
    "Classical": ["Baroque", "Modern", "Romantic", "Contemporary"],
    "Reggae": ["Dub", "Roots Reggae", "Dancehall"],
    "Folk, World, & Country": ["Folk", "Neofolk", "Country", "Bluegrass"],
}
GENRE_WEIGHTS = [27, 20, 10, 9, 10, 8, 6, 4, 6]
FORMATS = {"CD": 55, "Vinyl": 28, "Cassette": 10, "CDr": 7}
YEAR_RANGES = {"CD": (1984, 2020, 1997), "Vinyl": (1962, 2024, 1978),
               "Cassette": (1972, 2002, 1988), "CDr": (1996, 2016, 2004)}
COUNTRIES = {"US": 30, "UK": 15, "Europe": 15, "Germany": 10, "Japan": 12,
             "France": 6, "Canada": 5, "Netherlands": 4, "Australia": 3}
DESCRIPTIONS = {"Album": 60, "Compilation": 10, "EP": 8, "Limited Edition": 8,
                "Reissue": 7, "Remastered": 5, "Promo": 2}

ADJECTIVES = ["Velvet", "Hollow", "Crimson", "Silent", "Electric", "Paper", "Neon", "Glass",
              "Static", "Golden", "Broken", "Midnight", "Lunar", "Copper", "Wild", "Frozen",
              "Distant", "Burning", "Quiet", "Violet", "Iron", "Amber", "Endless", "Fading"]
NOUNS = ["Harbor", "Echoes", "Satellites", "Gardens", "Machines", "Rivers", "Signals",
         "Mirrors", "Tides", "Lanterns", "Engines", "Ghosts", "Orchards", "Horizons",
         "Cathedrals", "Wolves", "Comets", "Islands", "Choirs", "Avenues"]
FIRST_NAMES = ["Mara", "Jonah", "Ilse", "Theo", "Rosa", "Kenji", "Amara", "Felix", "Noor",
               "Elias", "Saskia", "Dario", "Yuki", "Odile", "Malik", "Greta", "Lucien", "Ines"]
LAST_NAMES = ["Vance", "Okafor", "Lindqvist", "Moreau", "Hale", "Takeda", "Brandt", "Castell",
              "Reyes", "Novak", "Achterberg", "Quill", "Søndergaard", "Belanger"]
ENSEMBLES = ["Quartet", "Trio", "Ensemble", "Orchestra", "Collective"]

_DISAMBIGUATION = re.compile(r"\s\(\d+\)$")  # Discogs suffixes duplicate names: "Nirvana (2)"


# --- 1. Generate a synthetic dump ------------------------------------------------

def _weighted(rng: random.Random, table: dict[str, int]) -> str:
    return rng.choices(list(table), weights=list(table.values()))[0]


def _artist_name(rng: random.Random, genre: str) -> str:
    adj, noun = rng.choice(ADJECTIVES), rng.choice(NOUNS)
    first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
    if genre in ("Jazz", "Classical") and rng.random() < 0.5:
        return f"The {first} {last} {rng.choice(ENSEMBLES)}"
    pattern = rng.randrange(4)
    if pattern == 0:
        name = f"The {adj} {noun}"
    elif pattern == 1:
        name = f"{first} {last}"
    elif pattern == 2:
        name = f"{adj}{noun.lower()}"
    else:
        name = f"DJ {first}" if genre in ("Electronic", "Hip Hop") else f"{adj} {noun}"
    if rng.random() < 0.03:
        name += " (2)"  # mimic Discogs' duplicate-name suffix; the parser strips it
    return name


def _title(rng: random.Random) -> str:
    adj, noun, noun2 = rng.choice(ADJECTIVES), rng.choice(NOUNS), rng.choice(NOUNS)
    return rng.choice([
        f"{adj} {noun}",
        f"{noun} of the {adj} {noun2}",
        f"{adj} {noun} {rng.choice(['II', 'III', 'Vol. 1', 'Vol. 2'])}",
        f"Live at the {adj} {noun2}",
        f"{noun}",
    ])


def _community_counts(rng: random.Random, fmt: str, country: str, description: str) -> tuple[int, int]:
    """Draw (wants, haves). Small pressings, Japanese releases and limited editions skew scarcer."""
    haves = max(1, int(rng.lognormvariate(math.log(180), 1.1)))
    if fmt == "Vinyl":
        haves = int(haves * 1.4)
    mu = math.log(0.75) - 0.22 * (math.log(max(haves, 1)) - math.log(180))
    if description == "Limited Edition":
        mu += 0.35
    if country == "Japan":
        mu += 0.20
    ratio = math.exp(rng.gauss(mu, 0.85))
    return int(round(haves * ratio)), haves


def _release_element(rng: random.Random, release_id: int) -> ET.Element:
    genre = rng.choices(list(GENRE_STYLES), weights=GENRE_WEIGHTS)[0]
    fmt = _weighted(rng, FORMATS)
    country = _weighted(rng, COUNTRIES)
    description = _weighted(rng, DESCRIPTIONS)
    lo, hi, mode = YEAR_RANGES[fmt]
    year = int(rng.triangular(lo, hi, mode))
    wants, haves = _community_counts(rng, fmt, country, description)

    rel = ET.Element("release", id=str(release_id),
                     status="Accepted" if rng.random() > 0.01 else "Draft")
    artists = ET.SubElement(rel, "artists")
    artist = ET.SubElement(artists, "artist")
    ET.SubElement(artist, "id").text = str(rng.randrange(1, 9_000_000))
    ET.SubElement(artist, "name").text = _artist_name(rng, genre)
    ET.SubElement(rel, "title").text = _title(rng)
    labels = ET.SubElement(rel, "labels")
    ET.SubElement(labels, "label", name=f"{rng.choice(NOUNS)} Records",
                  catno=f"{rng.choice('ABCDEFGHJKLMNPRSTVWXYZ')}{rng.choice('ABCDEFGHJKLMNPRSTVWXYZ')}-{rng.randrange(100, 9999)}",
                  id=str(rng.randrange(1, 2_000_000)))
    formats = ET.SubElement(rel, "formats")
    fmt_el = ET.SubElement(formats, "format", name=fmt, qty=str(rng.choice([1, 1, 1, 2])), text="")
    ET.SubElement(ET.SubElement(fmt_el, "descriptions"), "description").text = description
    ET.SubElement(ET.SubElement(rel, "genres"), "genre").text = genre
    ET.SubElement(ET.SubElement(rel, "styles"), "style").text = rng.choice(GENRE_STYLES[genre])
    ET.SubElement(rel, "country").text = country
    if rng.random() > 0.04:  # a few releases have no release date, like real data
        released = str(year) if rng.random() < 0.3 else f"{year}-{rng.randrange(1, 13):02d}-00"
        ET.SubElement(rel, "released").text = released
    ET.SubElement(rel, "data_quality").text = rng.choice(["Correct", "Needs Vote", "Complete and Correct"])
    community = ET.SubElement(rel, "community")  # simulated extension (see module docstring)
    ET.SubElement(community, "want").text = str(wants)
    ET.SubElement(community, "have").text = str(haves)
    return rel


def generate_simulated_dump(path: Path, *, count: int, seed: int = 42) -> Path:
    """Write `count` synthetic releases in Discogs dump format. Deterministic for a given seed."""
    rng = random.Random(seed)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?>\n<releases>\n')
        for i in range(count):
            fh.write(ET.tostring(_release_element(rng, SIMULATED_ID_START + i), encoding="unicode"))
            fh.write("\n")
        fh.write("</releases>\n")
    return path


# --- 2. Stream-parse the dump ------------------------------------------------------

def _parse_year(released: str | None) -> int | None:
    """Discogs dates look like '1997', '1997-05-00' or '1997-05-12'."""
    if released and len(released) >= 4 and released[:4].isdigit():
        year = int(released[:4])
        return year if year > 1800 else None
    return None


def clean_artist_name(name: str) -> str:
    """Drop Discogs' "(2)" duplicate-name suffix and "*" name-variation marker."""
    return _DISAMBIGUATION.sub("", name.strip().rstrip("*").strip())


def _to_int(text: str | None) -> int:
    try:
        return max(0, int(text)) if text is not None else 0
    except ValueError:
        return 0


def _parse_release(elem: ET.Element) -> dict | None:
    """Convert one <release> element to a row dict, or None if it should be skipped."""
    if elem.get("status", "Accepted") != "Accepted":
        return None
    title = (elem.findtext("title") or "").strip()
    names = [clean_artist_name(a.findtext("name") or "") for a in elem.findall("artists/artist")]
    artist = " & ".join(n for n in names if n)
    fmt_el = elem.find("formats/format")
    fmt = fmt_el.get("name") if fmt_el is not None else None
    if not (elem.get("id") or "").isdigit() or not title or not artist or not fmt:
        return None
    return {
        "release_id": int(elem.get("id")),
        "title": title,
        "artist": artist,
        "year": _parse_year(elem.findtext("released")),
        "genre": elem.findtext("genres/genre"),
        "style": elem.findtext("styles/style"),
        "format": fmt,
        "country": elem.findtext("country"),
        "wants": _to_int(elem.findtext("community/want")),
        "haves": _to_int(elem.findtext("community/have")),
        "source": "simulated",
    }


def iter_dump_releases(path: Path, stats: Counter | None = None) -> Iterator[dict]:
    """Yield release rows from a Discogs-format XML dump using constant memory."""
    stats = stats if stats is not None else Counter()
    context = ET.iterparse(path, events=("start", "end"))
    _, root = next(context)  # the <releases> root element
    for event, elem in context:
        if event != "end" or elem.tag != "release":
            continue
        row = _parse_release(elem)
        root.clear()  # drop parsed elements so memory stays flat even on multi-GB dumps
        if row is None:
            stats["skipped"] += 1
            continue
        stats["parsed"] += 1
        yield row


# --- 3. The funnel query -------------------------------------------------------

def funnel_report(
    conn: sqlite3.Connection, *, min_ratio: float, fmt: str, top: int,
    min_wants: int = config.MIN_WANTS,
) -> list[sqlite3.Row]:
    total, n_fmt, n_flagged = conn.execute(
        "SELECT COUNT(*), "
        "       COALESCE(SUM(format = :fmt), 0), "
        "       COALESCE(SUM(format = :fmt AND want_have_ratio > :r AND wants >= :w), 0) "
        "FROM releases",
        {"fmt": fmt, "r": min_ratio, "w": min_wants},
    ).fetchone()
    flagged = db.get_flagged_releases(conn, min_ratio=min_ratio, fmt=fmt, limit=top,
                                      min_wants=min_wants)
    print("\nFunnel")
    for label, n in [("all releases loaded", total),
                     (f"format = '{fmt}'", n_fmt),
                     (f"want/have > {min_ratio}, wants >= {min_wants}", n_flagged),
                     ("handed to the price worker", len(flagged))]:
        print(f"  {label:<30}{n:>7,}")
    if flagged:
        preview = pd.DataFrame([dict(r) for r in flagged[:10]])
        preview["want_have_ratio"] = preview["want_have_ratio"].round(2)
        cols = ["release_id", "artist", "title", "year", "genre", "wants", "haves", "want_have_ratio"]
        print("\nTop 10 flagged releases:")
        print(preview[cols].to_string(index=False))
    return flagged


def run_seed(
    conn: sqlite3.Connection,
    *,
    count: int = 5000,
    seed: int = 42,
    dump_path: Path = config.SIMULATED_DUMP_PATH,
    min_ratio: float = config.MIN_WANT_HAVE_RATIO,
    top: int = config.TOP_N_RELEASES,
    min_wants: int = config.MIN_WANTS,
    force: bool = False,
) -> list[sqlite3.Row]:
    db.init_schema(conn)
    live = db.count_releases_by_source(conn).get("discogs", 0)
    if live and not force:
        raise db.ScreenerError(
            f"The database holds {live:,} real Discogs releases (and their price history). "
            "Seeding would wipe them. Re-run with --force, or set CD_SCREENER_DB to another file."
        )
    db.reset_all_data(conn)

    print(f"Generating {count:,} simulated Discogs releases -> {dump_path}")
    generate_simulated_dump(dump_path, count=count, seed=seed)

    stats: Counter = Counter()
    loaded = db.upsert_releases(conn, iter_dump_releases(dump_path, stats))
    print(f"Parsed {stats['parsed']:,} releases ({stats['skipped']} skipped: non-accepted or "
          f"incomplete) and loaded {loaded:,} rows into SQLite")
    return funnel_report(conn, min_ratio=min_ratio, fmt=config.TARGET_FORMAT, top=top,
                         min_wants=min_wants)
