"""Controle du serveur Snapcast : membres, volumes, noms, pieces.

Le navigateur ne parle jamais directement a snapserver : tout passe par l'API.
Cela evite d'exposer snapserver au reseau des postes, de gerer du CORS, et
garde une seule origine a servir derriere nginx.
"""

import asyncio
import logging

import websockets
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as DbSession

from app.auth import CurrentUser
from app.db import SessionLocal, get_db
from app.services import appsettings, snapcast

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/snapcast", tags=["snapcast"])


class SnapcastConfig(BaseModel):
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    # Port HTTP/WebSocket de snapserver, par lequel passe l'audio du navigateur.
    http_port: int = Field(default=1780, ge=1, le=65535)
    enabled: bool
    advertise_host: str = Field(min_length=1, max_length=255)


class VolumeUpdate(BaseModel):
    percent: int = Field(ge=0, le=100)
    muted: bool = False


class NameUpdate(BaseModel):
    name: str = Field(max_length=100)


class StreamUpdate(BaseModel):
    stream_id: str


class ClientsUpdate(BaseModel):
    client_ids: list[str]


class MuteUpdate(BaseModel):
    muted: bool


def _config(db: DbSession) -> dict:
    return appsettings.snapcast_config(db)


def _call(db: DbSession, method: str, params: dict | None = None):
    config = _config(db)
    if not config["enabled"]:
        raise HTTPException(status_code=409, detail="Snapcast est desactive")
    try:
        return snapcast.call(config["host"], config["http_port"], method, params)
    except snapcast.SnapcastError as exc:
        raise HTTPException(status_code=502, detail=f"Snapserver: {exc}") from exc


@router.get("/config")
def get_config(user: CurrentUser, db: DbSession = Depends(get_db)) -> dict:
    return _config(db)


@router.put("/config")
def update_config(
    payload: SnapcastConfig, user: CurrentUser, db: DbSession = Depends(get_db)
) -> dict:
    appsettings.set_value(db, appsettings.SNAPCAST_HOST, payload.host)
    appsettings.set_value(db, appsettings.SNAPCAST_PORT, str(payload.port))
    appsettings.set_value(db, appsettings.SNAPCAST_HTTP_PORT, str(payload.http_port))
    appsettings.set_value(db, appsettings.SNAPCAST_ENABLED, str(payload.enabled))
    appsettings.set_value(
        db, appsettings.SNAPCAST_ADVERTISE_HOST, payload.advertise_host
    )
    db.commit()
    return _config(db)


@router.get("/status")
def get_status(user: CurrentUser, db: DbSession = Depends(get_db)) -> dict:
    """Etat du serveur. Ne leve jamais : l'interface doit pouvoir afficher
    proprement un snapserver injoignable plutot que de casser la page."""
    config = _config(db)
    if not config["enabled"]:
        return {"connected": False, "error": None, "groups": [], "streams": []}
    try:
        raw = snapcast.call(config["host"], config["http_port"], "Server.GetStatus")
    except snapcast.SnapcastError as exc:
        return {"connected": False, "error": str(exc), "groups": [], "streams": []}
    return {"connected": True, "error": None, **snapcast.normalise_status(raw)}


@router.post("/clients/{client_id}/volume")
def set_client_volume(
    client_id: str,
    payload: VolumeUpdate,
    user: CurrentUser,
    db: DbSession = Depends(get_db),
) -> dict:
    return _call(
        db,
        "Client.SetVolume",
        {"id": client_id, "volume": {"percent": payload.percent, "muted": payload.muted}},
    )


@router.put("/clients/{client_id}/name")
def set_client_name(
    client_id: str,
    payload: NameUpdate,
    user: CurrentUser,
    db: DbSession = Depends(get_db),
) -> dict:
    return _call(db, "Client.SetName", {"id": client_id, "name": payload.name})


@router.post("/groups/{group_id}/stream")
def set_group_stream(
    group_id: str,
    payload: StreamUpdate,
    user: CurrentUser,
    db: DbSession = Depends(get_db),
) -> dict:
    return _call(
        db, "Group.SetStream", {"id": group_id, "stream_id": payload.stream_id}
    )


@router.post("/groups/{group_id}/clients")
def set_group_clients(
    group_id: str,
    payload: ClientsUpdate,
    user: CurrentUser,
    db: DbSession = Depends(get_db),
) -> dict:
    """Compose un groupe : deplace des appareils vers lui, quel que soit le
    groupe (ephemere) auquel ils appartenaient jusque-la."""
    return _call(db, "Group.SetClients", {"id": group_id, "clients": payload.client_ids})


@router.post("/groups/{group_id}/mute")
def set_group_mute(
    group_id: str,
    payload: MuteUpdate,
    user: CurrentUser,
    db: DbSession = Depends(get_db),
) -> dict:
    return _call(db, "Group.SetMute", {"id": group_id, "mute": payload.muted})


@router.put("/groups/{group_id}/name")
def set_group_name(
    group_id: str,
    payload: NameUpdate,
    user: CurrentUser,
    db: DbSession = Depends(get_db),
) -> dict:
    return _call(db, "Group.SetName", {"id": group_id, "name": payload.name})


@router.websocket("/stream")
async def stream_proxy(websocket: WebSocket) -> None:
    """Relaie le flux audio de snapserver vers le navigateur.

    Le front est un snapclient a part entiere : il parle le protocole binaire
    de Snapcast. On relaie plutot que de le laisser joindre snapserver
    directement pour trois raisons : l'adresse du serveur est modifiable a
    chaud depuis l'interface (nginx, lui, est statique), snapserver n'a pas a
    etre expose au reseau des postes, et tout reste sur une seule origine — donc
    compatible TLS le jour venu.

    Contrepartie assumee : l'audio transite par l'API, ~1,5 Mbit/s par
    navigateur a l'ecoute. Sur un reseau local, c'est negligeable.
    """
    await websocket.accept()

    with SessionLocal() as db:
        config = appsettings.snapcast_config(db)

    if not config["enabled"]:
        await websocket.close(code=1011, reason="Snapcast desactive")
        return

    upstream_url = f"ws://{config['host']}:{config['http_port']}/stream"
    try:
        upstream = await websockets.connect(upstream_url, max_size=None)
    except Exception as exc:
        logger.warning("flux snapcast injoignable (%s) : %s", upstream_url, exc)
        await websocket.close(code=1011, reason="snapserver injoignable")
        return

    async def browser_to_server() -> None:
        while True:
            await upstream.send(await websocket.receive_bytes())

    async def server_to_browser() -> None:
        async for message in upstream:
            if isinstance(message, bytes):
                await websocket.send_bytes(message)

    tasks = [asyncio.create_task(browser_to_server()), asyncio.create_task(server_to_browser())]
    try:
        # La premiere extremite qui se ferme met fin au relais.
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
    except WebSocketDisconnect:
        pass
    finally:
        for task in tasks:
            task.cancel()
        await upstream.close()
