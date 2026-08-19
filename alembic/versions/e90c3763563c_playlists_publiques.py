"""playlists publiques

Revision ID: e90c3763563c
Revises: d3f8a6c1e402
Create Date: 2026-08-20

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e90c3763563c'
down_revision: Union[str, Sequence[str], None] = 'd3f8a6c1e402'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Une colonne : les playlists existantes restent privees.

    `server_default` autant que `default` : sans lui, l'ajout de la colonne
    echouerait sur une table deja peuplee, la colonne etant NOT NULL.
    """
    op.add_column(
        "playlists",
        sa.Column("is_public", sa.Boolean(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("playlists", "is_public")
