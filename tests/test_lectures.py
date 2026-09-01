"""Compteur de lectures d'un titre, et tri des albums par ce compte.

Le nom du fichier place cette suite apres `test_auth` : elle DOIT recharger
`app.main` elle-meme, comme les autres. Un test de `test_auth` fait echouer ce
rechargement a dessein et laisse le module avec une application sans aucune
route, dont heriterait tout fichier suivant qui ne recharge pas.

Une ecoute est comptee PAR PERSONNE : un titre passe en session a trois en
vaut trois. C'est le meme comptage que la page Statistiques.
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
from app.models import Album, Artist, Listen, Track, User

#: (album, [nombre d'ecoutes de chaque titre]). Un album tres ecoute, un peu,
#: et un que personne n'a jamais lance — c'est celui-la qui doit etre facile a
#: retrouver, c'est tout l'interet du compteur.
CATALOGUE = [
    ("Ecoute", [3, 0]),
    ("Peu", [1]),
    ("Jamais", [0, 0]),
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
        # Deux personnes : les ecoutes d'un meme titre par des gens differents
        # comptent chacune pour une.
        gens = [User(subject=f"s{i}", name=f"P{i}", email=f"p{i}@x") for i in range(2)]
        db.add_all(gens)
        artiste = Artist(name="Groupe")
        db.add(artiste)
        db.flush()

        cle = 0
        for titre_album, ecoutes in CATALOGUE:
            album = Album(
                artist_id=artiste.id, source_title=titre_album, title=titre_album
            )
            db.add(album)
            db.flush()
            for rang, combien in enumerate(ecoutes):
                piste = Track(
                    album_id=album.id, artist_id=artiste.id,
                    object_key=f"k{cle}", object_key_hash=f"h{cle}",
                    source_title=f"{titre_album} {rang}", title=f"{titre_album} {rang}",
                    track_no=rang + 1, etag=f"e{cle}", size_bytes=1,
                )
                db.add(piste)
                db.flush()
                for n in range(combien):
                    # Alternance des auditeurs : la meme personne peut
                    # reecouter, et cela compte a chaque fois.
                    db.add(Listen(
                        user_id=gens[n % 2].id, track_id=piste.id, seconds=120.0
                    ))
                cle += 1
        db.commit()

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


def albums(client, **params):
    params.setdefault("limit", 10)
    r = client.get("/api/albums", params=params, headers={"X-User-Name": "A"})
    assert r.status_code == 200, r.text
    return r.json()["items"]


def pistes_de(client, titre_album):
    identifiant = next(a["id"] for a in albums(client) if a["title"] == titre_album)
    r = client.get(f"/api/albums/{identifiant}", headers={"X-User-Name": "A"})
    assert r.status_code == 200, r.text
    return r.json()["tracks"]


class TestCompteurParTitre:
    def test_un_titre_ecoute_annonce_ses_lectures(self, client):
        pistes = pistes_de(client, "Ecoute")
        assert pistes[0]["listen_count"] == 3

    def test_un_titre_jamais_lance_annonce_zero(self, client):
        """Le zero est l'information utile : c'est lui qui designe ce qui ne
        merite peut-etre plus sa place dans le bucket."""
        assert pistes_de(client, "Ecoute")[1]["listen_count"] == 0
        assert [p["listen_count"] for p in pistes_de(client, "Jamais")] == [0, 0]

    def test_la_fiche_d_un_titre_le_donne_aussi(self, client):
        piste = pistes_de(client, "Ecoute")[0]
        r = client.get(f"/api/tracks/{piste['id']}", headers={"X-User-Name": "A"})
        assert r.json()["listen_count"] == 3



class TestTriParLectures:
    def test_les_plus_ecoutes_d_abord(self, client):
        assert [a["title"] for a in albums(client, sort="lectures")] == [
            "Ecoute", "Peu", "Jamais"
        ]

    def test_en_sens_inverse_les_oublies_remontent(self, client):
        """L'usage annonce : retrouver ce que personne n'ecoute."""
        assert [a["title"] for a in albums(client, sort="lectures", reverse=True)] == [
            "Jamais", "Peu", "Ecoute"
        ]

    def test_le_nombre_de_titres_reste_juste(self, client):
        """Le piege des comptes par album : une JOINTURE sur `listens`
        multiplierait les lignes `tracks` et fausserait `track_count` — pour
        ce tri comme pour tous les autres. D'ou la sous-requete correlee."""
        comptes = {a["title"]: a["track_count"] for a in albums(client, sort="lectures")}
        assert comptes == {"Ecoute": 2, "Peu": 1, "Jamais": 2}

    def test_les_autres_tris_ne_bougent_pas(self, client):
        """Meme garde-fou, vu d'un tri qui n'a rien a voir."""
        comptes = {a["title"]: a["track_count"] for a in albums(client, sort="titre")}
        assert comptes == {"Ecoute": 2, "Peu": 1, "Jamais": 2}
