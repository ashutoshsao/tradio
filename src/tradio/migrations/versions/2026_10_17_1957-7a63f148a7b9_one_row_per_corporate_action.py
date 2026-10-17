"""one row per corporate action

Revision ID: 7a63f148a7b9
Revises: 5b205cc14c86
Create Date: 2026-10-17 19:57:46.674134

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7a63f148a7b9'
down_revision: Union[str, Sequence[str], None] = '5b205cc14c86'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Actions are re-loaded from NSE in the new shape; clear the old combined rows.
    op.execute("DELETE FROM price_adjustments")
    op.execute("UPDATE trading_days SET actions_loaded = false")
    op.add_column("price_adjustments", sa.Column("action", sa.String(length=32), nullable=False))
    op.add_column("price_adjustments", sa.Column("source", sa.String(length=8), nullable=False))
    op.drop_constraint("pk_price_adjustments", "price_adjustments", type_="primary")
    op.create_primary_key("pk_price_adjustments", "price_adjustments", ["symbol", "ex_date", "action"])


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DELETE FROM price_adjustments")
    op.execute("UPDATE trading_days SET actions_loaded = false")
    op.drop_constraint("pk_price_adjustments", "price_adjustments", type_="primary")
    op.create_primary_key("pk_price_adjustments", "price_adjustments", ["symbol", "ex_date"])
    op.drop_column("price_adjustments", "source")
    op.drop_column("price_adjustments", "action")
