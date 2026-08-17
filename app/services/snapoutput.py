"""Sortie audio serveur : lit la file d'une session et pousse du PCM vers snapserver.

Rappel de l'architecture : un navigateur ne peut pas alimenter Snapcast, qui
consomme un flux PCM brut. En mode snapcast, la lecture quitte donc le poste et
se fait ici — MinIO -> ffmpeg -> socket TCP -> snapserver -> les pieces.

Chaque session possede son propre flux, enregistre dynamiquement via
Stream.AddStream en `mode=client` : c'est snapserver qui vient se connecter au
port que l'on ouvre, ce qui evite de declarer un port par session dans
snapserver.conf.

Le thread de lecture ne recoit aucun message : il relit periodiquement l'etat
en base, qui fait autorite. Les commandes (lecture, pause, saut) se contentent
d'ecrire en base et d'incrementer `command_seq`.
"""

import logging
import re
import socket
import subprocess
import threading
import time
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session as DbSession

from app.config import get_settings
from app.db import SessionLocal
from app.models import QueueItem, Session, Track
from app.services import appsettings, queue as queue_service, s3, snapcast

logger = logging.getLogger(__name__)

SAMPLE_RATE = 48000
CHANNELS = 2
BYTES_PER_SAMPLE = 2
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE
CHUNK = BYTES_PER_SECOND // 50  # 20 ms, la granularite de snapcast

_STATE_REFRESH_S = 0.25
_POSITION_FLUSH_S = 1.0

_outputs: dict[int, "SessionOutput"] = {}
_lock = threading.RLock()

_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")


def stream_name(session_name: str) -> str:
    """Nom du flux cote snapserver. Doit tenir dans un parametre d'URI."""
    cleaned = _UNSAFE.sub("-", session_name).strip("-")
    return cleaned or "session"


@dataclass
class _Desired:
    """Instantane de ce que la base demande a la sortie audio."""

    command_seq: int
    is_playing: bool
    item_id: int | None
    track_id: int | None
    object_key: str | None
    position_s: float


class SessionOutput:
    def __init__(self, session_id: int, name: str, port: int) -> None:
        self.session_id = session_id
        self.name = name
        self.port = port

        self._listener: socket.socket | None = None
        self._conn: socket.socket | None = None
        self._ffmpeg: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

        self._applied_seq = -1
        self._item_id: int | None = None
        self._bytes_written = 0
        self._track_start_s = 0.0

    # --- cycle de vie ----------------------------------------------------

    def start(self) -> None:
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("0.0.0.0", self.port))
        self._listener.listen(1)
        self._listener.settimeout(1.0)

        self._thread = threading.Thread(
            target=self._run, name=f"snapoutput-{self.session_id}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._kill_ffmpeg()
        for sock in (self._conn, self._listener):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        self._conn = self._listener = None

    def wake(self) -> None:
        self._wake.set()

    @property
    def position_s(self) -> float:
        return self._track_start_s + self._bytes_written / BYTES_PER_SECOND

    # --- boucle de lecture -----------------------------------------------

    def _run(self) -> None:
        db = SessionLocal()
        last_state = 0.0
        last_flush = 0.0
        desired: _Desired | None = None
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if desired is None or now - last_state >= _STATE_REFRESH_S:
                    desired = self._read_desired(db)
                    last_state = now
                    if desired is not None:
                        self._reconcile(desired)

                if desired is None or not desired.is_playing or self._ffmpeg is None:
                    self._wake.wait(0.2)
                    self._wake.clear()
                    continue

                if not self._ensure_connection():
                    continue

                chunk = self._ffmpeg.stdout.read(CHUNK)
                if not chunk:
                    self._on_track_finished(db)
                    desired = None
                    continue

                try:
                    self._conn.sendall(chunk)
                except OSError as exc:
                    logger.warning("session %s : snapserver s'est deconnecte (%s)",
                                   self.session_id, exc)
                    self._close_connection()
                    continue

                self._bytes_written += len(chunk)
                if now - last_flush >= _POSITION_FLUSH_S:
                    self._flush_position(db)
                    last_flush = now
        except Exception:
            logger.exception("session %s : boucle de sortie interrompue", self.session_id)
        finally:
            db.close()

    def _ensure_connection(self) -> bool:
        """Attend que snapserver vienne se connecter a notre port."""
        if self._conn is not None:
            return True
        try:
            conn, addr = self._listener.accept()
        except (TimeoutError, OSError):
            return False
        conn.settimeout(5.0)
        self._conn = conn
        logger.info("session %s : snapserver connecte depuis %s", self.session_id, addr)
        return True

    def _close_connection(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
        self._conn = None

    # --- etat ------------------------------------------------------------

    def _read_desired(self, db: DbSession) -> _Desired | None:
        # MariaDB est en REPEATABLE READ : tant que la transaction du thread
        # reste ouverte, elle conserve son instantane et ne verrait jamais les
        # ordres ecrits par l'API. On la clot donc avant chaque lecture.
        db.rollback()
        session = db.get(Session, self.session_id)
        if session is None:
            self._stop.set()
            return None

        item = track = None
        if session.current_item_id is not None:
            row = db.execute(
                select(QueueItem, Track)
                .join(Track, Track.id == QueueItem.track_id)
                .where(QueueItem.id == session.current_item_id)
            ).first()
            if row is not None:
                item, track = row

        return _Desired(
            command_seq=session.command_seq or 0,
            is_playing=bool(session.is_playing),
            item_id=item.id if item else None,
            track_id=track.id if track else None,
            object_key=track.object_key if track else None,
            position_s=session.position_s or 0.0,
        )

    def _reconcile(self, desired: _Desired) -> None:
        """N'agit que sur un ordre utilisateur, repere par `command_seq`."""
        if desired.command_seq == self._applied_seq:
            return
        self._applied_seq = desired.command_seq

        if not desired.is_playing or desired.object_key is None:
            self._kill_ffmpeg()
            self._item_id = desired.item_id
            return

        self._start_ffmpeg(desired)

    def _start_ffmpeg(self, desired: _Desired) -> None:
        self._kill_ffmpeg()
        url = s3.internal_stream_url(desired.object_key)
        # `-re` cadence la sortie sur le temps reel : sans lui, on inonderait
        # snapserver de plusieurs minutes d'audio en quelques secondes.
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-re",
            "-ss", f"{max(0.0, desired.position_s):.3f}",
            "-i", url,
            "-vn",
            "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
            "pipe:1",
        ]
        self._ffmpeg = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        self._item_id = desired.item_id
        self._bytes_written = 0
        self._track_start_s = max(0.0, desired.position_s)
        logger.info(
            "session %s : lecture de %s a %.1fs",
            self.session_id, desired.object_key, self._track_start_s,
        )

    def _kill_ffmpeg(self) -> None:
        if self._ffmpeg is None:
            return
        try:
            self._ffmpeg.kill()
            self._ffmpeg.wait(timeout=2)
        except Exception:
            pass
        self._ffmpeg = None

    def _on_track_finished(self, db: DbSession) -> None:
        """Fin naturelle d'une piste : on avance la file et on repart."""
        self._kill_ffmpeg()
        db.rollback()
        session = db.get(Session, self.session_id)
        if session is None:
            return
        # Si l'utilisateur a change de piste entre-temps, ne pas avancer par-dessus.
        if session.current_item_id == self._item_id:
            queue_service.next_item(db, session)
            db.commit()
        self._applied_seq = -1  # force la reconciliation au prochain tour

    def _flush_position(self, db: DbSession) -> None:
        """Remonte la position pour l'interface, sans passer pour un ordre.

        UPDATE cible plutot que l'ORM : la ligne est modifiee en parallele par
        l'API, et on ne veut ecrire que ces deux colonnes, sans jamais toucher
        a `command_seq`.
        """
        db.rollback()
        try:
            db.execute(
                update(Session)
                .where(
                    Session.id == self.session_id,
                    Session.current_item_id == self._item_id,
                )
                .values(position_s=self.position_s, updated_at=queue_service.utcnow())
            )
            db.commit()
        except SQLAlchemyError as exc:
            logger.debug("session %s : position non remontee (%s)", self.session_id, exc)
            db.rollback()


# --- pilotage depuis l'API -------------------------------------------------


def _allocate_port(taken: set[int]) -> int:
    settings = get_settings()
    for port in range(
        settings.snapcast_port_start,
        settings.snapcast_port_start + settings.snapcast_port_count,
    ):
        if port not in taken:
            return port
    raise RuntimeError("plus aucun port disponible pour un flux snapcast")


def provision(db: DbSession, session: Session) -> None:
    """Enregistre le flux Snapcast d'une session. Idempotent : ne fait rien si
    elle en a deja un (ex. restauration au redemarrage)."""
    if session.snapcast_stream_id is not None:
        return

    config = appsettings.snapcast_config(db)
    if not config["enabled"]:
        raise RuntimeError("Snapcast est desactive dans la configuration")

    with _lock:
        port = _allocate_port({o.port for o in _outputs.values()})
        name = stream_name(session.name)

        output = SessionOutput(session.id, name, port)
        output.start()
        _outputs[session.id] = output

    try:
        advertise_ip = socket.gethostbyname(config["advertise_host"])
    except OSError as exc:
        _drop(session.id)
        raise RuntimeError(
            f"impossible de resoudre {config['advertise_host']!r} en adresse IP"
        ) from exc

    uri = snapcast.stream_uri(advertise_ip, port, name)
    try:
        # Un AddStream en echec laisse malgre tout le nom enregistre cote
        # snapserver : on retire systematiquement avant d'ajouter.
        try:
            snapcast.call(config["host"], config["http_port"], "Stream.RemoveStream", {"id": name})
        except snapcast.SnapcastError:
            pass
        snapcast.call(config["host"], config["http_port"], "Stream.AddStream", {"streamUri": uri})
    except snapcast.SnapcastError:
        _drop(session.id)
        raise

    session.snapcast_stream_id = name
    session.snapcast_port = port


def teardown(db: DbSession, session: Session) -> None:
    """Retire le flux Snapcast d'une session (avant sa suppression)."""
    _drop(session.id)
    if session.snapcast_stream_id:
        config = appsettings.snapcast_config(db)
        try:
            snapcast.call(
                config["host"], config["http_port"],
                "Stream.RemoveStream", {"id": session.snapcast_stream_id},
            )
        except snapcast.SnapcastError as exc:
            logger.warning("retrait du flux %s impossible : %s",
                           session.snapcast_stream_id, exc)
    session.snapcast_stream_id = None
    session.snapcast_port = None


def _drop(session_id: int) -> None:
    with _lock:
        output = _outputs.pop(session_id, None)
    if output is not None:
        output.stop()


def sync(session: Session) -> None:
    """Reveille la sortie apres un changement d'etat (sans rien lui transmettre)."""
    with _lock:
        output = _outputs.get(session.id)
    if output is not None:
        output.wake()


def live_position(session: Session) -> float | None:
    """Position mesuree par la sortie audio, seule source fiable en snapcast."""
    with _lock:
        output = _outputs.get(session.id)
    return output.position_s if output is not None else None


def restore(db: DbSession) -> None:
    """Reouvre le flux de chaque session apres un redemarrage du backend.

    La ligne en base garde le nom de flux d'avant l'arret, mais le port TCP et
    l'inscription cote snapserver, eux, n'ont pas survecu : on les efface pour
    forcer `provision` a tout reconstruire plutot que de le voir (a tort) comme
    deja fait.
    """
    for session in db.scalars(select(Session)):
        session.snapcast_stream_id = None
        session.snapcast_port = None
        try:
            provision(db, session)
        except Exception as exc:
            logger.warning("session %s non restauree : %s", session.name, exc)
    db.commit()


def shutdown() -> None:
    with _lock:
        outputs = list(_outputs.values())
        _outputs.clear()
    for output in outputs:
        output.stop()
