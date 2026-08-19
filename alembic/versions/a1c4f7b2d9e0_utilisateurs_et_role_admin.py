"""utilisateurs et role admin

Revision ID: a1c4f7b2d9e0
Revises: 585d6ce1c9e5
Create Date: 2026-08-18

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1c4f7b2d9e0'
down_revision: Union[str, Sequence[str], None] = '585d6ce1c9e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """La table n'existe que pour porter `is_admin`.

    Le reste de l'identite vient du jeton a chaque requete ; `name` et `email`
    n'en sont qu'une copie rafraichie a chaque connexion, pour que la page
    Administration affiche une liste lisible.
    """
    op.create_table(
        'users',
        sa.Column('id', sa.Integer(), nullable=False),
        # Identifiant du fournisseur : stable meme apres un changement de nom
        # ou d'adresse. C'est lui qui fait l'identite.
        sa.Column('subject', sa.String(length=255), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=False, server_default=''),
        sa.Column('is_admin', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('subject', name='uq_users_subject'),
    )


def downgrade() -> None:
    op.drop_table('users')
