"""Point d'entree unique de l'authentification et de l'identite.

En v1 l'application tourne sans authentification : l'identite se resume au
pseudo que le navigateur envoie dans l'en-tete `X-User-Name`, choisi dans
l'ecran de configuration. Toutes les routes injectent deja `CurrentUser`, si
bien que le passage a OIDC/Authentik ne demandera de modifier que ce fichier
(validation du JWT + cache JWKS) sans toucher a une seule route.

Les roles suivront le meme chemin : `User.role` est deja porte ici, et les
verifications viendront s'ajouter dans une dependance voisine.
"""

import re
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header

ANONYMOUS_NAME = "anonyme"
_INVALID = re.compile(r"[^\w .'\-]", re.UNICODE)


@dataclass(frozen=True)
class User:
    subject: str
    name: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def _clean_name(raw: str | None) -> str:
    if not raw:
        return ANONYMOUS_NAME
    name = _INVALID.sub("", raw).strip()[:60]
    return name or ANONYMOUS_NAME


def get_current_user(x_user_name: Annotated[str | None, Header()] = None) -> User:
    name = _clean_name(x_user_name)
    # Tant qu'il n'y a pas d'authentification, tout le monde est administrateur :
    # c'est l'implementation OIDC qui distinguera les roles.
    return User(subject=name, name=name, role="admin")


CurrentUser = Annotated[User, Depends(get_current_user)]
