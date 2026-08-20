"""File d'une session d'ecoute : ce que le lecteur recoit de chaque piste.

Pas de TestClient ici : creer une session par l'API provisionne un flux
Snapcast, donc exige un serveur. C'est `_items_out` qui assemble la file, et
c'est lui qu'on eprouve, sur une base SQLite ephemere.
"""

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.api.sessions import _items_out
from app.db import Base
from app.models import Album, Artist, QueueItem, Session, Track


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
