"""Les trois sondes : diagnostic humain, vivacite, disponibilite.

Ce qui compte ici n'est pas le corps des reponses mais leur CODE : c'est lui,
et lui seul, que Docker et Kubernetes lisent. Une sonde qui repond 200 quoi
qu'il arrive ne surveille rien.
"""

import importlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.config import get_settings
from app.db import Base, get_db


@pytest.fixture
def application(monkeypatch):
    monkeypatch.setenv("OIDC_ENABLED", "false")
    get_settings.cache_clear()

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)

    # Meme rechargement que dans les autres suites : `main` lit les reglages au
    # montage, et un test de `test_auth` le fait echouer a dessein.
    import app.main

    importlib.reload(app.main)
    yield app.main.app, sessionmaker(bind=engine)
    app.main.app.dependency_overrides.clear()
    get_settings.cache_clear()
    engine.dispose()


@pytest.fixture
def client(application):
    api, Session = application

    def _db():
        with Session() as session:
            yield session

    api.dependency_overrides[get_db] = _db
    return TestClient(api)


class BaseMorte:
    """Une base injoignable : toute requete leve, comme un MariaDB arrete.

    Elle compte ses appels, car une sonde qui interroge la base sans le dire
    reste indetectable par son seul code de reponse — l'erreur est rattrapee.
    """

    def __init__(self) -> None:
        self.appels = 0

    def execute(self, *_args, **_kwargs):
        self.appels += 1
        raise OSError("connexion refusee")


@pytest.fixture
def base_hs(application):
    api, _ = application
    morte = BaseMorte()

    def _db():
        yield morte

    api.dependency_overrides[get_db] = _db
    # `raise_server_exceptions=False` : sans lui, une exception non rattrapee
    # remonterait dans le test au lieu de devenir une reponse, et on ne
    # verrait pas ce que la sonde repond reellement.
    return TestClient(api, raise_server_exceptions=False), morte


class TestVivacite:
    def test_repond_ok(self, client):
        reponse = client.get("/api/health/live")
        assert reponse.status_code == 200
        assert reponse.json() == {"status": "ok"}

    def test_ne_touche_pas_la_base(self, base_hs):
        """Le point entier de cette sonde.

        Si elle interrogeait la base, une panne de MariaDB ferait tuer et
        redemarrer l'API en boucle — sans jamais rien reparer, et en
        l'empechant de reprendre quand la base revient. Le code de reponse ne
        suffit pas a le voir : l'erreur serait rattrapee et la sonde
        repondrait 200 quand meme. On compte donc les appels.
        """
        client, morte = base_hs
        assert client.get("/api/health/live").status_code == 200
        assert morte.appels == 0


class TestDisponibilite:
    def test_ok_quand_la_base_repond(self, client):
        reponse = client.get("/api/health/ready")
        assert reponse.status_code == 200
        assert reponse.json() == {"status": "ok", "database": "ok"}

    def test_503_quand_la_base_est_injoignable(self, base_hs):
        client, morte = base_hs
        reponse = client.get("/api/health/ready")
        assert morte.appels == 1
        assert reponse.status_code == 503
        assert reponse.json()["status"] == "degraded"
        # Le type de l'erreur, jamais son message : celui-ci peut contenir
        # l'hote, le port, voire l'utilisateur de connexion.
        assert reponse.json()["database"] == "OSError"
        assert "refusee" not in reponse.text


class TestDiagnostic:
    def test_route_historique_inchangee(self, client):
        reponse = client.get("/api/health")
        assert reponse.status_code == 200
        assert reponse.json() == {"status": "ok", "database": "ok"}

    def test_reste_a_200_meme_degradee(self, base_hs):
        """Elle est faite pour un humain avec un curl : le corps porte l'etat.

        Elle ne doit surtout pas passer a 503 — le README la documente, et un
        code d'erreur ferait croire a une API en panne alors qu'elle repond.
        """
        client, _ = base_hs
        reponse = client.get("/api/health")
        assert reponse.status_code == 200
        assert reponse.json() == {"status": "degraded", "database": "OSError"}
