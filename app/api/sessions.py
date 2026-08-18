"""Sessions d'ecoute : file d'attente partagee et controle de la lecture.

Tout le monde peut agir sur la lecture pour l'instant. Les verifications de
role viendront se greffer ici, sur `user` deja injecte par `CurrentUser`.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.auth import CurrentUser, user_row
from app.db import get_db
from app.models import Album, Artist, QueueItem, Session, Track
from app.schemas import (
    MoveItem,
    QueueAdd,
    QueueItemOut,
    SeekRequest,
    SessionCreate,
    SessionDetail,
    SessionOut,
    TrackOut,
)
from app.services import queue as queue_service, stats, snapoutput

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _track_out(track: Track, album: Album, artist: Artist) -> TrackOut:
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
        genre=track.genre,
        has_lyrics=False,
        overrides=track.overrides or {},
    )


def _items_out(db: DbSession, session: Session) -> list[QueueItemOut]:
    """Charge la file en une requete plutot qu'un aller-retour par element."""
    rows = db.execute(
        select(QueueItem, Track, Album, Artist)
        .join(Track, Track.id == QueueItem.track_id)
        .join(Album, Album.id == Track.album_id)
        .join(Artist, Artist.id == Track.artist_id)
        .where(QueueItem.session_id == session.id)
        .order_by(QueueItem.position, QueueItem.id)
    )
    return [
        QueueItemOut(
            id=item.id,
            position=item.position,
            added_by=item.added_by,
            added_at=item.added_at,
            track=_track_out(track, album, artist),
        )
        for item, track, album, artist in rows
    ]


def _detail(db: DbSession, session: Session) -> SessionDetail:
    items = _items_out(db, session)
    # La sortie audio serveur, quand elle est etablie, connait la position
    # exacte ; sinon (flux pas encore monte) on extrapole depuis le dernier
    # ordre recu.
    live_position = snapoutput.live_position(session)
    return SessionDetail(
        id=session.id,
        name=session.name,
        created_by=session.created_by,
        created_at=session.created_at,
        snapcast_stream_id=session.snapcast_stream_id,
        item_count=len(items),
        items=items,
        current_item_id=session.current_item_id,
        is_playing=session.is_playing,
        position_s=live_position if live_position is not None else queue_service.effective_position(session),
        command_seq=session.command_seq,
    )


def _get(db: DbSession, session_id: int) -> Session:
    session = db.get(Session, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session introuvable")
    return session


def _get_item(db: DbSession, session: Session, item_id: int) -> QueueItem:
    item = db.get(QueueItem, item_id)
    if item is None or item.session_id != session.id:
        raise HTTPException(status_code=404, detail="Element introuvable")
    return item


@router.get("", response_model=list[SessionOut])
def list_sessions(user: CurrentUser, db: DbSession = Depends(get_db)) -> list[SessionOut]:
    rows = db.execute(
        select(Session, func.count(QueueItem.id))
        .outerjoin(QueueItem, QueueItem.session_id == Session.id)
        .group_by(Session.id)
        .order_by(Session.name)
    )
    return [
        SessionOut(
            id=session.id,
            name=session.name,
            created_by=session.created_by,
            created_at=session.created_at,
            snapcast_stream_id=session.snapcast_stream_id,
            item_count=count,
        )
        for session, count in rows
    ]


@router.post("", response_model=SessionDetail, status_code=201)
def create_session(
    payload: SessionCreate, user: CurrentUser, db: DbSession = Depends(get_db)
) -> SessionDetail:
    name = payload.name.strip()
    if db.scalar(select(Session.id).where(Session.name == name)):
        raise HTTPException(status_code=409, detail="Ce nom de session existe deja")

    session = Session(name=name, created_by=user.name)
    db.add(session)
    # Commit avant de provisionner : la sortie audio lit la session depuis un
    # thread separe, sur sa propre connexion. Sous REPEATABLE READ, elle ne
    # verrait jamais une ligne encore seulement flushee dans cette transaction
    # -- et s'arreterait aussitot, la croyant supprimee (voir _read_desired).
    db.commit()
    db.refresh(session)

    # Rejoindre une session, c'est se synchroniser via Snapcast : son flux est
    # donc monte des la creation, pas a la demande.
    try:
        snapoutput.provision(db, session)
    except Exception as exc:
        db.delete(session)
        db.commit()
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    db.commit()
    db.refresh(session)
    return _detail(db, session)


@router.get("/{session_id}", response_model=SessionDetail)
def get_session(
    session_id: int, user: CurrentUser, db: DbSession = Depends(get_db)
) -> SessionDetail:
    """Detail d'une session, et seul signal d'appartenance dont on dispose.

    Le navigateur interroge cette route en boucle tant qu'il suit une session,
    et seulement celle-la : c'est ce qui permet au serveur de savoir qui
    ecoute, information qui ne vivait jusqu'ici que dans le navigateur. Voir
    services/stats.marquer_present pour l'approximation que cela represente.
    """
    session = _get(db, session_id)
    stats.marquer_present(db, session.id, user_row(db, user).id)
    return _detail(db, session)


@router.delete("/{session_id}", status_code=204)
def delete_session(
    session_id: int, user: CurrentUser, db: DbSession = Depends(get_db)
) -> None:
    session = _get(db, session_id)
    snapoutput.teardown(db, session)
    db.delete(session)
    db.commit()


@router.post("/{session_id}/queue", response_model=SessionDetail)
def add_to_queue(
    session_id: int,
    payload: QueueAdd,
    user: CurrentUser,
    db: DbSession = Depends(get_db),
) -> SessionDetail:
    session = _get(db, session_id)

    track_ids = list(payload.track_ids)
    if payload.album_id is not None:
        track_ids += list(
            db.scalars(
                select(Track.id)
                .where(Track.album_id == payload.album_id)
                .order_by(
                    Track.disc_no.is_(None), Track.disc_no, Track.track_no, Track.title
                )
            )
        )
    if not track_ids:
        raise HTTPException(status_code=400, detail="Aucune piste a ajouter")

    queue_service.append_tracks(db, session, track_ids, user.name)
    # Trace a part : `queue_items` disparait des qu'on retire le titre ou vide
    # la file, l'information serait perdue pour les statistiques.
    stats.enregistrer_ajout(db, user_row(db, user).id, track_ids, session)
    db.commit()
    db.refresh(session)
    return _detail(db, session)


@router.delete("/{session_id}/queue/{item_id}", response_model=SessionDetail)
def remove_from_queue(
    session_id: int, item_id: int, user: CurrentUser, db: DbSession = Depends(get_db)
) -> SessionDetail:
    session = _get(db, session_id)
    queue_service.remove_item(db, session, _get_item(db, session, item_id))
    db.commit()
    db.refresh(session)
    return _detail(db, session)


@router.delete("/{session_id}/queue", response_model=SessionDetail)
def clear_queue(
    session_id: int,
    user: CurrentUser,
    keep_current: bool = False,
    db: DbSession = Depends(get_db),
) -> SessionDetail:
    session = _get(db, session_id)
    queue_service.clear(db, session, keep_current=keep_current)
    db.commit()
    db.refresh(session)
    return _detail(db, session)


@router.post("/{session_id}/queue/{item_id}/move", response_model=SessionDetail)
def move_in_queue(
    session_id: int,
    item_id: int,
    payload: MoveItem,
    user: CurrentUser,
    db: DbSession = Depends(get_db),
) -> SessionDetail:
    session = _get(db, session_id)
    queue_service.move_item(db, session, _get_item(db, session, item_id), payload.to_index)
    db.commit()
    db.refresh(session)
    return _detail(db, session)


@router.post("/{session_id}/play", response_model=SessionDetail)
def play(
    session_id: int,
    user: CurrentUser,
    item_id: int | None = None,
    db: DbSession = Depends(get_db),
) -> SessionDetail:
    session = _get(db, session_id)
    item = _get_item(db, session, item_id) if item_id is not None else None
    queue_service.play(db, session, item)
    return _apply(db, session)


@router.post("/{session_id}/pause", response_model=SessionDetail)
def pause(
    session_id: int, user: CurrentUser, db: DbSession = Depends(get_db)
) -> SessionDetail:
    session = _get(db, session_id)
    queue_service.pause(db, session)
    return _apply(db, session)


@router.post("/{session_id}/next", response_model=SessionDetail)
def next_track(
    session_id: int, user: CurrentUser, db: DbSession = Depends(get_db)
) -> SessionDetail:
    session = _get(db, session_id)
    queue_service.next_item(db, session)
    return _apply(db, session)


@router.post("/{session_id}/previous", response_model=SessionDetail)
def previous_track(
    session_id: int, user: CurrentUser, db: DbSession = Depends(get_db)
) -> SessionDetail:
    session = _get(db, session_id)
    queue_service.previous_item(db, session)
    return _apply(db, session)


@router.post("/{session_id}/seek", response_model=SessionDetail)
def seek(
    session_id: int,
    payload: SeekRequest,
    user: CurrentUser,
    db: DbSession = Depends(get_db),
) -> SessionDetail:
    session = _get(db, session_id)
    queue_service.seek(db, session, payload.position_s)
    return _apply(db, session)


def _apply(db: DbSession, session: Session) -> SessionDetail:
    """Persiste l'etat puis, en mode snapcast, le repercute sur la sortie audio."""
    db.commit()
    db.refresh(session)
    snapoutput.sync(session)
    return _detail(db, session)
