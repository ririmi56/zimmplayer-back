"""Scan des singles zimmporter.

Un single vit dans `Artiste/Singles/Titre.m4a`, un dossier partage par tous les
singles de l'artiste. Le scanner doit pourtant en faire une sortie a part
entiere : un album par fichier, avec son propre titre et sa propre pochette.
"""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Album, Artist, Track
from app.services import scanner
from app.services.scanner import _Parsed, _upsert_track, key_hash
from app.services.tags import TrackTags


@pytest.fixture
def session(tmp_path, monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    # Aucune pochette a chercher dans le bucket : les tests ne telechargent rien.
    monkeypatch.setattr(scanner.covers, "find_folder_cover", lambda _key: None)

    with Session() as db:
        yield db
    engine.dispose()


def tags(title: str, *, artist: str = "Artiste", album: str = "Album", single: bool = False) -> TrackTags:
    return TrackTags(
        title=title,
        artist=artist,
        album_artist=artist,
        album=album,
        track_no=None if single else 1,
        disc_no=None,
        year=2024,
        duration_s=180.0,
        fmt="m4a",
        bitrate=256_000,
        sample_rate=44_100,
        channels=2,
        album_mbid=None,
        artist_mbid=None,
        genre=None,
        lyrics=None,
        is_single=single,
    )


def parsed(object_key: str, track_tags: TrackTags, etag: str = "etag") -> _Parsed:
    return _Parsed(
        obj={
            "key": object_key,
            "etag": etag,
            "size": 1,
            "last_modified": None,
        },
        tags=track_tags,
        cover=None,
    )


def test_deux_singles_du_meme_artiste_sont_deux_albums(session):
    _upsert_track(
        session,
        parsed("Artiste/Singles/One.m4a", tags("One", single=True, album="Singles")),
    )
    _upsert_track(
        session,
        parsed("Artiste/Singles/Two.m4a", tags("Two", single=True, album="Singles")),
    )
    session.commit()

    albums = session.scalars(select(Album).order_by(Album.title)).all()
    assert [(a.title, a.release_type) for a in albums] == [
        ("One", "single"),
        ("Two", "single"),
    ]
    assert all(a.artist_id == albums[0].artist_id for a in albums)
    assert all(len(a.tracks) == 1 for a in albums)


def test_un_album_normal_reste_un_album(session):
    _upsert_track(
        session,
        parsed("Artiste/Homework/01 - Around the World.m4a", tags("Around the World", album="Homework")),
    )
    session.commit()

    album = session.scalars(select(Album)).one()
    assert album.release_type == "album"
    assert album.source_title == "Homework"
    assert album.title == "Homework"


def test_reindexer_le_meme_single_ne_cree_pas_de_doublon(session):
    track = parsed("Artiste/Singles/One.m4a", tags("One", single=True, album="Singles"))
    _upsert_track(session, track)
    session.commit()
    first_id = session.scalars(select(Album.id)).one()

    _upsert_track(session, parsed("Artiste/Singles/One.m4a", tags("One", single=True, album="Singles"), etag="changed"))
    session.commit()

    albums = session.scalars(select(Album)).all()
    assert [album.id for album in albums] == [first_id]
    assert len(session.scalars(select(Track)).all()) == 1


def test_un_single_peut_partager_le_titre_d_un_album(session):
    """L'identite inclut le type : un single « Homework » ne rejoint pas l'album."""
    _upsert_track(
        session,
        parsed("Artiste/Homework/01 - Homework.m4a", tags("Homework", album="Homework")),
    )
    _upsert_track(
        session,
        parsed("Artiste/Singles/Homework.m4a", tags("Homework", single=True, album="Singles")),
    )
    session.commit()

    albums = session.scalars(select(Album).order_by(Album.release_type)).all()
    assert [(a.title, a.release_type) for a in albums] == [
        ("Homework", "album"),
        ("Homework", "single"),
    ]


def test_les_singles_agreges_sont_eclates_puis_l_ancien_album_purge(session):
    """Etat d'avant la migration : un album « Singles » avec plusieurs pistes."""
    artist = Artist(name="Artiste")
    session.add(artist)
    session.flush()
    old = Album(
        artist_id=artist.id,
        source_title="Singles",
        title="Singles",
        release_type="album",
    )
    session.add(old)
    session.flush()

    cles = ["Artiste/Singles/One.m4a", "Artiste/Singles/Two.m4a"]
    for index, cle in enumerate(cles):
        session.add(
            Track(
                album_id=old.id,
                artist_id=artist.id,
                source_title=f"Titre {index}",
                title=f"Titre {index}",
                object_key=cle,
                object_key_hash=key_hash(cle),
                etag="",
                size_bytes=1,
            )
        )
    session.commit()
    old_id = old.id

    for cle, titre in zip(cles, ("One", "Two"), strict=True):
        _upsert_track(
            session,
            parsed(cle, tags(titre, single=True, album="Singles"), etag="relu"),
        )
    session.flush()
    scanner._purge_orphans(session)
    session.commit()

    assert session.get(Album, old_id) is None
    albums = session.scalars(select(Album).order_by(Album.title)).all()
    assert [(a.title, a.release_type) for a in albums] == [
        ("One", "single"),
        ("Two", "single"),
    ]
    assert all(len(a.tracks) == 1 for a in albums)
