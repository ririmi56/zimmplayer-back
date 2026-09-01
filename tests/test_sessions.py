"""File d'une session d'ecoute : ce que le lecteur recoit de chaque piste.

Pas de TestClient ici : creer une session par l'API provisionne un flux
Snapcast, donc exige un serveur. C'est `_items_out` qui assemble la file, et
c'est lui qu'on eprouve, sur une base SQLite ephemere.
"""

import random

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.api.sessions import _items_out
from app.db import Base
from app.services import queue as queue_service
from app.models import Album, Artist, Listen, QueueItem, Session, Track


@pytest.fixture
def base():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


@pytest.fixture
def file_dattente(base):
    """Une session de deux titres : l'un avec des paroles, l'autre sans."""
    with base() as db:
        artiste = Artist(name="Kanye West")
        db.add(artiste)
        db.flush()
        album = Album(
            artist_id=artiste.id, source_title="Yeezus", title="Yeezus",
            cover_file="yeezus.jpg",
        )
        db.add(album)
        db.flush()

        session = Session(name="salon", created_by="Adrien")
        db.add(session)
        db.flush()

        for i, (titre, paroles) in enumerate(
            [("On Sight", "Yeezy season approaching"), ("Muet", None)]
        ):
            piste = Track(
                album_id=album.id, artist_id=artiste.id,
                object_key=f"k{i}", object_key_hash=f"h{i}",
                source_title=titre, title=titre, track_no=i + 1,
                etag=f"e{i}", size_bytes=1, lyrics=paroles,
            )
            db.add(piste)
            db.flush()
            db.add(QueueItem(
                session_id=session.id, track_id=piste.id, position=i, added_by="Adrien"
            ))
        db.commit()
        yield base, session.id


class TestParolesDansLaFile:
    """Le defaut du 2026-08-20 : la file annoncait `has_lyrics` toujours faux.

    L'API des sessions assemblait son propre `TrackOut`, en doublon de celui du
    catalogue, avec `has_lyrics=False` ecrit en dur. Le panneau des paroles se
    fiant a ce drapeau, il n'a JAMAIS rien affiche pendant une lecture en
    session — alors que la meme piste, vue du catalogue, annonçait bien ses
    paroles.
    """

    def test_une_piste_qui_a_des_paroles_l_annonce(self, file_dattente):
        base, session_id = file_dattente
        with base() as db:
            session = db.get(Session, session_id)
            items = _items_out(db, session)
        assert [i.track.title for i in items] == ["On Sight", "Muet"]
        assert items[0].track.has_lyrics is True

    def test_une_piste_sans_paroles_ne_les_annonce_pas(self, file_dattente):
        base, session_id = file_dattente
        with base() as db:
            session = db.get(Session, session_id)
            items = _items_out(db, session)
        assert items[1].track.has_lyrics is False

    def test_le_reste_de_la_piste_est_assemble(self, file_dattente):
        """La file passe par l'assembleur du catalogue : album, artiste et
        pochette doivent suivre, et pas seulement les paroles."""
        base, session_id = file_dattente
        with base() as db:
            session = db.get(Session, session_id)
            piste = _items_out(db, session)[0].track
        assert piste.album_title == "Yeezus"
        assert piste.artist_name == "Kanye West"
        assert piste.has_cover is True

    def test_le_texte_des_paroles_n_est_pas_charge(self, file_dattente):
        """`lyrics` est une colonne differee, et la file peut faire des
        centaines de titres : on remonte la PRESENCE des paroles, calculee en
        SQL. Lire `track.lyrics` a la place declencherait une requete par
        piste, et traînerait des kilo-octets de texte dans chaque reponse."""
        base, session_id = file_dattente
        with base() as db:
            session = db.get(Session, session_id)
            _items_out(db, session)
            piste = db.query(Track).filter_by(title="On Sight").one()
            assert "lyrics" in inspect(piste).unloaded


@pytest.fixture
def longue_file(base):
    """Une session de huit titres, sans titre courant au depart."""
    with base() as db:
        artiste = Artist(name="Adele")
        db.add(artiste)
        db.flush()
        album = Album(artist_id=artiste.id, source_title="19", title="19")
        db.add(album)
        db.flush()
        session = Session(name="salon", created_by="Adrien")
        db.add(session)
        db.flush()
        for i in range(8):
            piste = Track(
                album_id=album.id, artist_id=artiste.id,
                object_key=f"a{i}", object_key_hash=f"ha{i}",
                source_title=f"Titre {i}", title=f"Titre {i}", track_no=i + 1,
                etag=f"ea{i}", size_bytes=1,
            )
            db.add(piste)
            db.flush()
            db.add(QueueItem(
                session_id=session.id, track_id=piste.id, position=i, added_by="Adrien"
            ))
        db.commit()
        yield base, session.id


def _titres(db, session_id):
    return [i.track.title for i in queue_service.ordered_items(db, session_id)]


class TestMelangerLaSuite:
    """« Melanger » ne touche qu'a ce qui reste a jouer.

    Deplacer le titre en cours ferait sauter la lecture : la position d'un
    element sert aussi d'ordre de lecture, et le serveur enchaine sur le
    voisin de droite.
    """

    def test_le_debut_et_le_titre_courant_ne_bougent_pas(self, longue_file):
        base, session_id = longue_file
        with base() as db:
            session = db.get(Session, session_id)
            items = queue_service.ordered_items(db, session_id)
            session.current_item_id = items[2].id
            avant = _titres(db, session_id)

            random.seed(1)
            queue_service.shuffle_upcoming(db, session)
            db.commit()

            apres = _titres(db, session_id)
        # Les trois premiers, titre courant compris, sont intacts.
        assert apres[:3] == avant[:3]
        assert apres != avant

    def test_la_suite_est_une_permutation_exacte(self, longue_file):
        base, session_id = longue_file
        with base() as db:
            session = db.get(Session, session_id)
            session.current_item_id = queue_service.ordered_items(db, session_id)[2].id
            avant = _titres(db, session_id)

            random.seed(1)
            queue_service.shuffle_upcoming(db, session)
            db.commit()

            apres = _titres(db, session_id)
            positions = [i.position for i in queue_service.ordered_items(db, session_id)]
        # Rien n'est perdu ni duplique...
        assert sorted(apres[3:]) == sorted(avant[3:])
        # ...et les positions restent contigues a partir de zero, sans quoi un
        # ajout ulterieur se placerait au mauvais rang.
        assert positions == list(range(8))

    def test_sans_titre_courant_toute_la_file_est_a_suivre(self, longue_file):
        base, session_id = longue_file
        with base() as db:
            session = db.get(Session, session_id)
            assert session.current_item_id is None
            avant = _titres(db, session_id)

            random.seed(3)
            queue_service.shuffle_upcoming(db, session)
            db.commit()

            apres = _titres(db, session_id)
        assert sorted(apres) == sorted(avant)
        assert apres[0] != avant[0] or apres != avant

    def test_le_dernier_titre_en_cours_ne_change_rien(self, longue_file):
        """Rien apres le titre courant : il n'y a rien a rebattre."""
        base, session_id = longue_file
        with base() as db:
            session = db.get(Session, session_id)
            session.current_item_id = queue_service.ordered_items(db, session_id)[-1].id
            avant = _titres(db, session_id)

            random.seed(4)
            queue_service.shuffle_upcoming(db, session)
            db.commit()

            assert _titres(db, session_id) == avant

    def test_un_seul_titre_a_suivre_ne_change_rien(self, longue_file):
        base, session_id = longue_file
        with base() as db:
            session = db.get(Session, session_id)
            session.current_item_id = queue_service.ordered_items(db, session_id)[-2].id
            avant = _titres(db, session_id)

            random.seed(5)
            queue_service.shuffle_upcoming(db, session)
            db.commit()

            assert _titres(db, session_id) == avant

    def test_la_lecture_n_est_pas_interrompue(self, longue_file):
        """Melanger n'est pas un ordre de transport : le titre courant, l'etat
        de lecture et `command_seq` ne bougent pas. Sans quoi le front
        resynchroniserait la lecture pour un simple reordonnancement."""
        base, session_id = longue_file
        with base() as db:
            session = db.get(Session, session_id)
            courant = queue_service.ordered_items(db, session_id)[1].id
            session.current_item_id = courant
            session.is_playing = True
            session.command_seq = 7

            random.seed(6)
            queue_service.shuffle_upcoming(db, session)
            db.commit()

            assert session.current_item_id == courant
            assert session.is_playing is True
            assert session.command_seq == 7

    def test_melange_vraiment(self, longue_file):
        """Sur plusieurs tirages, l'ordre obtenu varie : un `shuffle` qui ne
        melangerait rien passerait tous les tests precedents."""
        base, session_id = longue_file
        ordres = set()
        with base() as db:
            session = db.get(Session, session_id)
            for graine in range(12):
                random.seed(graine)
                queue_service.shuffle_upcoming(db, session)
                db.flush()
                ordres.add(tuple(_titres(db, session_id)))
        assert len(ordres) > 1


class TestLecturesDansLaFile:
    """La file annonce le compteur d'ecoutes de chaque titre.

    Elle assemble ses `TrackOut` par une requete a elle : le compteur doit y
    etre remonte explicitement. C'est exactement le chemin qui avait fait
    annoncer `has_lyrics: false` partout pendant des mois — un assembleur
    nourri d'une valeur par defaut ne se voit pas, il se decouvre.
    """

    def test_le_compteur_suit_les_ecoutes(self, file_dattente):
        base, session_id = file_dattente
        with base() as db:
            session = db.get(Session, session_id)
            premier = queue_service.ordered_items(db, session_id)[0]
            for _ in range(4):
                db.add(Listen(user_id=None, track_id=premier.track_id, seconds=90.0))
            db.commit()

            items = _items_out(db, session)
        assert items[0].track.listen_count == 4
        assert items[1].track.listen_count == 0
