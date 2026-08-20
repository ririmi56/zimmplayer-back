from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import AdminUser, is_super_admin
from app.db import get_db
from app.models import Listen, Playlist, ScanError, ScanRun, User
from app.schemas import AdminUpdate, ScanErrorOut, ScanRunOut, UserOut
from app.services import scanner

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.post("/scan", response_model=ScanRunOut, status_code=202)
def start_scan(
    user: AdminUser,
    background: BackgroundTasks,
    force: bool = Query(
        default=False,
        description=(
            "Relit tous les fichiers au lieu des seuls modifies. A utiliser "
            "apres une evolution de la lecture des tags : un scan normal ne "
            "retelecharge jamais un fichier dont l'etag n'a pas change."
        ),
    ),
    db: Session = Depends(get_db),
) -> ScanRun:
    if scanner.is_running(db):
        raise HTTPException(status_code=409, detail="Un scan est deja en cours")
    run = scanner.start_run(db)
    background.add_task(scanner.run_scan, run.id, force)
    return run


@router.get("/scan/status", response_model=ScanRunOut | None)
def scan_status(user: AdminUser, db: Session = Depends(get_db)) -> ScanRun | None:
    """Dernier scan lance, en cours ou termine."""
    return db.scalar(select(ScanRun).order_by(ScanRun.id.desc()).limit(1))


@router.get("/scan/history", response_model=list[ScanRunOut])
def scan_history(
    user: AdminUser, limit: int = 10, db: Session = Depends(get_db)
) -> list[ScanRun]:
    return list(db.scalars(select(ScanRun).order_by(ScanRun.id.desc()).limit(limit)))


@router.get("/scan/errors", response_model=list[ScanErrorOut])
def scan_errors(
    user: AdminUser,
    run_id: int | None = None,
    limit: int = 200,
    db: Session = Depends(get_db),
) -> list[ScanError]:
    """Erreurs du scan demande, ou du dernier scan par defaut."""
    if run_id is None:
        run_id = db.scalar(select(ScanRun.id).order_by(ScanRun.id.desc()).limit(1))
        if run_id is None:
            return []
    return list(
        db.scalars(
            select(ScanError)
            .where(ScanError.scan_run_id == run_id)
            .order_by(ScanError.id)
            .limit(limit)
        )
    )


# --- Utilisateurs ----------------------------------------------------------


def _sortie(row: User, playlists: int = 0, ecoutes: int = 0) -> UserOut:
    return UserOut(
        id=row.id,
        subject=row.subject,
        name=row.name,
        email=row.email,
        playlist_count=playlists,
        listen_count=ecoutes,
        # Un super-administrateur est admin sans etre marque en base : son
        # role vient de la configuration, pas d'une promotion.
        is_admin=row.is_admin or is_super_admin(row.subject, row.email),
        is_super_admin=is_super_admin(row.subject, row.email),
        last_seen_at=row.last_seen_at,
    )


@router.get("/users", response_model=list[UserOut])
def list_users(user: AdminUser, db: Session = Depends(get_db)) -> list[UserOut]:
    """Les personnes deja connectees, les plus recentes d'abord.

    Il n'y a personne d'autre a proposer : aucune API OIDC standard ne permet
    de lister les comptes d'un fournisseur. On ne promeut donc que quelqu'un
    qui s'est deja connecte au moins une fois.
    """
    # Deux sous-requetes correlees : l'ecran de suppression doit annoncer ce
    # qui va disparaitre, et ce qui va survivre.
    playlists = (
        select(func.count(Playlist.id))
        .where(Playlist.owner_id == User.id)
        .correlate(User)
        .scalar_subquery()
    )
    ecoutes = (
        select(func.count(Listen.id))
        .where(Listen.user_id == User.id)
        .correlate(User)
        .scalar_subquery()
    )
    rows = db.execute(
        select(User, playlists, ecoutes).order_by(User.last_seen_at.desc())
    )
    return [_sortie(row, n_playlists, n_ecoutes) for row, n_playlists, n_ecoutes in rows]


@router.put("/users/{user_id}/admin", response_model=UserOut)
def set_admin(
    user_id: int,
    body: AdminUpdate,
    user: AdminUser,
    db: Session = Depends(get_db),
) -> UserOut:
    """Accorde ou retire le role d'administrateur."""
    row = db.get(User, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")

    if is_super_admin(row.subject, row.email):
        raise HTTPException(
            status_code=409,
            detail=(
                "Ce compte est administrateur par la configuration du serveur "
                "(SUPER_ADMINS) : son role ne se change pas ici."
            ),
        )

    # Se retirer soi-meme le role fermerait la porte derriere soi, sans
    # avertissement et sans retour possible depuis l'interface.
    if not body.is_admin and row.subject == user.subject:
        raise HTTPException(
            status_code=409,
            detail="Vous ne pouvez pas retirer votre propre role d'administrateur.",
        )

    # Et sans super-administrateur configure, retirer le dernier rendrait
    # l'application inadministrable autrement qu'en base.
    if not body.is_admin and row.is_admin:
        restants = db.scalar(
            select(func.count(User.id)).where(User.is_admin, User.id != row.id)
        )
        if not restants and not _un_super_admin_existe(db):
            raise HTTPException(
                status_code=409,
                detail=(
                    "C'est le dernier administrateur, et aucun compte n'est "
                    "nomme dans SUPER_ADMINS : le retirer rendrait "
                    "l'application inadministrable."
                ),
            )

    row.is_admin = body.is_admin
    db.commit()
    return _sortie(row)


def _un_super_admin_existe(db: Session) -> bool:
    """Un compte deja connu correspond-il a la configuration ?

    On ne se contente pas de regarder si SUPER_ADMINS est renseigne : une
    entree qui ne correspond a personne ne protege de rien.
    """
    return any(
        is_super_admin(subject, email)
        for subject, email in db.execute(select(User.subject, User.email))
    )


@router.delete("/users/{user_id}", status_code=204)
def delete_user(user_id: int, user: AdminUser, db: Session = Depends(get_db)) -> None:
    """Retire une personne de la base, pour faire le menage.

    Suppression au sens de l'annuaire, pas de l'histoire : les ecoutes sont
    detachees et non effacees (`listens.user_id` passe a NULL), si bien que les
    totaux et le classement des titres restent justes. Partent en revanche avec
    le compte, par cascade : ses playlists, ses likes, ses favoris et les
    partages dont il beneficiait.

    Sans OIDC, ou si la personne se reconnecte, la ligne reapparait a la
    prochaine visite : ce menage vaut pour qui n'est jamais revenu.
    """
    row = db.get(User, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")

    # Se supprimer soi-meme fermerait la porte derriere soi, comme se retirer
    # son propre role.
    if row.subject == user.subject:
        raise HTTPException(
            status_code=409, detail="Vous ne pouvez pas supprimer votre propre compte."
        )

    # Un super-administrateur revient administrateur des sa prochaine visite :
    # supprimer sa ligne ne ferait que perdre ses playlists pour rien.
    if is_super_admin(row.subject, row.email):
        raise HTTPException(
            status_code=409,
            detail=(
                "Ce compte est administrateur par la configuration du serveur "
                "(SUPER_ADMINS) : le supprimer ne ferait que le recreer a sa "
                "prochaine connexion."
            ),
        )

    db.delete(row)
    db.commit()
