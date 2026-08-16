"""Indexation incrementale du bucket vers la base.

Le scan compare (cle, etag, taille) avec ce qui est deja indexe : un second scan
sur une bibliotheque inchangee ne telecharge donc aucun fichier. Le
telechargement et le parsing, purement I/O, sont paralleles ; toutes les
ecritures en base restent dans le thread principal, ce qui evite d'avoir a
gerer une session par worker.
"""

import hashlib
import logging
import tempfile
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.models import (
    Album,
    Artist,
    ScanError,
    ScanRun,
    Track,
    refresh_effective,
    utcnow,
)
from app.services import covers, s3
from app.services.tags import TrackTags, read_tags

logger = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {
    ".mp3", ".flac", ".m4a", ".mp4", ".ogg", ".oga", ".opus", ".wav", ".aac", ".wma",
}


def key_hash(object_key: str) -> str:
    return hashlib.sha256(object_key.encode("utf-8")).hexdigest()


@dataclass
class _Parsed:
    obj: dict[str, Any]
    tags: TrackTags
    cover: bytes | None


def _chunks(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _fetch_and_parse(obj: dict[str, Any]) -> _Parsed:
    """Telecharge l'objet dans un fichier temporaire puis en lit les tags.

    Le telechargement complet est volontairement prefere a des GET Range
    partiels : c'est robuste pour tous les conteneurs et le cout reste marginal
    sur un reseau local.
    """
    suffix = Path(obj["key"]).suffix
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as handle:
        local = Path(handle.name)
        s3.download_to(obj["key"], local)
        tags = read_tags(local, obj["key"])
        cover = covers.extract_embedded_cover(local)
    return _Parsed(obj=obj, tags=tags, cover=cover)


def _get_or_create_artist(session: Session, name: str, mbid: str | None) -> Artist:
    artist = session.scalar(select(Artist).where(Artist.name == name))
    if artist is None:
        artist = Artist(name=name[:255], mbid=mbid)
        session.add(artist)
        session.flush()
    elif mbid and not artist.mbid:
        artist.mbid = mbid
    return artist


def _get_or_create_album(session: Session, artist: Artist, tags: TrackTags) -> Album:
    source_title = tags.album[:255]
    album = session.scalar(
        select(Album).where(
            Album.artist_id == artist.id, Album.source_title == source_title
        )
    )
    if album is None:
        album = Album(
            artist_id=artist.id,
            source_title=source_title,
            source_year=tags.year,
            title=source_title,
            mbid=tags.album_mbid,
            overrides={},
        )
        session.add(album)
        session.flush()
    else:
        album.source_title = source_title
        if tags.year:
            album.source_year = tags.year
        if tags.album_mbid and not album.mbid:
            album.mbid = tags.album_mbid
    refresh_effective(album)
    return album


def _upsert_track(session: Session, parsed: _Parsed) -> None:
    tags = parsed.tags
    obj = parsed.obj

    album_artist = _get_or_create_artist(session, tags.album_artist, tags.artist_mbid)
    track_artist = (
        album_artist
        if tags.artist == tags.album_artist
        else _get_or_create_artist(session, tags.artist, None)
    )
    album = _get_or_create_album(session, album_artist, tags)

    if album.cover_file is None:
        cover = parsed.cover or covers.find_folder_cover(obj["key"])
        if cover:
            album.cover_file = covers.store_cover(album.id, cover)

    hashed = key_hash(obj["key"])
    track = session.scalar(select(Track).where(Track.object_key_hash == hashed))
    if track is None:
        track = Track(object_key=obj["key"], object_key_hash=hashed, overrides={})
        session.add(track)

    track.album_id = album.id
    track.artist_id = track_artist.id
    track.source_title = tags.title[:255]
    track.source_track_no = tags.track_no
    track.source_disc_no = tags.disc_no
    track.duration_s = tags.duration_s
    track.format = tags.fmt
    track.bitrate = tags.bitrate
    track.sample_rate = tags.sample_rate
    track.channels = tags.channels
    track.genre = tags.genre[:100] if tags.genre else None
    track.lyrics = tags.lyrics
    track.etag = obj["etag"]
    track.size_bytes = obj["size"]
    track.last_modified = obj["last_modified"]
    track.indexed_at = utcnow()
    refresh_effective(track)


def _purge_orphans(session: Session) -> None:
    """Supprime les albums sans piste, puis les artistes sans rien."""
    orphan_albums = session.scalars(
        select(Album.id).where(Album.id.not_in(select(Track.album_id).distinct()))
    ).all()
    if orphan_albums:
        for album_id in orphan_albums:
            for thumb in (False, True):
                covers.cover_path(album_id, thumb).unlink(missing_ok=True)
        session.execute(
            delete(Album).where(Album.id.in_(orphan_albums)),
            execution_options={"synchronize_session": False},
        )
        session.flush()

    used_artists = select(Album.artist_id).distinct().union(
        select(Track.artist_id).distinct()
    )
    session.execute(
        delete(Artist).where(Artist.id.not_in(used_artists)),
        execution_options={"synchronize_session": False},
    )


def is_running(session: Session) -> bool:
    return session.scalar(
        select(ScanRun.id).where(ScanRun.status == "running").limit(1)
    ) is not None


def start_run(session: Session) -> ScanRun:
    run = ScanRun(status="running")
    session.add(run)
    session.commit()
    return run


def run_scan(run_id: int, force: bool = False) -> None:
    """Corps du scan, execute dans un thread de fond.

    `force` ignore la comparaison (etag, taille) et relit donc tous les
    fichiers. Utile apres une evolution de la lecture des tags, puisqu'un scan
    normal ne retelecharge jamais un fichier inchange.
    """
    settings = get_settings()
    session = SessionLocal()
    run = session.get(ScanRun, run_id)
    try:
        existing = {
            row.object_key_hash: (row.etag, row.size_bytes)
            for row in session.execute(
                select(Track.object_key_hash, Track.etag, Track.size_bytes)
            )
        }

        seen: set[str] = set()
        pending: list[dict[str, Any]] = []
        for obj in s3.iter_objects(settings.s3_prefix):
            if Path(obj["key"]).suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            hashed = key_hash(obj["key"])
            seen.add(hashed)
            run.files_seen += 1
            previous = existing.get(hashed)
            if not force and previous == (obj["etag"], obj["size"]):
                continue
            pending.append(obj)
        session.commit()

        batch_size = max(settings.scan_concurrency * 4, 1)
        with ThreadPoolExecutor(max_workers=settings.scan_concurrency) as pool:
            for batch in _chunks(pending, batch_size):
                futures = {pool.submit(_fetch_and_parse, obj): obj for obj in batch}
                for future in as_completed(futures):
                    obj = futures[future]
                    try:
                        # Un SAVEPOINT par piste : un fichier illisible ne doit
                        # pas annuler le travail deja fait dans le meme lot.
                        with session.begin_nested():
                            _upsert_track(session, future.result())
                        run.files_indexed += 1
                    except Exception as exc:
                        logger.warning("echec sur %s : %s", obj["key"], exc)
                        run.files_failed += 1
                        session.add(
                            ScanError(
                                scan_run_id=run_id,
                                object_key=obj["key"],
                                message=f"{type(exc).__name__}: {exc}"[:2000],
                            )
                        )
                session.commit()

        # Les objets disparus du bucket sortent du catalogue.
        removed = session.execute(
            delete(Track).where(Track.object_key_hash.not_in(seen)),
            execution_options={"synchronize_session": False},
        )
        run.files_removed = removed.rowcount or 0
        session.flush()

        _purge_orphans(session)

        run.status = "completed"
        run.finished_at = utcnow()
        session.commit()

    except Exception as exc:
        logger.exception("scan interrompu")
        session.rollback()
        run = session.get(ScanRun, run_id)
        if run:
            run.status = "failed"
            run.error = f"{type(exc).__name__}: {exc}"[:2000]
            run.finished_at = utcnow()
            session.commit()
    finally:
        session.close()
