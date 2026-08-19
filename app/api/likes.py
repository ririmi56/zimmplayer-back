"""Likes : les titres qu'on aime, un par un.

Le like ne touche a aucun schema existant. `TrackOut` ne porte pas
d'indicateur « aime », et c'est deliberé : il est assemble par le catalogue,
les playlists et les sessions, qui n'ont aucune raison de connaitre la
personne connectee. L'interface recupere donc une fois la liste de SES likes
(`GET /api/likes`) et s'en sert partout — un seul appel, au lieu d'un champ a
faire descendre dans trois chaines d'assemblage.

Les likes portent sur les titres seulement, jamais sur les albums.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.api.catalog import _TRACK_SELECT, _track_out
from app.auth import CurrentUser, user_row
from app.db import get_db
from app.models import Track, TrackLike
from app.schemas import TrackOut

router = APIRouter(prefix="/api/likes", tags=["likes"])


@router.get("", response_model=list[int])
def my_likes(user: CurrentUser, db: Session = Depends(get_db)) -> list[int]:
    """Les identifiants des titres que j'aime.

    Volontairement des identifiants nus : l'interface n'a besoin que de savoir
    quel coeur remplir, et cette liste tient en memoire meme sur une grosse
    bibliotheque.
    """
    moi = user_row(db, user)
    return list(db.scalars(select(TrackLike.track_id).where(TrackLike.user_id == moi.id)))


@router.get("/tracks", response_model=list[TrackOut])
def my_liked_tracks(user: CurrentUser, db: Session = Depends(get_db)) -> list[TrackOut]:
    """Mes titres aimes, le dernier ajoute en premier."""
    moi = user_row(db, user)
    rows = db.execute(
        _TRACK_SELECT.join(TrackLike, TrackLike.track_id == Track.id)
        .where(TrackLike.user_id == moi.id)
        .order_by(TrackLike.created_at.desc(), TrackLike.id.desc())
    )
    return [_track_out(*row) for row in rows]


@router.put("/{track_id}", status_code=204)
def like(track_id: int, user: CurrentUser, db: Session = Depends(get_db)) -> None:
    """Aimer un titre. Deux fois de suite ne change rien, et ne fait pas d'erreur."""
    moi = user_row(db, user)
    if not db.scalar(select(Track.id).where(Track.id == track_id)):
        raise HTTPException(status_code=404, detail="Titre introuvable")
    existe = db.scalar(
        select(TrackLike.id).where(
            TrackLike.user_id == moi.id, TrackLike.track_id == track_id
        )
    )
    if existe is None:
        db.add(TrackLike(user_id=moi.id, track_id=track_id))
        db.commit()


@router.delete("/{track_id}", status_code=204)
def unlike(track_id: int, user: CurrentUser, db: Session = Depends(get_db)) -> None:
    """Ne plus aimer. Silencieux si on ne l'aimait pas : le resultat est le meme."""
    moi = user_row(db, user)
    db.execute(
        delete(TrackLike).where(
            TrackLike.user_id == moi.id, TrackLike.track_id == track_id
        )
    )
    db.commit()
