"""Correction manuelle des metadonnees.

Rien n'est jamais reecrit dans le bucket : les corrections vivent dans la
colonne `overrides` et sont reappliquees a chaque rescan (voir
`models.refresh_effective`). En reseau airgap, c'est le seul moyen de rattraper
un fichier mal tagge.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import CurrentUser
from app.db import get_db
from app.models import Album, Artist, Track, refresh_effective
from app.schemas import AlbumOut, AlbumUpdate, TrackOut, TrackUpdate

router = APIRouter(prefix="/api", tags=["edition"])


def _merge_overrides(entity: Album | Track, payload: BaseModel) -> None:
    """Fusionne la correction. Un champ a null retire la correction existante,
    et la valeur du tag reprend alors le dessus."""
    changes: dict[str, Any] = payload.model_dump(exclude_unset=True)
    overrides = dict(entity.overrides or {})

    for field, value in changes.items():
        if field not in entity.EDITABLE_FIELDS:
            continue
        if value is None:
            overrides.pop(field, None)
        else:
            overrides[field] = value

    entity.overrides = overrides
    refresh_effective(entity)


@router.patch("/tracks/{track_id}", response_model=TrackOut)
def update_track(
    track_id: int,
    payload: TrackUpdate,
    user: CurrentUser,
    db: Session = Depends(get_db),
) -> TrackOut:
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=404, detail="Piste introuvable")

    _merge_overrides(track, payload)
    db.commit()
    db.refresh(track)

    album = db.get(Album, track.album_id)
    artist = db.get(Artist, track.artist_id)
    return TrackOut(
        id=track.id,
        title=track.title,
        track_no=track.track_no,
        disc_no=track.disc_no,
        duration_s=track.duration_s,
        format=track.format,
        bitrate=track.bitrate,
        album_id=track.album_id,
        album_title=album.title,
        artist_id=track.artist_id,
        artist_name=artist.name,
        has_cover=album.cover_file is not None,
        overrides=track.overrides or {},
    )


@router.patch("/albums/{album_id}", response_model=AlbumOut)
def update_album(
    album_id: int,
    payload: AlbumUpdate,
    user: CurrentUser,
    db: Session = Depends(get_db),
) -> AlbumOut:
    album = db.get(Album, album_id)
    if album is None:
        raise HTTPException(status_code=404, detail="Album introuvable")

    _merge_overrides(album, payload)
    db.commit()
    db.refresh(album)

    artist = db.get(Artist, album.artist_id)
    return AlbumOut(
        id=album.id,
        title=album.title,
        year=album.year,
        artist_id=album.artist_id,
        artist_name=artist.name,
        has_cover=album.cover_file is not None,
        track_count=len(album.tracks),
        overrides=album.overrides or {},
    )
