import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Select, func, select, text
from sqlalchemy.orm import Session

from app.auth import CurrentUser
from app.db import get_db
from app.models import Album, Artist, Track, TrackLike
from app.schemas import (
    AlbumDetail,
    AlbumOut,
    ArtistDetail,
    ArtistOut,
    GenreOut,
    LyricsOut,
    Page,
    SearchResults,
    TrackOut,
)

router = APIRouter(prefix="/api", tags=["catalogue"])

# Doit rester aligne sur --innodb-ft-min-token-size dans docker-compose.
_FT_MIN_TOKEN = 2
_FT_OPERATORS = re.compile(r'[+\-><()~*"@]')


def _boolean_query(q: str) -> str | None:
    """Traduit une saisie libre en requete FULLTEXT booleenne.

    Chaque mot devient obligatoire et prefixe (`+beat*`), ce qui donne le
    comportement attendu d'une recherche incrementale. Renvoie None si aucun mot
    n'atteint la taille minimale indexee : l'appelant retombe alors sur un LIKE.
    """
    tokens = [_FT_OPERATORS.sub("", token) for token in q.split()]
    tokens = [token for token in tokens if len(token) >= _FT_MIN_TOKEN]
    if not tokens:
        return None
    return " ".join(f"+{token}*" for token in tokens)


def _text_filter(column, qualified_name: str, q: str):
    boolean_query = _boolean_query(q)
    if boolean_query is None:
        return column.like(f"%{q}%")
    return text(f"MATCH ({qualified_name}) AGAINST (:ftq IN BOOLEAN MODE)").bindparams(
        ftq=boolean_query
    )


def _paginate(db: Session, stmt: Select, limit: int, offset: int) -> tuple[list, int]:
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    return list(db.execute(stmt.limit(limit).offset(offset))), total


_ARTIST_SELECT = (
    select(Artist, func.count(func.distinct(Album.id)))
    .outerjoin(Album, Album.artist_id == Artist.id)
    .group_by(Artist.id)
)

# Le genre d'un album est agrege depuis ses pistes plutot que duplique dans une
# colonne : les tags portent le genre au niveau du fichier.

# Tris proposes pour la liste des albums. Chaque cle porte son sens naturel :
# on lit un catalogue de A a Z, mais on veut voir les derniers ajouts EN
# PREMIER. `reverse` retourne ce sens-la, il ne le remplace pas.
#
# `Album.id` clot systematiquement la liste : sans depart unique, deux albums
# ex aequo (meme genre, meme annee) peuvent changer de place d'une requete a
# l'autre, et le defilement infini afficherait alors des doublons tout en
# sautant d'autres albums. Il ne se retourne jamais : il n'est la que pour
# rendre l'ordre stable.
AlbumSort = Literal["artiste", "titre", "annee", "ajout", "genre", "likes"]

# Sous-requete correlee plutot qu'une jointure de plus : joindre `track_likes`
# a `_ALBUM_SELECT` multiplierait les lignes `tracks` par leurs likes, et le
# `count(Track.id)` du nombre de titres deviendrait faux pour TOUS les tris.
_LIKES_PAR_ALBUM = (
    select(func.count(TrackLike.id))
    .select_from(TrackLike)
    .join(Track, Track.id == TrackLike.track_id)
    .where(Track.album_id == Album.id)
    .correlate(Album)
    .scalar_subquery()
)

#: Par tri : la cle principale, son sens par defaut, puis les cles de
#: departage. Seule la principale se retourne — inverser « Artiste » doit
#: remonter les Z, pas rejouer les discographies a l'envers.
_ALBUM_ORDERS: dict[str, tuple] = {
    "artiste": (Artist.name, False, [Album.year.is_(None), Album.year]),
    "titre": (Album.title, False, []),
    "annee": (Album.year, True, []),
    "ajout": (Album.created_at, True, []),
    # Colonne agregee du SELECT : le genre d'un album est celui de ses pistes.
    "genre": (func.max(Track.genre), False, [Artist.name]),
    # Tous comptes confondus : c'est ce qui plait dans la maison, pas ce que
    # j'aime moi. Les albums sans aucun like retombent derriere, par artiste.
    "likes": (_LIKES_PAR_ALBUM, True, [Artist.name]),
}

#: Tris dont la cle principale peut manquer. Le test de nullite passe AVANT
#: elle et ne se retourne pas : un album non date, ou sans genre, n'a rien a
#: faire en tete, dans un sens comme dans l'autre.
_ALBUM_NULLS_LAST = {"annee", "genre"}


def _album_order_by(sort: str, reverse: bool) -> list:
    cle, descendant_par_defaut, departage = _ALBUM_ORDERS[sort]
    descendant = descendant_par_defaut != reverse
    clauses = [cle.is_(None)] if sort in _ALBUM_NULLS_LAST else []
    clauses.append(cle.desc() if descendant else cle.asc())
    return [*clauses, *departage, Album.id]

_ALBUM_SELECT = (
    select(Album, Artist.name, func.count(Track.id), func.max(Track.genre))
    .join(Artist, Artist.id == Album.artist_id)
    .outerjoin(Track, Track.album_id == Album.id)
    .group_by(Album.id, Artist.name)
)

# `lyrics` est une colonne differee : on ne remonte que sa presence, jamais son
# contenu, pour ne pas charger des kilo-octets de texte dans chaque liste.
_TRACK_SELECT = (
    select(Track, Album.title, Album.cover_file, Artist.name, Track.lyrics.is_not(None))
    .join(Album, Album.id == Track.album_id)
    .join(Artist, Artist.id == Track.artist_id)
)


def _artist_out(artist: Artist, album_count: int) -> ArtistOut:
    return ArtistOut(id=artist.id, name=artist.name, album_count=album_count)


def _album_out(
    album: Album, artist_name: str, track_count: int, genre: str | None = None
) -> AlbumOut:
    return AlbumOut(
        id=album.id,
        title=album.title,
        year=album.year,
        artist_id=album.artist_id,
        artist_name=artist_name,
        has_cover=album.cover_file is not None,
        track_count=track_count,
        genre=genre,
        overrides=album.overrides or {},
    )


def _track_out(
    track: Track,
    album_title: str,
    cover_file: str | None,
    artist_name: str,
    has_lyrics: bool = False,
) -> TrackOut:
    return TrackOut(
        id=track.id,
        title=track.title,
        track_no=track.track_no,
        disc_no=track.disc_no,
        duration_s=track.duration_s,
        format=track.format,
        bitrate=track.bitrate,
        album_id=track.album_id,
        album_title=album_title,
        artist_id=track.artist_id,
        artist_name=artist_name,
        has_cover=cover_file is not None,
        genre=track.genre,
        has_lyrics=bool(has_lyrics),
        overrides=track.overrides or {},
    )


@router.get("/artists", response_model=Page[ArtistOut])
def list_artists(
    user: CurrentUser,
    q: str | None = None,
    limit: int = Query(default=100, le=500),
    offset: int = 0,
    db: Session = Depends(get_db),
) -> Page[ArtistOut]:
    stmt = _ARTIST_SELECT.order_by(Artist.name)
    if q:
        stmt = stmt.where(_text_filter(Artist.name, "artists.name", q))
    rows, total = _paginate(db, stmt, limit, offset)
    return Page(
        items=[_artist_out(*row) for row in rows], total=total, limit=limit, offset=offset
    )


@router.get("/artists/{artist_id}", response_model=ArtistDetail)
def get_artist(
    artist_id: int, user: CurrentUser, db: Session = Depends(get_db)
) -> ArtistDetail:
    row = db.execute(_ARTIST_SELECT.where(Artist.id == artist_id)).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Artiste introuvable")

    albums = db.execute(
        _ALBUM_SELECT.where(Album.artist_id == artist_id).order_by(
            Album.year.is_(None), Album.year, Album.title
        )
    )
    appears_on = db.execute(
        _TRACK_SELECT.where(
            Track.artist_id == artist_id, Album.artist_id != artist_id
        ).order_by(Album.title, Track.track_no)
    )
    return ArtistDetail(
        **_artist_out(*row).model_dump(),
        albums=[_album_out(*album) for album in albums],
        appears_on=[_track_out(*track) for track in appears_on],
    )


@router.get("/albums", response_model=Page[AlbumOut])
def list_albums(
    user: CurrentUser,
    q: str | None = None,
    artist_id: int | None = None,
    genre: str | None = None,
    sort: AlbumSort = "artiste",
    reverse: bool = False,
    limit: int = Query(default=100, le=500),
    offset: int = 0,
    db: Session = Depends(get_db),
) -> Page[AlbumOut]:
    stmt = _ALBUM_SELECT.order_by(*_album_order_by(sort, reverse))
    if artist_id is not None:
        stmt = stmt.where(Album.artist_id == artist_id)
    if genre:
        stmt = stmt.where(
            Album.id.in_(select(Track.album_id).where(Track.genre == genre))
        )
    if q:
        stmt = stmt.where(_text_filter(Album.title, "albums.title", q))
    rows, total = _paginate(db, stmt, limit, offset)
    return Page(
        items=[_album_out(*row) for row in rows], total=total, limit=limit, offset=offset
    )


@router.get("/albums/{album_id}", response_model=AlbumDetail)
def get_album(
    album_id: int, user: CurrentUser, db: Session = Depends(get_db)
) -> AlbumDetail:
    row = db.execute(_ALBUM_SELECT.where(Album.id == album_id)).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Album introuvable")

    tracks = db.execute(
        _TRACK_SELECT.where(Track.album_id == album_id).order_by(
            Track.disc_no.is_(None), Track.disc_no, Track.track_no, Track.title
        )
    )
    return AlbumDetail(
        **_album_out(*row).model_dump(),
        tracks=[_track_out(*track) for track in tracks],
    )


@router.get("/tracks/{track_id}", response_model=TrackOut)
def get_track(
    track_id: int, user: CurrentUser, db: Session = Depends(get_db)
) -> TrackOut:
    row = db.execute(_TRACK_SELECT.where(Track.id == track_id)).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Piste introuvable")
    return _track_out(*row)


@router.get("/genres", response_model=list[GenreOut])
def list_genres(user: CurrentUser, db: Session = Depends(get_db)) -> list[GenreOut]:
    """Genres presents au catalogue, avec leur volume."""
    rows = db.execute(
        select(
            Track.genre,
            func.count(func.distinct(Track.album_id)),
            func.count(Track.id),
        )
        .where(Track.genre.is_not(None))
        .group_by(Track.genre)
        .order_by(Track.genre)
    )
    return [
        GenreOut(name=name, album_count=albums, track_count=tracks)
        for name, albums, tracks in rows
    ]


@router.get("/tracks/{track_id}/lyrics", response_model=LyricsOut)
def get_lyrics(
    track_id: int, user: CurrentUser, db: Session = Depends(get_db)
) -> LyricsOut:
    """Paroles d'une piste, servies a part car la colonne est differee."""
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=404, detail="Piste introuvable")
    return LyricsOut(track_id=track.id, lyrics=track.lyrics)


@router.get("/search", response_model=SearchResults)
def search(
    user: CurrentUser,
    q: str = Query(min_length=1),
    limit: int = Query(default=20, le=100),
    db: Session = Depends(get_db),
) -> SearchResults:
    artists = db.execute(
        _ARTIST_SELECT.where(_text_filter(Artist.name, "artists.name", q))
        .order_by(Artist.name)
        .limit(limit)
    )
    albums = db.execute(
        _ALBUM_SELECT.where(_text_filter(Album.title, "albums.title", q))
        .order_by(Album.title)
        .limit(limit)
    )
    tracks = db.execute(
        _TRACK_SELECT.where(_text_filter(Track.title, "tracks.title", q))
        .order_by(Track.title)
        .limit(limit)
    )
    return SearchResults(
        artists=[_artist_out(*row) for row in artists],
        albums=[_album_out(*row) for row in albums],
        tracks=[_track_out(*row) for row in tracks],
    )
