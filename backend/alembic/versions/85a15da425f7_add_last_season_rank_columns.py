"""add_last_season_rank_columns

Revision ID: 85a15da425f7
Revises: c4ec462cdcfa
Create Date: 2026-02-04 18:00:34.708519

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '85a15da425f7'
down_revision: Union[str, Sequence[str], None] = 'c4ec462cdcfa'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('players', schema=None) as batch_op:
        batch_op.add_column(sa.Column('last_season_rank', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('last_season_pos_rank', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('players', schema=None) as batch_op:
        batch_op.drop_column('last_season_pos_rank')
        batch_op.drop_column('last_season_rank')
