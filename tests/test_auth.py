"""Les deux modes d'identite, et la frontiere entre eux."""

from fastapi.testclient import TestClient

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401  (enregistre les tables)
from app.auth import is_super_admin, user_from_session
from app.config import get_settings
from app.db import Base, get_db
from app.models import User as UserRow


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


class TestSuperAdministrateurs:
    """Les comptes nommes dans la configuration, par ou l'on entre la premiere
    fois et qui ne peuvent pas etre retrogrades depuis l'interface."""

    def test_reconnu_par_son_sujet(self, configure):
        configure(super_admins="u-1")
        assert is_super_admin("u-1", "")

    def test_reconnu_par_son_courriel(self, configure):
        """Le `sub` d'un fournisseur est opaque : le courriel se configure a
        la main, c'est ce qu'on ecrira en pratique."""
        configure(super_admins="adrien@interne")
        assert is_super_admin("Cg0wLTM4NS0yODA4OS0w", "Adrien@Interne")

    def test_le_nom_affiche_n_est_jamais_compare(self, configure):
        """Chez beaucoup de fournisseurs chacun modifie le sien : il suffirait
        de se renommer pour devenir administrateur."""
        configure(super_admins="Adrien")
        user = user_from_session(session(name="Adrien", subject="u-9", email="x@y"))
        assert user.role == "user"

    def test_liste_et_espaces(self, configure):
        configure(super_admins=" a@x , b@y ")
        assert is_super_admin("", "b@y")
        assert not is_super_admin("", "c@z")

    def test_vide_ne_promeut_personne(self, configure):
        configure(super_admins="")
        assert not is_super_admin("u-1", "a@x")

    def test_role_admin_et_indicateur(self, configure):
        configure(oidc_enabled="true", super_admins="a@interne", session_secret="x")
        user = user_from_session(session(email="a@interne"))
        assert user.role == "admin" and user.is_super_admin

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
