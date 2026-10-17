import argparse
import logging
from datetime import date, timedelta
from pathlib import Path

from alembic import command
from alembic.config import Config

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def _alembic_config() -> Config:
    if ALEMBIC_INI.exists():
        return Config(str(ALEMBIC_INI))
    # Installed without the repo (e.g. container image): configure in code.
    cfg = Config()
    cfg.set_main_option("script_location", "tradio:migrations")
    return cfg


def db_upgrade(_: argparse.Namespace) -> None:
    command.upgrade(_alembic_config(), "head")


def backfill(args: argparse.Namespace) -> None:
    from tradio import ingest
    from tradio.data.nse import NseArchiveSource
    from tradio.db import session

    end = args.end or date.today()
    start = args.start or end - timedelta(days=round(365.25 * args.years))
    with session() as s:
        days = ingest.backfill(s, NseArchiveSource(), start, end, delay=args.delay)
    logging.info("backfill %s..%s done: %d trading days loaded", start, end, days)


def check_adjustments(args: argparse.Namespace) -> None:
    from tradio.adjustments import gap_checks
    from tradio.db import session

    with session() as s:
        checks = gap_checks(s)
    with_gap = [c for c in checks if c.gap is not None]
    off = [c for c in with_gap if abs(c.gap / c.factor - 1) > args.tolerance]
    print(f"{len(checks)} adjustments, {len(with_gap)} with prices around the ex-date")
    print(f"{len(with_gap) - len(off)} match the overnight gap within {args.tolerance:.0%}, {len(off)} don't:")
    for c in off:
        print(f"  {c.ex_date}  {c.symbol:<12} factor {c.factor:.4f}  gap {c.gap:.4f}  {c.actions}")


def backtest(args: argparse.Namespace) -> None:
    from tradio import backtest as bt
    from tradio.db import session

    with session() as s:
        print(bt.detail(s, [x.upper() for x in args.symbols], args.last) if args.symbols else bt.summary(s))


def bot(_: argparse.Namespace) -> None:
    from tradio.bot import run

    run()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(prog="tradio")
    sub = parser.add_subparsers(required=True)

    db = sub.add_parser("db", help="database management").add_subparsers(required=True)
    db.add_parser("upgrade", help="apply all migrations").set_defaults(func=db_upgrade)

    bf = sub.add_parser("backfill", help="download past bhavcopies")
    bf.add_argument("--years", type=float, default=2, help="how far back from --end (default 2)")
    bf.add_argument("--start", type=date.fromisoformat, help="first date, YYYY-MM-DD (overrides --years)")
    bf.add_argument("--end", type=date.fromisoformat, help="last date, YYYY-MM-DD (default today)")
    bf.add_argument("--delay", type=float, default=1.0, help="seconds between requests (default 1)")
    bf.set_defaults(func=backfill)

    ca = sub.add_parser("check-adjustments", help="compare split/bonus factors with actual price gaps")
    ca.add_argument("--tolerance", type=float, default=0.1, help="allowed relative difference (default 0.1)")
    ca.set_defaults(func=check_adjustments)

    bt = sub.add_parser("backtest", help="replay the signal over stored history (writes nothing)")
    bt.add_argument("symbols", nargs="*", help="stocks to detail; omit for a summary across all stocks")
    bt.add_argument("--last", type=int, default=10, help="events to show per stock (default 10)")
    bt.set_defaults(func=backtest)

    sub.add_parser("bot", help="run the Telegram bot").set_defaults(func=bot)

    args = parser.parse_args()
    args.func(args)
