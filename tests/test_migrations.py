from alembic import command
from sqlalchemy import inspect

from tradio.cli import _alembic_config
from tradio.models import Base


def test_migrations_create_every_table(migrated_db):
    assert set(Base.metadata.tables) <= set(inspect(migrated_db).get_table_names())


def test_models_and_migrations_in_sync(migrated_db):
    # Raises if autogenerate would produce a new migration.
    command.check(_alembic_config())


def test_downgrade_then_upgrade(migrated_db):
    cfg = _alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
