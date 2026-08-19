"""Playlists : composition, partage, et surtout ce que chacun a le droit de faire.

Base SQLite ephemere, et mode sans OIDC : l'identite se resume alors a
l'en-tete X-User-Name, ce qui permet de changer de personne d'une requete a
l'autre sans monter de fournisseur.
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
        album = Album(artist_id=artist.id, source_title="Album", title="Album")
        db.add(album)
        db.flush()
        for i in range(3):
            db.add(Track(
                album_id=album.id, artist_id=artist.id,
                object_key=f"k{i}", object_key_hash=f"h{i}",
                source_title=f"Titre {i}", title=f"Titre {i}", track_no=i + 1,
                etag=f"e{i}", size_bytes=1,
            ))
        db.commit()

    import importlib

    import app.main

    importlib.reload(app.main)
    application = app.main.app

    def _db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    application.dependency_overrides[get_db] = _db
    yield TestClient(application)
    get_settings.cache_clear()
    engine.dispose()


def comme(client, qui):
    """Un client agissant au nom de quelqu'un."""
    client.headers["X-User-Name"] = qui
    return client


def creer(client, qui, nom="Ma selection"):
    return comme(client, qui).post("/api/playlists", json={"name": nom}).json()


def id_de(client, qui):
    """Force la creation de la ligne `users` de quelqu'un, et renvoie son id.

    Sans OIDC, personne ne passe par une connexion : la ligne n'existe qu'a
    partir du premier appel fait en son nom.
    """
    comme(client, qui).get("/api/playlists")
    return next(p["id"] for p in client.get("/api/users").json() if p["name"] == qui)


class TestComposition:
    def test_creer_et_ajouter_des_titres(self, client):
        playlist = creer(client, "Adrien")
        assert playlist["is_owner"] and playlist["can_edit"]
        assert playlist["track_count"] == 0

        detail = comme(client, "Adrien").post(
            f"/api/playlists/{playlist['id']}/tracks", json={"track_ids": [1, 2]}
        ).json()
        assert [i["track"]["title"] for i in detail["items"]] == ["Titre 0", "Titre 1"]
        assert detail["items"][0]["added_by"] == "Adrien"

    def test_ajouter_un_album_entier(self, client):
        playlist = creer(client, "Adrien")
        detail = comme(client, "Adrien").post(
            f"/api/playlists/{playlist['id']}/tracks", json={"album_id": 1}
        ).json()
        assert detail["track_count"] == 3

    def test_le_meme_titre_peut_figurer_deux_fois(self, client):
        """Parfois voulu ; l'interdire surprendrait plus que ca n'aiderait."""
        playlist = creer(client, "Adrien")
        for _ in range(2):
            comme(client, "Adrien").post(
                f"/api/playlists/{playlist['id']}/tracks", json={"track_ids": [1]}
            )
        detail = client.get(f"/api/playlists/{playlist['id']}").json()
        assert detail["track_count"] == 2
        assert detail["items"][0]["id"] != detail["items"][1]["id"]

    def test_un_identifiant_caduc_ne_perd_pas_tout_l_ajout(self, client):
        """Sinon la contrainte de cle etrangere ferait echouer le lot entier
        pour un seul titre disparu entre-temps."""
        playlist = creer(client, "Adrien")
        detail = comme(client, "Adrien").post(
            f"/api/playlists/{playlist['id']}/tracks", json={"track_ids": [1, 9999, 2]}
        ).json()
        assert detail["track_count"] == 2

    def test_retirer_un_titre(self, client):
        playlist = creer(client, "Adrien")
        detail = comme(client, "Adrien").post(
            f"/api/playlists/{playlist['id']}/tracks", json={"track_ids": [1, 2]}
        ).json()
        apres = comme(client, "Adrien").delete(
            f"/api/playlists/{playlist['id']}/tracks/{detail['items'][0]['id']}"
        ).json()
        assert [i["track"]["title"] for i in apres["items"]] == ["Titre 1"]

    def test_renommer_et_supprimer(self, client):
        playlist = creer(client, "Adrien")
        renommee = comme(client, "Adrien").patch(
            f"/api/playlists/{playlist['id']}", json={"name": "Fete"}
        ).json()
        assert renommee["name"] == "Fete"
        assert comme(client, "Adrien").delete(f"/api/playlists/{playlist['id']}").status_code == 204
        assert comme(client, "Adrien").get("/api/playlists").json() == []


class TestReordonner:
    def ranger(self, client, qui="Adrien"):
        playlist = creer(client, qui)
        detail = comme(client, qui).post(
            f"/api/playlists/{playlist['id']}/tracks", json={"track_ids": [1, 2, 3]}
        ).json()
        return playlist["id"], detail

    def test_descendre_un_titre(self, client):
        pid, detail = self.ranger(client)
        premier = detail["items"][0]["id"]
        apres = comme(client, "Adrien").post(
            f"/api/playlists/{pid}/tracks/{premier}/move", json={"to_index": 2}
        ).json()
        assert [i["track"]["title"] for i in apres["items"]] == ["Titre 1", "Titre 2", "Titre 0"]

    def test_remonter_un_titre(self, client):
        pid, detail = self.ranger(client)
        dernier = detail["items"][2]["id"]
        apres = comme(client, "Adrien").post(
            f"/api/playlists/{pid}/tracks/{dernier}/move", json={"to_index": 0}
        ).json()
        assert [i["track"]["title"] for i in apres["items"]] == ["Titre 2", "Titre 0", "Titre 1"]

    def test_l_ordre_survit_a_la_relecture(self, client):
        """La position est persistee, pas seulement reflétée dans la reponse."""
        pid, detail = self.ranger(client)
        comme(client, "Adrien").post(
            f"/api/playlists/{pid}/tracks/{detail['items'][0]['id']}/move", json={"to_index": 2}
        )
        relu = comme(client, "Adrien").get(f"/api/playlists/{pid}").json()
        assert [i["track"]["title"] for i in relu["items"]] == ["Titre 1", "Titre 2", "Titre 0"]

    def test_un_rang_hors_limites_place_au_bout(self, client):
        """Le rang vient du client : on le borne plutot que de le refuser."""
        pid, detail = self.ranger(client)
        apres = comme(client, "Adrien").post(
            f"/api/playlists/{pid}/tracks/{detail['items'][0]['id']}/move",
            json={"to_index": 99},
        ).json()
        assert [i["track"]["title"] for i in apres["items"]] == ["Titre 1", "Titre 2", "Titre 0"]

    def test_les_rangs_restent_contigus_apres_un_retrait(self, client):
        """Un retrait laisse un trou dans les positions : sans renumerotation
        complete, le deplacement suivant partirait de rangs incoherents."""
        pid, detail = self.ranger(client)
        comme(client, "Adrien").delete(f"/api/playlists/{pid}/tracks/{detail['items'][1]['id']}")
        apres = comme(client, "Adrien").post(
            f"/api/playlists/{pid}/tracks/{detail['items'][2]['id']}/move", json={"to_index": 0}
        ).json()
        assert [i["track"]["title"] for i in apres["items"]] == ["Titre 2", "Titre 0"]

    def test_la_lecture_seule_ne_reordonne_pas(self, client):
        pid, detail = self.ranger(client)
        bea = id_de(client, "Bea")
        comme(client, "Adrien").put(
            f"/api/playlists/{pid}/shares/{bea}", json={"can_edit": False}
        )
        refus = comme(client, "Bea").post(
            f"/api/playlists/{pid}/tracks/{detail['items'][0]['id']}/move", json={"to_index": 2}
        )
        assert refus.status_code == 403

    def test_l_ecriture_reordonne(self, client):
        pid, detail = self.ranger(client)
        bea = id_de(client, "Bea")
        comme(client, "Adrien").put(
            f"/api/playlists/{pid}/shares/{bea}", json={"can_edit": True}
        )
        assert comme(client, "Bea").post(
            f"/api/playlists/{pid}/tracks/{detail['items'][0]['id']}/move", json={"to_index": 1}
        ).status_code == 200

    def test_titre_inconnu(self, client):
        pid, _ = self.ranger(client)
        assert comme(client, "Adrien").post(
            f"/api/playlists/{pid}/tracks/9999/move", json={"to_index": 0}
        ).status_code == 404


class TestPartage:
    def test_invisible_tant_qu_elle_n_est_pas_partagee(self, client):
        """Et 404, pas 403 : un 403 confirmerait son existence a qui n'a rien
        a y faire."""
        playlist = creer(client, "Adrien")
        assert comme(client, "Bea").get("/api/playlists").json() == []
        assert comme(client, "Bea").get(f"/api/playlists/{playlist['id']}").status_code == 404

    def test_partage_en_lecture_seule(self, client):
        playlist = creer(client, "Adrien")
        bea = id_de(client, "Bea")
        comme(client, "Adrien").put(
            f"/api/playlists/{playlist['id']}/shares/{bea}", json={"can_edit": False}
        )

        vue = comme(client, "Bea").get(f"/api/playlists/{playlist['id']}").json()
        assert vue["can_edit"] is False and vue["is_owner"] is False
        assert vue["owner_name"] == "Adrien"

        refus = comme(client, "Bea").post(
            f"/api/playlists/{playlist['id']}/tracks", json={"track_ids": [1]}
        )
        assert refus.status_code == 403
        assert "consultation seule" in refus.json()["detail"]

    def test_partage_en_ecriture(self, client):
        playlist = creer(client, "Adrien")
        bea = id_de(client, "Bea")
        comme(client, "Adrien").put(
            f"/api/playlists/{playlist['id']}/shares/{bea}", json={"can_edit": True}
        )
        detail = comme(client, "Bea").post(
            f"/api/playlists/{playlist['id']}/tracks", json={"track_ids": [1]}
        ).json()
        # L'auteur de l'ajout est trace : sur une playlist a plusieurs, c'est
        # la seule facon de savoir d'ou vient un titre.
        assert detail["items"][0]["added_by"] == "Bea"
        assert detail["can_edit"] is True and detail["is_owner"] is False

    def test_l_ecriture_ne_donne_ni_le_renommage_ni_la_suppression(self, client):
        """Partager en ecriture sert a composer a plusieurs, pas a se
        transmettre la playlist."""
        playlist = creer(client, "Adrien")
        bea = id_de(client, "Bea")
        comme(client, "Adrien").put(
            f"/api/playlists/{playlist['id']}/shares/{bea}", json={"can_edit": True}
        )
        for reponse in (
            comme(client, "Bea").patch(f"/api/playlists/{playlist['id']}", json={"name": "A moi"}),
            comme(client, "Bea").delete(f"/api/playlists/{playlist['id']}"),
            comme(client, "Bea").put(
                f"/api/playlists/{playlist['id']}/shares/1", json={"can_edit": True}
            ),
        ):
            assert reponse.status_code == 403, reponse.request.url

    def test_seul_le_proprietaire_voit_la_liste_des_partages(self, client):
        """La donner aux autres reviendrait a diffuser qui ecoute avec qui,
        sans que cela leur serve."""
        playlist = creer(client, "Adrien")
        bea = id_de(client, "Bea")
        comme(client, "Adrien").put(
            f"/api/playlists/{playlist['id']}/shares/{bea}", json={"can_edit": True}
        )
        assert len(comme(client, "Adrien").get(f"/api/playlists/{playlist['id']}").json()["shares"]) == 1
        assert comme(client, "Bea").get(f"/api/playlists/{playlist['id']}").json()["shares"] == []

    def test_changer_puis_retirer_le_partage(self, client):
        playlist = creer(client, "Adrien")
        bea = id_de(client, "Bea")
        comme(client, "Adrien").put(
            f"/api/playlists/{playlist['id']}/shares/{bea}", json={"can_edit": True}
        )
        rabaisse = comme(client, "Adrien").put(
            f"/api/playlists/{playlist['id']}/shares/{bea}", json={"can_edit": False}
        ).json()
        assert rabaisse["shares"][0]["can_edit"] is False

        comme(client, "Adrien").delete(f"/api/playlists/{playlist['id']}/shares/{bea}")
        assert comme(client, "Bea").get(f"/api/playlists/{playlist['id']}").status_code == 404

    def test_se_partager_a_soi_meme(self, client):
        playlist = creer(client, "Adrien")
        moi = id_de(client, "Adrien")
        reponse = comme(client, "Adrien").put(
            f"/api/playlists/{playlist['id']}/shares/{moi}", json={"can_edit": True}
        )
        assert reponse.status_code == 409

    def test_une_playlist_partagee_apparait_dans_ma_liste(self, client):
        playlist = creer(client, "Adrien", "Soiree")
        bea = id_de(client, "Bea")
        comme(client, "Adrien").put(
            f"/api/playlists/{playlist['id']}/shares/{bea}", json={"can_edit": False}
        )
        liste = comme(client, "Bea").get("/api/playlists").json()
        assert [p["name"] for p in liste] == ["Soiree"]
        assert liste[0]["is_owner"] is False


class TestPublique:
    """Publique = tout le monde consulte. Personne ne compose pour autant."""

    def test_privee_par_defaut(self, client):
        playlist = creer(client, "Adrien")
        assert playlist["is_public"] is False
        assert comme(client, "Bea").get(f"/api/playlists/{playlist['id']}").status_code == 404

    def test_publique_visible_de_tout_le_monde(self, client):
        playlist = creer(client, "Adrien", "Soiree")
        comme(client, "Adrien").patch(
            f"/api/playlists/{playlist['id']}", json={"is_public": True}
        )

        vue = comme(client, "Bea").get(f"/api/playlists/{playlist['id']}").json()
        assert vue["is_public"] is True
        assert vue["is_owner"] is False and vue["can_edit"] is False
        assert [p["name"] for p in comme(client, "Bea").get("/api/playlists").json()] == ["Soiree"]

    def test_publique_ne_donne_pas_l_ecriture(self, client):
        playlist = creer(client, "Adrien")
        comme(client, "Adrien").patch(
            f"/api/playlists/{playlist['id']}", json={"is_public": True}
        )
        refus = comme(client, "Bea").post(
            f"/api/playlists/{playlist['id']}/tracks", json={"track_ids": [1]}
        )
        assert refus.status_code == 403

    def test_publique_ne_donne_ni_renommage_ni_suppression_ni_partage(self, client):
        playlist = creer(client, "Adrien")
        pid = playlist["id"]
        comme(client, "Adrien").patch(f"/api/playlists/{pid}", json={"is_public": True})

        assert comme(client, "Bea").patch(f"/api/playlists/{pid}", json={"name": "A moi"}).status_code == 403
        assert comme(client, "Bea").delete(f"/api/playlists/{pid}").status_code == 403
        chloe = id_de(client, "Chloe")
        assert comme(client, "Bea").put(
            f"/api/playlists/{pid}/shares/{chloe}", json={"can_edit": True}
        ).status_code == 403

    def test_seul_le_proprietaire_l_ouvre_et_la_referme(self, client):
        playlist = creer(client, "Adrien")
        pid = playlist["id"]
        bea = id_de(client, "Bea")
        comme(client, "Adrien").put(f"/api/playlists/{pid}/shares/{bea}", json={"can_edit": True})

        # Meme partagee en edition : ouvrir au public reste au proprietaire.
        assert comme(client, "Bea").patch(f"/api/playlists/{pid}", json={"is_public": True}).status_code == 403

        comme(client, "Adrien").patch(f"/api/playlists/{pid}", json={"is_public": True})
        assert comme(client, "Chloe").get(f"/api/playlists/{pid}").status_code == 200
        comme(client, "Adrien").patch(f"/api/playlists/{pid}", json={"is_public": False})
        assert comme(client, "Chloe").get(f"/api/playlists/{pid}").status_code == 404

    def test_le_partage_en_edition_survit_a_la_fermeture(self, client):
        """Refermer ne doit pas emporter les partages nommement accordes."""
        playlist = creer(client, "Adrien")
        pid = playlist["id"]
        bea = id_de(client, "Bea")
        comme(client, "Adrien").put(f"/api/playlists/{pid}/shares/{bea}", json={"can_edit": True})
        comme(client, "Adrien").patch(f"/api/playlists/{pid}", json={"is_public": True})
        comme(client, "Adrien").patch(f"/api/playlists/{pid}", json={"is_public": False})

        vue = comme(client, "Bea").get(f"/api/playlists/{pid}").json()
        assert vue["can_edit"] is True

    def test_renommer_seul_ne_referme_pas(self, client):
        """Un champ absent du PATCH laisse la valeur en place."""
        playlist = creer(client, "Adrien")
        pid = playlist["id"]
        comme(client, "Adrien").patch(f"/api/playlists/{pid}", json={"is_public": True})
        renommee = comme(client, "Adrien").patch(f"/api/playlists/{pid}", json={"name": "Autre"}).json()
        assert renommee["name"] == "Autre" and renommee["is_public"] is True
