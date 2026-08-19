"""Statistiques du serveur, et enregistrement des ecoutes solo."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import AdminUser, CurrentUser, user_row
from app.db import get_db
from app.models import Track
from app.schemas import GlobalStats, ListenReport, SessionStats, UserStats
from app.services import stats as service

router = APIRouter(prefix="/api/stats", tags=["statistiques"])


@router.get("", response_model=GlobalStats)
def global_stats(user: CurrentUser, db: Session = Depends(get_db)) -> GlobalStats:
    """Ce que contient le serveur, et ce qui y a ete ecoute. Visible par tous."""
    return GlobalStats(
        catalogue=service.catalogue(db),
        listening=service.ecoute_globale(db),
        top_tracks=service.top_titres(db),
        sessions=service.par_session(db),
    )


@router.get("/users", response_model=list[UserStats])
def user_stats(user: AdminUser, db: Session = Depends(get_db)) -> list[UserStats]:
    """Ecoutes et ajouts par personne. Reserve aux administrateurs.

    C'est le seul endroit ou l'activite de quelqu'un est visible par un autre :
    la route est donc gardee, et non simplement l'onglet qui l'affiche.
    """
    return [UserStats(**ligne) for ligne in service.par_utilisateur(db)]


@router.post("/listens", status_code=204)
def report_listen(
    body: ListenReport, user: CurrentUser, db: Session = Depends(get_db)
) -> None:
    """Ecoute solo, annoncee par le navigateur.

    Hors session, l'API ne voit passer qu'une redirection vers le stockage :
    elle ne peut pas savoir ce qui a ete ecoute, ni combien de temps. Seul le
    navigateur le sait, d'ou cette annonce.

    Les valeurs viennent donc du client et sont verifiees ici : la duree est
    bornee par celle du titre, et le seuil est reapplique. Cela n'empeche pas
    un client de mentir dans ces bornes — le compteur n'est pas une preuve.
    """
    track = db.get(Track, body.track_id)
    if track is None:
        raise HTTPException(status_code=404, detail="Piste introuvable")

    secondes = min(body.seconds, track.duration_s or body.seconds)
    if secondes < service.seuil(track.duration_s):
        # En dessous du seuil, on ne compte pas : ce n'est pas une erreur.
        return

    service.enregistrer_ecoute(db, [user_row(db, user).id], track.id, secondes)
