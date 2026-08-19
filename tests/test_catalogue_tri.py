"""Tri de la liste des albums, et son retournement.

Le nom du fichier place cette suite entre `test_auth` et `test_oidc` : elle
DOIT donc recharger `app.main` elle-meme, comme les autres. Un test de
`test_auth` fait echouer ce rechargement a dessein et laisse le module avec
une application sans aucune route, dont heriterait tout fichier suivant qui
ne recharge pas.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.config import get_settings
from app.db import Base, get_db
from app.models import Album, Artist, Track

#: (artiste, titre, annee, genre) — volontairement melanges, et deux albums
#: sans annee ni genre pour eprouver la place des vides.
CATALOGUE = [
    ("Bea", "Cendres", 1998, "Rock"),
    ("Ana", "Zenith", 2015, "Jazz"),
    ("Cyd", "Aube", None, None),
    ("Ana", "Brume", 2003, "Ambient"),
]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("OIDC_ENABLED", "false")
    get_settings.cache_clear()

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    with Session() as db:
        artistes: dict[str, Artist] = {}
        for index, (nom, titre, annee, genre) in enumerate(CATALOGUE):
            if nom not in artistes:
                artistes[nom] = Artist(name=nom)
                db.add(artistes[nom])
                db.flush()
            album = Album(
                artist_id=artistes[nom].id, source_title=titre, title=titre,
                source_year=annee, year=annee,
            )
            db.add(album)
            db.flush()
            db.add(Track(
                album_id=album.id, artist_id=artistes[nom].id,
                object_key=f"k{index}", object_key_hash=f"h{index}",
                source_title=f"{titre} 1", title=f"{titre} 1", track_no=1,
                genre=genre, etag=f"e{index}", size_bytes=1,
            ))
        db.commit()

    import importlib

    import app.main

    importlib.reload(app.main)
    application = app.main.app

    def _db():
        with Session() as session:
            yield session

    application.dependency_overrides[get_db] = _db
    yield TestClient(application)
    application.dependency_overrides.clear()
    get_settings.cache_clear()
    engine.dispose()


def titres(client, **params):
    params.setdefault("limit", 10)
    reponse = client.get("/api/albums", params=params, headers={"X-User-Name": "Adrien"})
    assert reponse.status_code == 200, reponse.text
    return [a["title"] for a in reponse.json()["items"]]


class TestSensNaturel:
    """Ce que chaque tri fait sans qu'on lui demande rien : la reference."""

    def test_titre_de_a_a_z(self, client):
        assert titres(client, sort="titre") == ["Aube", "Brume", "Cendres", "Zenith"]

    def test_annee_la_plus_recente_en_premier(self, client):
        assert titres(client, sort="annee") == ["Zenith", "Brume", "Cendres", "Aube"]

    def test_artiste_puis_annee(self, client):
        assert titres(client, sort="artiste") == ["Brume", "Zenith", "Cendres", "Aube"]


class TestRetournement:
    def test_titre(self, client):
        assert titres(client, sort="titre", reverse=True) == [
            "Zenith", "Cendres", "Brume", "Aube",
        ]

    def test_annee(self, client):
        # « Aube » n'a pas d'annee : elle reste derriere, dans les deux sens.
        assert titres(client, sort="annee", reverse=True) == [
            "Cendres", "Brume", "Zenith", "Aube",
        ]

    def test_genre_les_sans_genre_restent_derriere(self, client):
        naturel = titres(client, sort="genre")
        inverse = titres(client, sort="genre", reverse=True)
        assert naturel[-1] == "Aube" and inverse[-1] == "Aube"
        assert naturel[:-1] == list(reversed(inverse[:-1]))

    def test_seule_la_cle_principale_se_retourne(self, client):
        """Inverser « Artiste » remonte les Z, mais ne rejoue pas les
        discographies a l'envers : Ana garde Brume (2003) avant Zenith (2015)."""
        assert titres(client, sort="artiste", reverse=True) == [
            "Aube", "Cendres", "Brume", "Zenith",
        ]

    def test_par_defaut_on_ne_retourne_rien(self, client):
        assert titres(client, sort="titre") == titres(client, sort="titre", reverse=False)


class TestPagination:
    def test_le_retournement_ne_perd_ni_ne_double_aucun_album(self, client):
        """Le defilement infini pagine : sans depart unique, un album pourrait
        apparaitre deux fois et un autre jamais."""
        for reverse in (False, True):
            page1 = titres(client, sort="genre", reverse=reverse, limit=2, offset=0)
            page2 = titres(client, sort="genre", reverse=reverse, limit=2, offset=2)
            assert sorted(page1 + page2) == sorted(t for _, t, _, _ in CATALOGUE)
