import argparse
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


def main() -> None:
    parser = argparse.ArgumentParser(prog="tradio")
    sub = parser.add_subparsers(required=True)

    db = sub.add_parser("db", help="database management").add_subparsers(required=True)
    db.add_parser("upgrade", help="apply all migrations").set_defaults(func=db_upgrade)

    args = parser.parse_args()
    args.func(args)
