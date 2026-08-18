"""Point d'entree unique de l'authentification et de l'identite.

Deux modes, choisis par OIDC_ENABLED.

Sans OIDC, l'identite se resume au pseudo que le navigateur envoie dans
l'en-tete `X-User-Name`, choisi dans l'ecran de configuration. Rien n'est
verifie : ce mode convient a un poste isole ou au developpement, pas a un
reseau ou plusieurs personnes se croisent.

Avec OIDC, l'identite vient d'un jeton valide par le fournisseur, conservee
dans un cookie de session signe. L'en-tete `X-User-Name` cesse alors d'etre
lue : la laisser active offrirait un chemin trivial pour se faire passer
pour quelqu'un d'autre, ce qui viderait l'authentification de son sens.

Toutes les routes injectent `CurrentUser`, si bien que le mode d'authentification
ne se lit qu'ici.
"""

import re
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends, Header, Request

from app.config import get_settings

ANONYMOUS_NAME = "anonyme"
_INVALID = re.compile(r"[^\w .'\-]", re.UNICODE)

#: Clef de l'identite dans la session. `app/api/auth.py` l'ecrit, on la lit.
SESSION_IDENTITY = "identity"


@dataclass(frozen=True)
class User:
    subject: str
    name: str
    role: str
    email: str = ""
    groups: list[str] = field(default_factory=list)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def _clean_name(raw: str | None) -> str:
    if not raw:
        return ANONYMOUS_NAME
    name = _INVALID.sub("", raw).strip()[:60]
    return name or ANONYMOUS_NAME


def user_from_session(session: dict) -> User | None:
    """Traduit une session ouverte en utilisateur, ou None s'il n'y en a pas."""
    identity = session.get(SESSION_IDENTITY)
    if not identity:
        return None
    settings = get_settings()
    groups = list(identity.get("groups") or [])
    # Sans groupe d'administration configure, personne n'est distingue : c'est
    # volontaire, un defaut qui donnerait le role admin sur un simple defaut de
    # configuration serait le mauvais sens de securite.
    admin = bool(settings.oidc_admin_group) and settings.oidc_admin_group in groups
    return User(
        subject=identity["subject"],
        name=_clean_name(identity.get("name")),
        role="admin" if admin else "user",
        email=identity.get("email", ""),
        groups=groups,
    )


def get_current_user(
    request: Request, x_user_name: Annotated[str | None, Header()] = None
) -> User:
    if get_settings().oidc_enabled:
        user = user_from_session(request.session)
        if user is not None:
            return user
        # Pas encore connecte. On ne refuse pas ici : c'est /api/auth/me qui
        # renseigne l'interface, et les routes ne sont pas restreintes tant que
        # les roles ne sont pas appliques.
        return User(subject="", name=ANONYMOUS_NAME, role="user")

    name = _clean_name(x_user_name)
    # Sans authentification, tout le monde est administrateur : c'est la mise
    # en place des roles qui distinguera.
    return User(subject=name, name=name, role="admin")


CurrentUser = Annotated[User, Depends(get_current_user)]
