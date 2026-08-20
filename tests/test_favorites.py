"""Favoris d'albums : ce que chacun garde sous la main, et le classement.

Base SQLite ephemere et mode sans OIDC, comme les autres suites : l'identite
se resume alors a l'en-tete X-User-Name, ce qui permet de changer de personne
d'une requete a l'autre.
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
        artist = Artist(name="Groupe")
        db.add(artist)
        db.flush()
        # Deux albums de deux titres : « Garde » (id 1) et « Ignore » (id 2).
        for titre, prefixe in (("Garde", "g"), ("Ignore", "i")):
            album = Album(artist_id=artist.id, source_title=titre, title=titre)
            db.add(album)
            db.flush()
            for i in range(2):
                db.add(Track(
                    album_id=album.id, artist_id=artist.id,
                    object_key=f"{prefixe}{i}", object_key_hash=f"h{prefixe}{i}",
                    source_title=f"{titre} {i}", title=f"{titre} {i}", track_no=i + 1,
                    etag=f"e{prefixe}{i}", size_bytes=1,
                ))
        db.commit()

    # Rechargement obligatoire, comme dans les autres suites : `main` lit les
    # reglages au montage, et surtout un test de `test_auth` fait echouer ce
    # meme rechargement a dessein — il laisse alors le module avec une
    # application SANS AUCUNE ROUTE. Ce fichier-ci se nomme entre `test_auth`
    # et `test_oidc` : sans ce rechargement il verrait des 404 partout.
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


def comme(client, qui):
    client.headers["X-User-Name"] = qui
    return client


class TestMettreEnFavori:
    def test_aucun_favori_au_depart(self, client):
        assert comme(client, "Adrien").get("/api/favorites").json() == []

    def test_mettre_puis_retirer(self, client):
        assert comme(client, "Adrien").put("/api/favorites/1").status_code == 204
        assert comme(client, "Adrien").get("/api/favorites").json() == [1]
        assert comme(client, "Adrien").delete("/api/favorites/1").status_code == 204
        assert comme(client, "Adrien").get("/api/favorites").json() == []

    def test_deux_fois_ne_double_pas(self, client):
        """L'unicite est en base, mais l'API ne doit pas repondre une erreur :
        le resultat voulu est atteint des le premier appel."""
        comme(client, "Adrien").put("/api/favorites/1")
        assert comme(client, "Adrien").put("/api/favorites/1").status_code == 204
        assert comme(client, "Adrien").get("/api/favorites").json() == [1]

    def test_retirer_ce_qui_n_y_etait_pas_est_silencieux(self, client):
        assert comme(client, "Adrien").delete("/api/favorites/1").status_code == 204

    def test_album_inconnu(self, client):
        assert comme(client, "Adrien").put("/api/favorites/9999").status_code == 404

    def test_les_favoris_sont_personnels(self, client):
        comme(client, "Adrien").put("/api/favorites/1")
        comme(client, "Bea").put("/api/favorites/2")
        assert comme(client, "Adrien").get("/api/favorites").json() == [1]
        assert comme(client, "Bea").get("/api/favorites").json() == [2]


class TestFiltrerSurMesFavoris:
    def albums(self, client, qui, **params):
        reponse = comme(client, qui).get("/api/albums", params=params)
        return [a["title"] for a in reponse.json()["items"]]

    def test_sans_filtre_tout_le_catalogue(self, client):
        assert self.albums(client, "Adrien") == ["Garde", "Ignore"]

    def test_le_filtre_ne_garde_que_les_miens(self, client):
        comme(client, "Adrien").put("/api/favorites/1")
        assert self.albums(client, "Adrien", favoris=True) == ["Garde"]

    def test_le_filtre_est_personnel(self, client):
        """Le tri compte tout le monde, mais le filtre est a soi : les favoris
        de Bea ne doivent pas remplir la bibliotheque d'Adrien."""
        comme(client, "Bea").put("/api/favorites/1")
        assert self.albums(client, "Adrien", favoris=True) == []

    def test_le_total_suit_le_filtre(self, client):
        """Le defilement infini s'arrete sur `total` : s'il comptait tout le
        catalogue, il redemanderait indefiniment des pages vides."""
        comme(client, "Adrien").put("/api/favorites/1")
        reponse = comme(client, "Adrien").get("/api/albums", params={"favoris": True})
        assert reponse.json()["total"] == 1


class TestTriParFavoris:
    def albums(self, client):
        reponse = comme(client, "Adrien").get("/api/albums", params={"sort": "favoris"})
        return [a["title"] for a in reponse.json()["items"]]

    def test_l_album_en_favori_passe_devant(self, client):
        # Sans favori, « Garde » et « Ignore » se rangent par artiste puis id.
        comme(client, "Adrien").put("/api/favorites/2")  # « Ignore »
        assert self.albums(client) == ["Ignore", "Garde"]

    def test_le_classement_compte_les_personnes(self, client):
        comme(client, "Adrien").put("/api/favorites/1")
        comme(client, "Adrien").put("/api/favorites/2")
        comme(client, "Bea").put("/api/favorites/2")
        assert self.albums(client) == ["Ignore", "Garde"]

    def test_ne_se_confond_pas_avec_les_likes_de_titres(self, client):
        """Les deux classements mesurent des choses differentes : trois likes
        sur les titres de « Ignore » ne doivent pas le faire passer devant un
        album mis en favori."""
        comme(client, "Adrien").put("/api/likes/3")
        comme(client, "Adrien").put("/api/likes/4")
        comme(client, "Bea").put("/api/likes/3")
        comme(client, "Adrien").put("/api/favorites/1")  # « Garde »
        assert self.albums(client) == ["Garde", "Ignore"]

    def test_le_nombre_de_titres_reste_juste(self, client):
        """Le piege : joindre les favoris multiplierait les lignes `tracks`, et
        le compte de titres de CHAQUE album deviendrait faux."""
        comme(client, "Adrien").put("/api/favorites/1")
        comme(client, "Bea").put("/api/favorites/1")
        reponse = comme(client, "Adrien").get("/api/albums", params={"sort": "favoris"})
        assert [a["track_count"] for a in reponse.json()["items"]] == [2, 2]
