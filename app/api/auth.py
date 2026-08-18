"""Routes d'authentification : connexion, retour du fournisseur, identite, deconnexion.

La session applicative est un cookie signe (SessionMiddleware) : le navigateur
n'y lit rien et ne peut pas le forger, et il accompagne aussi bien les appels
JSON que la WebSocket du relais audio.
"""

import hmac
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from sqlalchemy.orm import Session as DbSession

from app.auth import SESSION_IDENTITY, CurrentUser, remember
from app.db import get_db
from app.config import get_settings
from app.schemas import AuthStatus
from app.services import oidc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["authentification"])

# `_PENDING` ne vit qu'entre la redirection et le retour du fournisseur.
_PENDING = "pending"


def _require_oidc() -> None:
    if not get_settings().oidc_enabled:
        raise HTTPException(status_code=404, detail="OIDC n'est pas active")


@router.get("/login")
def login(request: Request) -> RedirectResponse:
    """Envoie le navigateur chez le fournisseur."""
    _require_oidc()
    try:
        url, pending = oidc.start()
    except oidc.OidcError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    request.session[_PENDING] = {
        "state": pending.state,
        "nonce": pending.nonce,
        "code_verifier": pending.code_verifier,
    }
    return RedirectResponse(url, status_code=307)


@router.get("/callback")
def callback(request: Request, code: str | None = None, state: str | None = None,
             error: str | None = None, error_description: str | None = None,
             db: DbSession = Depends(get_db)) -> RedirectResponse:
    """Retour du fournisseur. Redirige toujours vers l'application, pas de JSON.

    C'est le navigateur qui arrive ici, pas le code de l'interface : une erreur
    doit rester lisible dans la page, d'ou le passage par un parametre plutot
    qu'un corps d'erreur que personne n'afficherait.
    """
    _require_oidc()
    home = get_settings().public_base_url.rstrip("/") + "/"

    def echoue(raison: str) -> RedirectResponse:
        logger.warning("connexion OIDC refusee : %s", raison)
        request.session.pop(_PENDING, None)
        return RedirectResponse(f"{home}?auth_error={raison[:200]}", status_code=303)

    if error:
        return echoue(error_description or error)

    pending = request.session.pop(_PENDING, None)
    if not pending or not code or not state:
        return echoue("demande de connexion inconnue ou expiree")
    # Comparaison a temps constant : `state` est un secret de session.
    if not _egal(state, pending.get("state", "")):
        return echoue("etat de connexion invalide")

    try:
        identity = oidc.finish(
            code,
            oidc.Pending(pending["state"], pending["nonce"], pending["code_verifier"]),
        )
    except oidc.OidcError as exc:
        return echoue(str(exc))

    enregistree = {
        "subject": identity.subject,
        "name": identity.name,
        "email": identity.email,
        "groups": identity.groups,
    }
    request.session[SESSION_IDENTITY] = enregistree
    # Sans cette trace, la page Administration n'aurait personne a proposer.
    remember(db, enregistree)
    return RedirectResponse(home, status_code=303)


@router.get("/me", response_model=AuthStatus)
def me(request: Request, user: CurrentUser) -> AuthStatus:
    """Qui je suis, et comment l'application authentifie.

    Toujours 200, jamais 401 : l'interface a besoin de savoir s'il faut
    proposer une connexion, ce qu'un code d'erreur ne dirait pas.
    """
    settings = get_settings()
    session = request.session.get(SESSION_IDENTITY) or {}
    return AuthStatus(
        oidc_enabled=settings.oidc_enabled,
        authenticated=bool(session) or not settings.oidc_enabled,
        subject=user.subject,
        name=user.name,
        email=session.get("email", ""),
        groups=session.get("groups", []),
        role=user.role,
        is_super_admin=user.is_super_admin,
    )


@router.post("/logout")
def logout(request: Request) -> dict[str, str | None]:
    """Ferme la session locale, et indique ou poursuivre chez le fournisseur.

    La deconnexion globale reste au choix de l'interface : sur un poste
    partage on la veut, sur un poste personnel elle obligerait a ressaisir
    son mot de passe partout.
    """
    request.session.clear()
    settings = get_settings()
    url = None
    if settings.oidc_enabled:
        try:
            url = oidc.end_session_url(settings.public_base_url.rstrip("/") + "/")
        except oidc.OidcError:
            url = None
    return {"provider_logout_url": url}


def _egal(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)
