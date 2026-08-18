"""Playlists : selections de titres, personnelles puis partageables.

Un seul endroit decide de ce que chacun a le droit de faire — `_acces` — et
toutes les routes s'y referent. C'est deliberé : un droit recalcule route par
route finit toujours par diverger quelque part, et c'est la divergence qui
ouvre le trou.
"""

from enum import IntEnum

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.api.catalog import _track_out
from app.auth import CurrentUser, user_row
from app.db import get_db
from app.models import Album, Artist, Playlist, PlaylistShare, PlaylistTrack, Track
from app.models import User as UserRow
from app.schemas import (
    PlaylistCreate,
    PlaylistDetail,
    PlaylistOut,
    PlaylistTracksAdd,
    ShareUpdate,
)

router = APIRouter(prefix="/api/playlists", tags=["playlists"])


class Acces(IntEnum):
    """Ordonne : un droit superieur contient les inferieurs."""

    AUCUN = 0
    LECTURE = 1
    ECRITURE = 2
    PROPRIETAIRE = 3


def _acces(playlist: Playlist, moi: UserRow) -> Acces:
    """Ce que cette personne a le droit de faire sur cette playlist.

    Renommer, supprimer et partager restent au proprietaire : un partage en
    ecriture sert a composer a plusieurs, pas a se transmettre la playlist.
    """
    if playlist.owner_id == moi.id:
        return Acces.PROPRIETAIRE
    for share in playlist.shares:
        if share.user_id == moi.id:
            return Acces.ECRITURE if share.can_edit else Acces.LECTURE
    return Acces.AUCUN


def _charger(db: Session, playlist_id: int, moi: UserRow, requis: Acces) -> Playlist:
    """Retrouve une playlist et verifie le droit, ou echoue proprement.

    Une playlist qu'on n'a pas le droit de voir repond 404, jamais 403 : un
    403 confirmerait son existence a qui n'a rien a y faire.
    """
    playlist = db.scalar(
        select(Playlist)
        .where(Playlist.id == playlist_id)
        .options(
            selectinload(Playlist.shares).selectinload(PlaylistShare.user),
            selectinload(Playlist.items).selectinload(PlaylistTrack.track),
            selectinload(Playlist.items).selectinload(PlaylistTrack.added_by),
            selectinload(Playlist.owner),
        )
    )
    if playlist is None:
        raise HTTPException(status_code=404, detail="Playlist introuvable")

    acces = _acces(playlist, moi)
    if acces < Acces.LECTURE:
        raise HTTPException(status_code=404, detail="Playlist introuvable")
    if acces < requis:
        raise HTTPException(
            status_code=403,
            detail=(
                "Cette playlist est partagee en lecture seule."
                if requis == Acces.ECRITURE
                else "Seul le proprietaire peut faire cela."
            ),
        )
    return playlist


def _titres(db: Session, playlist: Playlist) -> dict[int, object]:
    """Les `TrackOut` des titres de la playlist, par identifiant de piste.

    `TrackOut` porte le nom de l'album, celui de l'artiste et la presence
    d'une pochette : autant de valeurs qui viennent d'une jointure, pas de la
    ligne `tracks`. On reprend l'assembleur du catalogue plutot que d'en
    ecrire un second, qui finirait par en differer.
    """
    ids = {item.track_id for item in playlist.items}
    if not ids:
        return {}
    rows = db.execute(
        select(Track, Album.title, Album.cover_file, Artist.name, Track.lyrics.is_not(None))
        .join(Album, Album.id == Track.album_id)
        .join(Artist, Artist.id == Track.artist_id)
        .where(Track.id.in_(ids))
    )
    return {row[0].id: _track_out(*row) for row in rows}


def _sortie(playlist: Playlist, moi: UserRow, detail: bool = False, db: Session | None = None):
    acces = _acces(playlist, moi)
    commun = {
        "id": playlist.id,
        "name": playlist.name,
        "owner_name": playlist.owner.name if playlist.owner else "",
        "is_owner": acces == Acces.PROPRIETAIRE,
        "can_edit": acces >= Acces.ECRITURE,
        "track_count": len(playlist.items),
        "updated_at": playlist.updated_at,
    }
    if not detail:
        return PlaylistOut(**commun)
    assert db is not None
    titres = _titres(db, playlist)
    return PlaylistDetail(
        **commun,
        items=[
            {
                "id": item.id,
                "track": titres[item.track_id],
                "added_by": item.added_by.name if item.added_by else None,
            }
            for item in playlist.items
            if item.track_id in titres
        ],
        # Qui voit quoi : seul le proprietaire gere les partages, donc lui seul
        # a besoin de la liste. La donner aux autres reviendrait a diffuser qui
        # ecoute avec qui, sans que cela leur serve a rien.
        shares=[
            {
                "user_id": share.user_id,
                "name": share.user.name,
                "can_edit": share.can_edit,
            }
            for share in playlist.shares
        ]
        if acces == Acces.PROPRIETAIRE
        else [],
    )


@router.get("", response_model=list[PlaylistOut])
def list_playlists(user: CurrentUser, db: Session = Depends(get_db)) -> list[PlaylistOut]:
    """Les miennes et celles qu'on m'a partagees, les plus recentes d'abord."""
    moi = user_row(db, user)
    partagees = select(PlaylistShare.playlist_id).where(PlaylistShare.user_id == moi.id)
    playlists = db.scalars(
        select(Playlist)
        .where((Playlist.owner_id == moi.id) | (Playlist.id.in_(partagees)))
        .options(
            selectinload(Playlist.shares),
            selectinload(Playlist.items),
            selectinload(Playlist.owner),
        )
        .order_by(Playlist.updated_at.desc())
    )
    return [_sortie(playlist, moi) for playlist in playlists]


@router.post("", response_model=PlaylistDetail, status_code=201)
def create_playlist(
    body: PlaylistCreate, user: CurrentUser, db: Session = Depends(get_db)
) -> PlaylistDetail:
    moi = user_row(db, user)
    playlist = Playlist(name=body.name.strip() or "Sans titre", owner_id=moi.id)
    db.add(playlist)
    db.commit()
    db.refresh(playlist)
    return _sortie(playlist, moi, detail=True, db=db)


@router.get("/{playlist_id}", response_model=PlaylistDetail)
def get_playlist(
    playlist_id: int, user: CurrentUser, db: Session = Depends(get_db)
) -> PlaylistDetail:
    moi = user_row(db, user)
    return _sortie(_charger(db, playlist_id, moi, Acces.LECTURE), moi, detail=True, db=db)


@router.patch("/{playlist_id}", response_model=PlaylistDetail)
def rename_playlist(
    playlist_id: int, body: PlaylistCreate, user: CurrentUser, db: Session = Depends(get_db)
) -> PlaylistDetail:
    moi = user_row(db, user)
    playlist = _charger(db, playlist_id, moi, Acces.PROPRIETAIRE)
    playlist.name = body.name.strip() or playlist.name
    db.commit()
    return _sortie(playlist, moi, detail=True, db=db)


@router.delete("/{playlist_id}", status_code=204)
def delete_playlist(
    playlist_id: int, user: CurrentUser, db: Session = Depends(get_db)
) -> None:
    moi = user_row(db, user)
    db.delete(_charger(db, playlist_id, moi, Acces.PROPRIETAIRE))
    db.commit()


@router.post("/{playlist_id}/tracks", response_model=PlaylistDetail)
def add_tracks(
    playlist_id: int,
    body: PlaylistTracksAdd,
    user: CurrentUser,
    db: Session = Depends(get_db),
) -> PlaylistDetail:
    """Ajoute des titres, ou tout un album, a la suite.

    Les doublons sont acceptes : mettre deux fois le meme titre est parfois
    voulu, et l'interdire surprendrait plus que ca n'aiderait.
    """
    moi = user_row(db, user)
    playlist = _charger(db, playlist_id, moi, Acces.ECRITURE)

    track_ids = list(body.track_ids or [])
    if body.album_id is not None:
        track_ids += list(
            db.scalars(
                select(Track.id)
                .where(Track.album_id == body.album_id)
                .order_by(Track.disc_no.is_(None), Track.disc_no, Track.track_no, Track.title)
            )
        )
    if not track_ids:
        raise HTTPException(status_code=400, detail="Aucun titre a ajouter")

    # Les identifiants viennent du client : on ne garde que ceux qui existent,
    # sinon la contrainte de cle etrangere echouerait en bloc et l'ajout entier
    # serait perdu pour un seul identifiant caduc.
    connus = set(db.scalars(select(Track.id).where(Track.id.in_(track_ids))))
    suivante = (
        db.scalar(
            select(func.coalesce(func.max(PlaylistTrack.position), -1)).where(
                PlaylistTrack.playlist_id == playlist.id
            )
        )
        + 1
    )
    for track_id in track_ids:
        if track_id not in connus:
            continue
        db.add(
            PlaylistTrack(
                playlist_id=playlist.id,
                track_id=track_id,
                position=suivante,
                added_by_id=moi.id,
            )
        )
        suivante += 1

    playlist.updated_at = func.now()
    db.commit()
    db.refresh(playlist)
    return _sortie(playlist, moi, detail=True, db=db)


@router.delete("/{playlist_id}/tracks/{item_id}", response_model=PlaylistDetail)
def remove_track(
    playlist_id: int, item_id: int, user: CurrentUser, db: Session = Depends(get_db)
) -> PlaylistDetail:
    moi = user_row(db, user)
    playlist = _charger(db, playlist_id, moi, Acces.ECRITURE)
    item = next((i for i in playlist.items if i.id == item_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail="Titre introuvable dans cette playlist")
    db.delete(item)
    db.commit()
    db.refresh(playlist)
    return _sortie(playlist, moi, detail=True, db=db)


@router.put("/{playlist_id}/shares/{user_id}", response_model=PlaylistDetail)
def set_share(
    playlist_id: int,
    user_id: int,
    body: ShareUpdate,
    user: CurrentUser,
    db: Session = Depends(get_db),
) -> PlaylistDetail:
    """Partage la playlist, ou change le droit accorde."""
    moi = user_row(db, user)
    playlist = _charger(db, playlist_id, moi, Acces.PROPRIETAIRE)

    if user_id == moi.id:
        raise HTTPException(
            status_code=409, detail="Vous etes deja proprietaire de cette playlist."
        )
    if db.get(UserRow, user_id) is None:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")

    share = next((s for s in playlist.shares if s.user_id == user_id), None)
    if share is None:
        share = PlaylistShare(playlist_id=playlist.id, user_id=user_id)
        db.add(share)
    share.can_edit = body.can_edit
    db.commit()
    db.refresh(playlist)
    return _sortie(playlist, moi, detail=True, db=db)


@router.delete("/{playlist_id}/shares/{user_id}", response_model=PlaylistDetail)
def remove_share(
    playlist_id: int, user_id: int, user: CurrentUser, db: Session = Depends(get_db)
) -> PlaylistDetail:
    moi = user_row(db, user)
    playlist = _charger(db, playlist_id, moi, Acces.PROPRIETAIRE)
    share = next((s for s in playlist.shares if s.user_id == user_id), None)
    if share is not None:
        db.delete(share)
        db.commit()
        db.refresh(playlist)
    return _sortie(playlist, moi, detail=True, db=db)
