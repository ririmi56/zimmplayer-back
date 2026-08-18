"""Les deux modes d'identite, et la frontiere entre eux."""

import pytest
from fastapi.testclient import TestClient

from app.auth import user_from_session
from app.config import get_settings


@pytest.fixture
def configure(monkeypatch):
    def _configure(**env):
        for key, value in env.items():
            monkeypatch.setenv(key.upper(), value)
        get_settings.cache_clear()

    yield _configure
    get_settings.cache_clear()


def client() -> TestClient:
    # Importe apres la configuration : le middleware de session lit les
    # reglages au montage.
    import importlib

    import app.main

    importlib.reload(app.main)
    return TestClient(app.main.app)


def session(**identite):
    base = {"subject": "u-1", "name": "Adrien", "email": "a@interne", "groups": []}
    base.update(identite)
    return {"identity": base}


class TestRoleDepuisLesGroupes:
    def test_le_groupe_configure_donne_admin(self, configure):
        configure(oidc_enabled="true", oidc_admin_group="zimmplayer-admins",
                  session_secret="x")
        user = user_from_session(session(groups=["ecoute", "zimmplayer-admins"]))
        assert user.role == "admin" and user.is_admin

    def test_les_autres_groupes_ne_donnent_rien(self, configure):
        configure(oidc_enabled="true", oidc_admin_group="zimmplayer-admins",
                  session_secret="x")
        assert user_from_session(session(groups=["ecoute"])).role == "user"

    def test_sans_groupe_configure_personne_n_est_admin(self, configure):
        """Un defaut qui donnerait admin sur une configuration incomplete
        serait le mauvais sens de securite."""
        configure(oidc_enabled="true", oidc_admin_group="", session_secret="x")
        assert user_from_session(session(groups=["admins", "root"])).role == "user"

    def test_session_vide(self, configure):
        configure(oidc_enabled="true", session_secret="x")
        assert user_from_session({}) is None


class TestFrontiereEntreLesDeuxModes:
    def test_sans_oidc_l_en_tete_fait_foi(self, configure):
        configure(oidc_enabled="false")
        reponse = client().get("/api/auth/me", headers={"X-User-Name": "Adrien"})
        corps = reponse.json()
        assert corps["name"] == "Adrien"
        assert corps["authenticated"] is True and corps["oidc_enabled"] is False

    def test_avec_oidc_l_en_tete_est_ignoree(self, configure):
        """Le controle qui donne son sens a l'authentification : si l'en-tete
        restait lue, il suffirait de l'envoyer pour se faire passer pour
        n'importe qui, sans jamais voir le fournisseur."""
        configure(oidc_enabled="true", oidc_issuer="https://idp.interne",
                  oidc_client_id="z", session_secret="secret-de-test")
        reponse = client().get("/api/auth/me", headers={"X-User-Name": "Administrateur"})
        corps = reponse.json()
        assert corps["name"] != "Administrateur"
        assert corps["authenticated"] is False
        assert corps["role"] == "user"

    def test_login_absent_quand_oidc_est_desactive(self, configure):
        configure(oidc_enabled="false")
        assert client().get("/api/auth/login", follow_redirects=False).status_code == 404

    def test_deconnexion_vide_la_session(self, configure):
        configure(oidc_enabled="false")
        assert client().post("/api/auth/logout").status_code == 200


class TestDemarrage:
    def test_oidc_sans_cle_de_session_refuse_de_demarrer(self, configure):
        """Une cle connue laisserait forger un cookie de session, donc
        l'identite de n'importe qui. Mieux vaut ne pas demarrer."""
        configure(oidc_enabled="true", session_secret="")
        with pytest.raises(RuntimeError, match="SESSION_SECRET"):
            client()
