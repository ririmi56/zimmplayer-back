"""Favoris : les albums qu'on garde sous la main.

Meme forme que les likes de titres, et pour la meme raison : `AlbumOut` est
assemble par le catalogue et la recherche, qui n'ont pas a connaitre la
personne connectee. L'interface recupere donc une fois la liste de SES favoris
(`GET /api/favorites`) et s'en sert sur chaque vignette, plutot que de faire
descendre un champ « en favori » dans toutes les chaines d'assemblage.

Filtrer et classer, en revanche, se font au catalogue (`GET /api/albums`) :
c'est lui qui pagine.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.auth import CurrentUser, user_row
from app.db import get_db
from app.models import Album, AlbumFavorite

router = APIRouter(prefix="/api/favorites", tags=["favoris"])


@router.get("", response_model=list[int])
def my_favorites(user: CurrentUser, db: Session = Depends(get_db)) -> list[int]:
    """Les identifiants des albums que j'ai mis en favori.

    Des identifiants nus, comme pour les likes : l'interface n'a besoin que de
    savoir quelle etoile remplir.
    """
    moi = user_row(db, user)
    return list(
        db.scalars(select(AlbumFavorite.album_id).where(AlbumFavorite.user_id == moi.id))
    )


@router.put("/{album_id}", status_code=204)
def favorite(album_id: int, user: CurrentUser, db: Session = Depends(get_db)) -> None:
    """Mettre en favori. Deux fois de suite ne change rien, et ne fait pas d'erreur."""
    moi = user_row(db, user)
    if not db.scalar(select(Album.id).where(Album.id == album_id)):
        raise HTTPException(status_code=404, detail="Album introuvable")
    existe = db.scalar(
        select(AlbumFavorite.id).where(
            AlbumFavorite.user_id == moi.id, AlbumFavorite.album_id == album_id
        )
    )
    if existe is None:
        db.add(AlbumFavorite(user_id=moi.id, album_id=album_id))
        db.commit()


@router.delete("/{album_id}", status_code=204)
def unfavorite(album_id: int, user: CurrentUser, db: Session = Depends(get_db)) -> None:
    """Retirer des favoris. Silencieux s'il n'y etait pas : le resultat est le meme."""
    moi = user_row(db, user)
    db.execute(
        delete(AlbumFavorite).where(
            AlbumFavorite.user_id == moi.id, AlbumFavorite.album_id == album_id
        )
    )
    db.commit()
