"""Extraction et stockage des pochettes.

Les images sont ecrites sur un volume disque (COVER_DIR) plutot qu'en base :
une pochette est un binaire volumineux, sans interet pour les requetes SQL.
"""

import io
import logging
from pathlib import Path

import mutagen
from PIL import Image

from app.config import get_settings
from app.services import s3

logger = logging.getLogger(__name__)

FULL_SIZE = (1000, 1000)
THUMB_SIZE = (300, 300)
FOLDER_COVER_NAMES = ("cover.jpg", "cover.jpeg", "cover.png", "folder.jpg", "folder.png")


def extract_embedded_cover(path: Path) -> bytes | None:
    """Recupere l'image embarquee ; l'API differe pour chaque conteneur."""
    try:
        audio = mutagen.File(path)
    except Exception:
        return None
    if audio is None:
        return None

    # MP3 / ID3 : frames APIC
    if getattr(audio, "tags", None) is not None:
        for key in audio.tags.keys() if hasattr(audio.tags, "keys") else []:
            if str(key).startswith("APIC"):
                return audio.tags[key].data

    # FLAC / OGG : blocs Picture
    pictures = getattr(audio, "pictures", None)
    if pictures:
        return pictures[0].data

    # MP4 / M4A : atome covr
    try:
        covr = audio.tags.get("covr") if audio.tags else None
        if covr:
            return bytes(covr[0])
    except (AttributeError, TypeError):
        pass

    return None


def find_folder_cover(object_key: str) -> bytes | None:
    """Cherche un cover.jpg / folder.jpg dans le dossier de l'album."""
    folder = object_key.rsplit("/", 1)[0] if "/" in object_key else ""
    for name in FOLDER_COVER_NAMES:
        key = f"{folder}/{name}" if folder else name
        try:
            return s3.get_object_bytes(key)
        except Exception:
            continue
    return None


def store_cover(album_id: int, data: bytes) -> str | None:
    """Ecrit la pochette et sa miniature, renvoie le nom de fichier ou None."""
    settings = get_settings()
    settings.cover_dir.mkdir(parents=True, exist_ok=True)

    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception:
        logger.warning("pochette illisible pour l'album %s", album_id)
        return None

    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")

    for suffix, size in (("", FULL_SIZE), ("_thumb", THUMB_SIZE)):
        resized = image.copy()
        resized.thumbnail(size, Image.LANCZOS)
        resized.save(settings.cover_dir / f"{album_id}{suffix}.jpg", "JPEG", quality=85)

    return f"{album_id}.jpg"


def cover_path(album_id: int, thumb: bool = False) -> Path:
    suffix = "_thumb" if thumb else ""
    return get_settings().cover_dir / f"{album_id}{suffix}.jpg"
