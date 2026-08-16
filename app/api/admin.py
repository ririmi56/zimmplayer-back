from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import CurrentUser
from app.db import get_db
from app.models import ScanError, ScanRun
from app.schemas import ScanErrorOut, ScanRunOut
from app.services import scanner

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.post("/scan", response_model=ScanRunOut, status_code=202)
def start_scan(
    user: CurrentUser,
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
def scan_status(user: CurrentUser, db: Session = Depends(get_db)) -> ScanRun | None:
    """Dernier scan lance, en cours ou termine."""
    return db.scalar(select(ScanRun).order_by(ScanRun.id.desc()).limit(1))


@router.get("/scan/history", response_model=list[ScanRunOut])
def scan_history(
    user: CurrentUser, limit: int = 10, db: Session = Depends(get_db)
) -> list[ScanRun]:
    return list(db.scalars(select(ScanRun).order_by(ScanRun.id.desc()).limit(limit)))


@router.get("/scan/errors", response_model=list[ScanErrorOut])
def scan_errors(
    user: CurrentUser,
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
