"""Acces a la page Administration, et attribution du role.

Base SQLite ephemere : ces tests ecrivent des utilisateurs, ils ne doivent
toucher aucune base reelle.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (enregistre les tables)
from app.config import get_settings
from app.db import Base, get_db
from app.models import Album, Artist, Listen, Playlist, Track
from app.models import User as UserRow


@pytest.fixture
def base():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    # SQLite ignore les cles etrangeres tant qu'on ne le lui demande pas, et
    # c'est le serveur qui applique ici les regles de suppression : sans ce
    # PRAGMA, supprimer un compte laisserait ses playlists et ses ecoutes
    # intactes dans la base de test, et les tests attesteraient d'un
    # comportement que MariaDB n'a pas.
    @event.listens_for(engine, "connect")
    def _cles_etrangeres(connexion, _record):  # pragma: no cover - branchement
        connexion.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    yield Session
    engine.dispose()


@pytest.fixture
def app_test(monkeypatch, base):
    """Application configuree en OIDC, branchee sur la base ephemere."""

    def _app(session_identite: dict | None, **env):
        env.setdefault("oidc_enabled", "true")
        env.setdefault("session_secret", "cle-de-test")
        for key, value in env.items():
            monkeypatch.setenv(key.upper(), value)
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

        client = TestClient(application)
        if session_identite is not None:
            # Ecrit une vraie session signee, en passant par le middleware.
            @application.get("/_test/ouvrir")
            def _ouvrir(request):  # pragma: no cover - utilitaire de test
                request.session["identity"] = session_identite
                return {}

        return client

    yield _app
    get_settings.cache_clear()


def peupler(base, *utilisateurs):
    with base() as db:
        for subject, name, email, admin in utilisateurs:
            db.add(UserRow(subject=subject, name=name, email=email, is_admin=admin))
        db.commit()


def connecter(client, subject, email=""):
    """Ouvre une session en injectant l'identite dans le cookie signe."""
    from itsdangerous import TimestampSigner
    import base64
    import json

    from app.config import get_settings as reglages

    data = json.dumps({"identity": {"subject": subject, "name": subject, "email": email, "groups": []}})
    signe = TimestampSigner(reglages().session_secret).sign(base64.b64encode(data.encode()))
    client.cookies.set("zimmplayer_session", signe.decode())


class TestAccesReserve:
    def test_un_non_administrateur_est_refuse(self, app_test, base):
        """« pas admin = pas acces a cette page », cote serveur et pas
        seulement en masquant un onglet."""
        peupler(base, ("u-1", "Simple", "s@x", False))
        client = app_test(None, super_admins="")
        connecter(client, "u-1", "s@x")
        assert client.get("/api/admin/users").status_code == 403
        assert client.get("/api/admin/scan/status").status_code == 403

    def test_un_super_administrateur_passe(self, app_test, base):
        peupler(base, ("u-1", "Chef", "chef@x", False))
        client = app_test(None, super_admins="chef@x")
        connecter(client, "u-1", "chef@x")
        assert client.get("/api/admin/users").status_code == 200

    def test_un_administrateur_promu_passe(self, app_test, base):
        peupler(base, ("u-2", "Promu", "p@x", True))
        client = app_test(None, super_admins="")
        connecter(client, "u-2", "p@x")
        assert client.get("/api/admin/users").status_code == 200

    def test_sans_oidc_tout_le_monde_passe(self, app_test, base):
        """Sans fournisseur d'identite, distinguer les roles n'aurait aucun
        fondement — et restreindre rendrait l'application inadministrable."""
        client = app_test(None, oidc_enabled="false")
        assert client.get("/api/admin/users").status_code == 200


class TestAttribution:
    def test_promouvoir_puis_retrograder(self, app_test, base):
        peupler(base, ("u-1", "Chef", "chef@x", False), ("u-2", "Autre", "a@x", False))
        client = app_test(None, super_admins="chef@x")
        connecter(client, "u-1", "chef@x")

        cible = next(u for u in client.get("/api/admin/users").json() if u["subject"] == "u-2")
        assert cible["is_admin"] is False

        promu = client.put(f"/api/admin/users/{cible['id']}/admin", json={"is_admin": True})
        assert promu.status_code == 200 and promu.json()["is_admin"] is True

        retire = client.put(f"/api/admin/users/{cible['id']}/admin", json={"is_admin": False})
        assert retire.status_code == 200 and retire.json()["is_admin"] is False

    def test_un_super_administrateur_ne_se_retrograde_pas(self, app_test, base):
        """Son role vient de la configuration : le changer ici donnerait
        l'illusion d'avoir agi."""
        peupler(base, ("u-1", "Chef", "chef@x", False), ("u-3", "Fixe", "fixe@x", False))
        client = app_test(None, super_admins="chef@x,fixe@x")
        connecter(client, "u-1", "chef@x")
        cible = next(u for u in client.get("/api/admin/users").json() if u["subject"] == "u-3")
        assert cible["is_admin"] is True and cible["is_super_admin"] is True
        reponse = client.put(f"/api/admin/users/{cible['id']}/admin", json={"is_admin": False})
        assert reponse.status_code == 409
        assert "SUPER_ADMINS" in reponse.json()["detail"]

    def test_on_ne_se_retire_pas_son_propre_role(self, app_test, base):
        """Fermer la porte derriere soi, sans retour possible a l'ecran."""
        peupler(base, ("u-2", "Promu", "p@x", True))
        client = app_test(None, super_admins="")
        connecter(client, "u-2", "p@x")
        moi = client.get("/api/admin/users").json()[0]
        reponse = client.put(f"/api/admin/users/{moi['id']}/admin", json={"is_admin": False})
        assert reponse.status_code == 409
        assert "propre role" in reponse.json()["detail"]

    def test_le_dernier_administrateur_est_protege(self, app_test, base):
        """Deux promus, aucun super-admin : retirer le second rendrait
        l'application inadministrable autrement qu'en base."""
        peupler(base, ("u-2", "A", "a@x", True), ("u-3", "B", "b@x", True))
        client = app_test(None, super_admins="")
        connecter(client, "u-2", "a@x")
        autre = next(u for u in client.get("/api/admin/users").json() if u["subject"] == "u-3")
        # Le premier retrait est permis : il en resterait un (moi).
        assert client.put(f"/api/admin/users/{autre['id']}/admin", json={"is_admin": False}).status_code == 200
        # Et l'on ne peut pas se retirer soi-meme ensuite (garde precedente).
        moi = next(u for u in client.get("/api/admin/users").json() if u["subject"] == "u-2")
        assert client.put(f"/api/admin/users/{moi['id']}/admin", json={"is_admin": False}).status_code == 409

    def test_utilisateur_inconnu(self, app_test, base):
        peupler(base, ("u-1", "Chef", "chef@x", False))
        client = app_test(None, super_admins="chef@x")
        connecter(client, "u-1", "chef@x")
        assert client.put("/api/admin/users/999/admin", json={"is_admin": True}).status_code == 404


def musique(base):
    """Un titre, le minimum pour pouvoir enregistrer une ecoute."""
    with base() as db:
        artiste = Artist(name="Groupe")
        db.add(artiste)
        db.flush()
        album = Album(artist_id=artiste.id, source_title="Album", title="Album")
        db.add(album)
        db.flush()
        piste = Track(
            album_id=album.id, artist_id=artiste.id,
            object_key="k", object_key_hash="h",
            source_title="Titre", title="Titre", track_no=1,
            etag="e", size_bytes=1,
        )
        db.add(piste)
        db.commit()
        return piste.id


class TestSuppression:
    """Le menage dans l'annuaire : qui part, et ce qu'il laisse derriere lui.

    L'enjeu n'est pas la ligne `users`, c'est ce qui y est accroche. Les
    ecoutes sont detachees pour que les statistiques globales restent justes,
    les playlists partent avec leur proprietaire.
    """

    def test_le_compte_disparait_de_la_liste(self, app_test, base):
        peupler(base, ("u-1", "Chef", "chef@x", False), ("u-2", "Passant", "p@x", False))
        client = app_test(None, super_admins="chef@x")
        connecter(client, "u-1", "chef@x")

        cible = next(u for u in client.get("/api/admin/users").json() if u["subject"] == "u-2")
        assert client.delete(f"/api/admin/users/{cible['id']}").status_code == 204
        restants = [u["subject"] for u in client.get("/api/admin/users").json()]
        assert restants == ["u-1"]

    def test_les_ecoutes_survivent_detachees(self, app_test, base):
        """Le coeur de la fonctionnalite : ce qui a ete ecoute dans la maison
        l'a ete pour de bon. Un `ON DELETE CASCADE` ici ferait maigrir les
        statistiques globales a chaque menage."""
        peupler(base, ("u-1", "Chef", "chef@x", False), ("u-2", "Passant", "p@x", False))
        track_id = musique(base)
        client = app_test(None, super_admins="chef@x")
        connecter(client, "u-1", "chef@x")
        cible = next(u for u in client.get("/api/admin/users").json() if u["subject"] == "u-2")

        with base() as db:
            db.add(Listen(user_id=cible["id"], track_id=track_id, seconds=120.0))
            db.commit()

        assert client.delete(f"/api/admin/users/{cible['id']}").status_code == 204

        with base() as db:
            ecoutes = db.query(Listen).all()
            assert len(ecoutes) == 1
            assert ecoutes[0].user_id is None
            assert ecoutes[0].seconds == 120.0

    def test_les_playlists_partent_avec_le_compte(self, app_test, base):
        peupler(base, ("u-1", "Chef", "chef@x", False), ("u-2", "Passant", "p@x", False))
        client = app_test(None, super_admins="chef@x")
        connecter(client, "u-1", "chef@x")
        cible = next(u for u in client.get("/api/admin/users").json() if u["subject"] == "u-2")

        with base() as db:
            db.add(Playlist(owner_id=cible["id"], name="La sienne"))
            db.commit()

        assert client.delete(f"/api/admin/users/{cible['id']}").status_code == 204
        with base() as db:
            assert db.query(Playlist).count() == 0

    def test_la_liste_annonce_ce_qui_est_en_jeu(self, app_test, base):
        """L'ecran doit pouvoir avertir AVANT de supprimer : sans ces
        compteurs, on effacerait des playlists sans le savoir."""
        peupler(base, ("u-1", "Chef", "chef@x", False), ("u-2", "Passant", "p@x", False))
        track_id = musique(base)
        client = app_test(None, super_admins="chef@x")
        connecter(client, "u-1", "chef@x")
        cible = next(u for u in client.get("/api/admin/users").json() if u["subject"] == "u-2")

        with base() as db:
            db.add(Playlist(owner_id=cible["id"], name="La sienne"))
            db.add(Listen(user_id=cible["id"], track_id=track_id, seconds=1.0))
            db.add(Listen(user_id=cible["id"], track_id=track_id, seconds=2.0))
            db.commit()

        vue = {u["subject"]: u for u in client.get("/api/admin/users").json()}
        assert vue["u-2"]["playlist_count"] == 1
        assert vue["u-2"]["listen_count"] == 2
        # Les compteurs sont bien par personne, pas des totaux recopies.
        assert vue["u-1"]["playlist_count"] == 0
        assert vue["u-1"]["listen_count"] == 0

    def test_on_ne_se_supprime_pas_soi_meme(self, app_test, base):
        peupler(base, ("u-2", "Promu", "p@x", True))
        client = app_test(None, super_admins="")
        connecter(client, "u-2", "p@x")
        moi = client.get("/api/admin/users").json()[0]
        reponse = client.delete(f"/api/admin/users/{moi['id']}")
        assert reponse.status_code == 409
        assert "propre compte" in reponse.json()["detail"]

    def test_un_super_administrateur_ne_se_supprime_pas(self, app_test, base):
        """Sa ligne reviendrait a sa prochaine visite : on n'aurait perdu que
        ses playlists."""
        peupler(base, ("u-1", "Chef", "chef@x", False), ("u-3", "Fixe", "fixe@x", False))
        client = app_test(None, super_admins="chef@x,fixe@x")
        connecter(client, "u-1", "chef@x")
        cible = next(u for u in client.get("/api/admin/users").json() if u["subject"] == "u-3")
        reponse = client.delete(f"/api/admin/users/{cible['id']}")
        assert reponse.status_code == 409
        assert "SUPER_ADMINS" in reponse.json()["detail"]

    def test_compte_inconnu(self, app_test, base):
        peupler(base, ("u-1", "Chef", "chef@x", False))
        client = app_test(None, super_admins="chef@x")
        connecter(client, "u-1", "chef@x")
        assert client.delete("/api/admin/users/9999").status_code == 404

    def test_un_non_administrateur_est_refuse(self, app_test, base):
        peupler(base, ("u-1", "Simple", "s@x", False), ("u-2", "Autre", "a@x", False))
        client = app_test(None, super_admins="")
        connecter(client, "u-1", "s@x")
        # Il ne peut meme pas lister : on vise donc un identifiant au hasard.
        assert client.delete("/api/admin/users/2").status_code == 403
        with base() as db:
            assert db.query(UserRow).count() == 2
