"""ecoutes, ajouts en file et presence

Revision ID: d3f8a6c1e402
Revises: c7e2a1f5b830
Create Date: 2026-08-19

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd3f8a6c1e402'
down_revision: Union[str, Sequence[str], None] = 'c7e2a1f5b830'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Trois tables, parce que rien n'etait enregistre jusqu'ici.

    Les statistiques du catalogue se calculent sur l'existant ; celles par
    utilisateur repartent de zero, faute de trace.
    """
    op.create_table(
        'listens',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('track_id', sa.Integer(), nullable=False),
        sa.Column('session_id', sa.Integer(), nullable=True),
        # Copie volontaire : les sessions sont supprimees couramment, et une
        # statistique qui disparait avec elles ne servirait a rien.
        sa.Column('session_name', sa.String(length=120), nullable=True),
        sa.Column('seconds', sa.Float(), nullable=False, server_default='0'),
        sa.Column('listened_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['track_id'], ['tracks.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['session_id'], ['sessions.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_listens_listened_at', 'listens', ['listened_at'])
    op.create_index('ix_listens_user_at', 'listens', ['user_id', 'listened_at'])

    op.create_table(
        'queue_additions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('track_id', sa.Integer(), nullable=False),
        sa.Column('session_id', sa.Integer(), nullable=True),
        sa.Column('session_name', sa.String(length=120), nullable=True),
        sa.Column('added_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['track_id'], ['tracks.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['session_id'], ['sessions.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_queue_additions_added_at', 'queue_additions', ['added_at'])

    op.create_table(
        'session_presence',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('session_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['session_id'], ['sessions.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('uq_session_presence', 'session_presence',
                    ['session_id', 'user_id'], unique=True)


def downgrade() -> None:
    op.drop_table('session_presence')
    op.drop_index('ix_queue_additions_added_at', table_name='queue_additions')
    op.drop_table('queue_additions')
    op.drop_index('ix_listens_user_at', table_name='listens')
    op.drop_index('ix_listens_listened_at', table_name='listens')
    op.drop_table('listens')
