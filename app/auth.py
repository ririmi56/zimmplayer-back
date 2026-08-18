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

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.config import get_settings
from app.db import get_db
from app.models import User as UserRow, utcnow

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
    #: Nomme dans la configuration : son role ne se revoque pas a l'ecran.
    is_super_admin: bool = False

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def _clean_name(raw: str | None) -> str:
    if not raw:
        return ANONYMOUS_NAME
    name = _INVALID.sub("", raw).strip()[:60]
    return name or ANONYMOUS_NAME


def super_admins() -> list[str]:
    """Comptes toujours administrateurs, lus dans la configuration."""
    raw = get_settings().super_admins
    return [entry.strip() for entry in raw.split(",") if entry.strip()]


def is_super_admin(subject: str, email: str) -> bool:
    """Un compte de la liste de configuration ?

    Compare au `sub` et au courriel, jamais au nom affiche : chez beaucoup de
    fournisseurs chacun peut modifier le sien, il suffirait de se renommer
    pour devenir administrateur.
    """
    for entry in super_admins():
        if entry == subject:
            return True
        if email and entry.casefold() == email.casefold():
            return True
    return False


def user_from_session(session: dict, db: DbSession | None = None) -> User | None:
    """Traduit une session ouverte en utilisateur, ou None s'il n'y en a pas.

    Le role vient de deux sources : la liste de super-administrateurs de la
    configuration, et la promotion accordee en base depuis la page
    Administration. Sans `db`, seule la premiere est consultee — ce qui suffit
    a `/api/auth/me` en mode degrade, jamais a autoriser une action.
    """
    identity = session.get(SESSION_IDENTITY)
    if not identity:
        return None

    subject = identity["subject"]
    email = identity.get("email", "")
    admin = is_super_admin(subject, email)
    if not admin and db is not None:
        admin = bool(db.scalar(select(UserRow.is_admin).where(UserRow.subject == subject)))

    return User(
        subject=subject,
        name=_clean_name(identity.get("name")),
        role="admin" if admin else "user",
        email=email,
        groups=list(identity.get("groups") or []),
        is_super_admin=is_super_admin(subject, email),
    )


def remember(db: DbSession, identity: dict) -> UserRow:
    """Enregistre ou rafraichit la personne qui vient de se connecter.

    Sans cette trace, la page Administration n'aurait personne a proposer :
    aucune API OIDC standard ne permet de lister les comptes d'un fournisseur.
    On ne peut donc promouvoir que quelqu'un qui s'est deja connecte.
    """
    row = db.scalar(select(UserRow).where(UserRow.subject == identity["subject"]))
    if row is None:
        row = UserRow(subject=identity["subject"], is_admin=False)
        db.add(row)
    # Le nom et le courriel restent ceux du fournisseur : on recopie a chaque
    # passage plutot que de laisser vieillir un instantane.
    row.name = _clean_name(identity.get("name"))
    row.email = identity.get("email", "") or ""
    row.last_seen_at = utcnow()
    db.commit()
    return row


def get_current_user(
    request: Request,
    x_user_name: Annotated[str | None, Header()] = None,
    db: DbSession = Depends(get_db),
) -> User:
    if get_settings().oidc_enabled:
        user = user_from_session(request.session, db)
        if user is not None:
            return user
        # Pas encore connecte. On ne refuse pas ici : c'est /api/auth/me qui
        # renseigne l'interface, et les routes ne sont pas restreintes tant que
        # les roles ne sont pas appliques.
        return User(subject="", name=ANONYMOUS_NAME, role="user")

    name = _clean_name(x_user_name)
    # Sans authentification, tout le monde est administrateur. Ce n'est pas un
    # oubli : sans fournisseur d'identite, distinguer les roles n'aurait aucun
    # fondement, et restreindre la page Administration rendrait l'application
    # inadministrable.
    return User(subject=name, name=name, role="admin", is_super_admin=True)


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_admin(user: CurrentUser) -> User:
    """Reserve une route aux administrateurs."""
    if not user.is_admin:
        raise HTTPException(
            status_code=403, detail="Reserve aux administrateurs"
        )
    return user


AdminUser = Annotated[User, Depends(require_admin)]
