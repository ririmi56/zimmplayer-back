"""Client OpenID Connect : decouverte, flux de code, validation du jeton.

Rien n'est specifique a un fournisseur. Seule l'URL de l'emetteur est
configuree ; les points d'entree (autorisation, jetons, JWKS, deconnexion)
sont lus dans son document de decouverte. Authentik, Keycloak, Dex, Zitadel
ou Entra se branchent donc de la meme facon.

Le flux retenu est le code d'autorisation avec PKCE, l'API jouant le client
confidentiel : le navigateur ne recoit jamais de jeton, seulement un cookie
de session signe. C'est ce qui permet au relais audio, qui est une WebSocket,
d'etre authentifie — une WebSocket ouverte par le navigateur ne peut pas
porter d'en-tete Authorization, mais elle porte les cookies.

Aucun jeton n'est conserve apres la connexion : on ne garde que l'identite
validee. Il n'y a donc ni rafraichissement ni jeton au repos, et la session
applicative a sa propre duree de vie.
"""

import base64
import hashlib
import logging
import secrets
import ssl
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from jwt import PyJWKClient

from app.config import get_settings

logger = logging.getLogger(__name__)

_DISCOVERY_PATH = "/.well-known/openid-configuration"
_HTTP_TIMEOUT = 10.0
# Le document de decouverte et les cles bougent rarement, mais ils bougent
# (rotation de cles). On les relit periodiquement plutot qu'une fois pour toutes.
_DISCOVERY_TTL_S = 3600


class OidcError(RuntimeError):
    """Fournisseur injoignable, mal configure, ou reponse refusee."""


@dataclass
class Identity:
    """Ce qu'on retient d'une connexion reussie. Aucun jeton."""

    subject: str
    name: str
    email: str
    groups: list[str] = field(default_factory=list)


def ca_file() -> str:
    """Autorite a utiliser pour joindre le fournisseur.

    Propre a OIDC si elle est renseignee, sinon celle de toute l'application.
    En airgap, le fournisseur est presque toujours signe par la meme autorite
    maison que le reste — d'ou le repli, qui evite de la declarer deux fois.
    """
    settings = get_settings()
    return settings.oidc_ca_file or settings.tls_ca_file


def _echec_reseau(exc: Exception, quoi: str) -> OidcError:
    """Traduit une erreur reseau, en nommant la piste quand c'est le certificat.

    httpx enveloppe les erreurs TLS dans ses propres exceptions : sans
    remonter la chaine des causes, un certificat refuse ressort en simple
    « connexion impossible », et l'on cherche du cote du reseau une heure
    durant alors qu'il manque une autorite.
    """
    cause: BaseException | None = exc
    while cause is not None:
        if isinstance(cause, ssl.SSLCertVerificationError):
            reglage = "OIDC_CA_FILE ou TLS_CA_FILE"
            return OidcError(
                f"{quoi} : le certificat du fournisseur est refuse "
                f"({cause.verify_message}). Sur un reseau airgap, renseigner "
                f"{reglage} avec l'autorite qui l'a signe."
            )
        cause = cause.__cause__ or cause.__context__
    return OidcError(f"{quoi} : {exc}")


def _client() -> httpx.Client:
    """Client HTTP vers le fournisseur, avec le bon magasin de certificats.

    `verify` recoit un chemin ou True : jamais False. Desactiver la
    verification ici laisserait n'importe quel serveur du chemin se faire
    passer pour le fournisseur d'identite, donc forger des connexions.
    """
    return httpx.Client(timeout=_HTTP_TIMEOUT, verify=ca_file() or True)


_discovery: tuple[float, dict[str, Any]] | None = None
_jwks: tuple[str, PyJWKClient] | None = None


def discover(force: bool = False) -> dict[str, Any]:
    """Document de decouverte du fournisseur, mis en cache."""
    global _discovery
    if not force and _discovery is not None and time.monotonic() < _discovery[0]:
        return _discovery[1]

    issuer = get_settings().oidc_issuer.rstrip("/")
    if not issuer:
        raise OidcError("OIDC_ISSUER n'est pas renseigne")

    url = issuer + _DISCOVERY_PATH
    try:
        with _client() as client:
            response = client.get(url)
            response.raise_for_status()
            document = response.json()
    except httpx.HTTPError as exc:
        raise _echec_reseau(exc, f"decouverte OIDC impossible sur {url}") from exc

    # L'emetteur annonce doit etre celui qu'on a configure, sinon on suivrait
    # un fournisseur different de celui qu'on croit interroger.
    if document.get("issuer", "").rstrip("/") != issuer:
        raise OidcError(
            f"le fournisseur se declare emetteur de {document.get('issuer')!r}, "
            f"pas de {issuer!r}"
        )
    for required in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not document.get(required):
            raise OidcError(f"le document de decouverte n'expose pas {required}")

    _discovery = (time.monotonic() + _DISCOVERY_TTL_S, document)
    return document


def _jwk_client() -> PyJWKClient:
    """Cache des cles publiques du fournisseur, avec le meme contexte TLS."""
    global _jwks
    uri = discover()["jwks_uri"]
    if _jwks is not None and _jwks[0] == uri:
        return _jwks[1]

    context = ssl.create_default_context(cafile=ca_file() or None)
    client = PyJWKClient(uri, cache_keys=True, ssl_context=context)
    _jwks = (uri, client)
    return client


def reset_cache() -> None:
    """Oublie decouverte et cles. Appele quand la configuration change."""
    global _discovery, _jwks
    _discovery = _jwks = None


def redirect_uri() -> str:
    return get_settings().public_base_url.rstrip("/") + "/api/auth/callback"


@dataclass(frozen=True)
class Pending:
    """Ce qu'il faut retenir entre la redirection et le retour du fournisseur."""

    state: str
    nonce: str
    code_verifier: str


def start() -> tuple[str, Pending]:
    """URL vers laquelle envoyer le navigateur, et le secret a lui associer.

    PKCE (S256) protege l'echange meme si le code d'autorisation fuite dans un
    journal ou un en-tete Referer : sans le verificateur, il est inutilisable.
    `state` protege du CSRF sur le retour, `nonce` lie le jeton d'identite a
    CETTE demande, ce qui interdit de rejouer un jeton obtenu ailleurs.
    """
    settings = get_settings()
    pending = Pending(
        state=secrets.token_urlsafe(32),
        nonce=secrets.token_urlsafe(32),
        code_verifier=secrets.token_urlsafe(64),
    )
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(pending.code_verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    query = urlencode(
        {
            "response_type": "code",
            "client_id": settings.oidc_client_id,
            "redirect_uri": redirect_uri(),
            "scope": settings.oidc_scopes,
            "state": pending.state,
            "nonce": pending.nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{discover()['authorization_endpoint']}?{query}", pending


def finish(code: str, pending: Pending) -> Identity:
    """Echange le code contre les jetons, valide l'identite, et n'en garde rien d'autre."""
    settings = get_settings()
    document = discover()
    try:
        with _client() as client:
            response = client.post(
                document["token_endpoint"],
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri(),
                    "client_id": settings.oidc_client_id,
                    "client_secret": settings.oidc_client_secret,
                    "code_verifier": pending.code_verifier,
                },
                headers={"Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise _echec_reseau(exc, "echange du code impossible") from exc

    if response.status_code != 200:
        # Le corps d'erreur OAuth nomme la cause (redirect_uri_mismatch,
        # invalid_client...) : la citer evite une chasse a l'aveugle.
        raise OidcError(f"le fournisseur a refuse l'echange : {response.text[:300]}")

    id_token = response.json().get("id_token")
    if not id_token:
        raise OidcError("la reponse du fournisseur ne contient pas d'id_token")

    return identity_from_token(id_token, pending.nonce)


def identity_from_token(id_token: str, nonce: str | None) -> Identity:
    """Valide la signature et les revendications, puis en tire l'identite."""
    settings = get_settings()
    document = discover()
    try:
        key = _jwk_client().get_signing_key_from_jwt(id_token).key
    except jwt.PyJWKClientError as exc:
        raise _echec_reseau(exc, "cles publiques du fournisseur illisibles") from exc
    try:
        claims = jwt.decode(
            id_token,
            key,
            algorithms=document.get("id_token_signing_alg_values_supported", ["RS256"]),
            audience=settings.oidc_client_id,
            issuer=document["issuer"],
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise OidcError(f"jeton d'identite refuse : {exc}") from exc

    # Le nonce est verifie a la main : PyJWT ne connait pas cette revendication,
    # propre a OIDC. Sans ce controle, un jeton valide obtenu pour une autre
    # session pourrait etre rejoue sur celle-ci.
    if nonce is not None and claims.get("nonce") != nonce:
        raise OidcError("le nonce du jeton ne correspond pas a la demande")

    groups = claims.get(settings.oidc_groups_claim) or []
    if isinstance(groups, str):
        groups = [groups]

    subject = claims["sub"]
    return Identity(
        subject=subject,
        # Ordre de preference : nom affichable, puis identifiant lisible, puis
        # le sujet — opaque, mais on ne laisse jamais un utilisateur sans nom.
        name=claims.get("name") or claims.get("preferred_username") or claims.get("email") or subject,
        email=claims.get("email") or "",
        groups=[str(group) for group in groups],
    )


def end_session_url(post_logout: str) -> str | None:
    """Deconnexion aupres du fournisseur, s'il l'expose. Facultatif en OIDC."""
    endpoint = discover().get("end_session_endpoint")
    if not endpoint:
        return None
    return f"{endpoint}?{urlencode({'post_logout_redirect_uri': post_logout, 'client_id': get_settings().oidc_client_id})}"
