"""add_draft_order_tracking

Revision ID: c4ec462cdcfa
Revises: 8c291f7dabec
Create Date: 2026-01-30 14:20:43.922374

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4ec462cdcfa'
down_revision: Union[str, Sequence[str], None] = '8c291f7dabec'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add columns to draft_pick_history (no FK constraint for SQLite simplicity)
    with op.batch_alter_table('draft_pick_history', schema=None) as batch_op:
        batch_op.add_column(sa.Column('team_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('overall_pick', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('round_num', sa.Integer(), nullable=True))

    # Add columns to draft_sessions with defaults for existing rows
    with op.batch_alter_table('draft_sessions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('num_teams', sa.Integer(), nullable=False, server_default='12'))
        batch_op.add_column(sa.Column('user_draft_position', sa.Integer(), nullable=False, server_default='1'))
        batch_op.add_column(sa.Column('current_pick', sa.Integer(), nullable=False, server_default='1'))
        batch_op.add_column(sa.Column('draft_type', sa.String(length=20), nullable=False, server_default='snake'))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('draft_sessions', schema=None) as batch_op:
        batch_op.drop_column('draft_type')
        batch_op.drop_column('current_pick')
        batch_op.drop_column('user_draft_position')
        batch_op.drop_column('num_teams')

    with op.batch_alter_table('draft_pick_history', schema=None) as batch_op:
        batch_op.drop_column('round_num')
        batch_op.drop_column('overall_pick')
        batch_op.drop_column('team_id')
