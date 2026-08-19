"""Likes : ce que chacun aime, et ce que le classement en fait.

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
        # Deux albums : « Rare » ne recevra aucun like, « Aime » en recevra.
        for titre, prefixe in (("Aime", "a"), ("Rare", "r")):
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
    # application SANS AUCUNE ROUTE, que tout fichier suivant heriterait.
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


class TestLiker:
    def test_aucun_like_au_depart(self, client):
        assert comme(client, "Adrien").get("/api/likes").json() == []

    def test_liker_puis_delier(self, client):
        assert comme(client, "Adrien").put("/api/likes/1").status_code == 204
        assert comme(client, "Adrien").get("/api/likes").json() == [1]
        assert comme(client, "Adrien").delete("/api/likes/1").status_code == 204
        assert comme(client, "Adrien").get("/api/likes").json() == []

    def test_liker_deux_fois_ne_double_pas(self, client):
        """L'unicite est en base, mais l'API ne doit pas repondre une erreur :
        le resultat voulu est atteint des le premier appel."""
        comme(client, "Adrien").put("/api/likes/1")
        assert comme(client, "Adrien").put("/api/likes/1").status_code == 204
        assert comme(client, "Adrien").get("/api/likes").json() == [1]

    def test_delier_ce_qu_on_n_aimait_pas_est_silencieux(self, client):
        assert comme(client, "Adrien").delete("/api/likes/1").status_code == 204

    def test_titre_inconnu(self, client):
        assert comme(client, "Adrien").put("/api/likes/9999").status_code == 404

    def test_les_likes_sont_personnels(self, client):
        comme(client, "Adrien").put("/api/likes/1")
        comme(client, "Bea").put("/api/likes/2")
        assert comme(client, "Adrien").get("/api/likes").json() == [1]
        assert comme(client, "Bea").get("/api/likes").json() == [2]


class TestMesTitresAimes:
    def test_renvoie_les_titres_complets(self, client):
        comme(client, "Adrien").put("/api/likes/1")
        titres = comme(client, "Adrien").get("/api/likes/tracks").json()
        assert [t["title"] for t in titres] == ["Aime 0"]
        # Nom d'album et d'artiste viennent d'une jointure : c'est justement ce
        # qu'un assembleur maison oublierait.
        assert titres[0]["album_title"] == "Aime" and titres[0]["artist_name"] == "Groupe"

    def test_le_dernier_aime_en_premier(self, client):
        comme(client, "Adrien").put("/api/likes/1")
        comme(client, "Adrien").put("/api/likes/2")
        titres = comme(client, "Adrien").get("/api/likes/tracks").json()
        assert [t["title"] for t in titres] == ["Aime 1", "Aime 0"]

    def test_ne_montre_que_les_miens(self, client):
        comme(client, "Bea").put("/api/likes/1")
        assert comme(client, "Adrien").get("/api/likes/tracks").json() == []


class TestTriParLikes:
    def albums(self, client):
        reponse = comme(client, "Adrien").get("/api/albums", params={"sort": "likes"})
        return [a["title"] for a in reponse.json()["items"]]

    def test_l_album_aime_passe_devant(self, client):
        # Sans like, « Aime » et « Rare » se rangent par artiste puis id.
        comme(client, "Adrien").put("/api/likes/3")  # un titre de « Rare »
        assert self.albums(client) == ["Rare", "Aime"]

    def test_le_classement_additionne_tout_le_monde(self, client):
        comme(client, "Adrien").put("/api/likes/1")  # « Aime »
        comme(client, "Adrien").put("/api/likes/3")  # « Rare »
        comme(client, "Bea").put("/api/likes/4")     # « Rare » encore
        assert self.albums(client) == ["Rare", "Aime"]

    def test_le_nombre_de_titres_reste_juste(self, client):
        """Le piege : joindre les likes multiplierait les lignes `tracks`, et
        le compte de titres de CHAQUE album deviendrait faux."""
        comme(client, "Adrien").put("/api/likes/1")
        comme(client, "Adrien").put("/api/likes/2")
        comme(client, "Bea").put("/api/likes/1")
        reponse = comme(client, "Adrien").get("/api/albums", params={"sort": "likes"})
        assert [a["track_count"] for a in reponse.json()["items"]] == [2, 2]
