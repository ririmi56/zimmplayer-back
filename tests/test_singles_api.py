"""Albums et singles sont deux sorties distinctes dans l'API.

Un single zimmporter est un album a part entiere (un fichier = une sortie),
mais il ne doit ni se confondre avec les albums dans les listes, ni gonfler le
compteur d'albums des statistiques.
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
from app.models import Album, Artist, Track


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
        artist = Artist(name="Daft Punk")
        db.add(artist)
        db.flush()
        for index, (titre, release_type) in enumerate(
            [("Homework", "album"), ("One More Time", "single")]
        ):
            album = Album(
                artist_id=artist.id,
                source_title=titre,
                title=titre,
                release_type=release_type,
            )
            db.add(album)
            db.flush()
            db.add(
                Track(
                    album_id=album.id,
                    artist_id=artist.id,
                    source_title=f"{titre} 1",
                    title=f"{titre} 1",
                    object_key=f"k{index}",
                    object_key_hash=f"h{index}",
                    etag=f"e{index}",
                    size_bytes=1,
                )
            )
        db.commit()
        artist_id = artist.id

    import app.main

    importlib.reload(app.main)
    application = app.main.app

    def _db():
        with Session() as session:
            yield session

    application.dependency_overrides[get_db] = _db
    test_client = TestClient(application)
    test_client.artist_id = artist_id
    yield test_client
    application.dependency_overrides.clear()
    get_settings.cache_clear()
    engine.dispose()


def titres(client, **params):
    params.setdefault("limit", 10)
    reponse = client.get("/api/albums", params=params, headers={"X-User-Name": "Adrien"})
    assert reponse.status_code == 200, reponse.text
    return [album["title"] for album in reponse.json()["items"]]


class TestFiltreReleaseType:
    def test_defaut_tout_le_catalogue(self, client):
        """L'API reste compatible : sans filtre, albums et singles sont la."""
        assert titres(client) == ["Homework", "One More Time"]

    def test_albums_seulement(self, client):
        assert titres(client, release_type="album") == ["Homework"]

    def test_singles_seulement(self, client):
        assert titres(client, release_type="single") == ["One More Time"]

    def test_chaque_resultat_porte_son_type(self, client):
        reponse = client.get(
            "/api/albums", params={"limit": 10}, headers={"X-User-Name": "Adrien"}
        )
        types = {a["title"]: a["release_type"] for a in reponse.json()["items"]}
        assert types == {"Homework": "album", "One More Time": "single"}


class TestArtistes:
    def test_compte_albums_et_singles_separement(self, client):
        reponse = client.get("/api/artists", headers={"X-User-Name": "Adrien"})
        assert reponse.status_code == 200, reponse.text
        artiste = reponse.json()["items"][0]
        assert artiste["album_count"] == 1
        assert artiste["single_count"] == 1

    def test_detail_separe_les_deux_sorties(self, client):
        reponse = client.get(
            f"/api/artists/{client.artist_id}", headers={"X-User-Name": "Adrien"}
        )
        assert reponse.status_code == 200, reponse.text
        body = reponse.json()
        sorties = {a["title"]: a["release_type"] for a in body["albums"]}
        assert sorties == {"Homework": "album", "One More Time": "single"}
        assert body["single_count"] == 1


class TestStatistiques:
    def test_les_singles_ne_gonflent_pas_le_nombre_d_albums(self, client):
        reponse = client.get("/api/stats", headers={"X-User-Name": "Adrien"})
        assert reponse.status_code == 200, reponse.text
        catalogue = reponse.json()["catalogue"]
        assert catalogue["albums"] == 1
        assert catalogue["singles"] == 1
        assert catalogue["tracks"] == 2


class TestRecherche:
    def test_un_single_est_trouve_avec_son_type(self, client, monkeypatch):
        # SQLite ne connait pas MATCH ... AGAINST : on force le repli LIKE pour
        # eprouver la forme de la reponse, pas le moteur FULLTEXT de MariaDB.
        import app.api.catalog as catalog_module

        monkeypatch.setattr(catalog_module, "_FT_MIN_TOKEN", 10)
        reponse = client.get(
            "/api/search", params={"q": "One More"}, headers={"X-User-Name": "Adrien"}
        )
        assert reponse.status_code == 200, reponse.text
        albums = {a["title"]: a["release_type"] for a in reponse.json()["albums"]}
        assert albums == {"One More Time": "single"}
