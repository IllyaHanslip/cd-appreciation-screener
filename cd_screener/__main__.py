"""Command-line entry point:  python -m cd_screener <command> [options]

Pipeline:   seed  ->  fetch-prices  ->  analyze  ->  serve
            (`demo` runs the first three offline in one go)
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import sys
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime
from datetime import time as clock_time
from pathlib import Path

from . import analytics, config, db, ingest, mydiscogs, notify, price_worker, report, seed
from .discogs_client import DiscogsAuthError

log = logging.getLogger("cd_screener")


def _id_list(text: str) -> list[int]:
    try:
        return [int(part) for part in text.split(",") if part.strip()]
    except ValueError:
        raise argparse.ArgumentTypeError("expected comma-separated release IDs, e.g. 123,456")


def _positive_int(text: str) -> int:
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def _time_of_day(text: str) -> clock_time:
    try:
        return datetime.strptime(text, "%H:%M").time()
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a time like 12:00, got {text!r}") from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cd_screener",
        description="CD Market Appreciation Screener - scarce, appreciating CDs on Discogs.")
    parser.add_argument("--db", type=Path, help=f"SQLite file (default: {config.DB_PATH})")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    p = sub.add_parser("seed", help="stage 1: simulate a Discogs dump, load it, run the funnel")
    p.add_argument("--count", type=_positive_int, default=5000, help="releases to simulate")
    p.add_argument("--seed", type=int, default=42, help="random seed (same seed = same data)")
    p.add_argument("--min-ratio", type=float, default=config.MIN_WANT_HAVE_RATIO)
    p.add_argument("--min-wants", type=int, default=config.MIN_WANTS,
                   help="also require at least this many wants (filters tiny-sample noise)")
    p.add_argument("--top", type=_positive_int, default=config.TOP_N_RELEASES)
    p.add_argument("--force", action="store_true", help="allow wiping real Discogs data")

    p = sub.add_parser("ingest", help="stage 1 (live): load real CDs from Discogs search")
    p.add_argument("--genre", help='e.g. "Rock", "Electronic", "Funk / Soul"')
    p.add_argument("--style", help='e.g. "Shoegaze", "IDM"')
    p.add_argument("--year", help="e.g. 1997")
    p.add_argument("--country", help='e.g. "Japan"')
    p.add_argument("--query", help="free-text search")
    p.add_argument("--pages", type=_positive_int, default=2, help="pages of 100 results")

    p = sub.add_parser("fetch-prices", help="stage 2: rate-limited Discogs price worker")
    p.add_argument("--limit", type=_positive_int, default=config.TOP_N_RELEASES,
                   help="how many flagged releases to price")
    p.add_argument("--ids", type=_id_list, help="price these release IDs instead")
    p.add_argument("--simulate", action="store_true",
                   help="use the simulated market even if DISCOGS_TOKEN is set")
    p.add_argument("--backfill-days", type=int, default=0,
                   help="simulated mode: first generate N days of price history")
    p.add_argument("--rpm", type=float, help="requests per minute (live mode max: 60)")
    p.add_argument("--min-ratio", type=float, default=config.MIN_WANT_HAVE_RATIO)
    p.add_argument("--min-wants", type=int, default=config.MIN_WANTS,
                   help="minimum wants for a release to be priced")
    p.add_argument("--force", action="store_true", help="refetch releases already priced today")
    p.add_argument("--seed", type=int, default=42, help="simulated market seed")
    p.add_argument("--no-analyze", action="store_true", help="skip the analytics step")

    sub.add_parser("analyze", help="stage 3: compute appreciation metrics and the leaderboard")

    p = sub.add_parser("report", help="publish the weekly market report now "
                                      "(fetch-prices also does this once a week)")
    p.add_argument("--out", type=Path, help="folder for the Markdown copy (default: reports/)")

    p = sub.add_parser("sync-me", help="sync your Discogs wantlist + collection and price "
                                       "them (also part of `daily`)")
    p.add_argument("--force", action="store_true",
                   help="resync even if already done today (only new items are priced again)")

    p = sub.add_parser("daily", help="scheduled job: fetch-prices and sync-me (+ weekly report "
                                     "when due), logging to logs/daily.log; once it has "
                                     "finished for the day, later runs that day do nothing")
    p.add_argument("--not-before", type=_time_of_day, metavar="HH:MM",
                   help="do nothing before this time of day (the scheduled task also starts "
                        "the job when the PC wakes up or you log on)")

    p = sub.add_parser("open", help="start the dashboard and open it in your browser "
                                    "(what the desktop shortcut runs)")
    p.add_argument("--port", type=int, default=5000)

    p = sub.add_parser("serve", help="stage 4: start the dashboard web server")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--debug", action="store_true")

    p = sub.add_parser("demo", help="run the whole pipeline offline: seed, history, prices, "
                                    "analytics")
    p.add_argument("--count", type=_positive_int, default=5000)
    p.add_argument("--days", type=int, default=180, help="days of simulated price history")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--force", action="store_true", help="allow wiping real Discogs data")
    p.add_argument("--serve", action="store_true", help="start the dashboard afterwards")
    p.add_argument("--port", type=int, default=5000)
    return parser


DAILY_DONE_FILE = "last_daily_run.txt"  # next to the database: the day the job last finished


def _daily(conn, *, not_before: clock_time | None = None, done_file: Path | None = None,
           now: datetime | None = None) -> int:
    """The scheduled job, with Windows notifications when it starts and finishes real work.

    Task Scheduler starts it at the set time, hourly after that, and when the PC wakes up
    or you log on. Before `not_before`, or once today's run has finished, it does nothing.
    """
    now = now or datetime.now()
    if not_before and now.time() < not_before:
        log.info("Started at %s: the daily run waits until %s", now.strftime("%H:%M"),
                 not_before.strftime("%H:%M"))
        return 0
    today = now.date().isoformat()
    if done_file and done_file.exists() and done_file.read_text(encoding="utf-8").strip() == today:
        log.debug("Today's daily run has already finished")
        return 0
    log.info("=== Daily run started ===")

    def started(n_releases: int, eta_minutes: float) -> None:
        notify.toast("CD Screener: updating prices",
                     f"Fetching Discogs prices for {n_releases} CDs (about "
                     f"{max(1, round(eta_minutes))} min). You can keep working.")

    try:
        summary = price_worker.run_worker(conn, on_start=started)
    except (db.ScreenerError, DiscogsAuthError) as exc:
        notify.toast("CD Screener: run failed", str(exc)[:200])
        raise
    me = {}
    sync_failed = False
    if not summary.aborted:
        try:  # your wantlist + collection; a failure here never hides the price results
            me = mydiscogs.sync_me(conn)
        except Exception as exc:  # noqa: BLE001 - reported, then the run carries on
            log.error("Wantlist/collection sync failed: %s", exc)
            sync_failed = True
    log.info("=== Daily run finished ===")
    if done_file and not summary.aborted and not sync_failed:
        done_file.write_text(today, encoding="utf-8")  # later runs today do nothing

    if summary.queued == 0 and not summary.report_period and not me:
        return 0  # nothing new today: stay silent
    if summary.aborted:
        notify.toast("CD Screener: stopped early",
                     f"{summary.aborted[:150]}. It will retry later; see logs\\daily.log.")
        return 1  # non-zero lets Task Scheduler retry
    deals, reissues = conn.execute(
        "SELECT COALESCE(SUM(deal), 0), COALESCE(SUM(reissue_risk), 0) FROM release_metrics"
    ).fetchone()
    body = f"{summary.ok} CDs priced · {deals} deals · {reissues} reissue alerts."
    if me:
        want_deals = sum(a["deal"] for a in mydiscogs.wanted_albums(conn))
        body += f" Wantlist: {me['albums']} albums checked"
        body += f", {want_deals} below their usual price." if want_deals else "."
        if me["collection_priced"]:
            body += f" Collection: {me['collection_priced']} CDs priced."
    if summary.report_period:
        body += f" New weekly report ({summary.report_period})."
    notify.toast("CD Screener: prices updated", body)
    return 0


def _log_to_file() -> None:
    """Send prints and logs to logs/daily.log (kept to ~3 MB). No console when scheduled."""
    log_dir = config.PROJECT_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    path = log_dir / "daily.log"
    if path.exists() and path.stat().st_size > 1_000_000:  # simple rotation
        for i in (2, 1):
            older = log_dir / f"daily.log.{i}"
            if older.exists():
                older.replace(log_dir / f"daily.log.{i + 1}")
        path.replace(log_dir / "daily.log.1")
    stream = open(path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 - lives for the run
    sys.stdout = sys.stderr = stream


def _dashboard_running(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url}api/health", timeout=1) as resp:
            return resp.status == 200
    except OSError:
        return False


def open_dashboard(db_path: Path | None, port: int) -> int:
    """Start the dashboard (unless it's already running) and open it in the browser."""
    url = f"http://127.0.0.1:{port}/"
    if _dashboard_running(url):
        webbrowser.open(url)
        print(f"The dashboard is already running - opened {url}")
        return 0
    if sys.platform == "win32":
        ctypes.windll.kernel32.SetConsoleTitleW("CD Screener")

    def open_when_ready() -> None:
        for _ in range(60):  # up to ~30 s
            if _dashboard_running(url):
                webbrowser.open(url)
                return
            time.sleep(0.5)

    threading.Thread(target=open_when_ready, daemon=True).start()
    logging.getLogger("werkzeug").setLevel(logging.WARNING)  # keep the window readable
    print("CD Appreciation Screener")
    print(f"Dashboard: {url}  (opens in your browser)")
    print("Keep this window open while you use it. Close it to stop the dashboard.\n")
    try:
        serve(db_path, "127.0.0.1", port)
    except OSError as exc:
        print(f"Could not start the dashboard on port {port}: {exc}")
        input("Press Enter to close.")
        return 1
    return 0


def serve(db_path: Path | None, host: str, port: int, debug: bool = False) -> None:
    from .app import create_app

    print(f"\nDashboard running at http://{host}:{port}  (Ctrl+C to stop)")
    create_app(db_path).run(host=host, port=port, debug=debug)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "daily":
        _log_to_file()  # runs hidden from Task Scheduler: everything goes to logs/daily.log
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)  # keep prints and log lines in order
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S" if args.command == "daily" else "%H:%M:%S")
    if not args.verbose:
        logging.getLogger("urllib3").setLevel(logging.WARNING)

    if args.command == "serve":
        serve(args.db, args.host, args.port, args.debug)
        return 0
    if args.command == "open":
        return open_dashboard(args.db, args.port)

    conn = db.connect(args.db)
    db.init_schema(conn)  # creates tables and upgrades older databases in place
    # The simulated dump lives next to whichever database is in use.
    dump_path = Path(args.db or config.DB_PATH).parent / config.SIMULATED_DUMP_PATH.name
    try:
        if args.command == "seed":
            seed.run_seed(conn, count=args.count, seed=args.seed, dump_path=dump_path,
                          min_ratio=args.min_ratio, top=args.top, min_wants=args.min_wants,
                          force=args.force)
            print("\nNext: python -m cd_screener fetch-prices --backfill-days 180")
        elif args.command == "ingest":
            filters = {"genre": args.genre, "style": args.style, "year": args.year,
                       "country": args.country, "q": args.query}
            ingest.run_ingest(conn, pages=args.pages, filters=filters)
            print("\nNext: python -m cd_screener fetch-prices")
        elif args.command == "fetch-prices":
            price_worker.run_worker(
                conn, limit=args.limit, release_ids=args.ids, simulate=args.simulate,
                backfill_days=args.backfill_days, requests_per_minute=args.rpm,
                min_ratio=args.min_ratio, min_wants=args.min_wants, force=args.force,
                seed=args.seed,
                analyze=not args.no_analyze)
        elif args.command == "daily":
            return _daily(conn, not_before=args.not_before,
                          done_file=Path(args.db or config.DB_PATH).with_name(DAILY_DONE_FILE))
        elif args.command == "sync-me":
            result = mydiscogs.sync_me(conn, force=args.force)
            if result:
                v = result["collection_value"]
                print(f"Synced {result['username']}: {result['wants']} wantlist items in "
                      f"{result['albums']} albums ({result['priced']} priced today), "
                      f"{result['collection']} in your collection ({result['collection_priced']} "
                      f"priced today; median value {v['median']} {v['currency'] or ''})")
        elif args.command == "analyze":
            analytics.run_analytics(conn)
        elif args.command == "report":
            data = report.publish(conn, out_dir=args.out or report.reports_dir_for(conn))
            print(f"Published the report for {data['period_start'][:10]} to "
                  f"{data['period_end'][:10]} (dashboard + {data['file']})")
        elif args.command == "demo":
            seed.run_seed(conn, count=args.count, seed=args.seed, dump_path=dump_path,
                          force=args.force)
            price_worker.run_worker(conn, simulate=True, backfill_days=args.days, seed=args.seed)
            if not args.serve:
                print("\nDone. Start the dashboard with:  python -m cd_screener serve")
    except db.ScreenerError as exc:
        log.error("%s", exc)
        return 1
    except DiscogsAuthError as exc:
        log.error("Discogs rejected the request: %s", exc)
        return 2
    except KeyboardInterrupt:
        log.warning("Interrupted")
        return 130
    finally:
        conn.close()

    if args.command == "demo" and args.serve:
        serve(args.db, "127.0.0.1", args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
