import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api import (
    admin,
    auth,
    catalog,
    edit,
    favorites,
    likes,
    playlists,
    sessions,
    snapcast,
    stats,
    stream,
)
from app.config import get_settings
from app.db import SessionLocal, get_db
from app.services import snapoutput

# Sans cela, les journaux de l'application (scan, sortie snapcast) n'apparaissent
# nulle part : uvicorn ne configure que ses propres loggers.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)
settings = get_settings()


def _avertir_si_aucun_administrateur(db) -> None:
    """Signale une installation que personne ne peut administrer.

    Le cas se produit des qu'OIDC est actif sans SUPER_ADMINS et sans aucune
    promotion en base : la page Administration devient alors inaccessible a
    tout le monde, et il faut passer par la base pour reprendre la main. On
    n'empeche pas le demarrage — refuser de demarrer sur une edition de
    configuration serait pire — mais on le dit fort.
    """
    settings = get_settings()
    if not settings.oidc_enabled:
        return
    from sqlalchemy import select

    from app.auth import is_super_admin
    from app.models import User

    if db.scalar(select(User.id).where(User.is_admin).limit(1)):
        return
    connus = db.execute(select(User.subject, User.email)).all()
    if any(is_super_admin(subject, email) for subject, email in connus):
        return
    logger.error(
        "Aucun administrateur : SUPER_ADMINS ne designe personne de connu et "
        "aucun compte n'est promu en base. La page Administration sera "
        "inaccessible a tous. Renseigner SUPER_ADMINS (sub ou courriel) et "
        "redemarrer."
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Chaque session doit retrouver son flux Snapcast au redemarrage, sinon sa
    configuration pointerait vers un port que plus personne n'ecoute."""
    with SessionLocal() as db:
        _avertir_si_aucun_administrateur(db)
        try:
            snapoutput.restore(db)
        except Exception:
            logger.exception("restauration des sorties snapcast impossible")
    yield
    snapoutput.shutdown()


app = FastAPI(
    title="Zimmplayer API",
    version="1.0.0",
    openapi_url="/api/openapi.json",
    docs_url="/api/docs",
    lifespan=lifespan,
)

# Session applicative : cookie signe, jamais lisible ni forgeable par le
# navigateur. Monte meme sans OIDC, pour que `request.session` existe partout
# et que le mode ne se lise qu'a un seul endroit (app/auth.py).
#
# Sans OIDC la session ne contient rien : la cle de repli ne protege donc
# aucun secret. Des qu'OIDC est active, SESSION_SECRET est exige au demarrage
# (voir ci-dessous) — une cle connue laisserait forger l'identite de n'importe
# qui, ce qui reviendrait a n'avoir pas d'authentification du tout.
if settings.oidc_enabled and not settings.session_secret:
    raise RuntimeError(
        "SESSION_SECRET est obligatoire quand OIDC_ENABLED vaut true : "
        "sans cle propre, le cookie de session pourrait etre forge."
    )

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret or "sans-oidc-la-session-reste-vide",
    session_cookie="zimmplayer_session",
    max_age=settings.session_max_age_s,
    same_site="lax",
    # Le flux OIDC revient par une redirection du fournisseur : `strict`
    # empecherait le cookie d'accompagner ce retour, et la connexion
    # echouerait sans message comprehensible.
    https_only=settings.public_base_url.startswith("https://"),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(auth.router)
app.include_router(auth.directory)
app.include_router(catalog.router)
app.include_router(playlists.router)
app.include_router(likes.router)
app.include_router(favorites.router)
app.include_router(stats.router)
app.include_router(stream.router)
app.include_router(edit.router)
app.include_router(sessions.router)
app.include_router(snapcast.router)
app.include_router(admin.router)


def _etat_base(db: Session) -> tuple[bool, str]:
    """Vrai si la base repond, avec le nom de l'erreur sinon."""
    try:
        db.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - depend de l'infra
        return False, type(exc).__name__
    return True, "ok"


@app.get("/api/health")
def health(db: Session = Depends(get_db)) -> dict[str, str]:
    """Diagnostic humain : repond toujours 200, l'etat est dans le corps.

    C'est la route documentee depuis toujours ; les orchestrateurs, eux,
    utilisent /api/health/live et /api/health/ready, qui repondent par leur
    code HTTP.
    """
    ok, base = _etat_base(db)
    return {"status": "ok" if ok else "degraded", "database": base}


@app.get("/api/health/live")
def live() -> dict[str, str]:
    """Le processus repond-il encore ?

    Volontairement sans aucune dependance : une sonde de vivacite qui touche
    la base ferait TUER l'API a chaque indisponibilite de MariaDB, alors que
    le redemarrage n'y changerait rien — et le redemarrage en boucle
    empecherait la reprise au retour de la base.
    """
    return {"status": "ok"}


@app.get("/api/health/ready")
def ready(response: Response, db: Session = Depends(get_db)) -> dict[str, str]:
    """Peut-on lui envoyer du trafic ?

    Presque toutes les routes lisent la base : sans elle, l'API repondrait des
    erreurs a tout le monde. Un 503 la retire du service le temps que la base
    revienne, sans que le processus soit tue.
    """
    ok, base = _etat_base(db)
    if not ok:
        response.status_code = 503
    return {"status": "ok" if ok else "degraded", "database": base}
