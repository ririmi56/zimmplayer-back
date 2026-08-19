"""playlists et partages

Revision ID: c7e2a1f5b830
Revises: a1c4f7b2d9e0
Create Date: 2026-08-18

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c7e2a1f5b830'
down_revision: Union[str, Sequence[str], None] = 'a1c4f7b2d9e0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'playlists',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('owner_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        # Supprimer un compte emporte ses playlists : elles n'ont plus de
        # proprietaire, et personne d'autre ne peut les administrer.
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'playlist_tracks',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('playlist_id', sa.Integer(), nullable=False),
        sa.Column('track_id', sa.Integer(), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        # SET NULL et non CASCADE : le depart de quelqu'un ne doit pas vider
        # une playlist partagee des titres qu'il y avait mis.
        sa.Column('added_by_id', sa.Integer(), nullable=True),
        sa.Column('added_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['playlist_id'], ['playlists.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['track_id'], ['tracks.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['added_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_playlist_tracks_playlist_id', 'playlist_tracks', ['playlist_id'])
    op.create_table(
        'playlist_shares',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('playlist_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('can_edit', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['playlist_id'], ['playlists.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('uq_playlist_shares', 'playlist_shares',
                    ['playlist_id', 'user_id'], unique=True)
    op.create_index('ix_playlist_shares_playlist_id', 'playlist_shares', ['playlist_id'])


def downgrade() -> None:
    op.drop_table('playlist_shares')
    op.drop_index('ix_playlist_tracks_playlist_id', table_name='playlist_tracks')
    op.drop_table('playlist_tracks')
    op.drop_table('playlists')
