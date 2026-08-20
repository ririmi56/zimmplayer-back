"""favoris d'albums

Revision ID: f5a1c8d20b74
Revises: a73792a542aa
Create Date: 2026-08-20

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f5a1c8d20b74'
down_revision: Union[str, Sequence[str], None] = 'a73792a542aa'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Une table, et l'unicite qui empeche de mettre deux fois en favori."""
    op.create_table('album_favorites',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('album_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['album_id'], ['albums.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_album_favorites_album_id'), 'album_favorites', ['album_id'], unique=False)
    op.create_index(op.f('ix_album_favorites_user_id'), 'album_favorites', ['user_id'], unique=False)
    op.create_index('uq_album_favorites', 'album_favorites', ['user_id', 'album_id'], unique=True)


def downgrade() -> None:
    op.drop_index('uq_album_favorites', table_name='album_favorites')
    op.drop_index(op.f('ix_album_favorites_user_id'), table_name='album_favorites')
    op.drop_index(op.f('ix_album_favorites_album_id'), table_name='album_favorites')
    op.drop_table('album_favorites')
