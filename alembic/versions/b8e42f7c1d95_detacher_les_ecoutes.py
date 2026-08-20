"""detacher les ecoutes au lieu de les supprimer

Revision ID: b8e42f7c1d95
Revises: f5a1c8d20b74
Create Date: 2026-08-20

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b8e42f7c1d95'
down_revision: Union[str, Sequence[str], None] = 'f5a1c8d20b74'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _nom_de_la_cle() -> str:
    """Le nom que le serveur a donne a la cle etrangere `listens.user_id`.

    `alter_column` ne peut pas changer un `ON DELETE` : il faut retirer la cle
    et la reposer. Or MariaDB la nomme lui-meme (`listens_ibfk_1` ici), d'apres
    l'ordre de declaration — un nom qu'on ne peut pas ecrire en dur sans parier
    sur l'historique de la base visee. On le relit donc.
    """
    inspecteur = sa.inspect(op.get_bind())
    for cle in inspecteur.get_foreign_keys("listens"):
        if cle["constrained_columns"] == ["user_id"]:
            return cle["name"]
    raise RuntimeError("Aucune cle etrangere sur listens.user_id")


def upgrade() -> None:
    """Supprimer un compte detache ses ecoutes, il ne les efface plus.

    Ce qui a ete ecoute dans la maison l'a ete pour de bon : les totaux et le
    classement des titres doivent survivre a un menage dans les comptes.
    """
    nom = _nom_de_la_cle()
    op.drop_constraint(nom, "listens", type_="foreignkey")
    op.alter_column("listens", "user_id", existing_type=sa.Integer(), nullable=True)
    op.create_foreign_key(
        nom, "listens", "users", ["user_id"], ["id"], ondelete="SET NULL"
    )


def downgrade() -> None:
    """Les ecoutes deja detachees n'ont plus de compte a qui revenir : on ne
    peut pas revenir en arriere sans les supprimer."""
    nom = _nom_de_la_cle()
    op.execute(sa.text("DELETE FROM listens WHERE user_id IS NULL"))
    op.drop_constraint(nom, "listens", type_="foreignkey")
    op.alter_column("listens", "user_id", existing_type=sa.Integer(), nullable=False)
    op.create_foreign_key(
        nom, "listens", "users", ["user_id"], ["id"], ondelete="CASCADE"
    )
