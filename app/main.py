import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api import admin, catalog, edit, sessions, snapcast, stream
from app.config import get_settings
from app.db import SessionLocal, engine
from app.services import snapoutput

# Sans cela, les journaux de l'application (scan, sortie snapcast) n'apparaissent
# nulle part : uvicorn ne configure que ses propres loggers.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Chaque session doit retrouver son flux Snapcast au redemarrage, sinon sa
    configuration pointerait vers un port que plus personne n'ecoute."""
    with SessionLocal() as db:
        try:
            snapoutput.restore(db)
        except Exception:
            logger.exception("restauration des sorties snapcast impossible")
    yield
    snapoutput.shutdown()


app = FastAPI(
    title="Zimmplayer API",
    version="0.1.0",
    openapi_url="/api/openapi.json",
    docs_url="/api/docs",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(catalog.router)
app.include_router(stream.router)
app.include_router(edit.router)
app.include_router(sessions.router)
app.include_router(snapcast.router)
app.include_router(admin.router)


@app.get("/api/health")
def health() -> dict[str, str]:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - depend de l'infra
        return {"status": "degraded", "database": type(exc).__name__}
    return {"status": "ok", "database": "ok"}
