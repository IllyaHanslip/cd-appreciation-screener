"""Weekly market report: built from the data, frozen when published, shown on the dashboard.

    python -m cd_screener report      # publish a report now (+ reports/report-YYYY-MM-DD.md)
    GET /api/reports/latest           # the latest published report as JSON (dashboard card)
    GET /api/report                   # the same report as a Markdown download

`fetch-prices` publishes automatically once 7 days have passed since the last report's
period ended, so a report stays on the dashboard for a week until the next replaces it.
Sections: biggest movers (currency moves removed), deals, reissue alerts, supply changes.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from . import analytics, config, db

log = logging.getLogger(__name__)

REPORT_DAYS = 7
REPORTS_DIR = config.PROJECT_ROOT / "reports"
_ISO = "%Y-%m-%dT%H:%M:%SZ"


def _item(rid, row: dict, **extra) -> dict:
    return {
        "release_id": int(rid),
        "artist": row["artist"],
        "title": row["title"],
        "pressing": " · ".join(str(x) for x in (row.get("country"), row.get("catno")) if x),
        "discogs_url": (f"https://www.discogs.com/release/{rid}"
                        if row.get("source") == "discogs" else None),
        "currency": row.get("currency"),
        **extra,
    }


def _num(value):
    return None if value is None or pd.isna(value) else float(value)


def build_report(conn: sqlite3.Connection, *, days: int = REPORT_DAYS, top: int = 10,
                 now: datetime | None = None) -> dict:
    """Assemble the report as plain data (JSON-serialisable)."""
    db.ensure_releases_loaded(conn)
    now = now or datetime.now(timezone.utc)
    meta = pd.read_sql_query(
        "SELECT r.release_id, r.artist, r.title, r.country, r.catno, r.source, "
        "m.currency, m.current_price, m.scarcity_index, m.deal, m.deal_reason, "
        "m.listed_price, m.listed_count, m.reissue_risk, m.reissue_reason "
        "FROM release_metrics m JOIN releases r USING (release_id)", conn
    ).set_index("release_id")
    if meta.empty:
        raise db.ScreenerError("No analysed prices yet. Run `python -m cd_screener fetch-prices`.")

    history = analytics.load_price_history(conn)
    first_obs = history["fetched_at"].min().to_pydatetime()
    start = max(now - timedelta(days=days), first_obs)
    since = pd.Timestamp(start)
    rows = {rid: r for rid, r in meta.to_dict("index").items()}

    # Movers: each CD's own change (currency moves removed) within the period.
    adj = analytics.adjusted_history(history)
    window = adj[adj["fetched_at"] >= since]
    spans = window.groupby("release_id").agg(
        start=("adj_price", "first"), end=("adj_price", "last"), n=("adj_price", "size"))
    spans = spans[(spans["n"] >= 2) & spans.index.isin(meta.index)]
    spans["change"] = (spans["end"] / spans["start"] - 1) * 100
    moved = spans[spans["change"].abs() >= 0.5]
    rising = [_item(rid, rows[rid], change_pct=round(r["change"], 1),
                    price=_num(rows[rid]["current_price"]))
              for rid, r in moved.sort_values("change", ascending=False).head(top).iterrows()
              if r["change"] > 0]
    falling = [_item(rid, rows[rid], change_pct=round(r["change"], 1),
                     price=_num(rows[rid]["current_price"]))
               for rid, r in moved.sort_values("change").head(top).iterrows() if r["change"] < 0]

    deals_df = meta[meta["deal"] == 1].sort_values("scarcity_index", ascending=False)
    deals = [_item(rid, r, listed_price=_num(r["listed_price"]), price=_num(r["current_price"]),
                   reason=r["deal_reason"]) for rid, r in deals_df.head(top * 2).iterrows()]
    reissues = [_item(rid, r, reason=r["reissue_reason"])
                for rid, r in meta[meta["reissue_risk"] == 1].iterrows()]

    listings = pd.read_sql_query(
        "SELECT release_id, num_for_sale FROM listing_history WHERE fetched_at >= ? ORDER BY id",
        conn, params=(start.strftime(_ISO),))
    supply_df = listings.groupby("release_id")["num_for_sale"].agg(["first", "last", "size"])
    supply_df = supply_df[(supply_df["size"] >= 2) & (supply_df["first"] != supply_df["last"])
                          & supply_df.index.isin(meta.index)]
    supply_df = supply_df.assign(diff=supply_df["last"] - supply_df["first"]).sort_values("diff")
    supply = [_item(rid, rows[rid], copies_before=int(r["first"]), copies_now=int(r["last"]))
              for rid, r in supply_df.head(top).iterrows()]

    return {
        "period_start": start.strftime(_ISO),
        "period_end": now.strftime(_ISO),
        "tracked": len(meta),
        "history_since": first_obs.strftime(_ISO),
        "rising": rising,
        "falling": falling,
        "deals": deals,
        "deals_total": int(len(deals_df)),
        "reissues": reissues,
        "supply": supply,
    }


# --- Markdown ------------------------------------------------------------------------

def _money(value, currency) -> str:
    if value is None:
        return "—"
    symbol = {"GBP": "£", "USD": "$", "EUR": "€", "JPY": "¥"}.get(currency or "", "")
    return f"{symbol}{value:,.2f}" if symbol else f"{value:,.2f} {currency or ''}".strip()


def _link(item: dict) -> str:
    name = f"{item['artist']} – {item['title']}"
    return f"[{name}]({item['discogs_url']})" if item.get("discogs_url") else name


def _table(headers: list[str], rows: list[list]) -> str:
    def esc(cell) -> str:
        return str(cell).replace("|", "\\|")
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    return "\n".join(lines + ["| " + " | ".join(esc(c) for c in r) + " |" for r in rows])


def _day(iso: str) -> str:
    return datetime.strptime(iso, _ISO).strftime("%d %b %Y")


def to_markdown(r: dict) -> str:
    out = [f"# CD Market Report — {_day(r['period_start'])} to {_day(r['period_end'])}", "",
           f"{r['tracked']} scarce CDs tracked (want/have > {config.MIN_WANT_HAVE_RATIO}, "
           f"≥ {config.MIN_WANTS} wants) · price history since {_day(r['history_since'])}.", "",
           "## Biggest movers", "",
           "Change in each CD's own Near Mint estimate, with shared currency moves removed.", ""]
    if not (r["rising"] or r["falling"]):
        out += ["_No CD moved by 0.5% or more on its own this period._", ""]
    for title, items in (("Rising", r["rising"]), ("Falling", r["falling"])):
        if items:
            out += [f"**{title}**", "", _table(["CD", "Pressing", "NM estimate", "Change"], [
                [_link(i), i["pressing"] or "—", _money(i["price"], i["currency"]),
                 f"{i['change_pct']:+.1f}%"] for i in items]), ""]
    out += ["## Deals", ""]
    out += [_table(["CD", "Pressing", "Cheapest listing", "NM estimate", "Why"], [
        [_link(i), i["pressing"] or "—", _money(i["listed_price"], i["currency"]),
         _money(i["price"], i["currency"]), i["reason"]] for i in r["deals"]]), ""] \
        if r["deals"] else ["_No listings below their usual price or the NM estimate._", ""]
    out += ["## Reissue alerts", "", "A new CD pressing can cut the value of existing ones.", ""]
    out += [_table(["CD", "Pressing", "Alert"], [
        [_link(i), i["pressing"] or "—", i["reason"]] for i in r["reissues"]]), ""] \
        if r["reissues"] else ["_No new or recent CD reissues for tracked albums._", ""]
    out += ["## Supply changes", "",
            "Copies for sale of each pressing. Shrinking supply often comes before price rises.", ""]
    out += [_table(["CD", "Pressing", "Copies for sale"], [
        [_link(i), i["pressing"] or "—", f"{i['copies_before']} → {i['copies_now']}"]
        for i in r["supply"]]), ""] if r["supply"] else ["_No changes in copies for sale._", ""]
    out += ["---", "", "Method: Near Mint estimates are Discogs marketplace price suggestions for "
            "the exact pressing; listings are the cheapest copy for sale in any condition. "
            "Data provided by [Discogs](https://www.discogs.com).", ""]
    return "\n".join(out)


# --- Publishing ------------------------------------------------------------------------

def publish(conn: sqlite3.Connection, *, now: datetime | None = None,
            out_dir: Path | None = REPORTS_DIR) -> dict:
    """Build, freeze and store a report (and write it as Markdown if out_dir is set)."""
    data = build_report(conn, now=now)
    with conn:
        conn.execute(
            "INSERT INTO reports (period_start, period_end, published_at, data) VALUES (?, ?, ?, ?)",
            (data["period_start"], data["period_end"], db.utc_now_iso(), json.dumps(data)))
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"report-{data['period_end'][:10]}.md"
        path.write_text(to_markdown(data), encoding="utf-8")
        data["file"] = str(path)
    return data


def reports_dir_for(conn: sqlite3.Connection) -> Path:
    """reports/ in the project for the main database; next to any other database file."""
    db_file = Path(conn.execute("PRAGMA database_list").fetchone()[2] or config.DB_PATH)
    if db_file.resolve() == Path(config.DB_PATH).resolve():
        return REPORTS_DIR
    return db_file.parent / "reports"


def latest(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT data, published_at FROM reports ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return None
    return {**json.loads(row["data"]), "published_at": row["published_at"]}


def publish_if_due(conn: sqlite3.Connection, *, now: datetime | None = None,
                   out_dir: Path | None = None) -> dict | None:
    """Publish when there is no report yet or the last one ended a week or more ago."""
    now = now or datetime.now(timezone.utc)
    out_dir = out_dir or reports_dir_for(conn)
    last = latest(conn)
    if last is not None:
        ended = datetime.strptime(last["period_end"], _ISO).replace(tzinfo=timezone.utc)
        if now - ended < timedelta(days=REPORT_DAYS):
            return None
    try:
        data = publish(conn, now=now, out_dir=out_dir)
    except db.ScreenerError as exc:
        log.info("No weekly report yet: %s", exc)
        return None
    log.info("Published the weekly report (%s to %s)", data["period_start"][:10],
             data["period_end"][:10])
    return data
