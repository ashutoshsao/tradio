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
    with session() as s:
        stored = ingest.backfill(s, NseBhavcopySource(), start, end, delay=args.delay)
    logging.info("backfill %s..%s done: %d new trading days", start, end, stored)


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

    args = parser.parse_args()
    args.func(args)
