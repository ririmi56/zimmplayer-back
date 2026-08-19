"""Ce qui compte comme une ecoute, et qui en est credite."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.config import get_settings
from app.db import Base, get_db
from app.models import Album, Artist, Listen, Session, SessionPresence, Track, User, utcnow
from app.services import stats


class TestSeuil:
    """Regle de Last.fm : la moitie du titre, ou quatre minutes."""

    @pytest.mark.parametrize(
        "duree,attendu",
        [
            (180, 90),          # un titre court : la moitie
            (600, 240),         # un titre long : le plafond, pas la moitie
            (480, 240),         # exactement au basculement
            (10, 5),
        ],
    )
    def test_moitie_ou_plafond(self, duree, attendu):
        assert stats.seuil(duree) == attendu

    @pytest.mark.parametrize("duree", [None, 0])
    def test_duree_inconnue_retombe_sur_le_plafond(self, duree):
        """Compter des la premiere seconde ferait d'un titre sans duree un
        moyen de gonfler les compteurs."""
        assert stats.seuil(duree) == stats.PLAFOND_S


@pytest.fixture
def base():
    moteur = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(moteur)
    Fabrique = sessionmaker(bind=moteur)
    with Fabrique() as db:
        artiste = Artist(name="Groupe")
        db.add(artiste)
        db.flush()
        album = Album(artist_id=artiste.id, source_title="A", title="A")
        db.add(album)
        db.flush()
        db.add(Track(album_id=album.id, artist_id=artiste.id, object_key="k",
                     object_key_hash="h", source_title="T", title="T",
                     etag="e", size_bytes=1, duration_s=200))
        db.add_all([User(subject="a", name="Adrien"), User(subject="b", name="Bea"),
                    User(subject="c", name="Absent")])
        db.add(Session(name="Salon", created_by="Adrien"))
        db.commit()
    yield Fabrique
    moteur.dispose()


class TestPresence:
    def test_seuls_les_vus_recemment_sont_presents(self, base):
        with base() as db:
            stats.marquer_present(db, 1, 1)
            stats.marquer_present(db, 1, 2)
            # Quelqu'un vu il y a longtemps ne compte plus.
            vieux = db.scalar(select(SessionPresence).where(SessionPresence.user_id == 2))
            vieux.last_seen_at = utcnow() - timedelta(seconds=stats.PRESENCE_S + 30)
            db.commit()
            assert stats.presents(db, 1) == [1]

    def test_la_presence_n_est_pas_reecrite_a_chaque_appel(self, base):
        """Le navigateur interroge la session toutes les 1,5 s : sans ce
        garde-fou, chaque membre provoquerait une ecriture aussi souvent."""
        with base() as db:
            stats.marquer_present(db, 1, 1)
            avant = db.scalar(select(SessionPresence.last_seen_at))
            stats.marquer_present(db, 1, 1)
            assert db.scalar(select(SessionPresence.last_seen_at)) == avant


class TestAttribution:
    def test_tout_le_monde_est_credite_dans_une_session(self, base):
        """C'est le choix retenu : on mesure ce que chacun a ecoute, pas ce
        qu'il a fait jouer aux autres."""
        with base() as db:
            session = db.get(Session, 1)
            stats.enregistrer_ecoute(db, [1, 2], 1, 120.0, session)
            lignes = db.scalars(select(Listen)).all()
            assert {l.user_id for l in lignes} == {1, 2}
            assert all(l.seconds == 120.0 for l in lignes)

    def test_l_ecoute_solo_n_a_pas_de_session(self, base):
        with base() as db:
            stats.enregistrer_ecoute(db, [1], 1, 120.0)
            ligne = db.scalar(select(Listen))
            assert ligne.session_id is None and ligne.session_name is None

    def test_le_nom_de_session_survit_a_sa_suppression(self, base):
        """Sans cette copie, supprimer une session emporterait son historique."""
        with base() as db:
            session = db.get(Session, 1)
            stats.enregistrer_ecoute(db, [1], 1, 120.0, session)
            db.delete(session)
            db.commit()
            resume = stats.par_session(db)
            assert [(r["name"], r["still_open"]) for r in resume] == [("Salon", False)]


@pytest.fixture
def client(monkeypatch, base):
    monkeypatch.setenv("OIDC_ENABLED", "false")
    get_settings.cache_clear()
    import importlib

    import app.main

    importlib.reload(app.main)
    application = app.main.app

    def _db():
        db = base()
        try:
            yield db
        finally:
            db.close()

    application.dependency_overrides[get_db] = _db
    yield TestClient(application)
    get_settings.cache_clear()


class TestAnnonceDuNavigateur:
    def test_sous_le_seuil_rien_n_est_compte(self, client, base):
        """Hors session, seules les valeurs du navigateur sont disponibles :
        le seuil est donc reapplique ici."""
        r = client.post("/api/stats/listens", json={"track_id": 1, "seconds": 30})
        assert r.status_code == 204
        with base() as db:
            assert db.scalar(select(Listen)) is None

    def test_au_dessus_du_seuil_l_ecoute_compte(self, client, base):
        assert client.post("/api/stats/listens",
                           json={"track_id": 1, "seconds": 150}).status_code == 204
        with base() as db:
            assert db.scalar(select(Listen)).seconds == 150

    def test_la_duree_annoncee_est_bornee_par_le_titre(self, client, base):
        """Un client qui annonce dix heures sur un titre de trois minutes ne
        doit pas gonfler le total."""
        client.post("/api/stats/listens", json={"track_id": 1, "seconds": 36000})
        with base() as db:
            assert db.scalar(select(Listen)).seconds == 200

    def test_piste_inconnue(self, client):
        assert client.post("/api/stats/listens",
                           json={"track_id": 999, "seconds": 150}).status_code == 404


class TestCatalogue:
    def test_totaux(self, client):
        c = client.get("/api/stats").json()["catalogue"]
        assert c["tracks"] == 1 and c["albums"] == 1 and c["artists"] == 1
        assert c["total_seconds"] == 200
        assert c["tracks_without_duration"] == 0

    def test_les_statistiques_globales_sont_visibles_par_tous(self, client):
        assert client.get("/api/stats").status_code == 200
