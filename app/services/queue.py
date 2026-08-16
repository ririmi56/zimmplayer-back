"""File d'attente partagee et etat de lecture d'une session.

La file est une playlist ordonnee ; `session.current_item_id` designe l'element
en cours. Les elements ne sont pas consommes a la lecture : avancer ne fait que
deplacer ce pointeur. On garde ainsi gratuitement l'historique, le retour
arriere et la relecture, et « vider la file » reste une action explicite.
"""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.models import QueueItem, Session, Track, utcnow

# Au-dela de ce seuil, « precedent » rembobine la piste au lieu de reculer.
RESTART_THRESHOLD_S = 3.0


def effective_position(session: Session, now: datetime | None = None) -> float:
    """Position reelle, extrapolee depuis le dernier changement d'etat."""
    if not session.is_playing:
        return session.position_s
    elapsed = ((now or utcnow()) - session.updated_at).total_seconds()
    return max(0.0, session.position_s + elapsed)


def _touch(session: Session, position_s: float | None = None) -> None:
    """Marque un ordre utilisateur. `command_seq` n'avance qu'ici, jamais quand
    la sortie audio remonte simplement sa position."""
    if position_s is not None:
        session.position_s = max(0.0, position_s)
    session.updated_at = utcnow()
    session.command_seq = (session.command_seq or 0) + 1


def ordered_items(db: DbSession, session_id: int) -> list[QueueItem]:
    return list(
        db.scalars(
            select(QueueItem)
            .where(QueueItem.session_id == session_id)
            .order_by(QueueItem.position, QueueItem.id)
        )
    )


def current_item(db: DbSession, session: Session) -> QueueItem | None:
    if session.current_item_id is None:
        return None
    item = db.get(QueueItem, session.current_item_id)
    # Le pointeur n'a pas de contrainte de cle etrangere : il peut designer un
    # element retire entre-temps.
    if item is None or item.session_id != session.id:
        session.current_item_id = None
        return None
    return item


def append_tracks(
    db: DbSession, session: Session, track_ids: list[int], added_by: str
) -> list[QueueItem]:
    """Ajoute des pistes a la fin, en ignorant silencieusement les inconnues."""
    existing = set(db.scalars(select(Track.id).where(Track.id.in_(track_ids))))
    next_position = (
        db.scalar(
            select(func.coalesce(func.max(QueueItem.position), -1)).where(
                QueueItem.session_id == session.id
            )
        )
        + 1
    )

    added: list[QueueItem] = []
    for track_id in track_ids:
        if track_id not in existing:
            continue
        item = QueueItem(
            session_id=session.id,
            track_id=track_id,
            position=next_position,
            added_by=added_by,
        )
        db.add(item)
        added.append(item)
        next_position += 1

    db.flush()
    # Premiere piste d'une file vide : elle devient d'office la piste courante.
    if added and session.current_item_id is None:
        session.current_item_id = added[0].id
        _touch(session, 0.0)
    return added


def remove_item(db: DbSession, session: Session, item: QueueItem) -> None:
    was_current = session.current_item_id == item.id
    following = _neighbour(db, session, item, forward=True)

    db.delete(item)
    db.flush()

    if was_current:
        session.current_item_id = following.id if following else None
        session.is_playing = session.is_playing and following is not None
        _touch(session, 0.0)
    _renumber(db, session)


def clear(db: DbSession, session: Session, keep_current: bool = False) -> None:
    current = current_item(db, session) if keep_current else None
    for item in ordered_items(db, session.id):
        if current is not None and item.id == current.id:
            continue
        db.delete(item)
    db.flush()
    if current is None:
        session.current_item_id = None
        session.is_playing = False
        _touch(session, 0.0)
    _renumber(db, session)


def move_item(db: DbSession, session: Session, item: QueueItem, to_index: int) -> None:
    items = ordered_items(db, session.id)
    items = [i for i in items if i.id != item.id]
    to_index = max(0, min(to_index, len(items)))
    items.insert(to_index, item)
    for position, entry in enumerate(items):
        entry.position = position
    db.flush()


def _renumber(db: DbSession, session: Session) -> None:
    for position, entry in enumerate(ordered_items(db, session.id)):
        entry.position = position
    db.flush()


def _neighbour(
    db: DbSession, session: Session, item: QueueItem, forward: bool
) -> QueueItem | None:
    items = ordered_items(db, session.id)
    ids = [i.id for i in items]
    if item.id not in ids:
        return None
    index = ids.index(item.id) + (1 if forward else -1)
    if index < 0 or index >= len(items):
        return None
    return items[index]


def play(db: DbSession, session: Session, item: QueueItem | None = None) -> None:
    if item is not None:
        session.current_item_id = item.id
        _touch(session, 0.0)
    elif current_item(db, session) is None:
        first = next(iter(ordered_items(db, session.id)), None)
        if first is None:
            return
        session.current_item_id = first.id
        _touch(session, 0.0)
    else:
        _touch(session, session.position_s)
    session.is_playing = True


def pause(db: DbSession, session: Session) -> None:
    _touch(session, effective_position(session))
    session.is_playing = False


def seek(db: DbSession, session: Session, position_s: float) -> None:
    _touch(session, position_s)


def next_item(db: DbSession, session: Session) -> None:
    item = current_item(db, session)
    following = _neighbour(db, session, item, forward=True) if item else None
    if following is None:
        # Fin de file : on s'arrete sur place plutot que de boucler.
        session.is_playing = False
        _touch(session, 0.0)
        return
    session.current_item_id = following.id
    _touch(session, 0.0)


def previous_item(db: DbSession, session: Session) -> None:
    item = current_item(db, session)
    if item is None:
        return
    if effective_position(session) > RESTART_THRESHOLD_S:
        _touch(session, 0.0)
        return
    preceding = _neighbour(db, session, item, forward=False)
    if preceding is not None:
        session.current_item_id = preceding.id
    _touch(session, 0.0)
