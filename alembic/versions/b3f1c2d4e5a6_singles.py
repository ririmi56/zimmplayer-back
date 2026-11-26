"""singles

Un single zimmporter vit dans le dossier `Singles/` partage de l'artiste :
c'est le fichier, et non le dossier, qui est la sortie. On ajoute donc un type
de sortie a l'album et on integre ce type a l'identite (artiste, type, titre).

Les singles deja indexes forment un unique album « Singles » par artiste. On ne
les decoupe pas ici : on efface leur etag pour que le prochain scan normal les
relise et que le scanner cree un album par single (avec sa propre pochette).

Revision ID: b3f1c2d4e5a6
Revises: b8e42f7c1d95
Create Date: 2026-11-26

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3f1c2d4e5a6'
down_revision: Union[str, Sequence[str], None] = 'b8e42f7c1d95'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Ajoute le type de sortie et force la relecture des singles existants."""
    op.add_column(
        'albums',
        sa.Column(
            'release_type',
            sa.String(length=16),
            nullable=False,
            server_default='album',
        ),
    )
    op.create_index(
        op.f('ix_albums_release_type'), 'albums', ['release_type'], unique=False
    )

    # L'unicite inclut le type : un single peut partager son titre avec un
    # album du meme artiste sans le rejoindre. Le nouvel index est cree AVANT
    # de supprimer l'ancien : MariaDB exige un index commencant par `artist_id`
    # pour la cle etrangere, et l'ancien index joue ce role.
    op.create_index(
        'uq_albums_artist_release_title',
        'albums',
        ['artist_id', 'release_type', 'source_title'],
        unique=True,
    )
    op.drop_index('uq_albums_artist_source_title', table_name='albums')

    # Les singles anterieurs sont agreges dans un album « Singles ». Un etag
    # vide les fait relire au prochain scan, meme non force ; le scanner les
    # eclate alors en un album par fichier. Un album qui s'appellerait vraiment
    # « Singles » mais vivrait dans son propre dossier sera simplement relu,
    # puis reste un album (la detection se fait sur le chemin).
    op.execute(
        sa.text(
            "UPDATE tracks SET etag = '' WHERE album_id IN "
            "(SELECT id FROM albums WHERE lower(source_title) = 'singles')"
        )
    )


def downgrade() -> None:
    """Revient a l'ancienne identite.

    Les singles deja eclates restent des albums a part entiere : les fusionner
    a l'aveugle ferait perdre leurs pochettes et leurs favoris.
    """
    # Meme precaution qu'a l'aller : l'index (artist_id, source_title) doit
    # exister avant que celui qui porte le type ne soit supprime.
    op.create_index(
        'uq_albums_artist_source_title',
        'albums',
        ['artist_id', 'source_title'],
        unique=True,
    )
    op.drop_index('uq_albums_artist_release_title', table_name='albums')
    op.drop_index(op.f('ix_albums_release_type'), table_name='albums')
    op.drop_column('albums', 'release_type')
