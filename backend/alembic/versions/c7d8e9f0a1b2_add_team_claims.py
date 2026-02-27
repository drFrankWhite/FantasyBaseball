"""add_team_claims

Revision ID: c7d8e9f0a1b2
Revises: b1f2c3d4e5f6
Create Date: 2026-02-23 22:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7d8e9f0a1b2'
down_revision: Union[str, Sequence[str], None] = 'b1f2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('teams', schema=None) as batch_op:
        batch_op.add_column(sa.Column('claimed_by_user', sa.String(length=64), nullable=True))
        batch_op.create_index('ix_teams_claimed_by_user', ['claimed_by_user'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('teams', schema=None) as batch_op:
        batch_op.drop_index('ix_teams_claimed_by_user')
        batch_op.drop_column('claimed_by_user')
