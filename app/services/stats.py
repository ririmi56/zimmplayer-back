"""Enregistrement des ecoutes et calcul des statistiques.

Deux moities bien distinctes.

Les statistiques du CATALOGUE se calculent sur l'existant : compter des
titres, des albums, cumuler des durees. Elles sont justes depuis toujours.

Les statistiques par PERSONNE, elles, n'ont aucun passe : rien n'etait
enregistre, et `queue_items` est efface au fur et a mesure. Elles repartent
donc de zero le jour ou ce code est deploye — il n'y a rien a reconstituer.
"""

import logging
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.models import (
    Album,
    Artist,
    Listen,
    QueueAddition,
    Session,
    SessionPresence,
    Track,
    User,
    utcnow,
)

logger = logging.getLogger(__name__)

#: Au-dela de quoi une ecoute compte : la moitie du titre, ou quatre minutes.
#: Regle de Last.fm. Parcourir un album en sautant de titre en titre ne doit
#: pas gonfler les compteurs, et un morceau vraiment ecoute doit compter meme
#: si l'on coupe avant la fin.
PLAFOND_S = 240.0

#: Au-dela de ce silence, on ne considere plus quelqu'un comme present dans une
#: session. Le navigateur interroge la session toutes les 1,5 s : une minute
#: laisse passer une reprise de veille ou un rechargement de page.
PRESENCE_S = 60.0

#: On ne reecrit la presence que passe ce delai. Sans cela, chaque membre
#: provoquerait une ecriture toutes les 1,5 s, pour une information qui ne sert
#: qu'a la minute pres.
PRESENCE_ECRITURE_S = 10.0


def seuil(duration_s: float | None) -> float:
    """Secondes d'ecoute a partir desquelles un titre compte."""
    if not duration_s or duration_s <= 0:
        # Duree inconnue : on retombe sur le plafond, plutot que de compter
        # un titre des la premiere seconde.
        return PLAFOND_S
    return min(duration_s / 2, PLAFOND_S)


def marquer_present(db: DbSession, session_id: int, user_id: int) -> None:
    """Note que cette personne suit cette session, maintenant."""
    ligne = db.scalar(
        select(SessionPresence).where(
            SessionPresence.session_id == session_id, SessionPresence.user_id == user_id
        )
    )
    maintenant = utcnow()
    if ligne is None:
        db.add(SessionPresence(session_id=session_id, user_id=user_id, last_seen_at=maintenant))
        db.commit()
        return
    if (maintenant - ligne.last_seen_at).total_seconds() >= PRESENCE_ECRITURE_S:
        ligne.last_seen_at = maintenant
        db.commit()


def presents(db: DbSession, session_id: int) -> list[int]:
    """Identifiants des personnes vues recemment dans cette session."""
    limite = utcnow() - timedelta(seconds=PRESENCE_S)
    return list(
        db.scalars(
            select(SessionPresence.user_id).where(
                SessionPresence.session_id == session_id,
                SessionPresence.last_seen_at >= limite,
            )
        )
    )


def enregistrer_ecoute(
    db: DbSession,
    user_ids: list[int],
    track_id: int,
    seconds: float,
    session: Session | None = None,
) -> int:
    """Enregistre une ecoute pour chacune des personnes concernees.

    En session, tout le monde ecoute la meme chose en meme temps : la ligne est
    donc dupliquee par personne presente. C'est ce qui permet de repondre a
    « combien ai-je ecoute », et non seulement « qu'ai-je fait jouer ».
    """
    if not user_ids:
        return 0
    for user_id in user_ids:
        db.add(
            Listen(
                user_id=user_id,
                track_id=track_id,
                session_id=session.id if session else None,
                session_name=session.name if session else None,
                seconds=seconds,
            )
        )
    db.commit()
    return len(user_ids)


def enregistrer_ajout(
    db: DbSession, user_id: int, track_ids: list[int], session: Session
) -> None:
    """Trace un ajout a la file. Voir `QueueAddition` pour le pourquoi."""
    for track_id in track_ids:
        db.add(
            QueueAddition(
                user_id=user_id,
                track_id=track_id,
                session_id=session.id,
                session_name=session.name,
            )
        )


# --- Calcul ----------------------------------------------------------------


def catalogue(db: DbSession) -> dict:
    """Ce que contient le serveur. Ne depend d'aucun enregistrement."""
    total_s = db.scalar(select(func.coalesce(func.sum(Track.duration_s), 0.0))) or 0.0
    formats = db.execute(
        select(Track.format, func.count(Track.id))
        .group_by(Track.format)
        .order_by(func.count(Track.id).desc())
    ).all()
    genres = db.execute(
        select(Track.genre, func.count(Track.id))
        .where(Track.genre.is_not(None))
        .group_by(Track.genre)
        .order_by(func.count(Track.id).desc())
        .limit(8)
    ).all()
    return {
        "tracks": db.scalar(select(func.count(Track.id))) or 0,
        "albums": db.scalar(select(func.count(Album.id))) or 0,
        "artists": db.scalar(select(func.count(Artist.id))) or 0,
        "total_seconds": float(total_s),
        "total_bytes": int(db.scalar(select(func.coalesce(func.sum(Track.size_bytes), 0))) or 0),
        # Un titre sans duree fausse le cumul sans qu'on le voie : on le dit.
        "tracks_without_duration": db.scalar(
            select(func.count(Track.id)).where(Track.duration_s.is_(None))
        ) or 0,
        "formats": [{"label": f or "inconnu", "count": n} for f, n in formats],
        "genres": [{"label": g, "count": n} for g, n in genres],
    }


def _agregat_ecoutes(db: DbSession, colonne, depuis=None):
    stmt = select(
        colonne,
        func.count(Listen.id),
        func.coalesce(func.sum(Listen.seconds), 0.0),
    ).group_by(colonne)
    if depuis is not None:
        stmt = stmt.where(Listen.listened_at >= depuis)
    return db.execute(stmt).all()


def par_utilisateur(db: DbSession) -> list[dict]:
    """Ecoutes et ajouts, par personne. Reserve aux administrateurs."""
    ecoutes = {
        user_id: (n, secondes)
        for user_id, n, secondes in _agregat_ecoutes(db, Listen.user_id)
    }
    ajouts = {
        user_id: n
        for user_id, n in db.execute(
            select(QueueAddition.user_id, func.count(QueueAddition.id)).group_by(
                QueueAddition.user_id
            )
        ).all()
    }
    derniere = {
        user_id: quand
        for user_id, quand in db.execute(
            select(Listen.user_id, func.max(Listen.listened_at)).group_by(Listen.user_id)
        ).all()
    }

    lignes = []
    for user in db.scalars(select(User).order_by(User.name)):
        n, secondes = ecoutes.get(user.id, (0, 0.0))
        lignes.append(
            {
                "user_id": user.id,
                "name": user.name,
                "listens": n,
                "seconds": float(secondes),
                "queue_additions": ajouts.get(user.id, 0),
                "last_listen_at": derniere.get(user.id),
            }
        )
    # Les plus actifs d'abord : une liste triee par nom obligerait a chercher.
    lignes.sort(key=lambda ligne: (-ligne["seconds"], ligne["name"]))
    return lignes


def par_session(db: DbSession) -> list[dict]:
    """Ecoutes par session, y compris celles qui n'existent plus.

    D'ou le regroupement sur le nom recopie : une session supprimee emporterait
    sinon tout son historique.
    """
    lignes = db.execute(
        select(
            Listen.session_name,
            func.count(Listen.id),
            func.coalesce(func.sum(Listen.seconds), 0.0),
            func.count(func.distinct(Listen.user_id)),
            func.max(Listen.listened_at),
        )
        .where(Listen.session_name.is_not(None))
        .group_by(Listen.session_name)
        .order_by(func.sum(Listen.seconds).desc())
    ).all()
    vivantes = set(db.scalars(select(Session.name)))
    return [
        {
            "name": nom,
            "listens": n,
            "seconds": float(secondes),
            "listeners": auditeurs,
            "last_listen_at": quand,
            "still_open": nom in vivantes,
        }
        for nom, n, secondes, auditeurs, quand in lignes
    ]


def ecoute_globale(db: DbSession) -> dict:
    """Totaux d'ecoute, tous confondus.

    Les secondes sont comptees PAR PERSONNE : dans une session a trois, une
    heure de musique compte trois heures d'ecoute. C'est voulu — c'est bien du
    temps d'ecoute cumule — mais il faut le dire, sinon le total surprend.
    """
    n, secondes = db.execute(
        select(func.count(Listen.id), func.coalesce(func.sum(Listen.seconds), 0.0))
    ).one()
    titres = db.scalar(select(func.count(func.distinct(Listen.track_id)))) or 0
    return {
        "listens": n or 0,
        "seconds": float(secondes or 0.0),
        "distinct_tracks": titres,
        "queue_additions": db.scalar(select(func.count(QueueAddition.id))) or 0,
    }


def top_titres(db: DbSession, limite: int = 10) -> list[dict]:
    lignes = db.execute(
        select(Track, Artist.name, func.count(Listen.id))
        .join(Listen, Listen.track_id == Track.id)
        .join(Artist, Artist.id == Track.artist_id)
        .group_by(Track.id, Artist.name)
        .order_by(func.count(Listen.id).desc())
        .limit(limite)
    ).all()
    return [
        {"track_id": t.id, "title": t.title, "artist_name": a, "listens": n}
        for t, a, n in lignes
    ]
