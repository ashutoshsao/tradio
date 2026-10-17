import os

import pytest

# Point everything at the test database before tradio.config is read.
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://tradio:tradio@localhost:5432/tradio_test"
)

from alembic import command  # noqa: E402
from sqlalchemy import text  # noqa: E402

from tradio.cli import _alembic_config  # noqa: E402
from tradio.db import get_engine  # noqa: E402


@pytest.fixture(scope="session")
def migrated_db():
    """A test database reset to an empty schema and migrated to head."""
    with get_engine().begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    command.upgrade(_alembic_config(), "head")
    return get_engine()


@pytest.fixture
def db(migrated_db):
    """A session on a clean database; all tables are emptied after the test."""
    from tradio.db import session
    from tradio.models import Base

    with session() as s:
        yield s
    with migrated_db.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(Base.metadata.tables)} CASCADE"))
