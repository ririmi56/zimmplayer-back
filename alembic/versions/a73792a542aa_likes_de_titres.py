"""likes de titres

Revision ID: a73792a542aa
Revises: e90c3763563c
Create Date: 2026-08-20

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a73792a542aa'
down_revision: Union[str, Sequence[str], None] = 'e90c3763563c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Une table, et l'unicite qui empeche de liker deux fois."""
    op.create_table('track_likes',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('track_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['track_id'], ['tracks.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_track_likes_track_id'), 'track_likes', ['track_id'], unique=False)
    op.create_index(op.f('ix_track_likes_user_id'), 'track_likes', ['user_id'], unique=False)
    op.create_index('uq_track_likes', 'track_likes', ['user_id', 'track_id'], unique=True)


def downgrade() -> None:
    op.drop_index('uq_track_likes', table_name='track_likes')
    op.drop_index(op.f('ix_track_likes_user_id'), table_name='track_likes')
    op.drop_index(op.f('ix_track_likes_track_id'), table_name='track_likes')
    op.drop_table('track_likes')
