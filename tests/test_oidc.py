"""Validation du jeton d'identite.

Un vrai couple de cles RSA est genere pour la session de test : on signe donc
de vrais jetons, et les rejets constates sont ceux de la bibliotheque, pas
d'un simulacre. Le flux complet contre un fournisseur reel est verifie a part
(scripts/check_oidc.sh, avec Dex en conteneur).
"""

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import get_settings
from app.services import oidc

ISSUER = "https://idp.interne"
CLIENT_ID = "zimmplayer"


@pytest.fixture(scope="module")
def cles():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private, private.public_key()


@pytest.fixture
def fournisseur(monkeypatch, cles):
    """Un fournisseur OIDC credible : decouverte figee, cle publique reelle."""
    private, public = cles
    monkeypatch.setenv("OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("OIDC_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("OIDC_GROUPS_CLAIM", "groups")
    get_settings.cache_clear()

    monkeypatch.setattr(oidc, "discover", lambda force=False: {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/authorize",
        "token_endpoint": f"{ISSUER}/token",
        "jwks_uri": f"{ISSUER}/jwks",
        "id_token_signing_alg_values_supported": ["RS256"],
    })

    class FausseCle:
        key = public

    monkeypatch.setattr(oidc, "_jwk_client", lambda: type(
        "Client", (), {"get_signing_key_from_jwt": staticmethod(lambda token: FausseCle)}
    )())

    def signer(**revendications):
        maintenant = int(time.time())
        charge = {
            "iss": ISSUER, "aud": CLIENT_ID, "sub": "u-1",
            "iat": maintenant, "exp": maintenant + 300,
        }
        charge.update(revendications)
        return jwt.encode(charge, private, algorithm="RS256")

    yield signer
    get_settings.cache_clear()


class TestIdentiteDepuisJeton:
    def test_jeton_valide(self, fournisseur):
        jeton = fournisseur(nonce="n1", name="Adrien", email="a@interne",
                            groups=["ecoute", "admins"])
        identite = oidc.identity_from_token(jeton, "n1")
        assert identite.subject == "u-1"
        assert identite.name == "Adrien"
        assert identite.email == "a@interne"
        assert identite.groups == ["ecoute", "admins"]

    def test_nonce_different_refuse(self, fournisseur):
        """Sans ce controle, un jeton valide obtenu pour une autre connexion
        pourrait etre rejoue sur celle-ci."""
        jeton = fournisseur(nonce="obtenu-ailleurs")
        with pytest.raises(oidc.OidcError, match="nonce"):
            oidc.identity_from_token(jeton, "attendu-ici")

    def test_destinataire_different_refuse(self, fournisseur):
        """Un jeton emis pour une AUTRE application du meme fournisseur."""
        jeton = fournisseur(aud="une-autre-appli", nonce="n1")
        with pytest.raises(oidc.OidcError):
            oidc.identity_from_token(jeton, "n1")

    def test_jeton_expire_refuse(self, fournisseur):
        maintenant = int(time.time())
        jeton = fournisseur(nonce="n1", iat=maintenant - 600, exp=maintenant - 300)
        with pytest.raises(oidc.OidcError):
            oidc.identity_from_token(jeton, "n1")

    def test_emetteur_different_refuse(self, fournisseur):
        jeton = fournisseur(iss="https://un-autre-idp", nonce="n1")
        with pytest.raises(oidc.OidcError):
            oidc.identity_from_token(jeton, "n1")

    def test_signature_invalide_refusee(self, fournisseur):
        jeton = fournisseur(nonce="n1")
        # Une revendication modifiee apres coup : la signature ne colle plus.
        corps = jeton.split(".")
        corps[1] = corps[1][:-4] + "AAAA"
        with pytest.raises(oidc.OidcError):
            oidc.identity_from_token(".".join(corps), "n1")

    @pytest.mark.parametrize(
        "revendications,attendu",
        [
            ({"name": "Adrien", "preferred_username": "adri", "email": "a@x"}, "Adrien"),
            ({"preferred_username": "adri", "email": "a@x"}, "adri"),
            ({"email": "a@x"}, "a@x"),
            ({}, "u-1"),
        ],
    )
    def test_nom_affiche_par_ordre_de_preference(self, fournisseur, revendications, attendu):
        """On ne laisse jamais quelqu'un sans nom : au pire le sujet, opaque
        mais stable."""
        identite = oidc.identity_from_token(fournisseur(nonce="n1", **revendications), "n1")
        assert identite.name == attendu

    def test_photo_reprise_du_fournisseur(self, fournisseur):
        jeton = fournisseur(nonce="n1", picture="https://authentik.interne/media/a.png")
        identite = oidc.identity_from_token(jeton, "n1")
        assert identite.picture == "https://authentik.interne/media/a.png"

    def test_sans_photo_le_champ_reste_vide(self, fournisseur):
        """Beaucoup de fournisseurs n'emettent pas `picture` : l'interface
        dessine alors l'initiale, elle ne doit pas recevoir None."""
        identite = oidc.identity_from_token(fournisseur(nonce="n1"), "n1")
        assert identite.picture == ""

    def test_groupe_unique_annonce_comme_chaine(self, fournisseur):
        """Certains fournisseurs emettent une chaine quand il n'y a qu'un
        groupe. Sans ce rattrapage, on itererait sur ses lettres."""
        identite = oidc.identity_from_token(fournisseur(nonce="n1", groups="admins"), "n1")
        assert identite.groups == ["admins"]

    def test_claim_de_groupes_configurable(self, fournisseur, monkeypatch):
        """Keycloak, Entra et consorts ne rangent pas les groupes au meme endroit."""
        monkeypatch.setenv("OIDC_GROUPS_CLAIM", "roles")
        get_settings.cache_clear()
        identite = oidc.identity_from_token(
            fournisseur(nonce="n1", roles=["dj"], groups=["ignore"]), "n1"
        )
        assert identite.groups == ["dj"]
