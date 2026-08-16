from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import CurrentUser
from app.db import get_db
from app.models import Album, Track
from app.services import covers, s3

router = APIRouter(prefix="/api", tags=["lecture"])

AUDIO_MIME_TYPES = {
    "mp3": "audio/mpeg",
    "flac": "audio/flac",
    "m4a": "audio/mp4",
    "mp4": "audio/mp4",
    "aac": "audio/aac",
    "ogg": "audio/ogg",
    "oga": "audio/ogg",
    "opus": "audio/ogg",
    "wav": "audio/wav",
    "wma": "audio/x-ms-wma",
}


@router.get("/tracks/{track_id}/stream", response_class=RedirectResponse)
def stream_track(
    track_id: int, user: CurrentUser, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Redirige vers une URL presignee au lieu de relayer le flux.

    Le navigateur telecharge alors directement depuis MinIO : il gere lui-meme
    les requetes Range (donc le deplacement dans le morceau) et l'API ne
    consomme ni bande passante ni worker pendant la lecture.
    """
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=404, detail="Piste introuvable")

    url = s3.public_stream_url(
        track.object_key, content_type=AUDIO_MIME_TYPES.get(track.format or "")
    )
    response = RedirectResponse(url=url, status_code=302)
    # L'URL est signee et expire : elle ne doit jamais etre mise en cache.
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/albums/{album_id}/cover")
def album_cover(
    album_id: int,
    user: CurrentUser,
    size: str = Query(default="thumb", pattern="^(thumb|full)$"),
    db: Session = Depends(get_db),
) -> FileResponse:
    album = db.get(Album, album_id)
    if album is None or album.cover_file is None:
        raise HTTPException(status_code=404, detail="Pas de pochette")

    path = covers.cover_path(album_id, thumb=(size == "thumb"))
    if not path.exists():
        raise HTTPException(status_code=404, detail="Pas de pochette")

    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )
