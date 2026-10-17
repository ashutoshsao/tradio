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
    from tradio.data.nse import NseBhavcopySource
    from tradio.db import session

    end = args.end or date.today()
    start = args.start or end - timedelta(days=round(365.25 * args.years))
    from tradio.adjustments import detect_adjustments

    with session() as s:
        stored = ingest.backfill(s, NseBhavcopySource(), start, end, delay=args.delay)
        found = detect_adjustments(s)
        s.commit()
    logging.info("backfill %s..%s done: %d new trading days, %d new split/bonus adjustments", start, end, stored, found)


def detect(_: argparse.Namespace) -> None:
    from tradio.adjustments import detect_adjustments
    from tradio.db import session

    with session() as s:
        found = detect_adjustments(s)
        s.commit()
    logging.info("%d new split/bonus adjustments", found)


def backtest(args: argparse.Namespace) -> None:
    from tradio import backtest as bt
    from tradio.db import session

    with session() as s:
        print(bt.detail(s, [x.upper() for x in args.symbols], args.last) if args.symbols else bt.summary(s))


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

    sub.add_parser("detect-adjustments", help="find splits/bonuses in stored prices").set_defaults(func=detect)

    bt = sub.add_parser("backtest", help="replay the signal over stored history (writes nothing)")
    bt.add_argument("symbols", nargs="*", help="stocks to detail; omit for a summary across all stocks")
    bt.add_argument("--last", type=int, default=10, help="events to show per stock (default 10)")
    bt.set_defaults(func=backtest)

    args = parser.parse_args()
    args.func(args)
