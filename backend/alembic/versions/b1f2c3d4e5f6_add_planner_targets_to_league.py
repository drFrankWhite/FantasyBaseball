"""add_planner_targets_to_league

Revision ID: b1f2c3d4e5f6
Revises: a3b7c9d2e4f1
Create Date: 2026-02-23 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b1f2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'a3b7c9d2e4f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('leagues', schema=None) as batch_op:
        batch_op.add_column(sa.Column('category_planner_targets', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('leagues', schema=None) as batch_op:
        batch_op.drop_column('category_planner_targets')
