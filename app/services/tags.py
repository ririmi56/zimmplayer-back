"""Lecture des metadonnees embarquees, avec repli sur le chemin de l'objet.

En reseau airgap aucun enrichissement externe n'est possible : tout ce qui n'est
pas dans le fichier doit etre deduit de l'arborescence `Artiste/Album/NN - Titre.ext`
ou saisi a la main plus tard.
"""

import re
from dataclasses import dataclass
from pathlib import Path

import mutagen

# "01 - Titre", "01. Titre", "01 Titre"
_LEADING_TRACK_NO = re.compile(r"^\s*(\d{1,3})\s*[-._)]?\s+")


MAX_LYRICS_CHARS = 20_000

# Les paroles ne sont pas exposees par l'interface `easy` de mutagen et chaque
# conteneur les range ailleurs : frame USLT en ID3, atome ©lyr en MP4,
# commentaire LYRICS/UNSYNCEDLYRICS en Vorbis.
_LYRICS_KEYS = ("\xa9lyr", "lyrics", "LYRICS", "unsyncedlyrics", "UNSYNCEDLYRICS")


@dataclass
class TrackTags:
    title: str
    artist: str
    album_artist: str
    album: str
    track_no: int | None
    disc_no: int | None
    year: int | None
    duration_s: float | None
    fmt: str | None
    bitrate: int | None
    sample_rate: int | None
    channels: int | None
    album_mbid: str | None
    artist_mbid: str | None
    genre: str | None
    lyrics: str | None


def _clean(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        value = value[0] if value else None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _tag(audio: mutagen.FileType, key: str) -> str | None:
    try:
        return _clean(audio.get(key))
    except (KeyError, ValueError, TypeError):
        return None


def _to_int(value: str | None) -> int | None:
    """Gere les formes "3", "3/12" et les valeurs parasites."""
    if not value:
        return None
    head = value.split("/")[0].strip()
    match = re.search(r"\d+", head)
    return int(match.group()) if match else None


def _to_year(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"\d{4}", value)
    if not match:
        return None
    year = int(match.group())
    return year if 1000 <= year <= 2999 else None


def _from_path(object_key: str) -> tuple[str | None, str | None, str, int | None]:
    """Deduit (artiste, album, titre, numero) de `Artiste/Album/NN - Titre.ext`."""
    parts = [p for p in object_key.split("/") if p]
    filename = Path(parts[-1]).stem if parts else object_key

    artist = parts[-3] if len(parts) >= 3 else None
    album = parts[-2] if len(parts) >= 2 else None

    track_no = None
    match = _LEADING_TRACK_NO.match(filename)
    title = filename
    if match:
        track_no = int(match.group(1))
        title = filename[match.end():].strip() or filename

    return artist, album, title, track_no


def read_lyrics(path: Path) -> str | None:
    """Lit les paroles embarquees, quel que soit le conteneur.

    Necessite l'API brute de mutagen : l'interface `easy` n'expose pas ce tag.
    """
    try:
        audio = mutagen.File(path)
    except Exception:
        return None
    tags = getattr(audio, "tags", None) if audio is not None else None
    if tags is None:
        return None

    # ID3 (mp3) : une ou plusieurs frames USLT, une par langue.
    getall = getattr(tags, "getall", None)
    if getall is not None:
        try:
            frames = getall("USLT")
        except Exception:
            frames = []
        for frame in frames:
            text = _clean(getattr(frame, "text", None))
            if text:
                return text[:MAX_LYRICS_CHARS]

    for key in _LYRICS_KEYS:
        try:
            text = _clean(tags.get(key))
        except (KeyError, ValueError, TypeError):
            continue
        if text:
            return text[:MAX_LYRICS_CHARS]

    return None


def read_tags(path: Path, object_key: str) -> TrackTags:
    """Lit un fichier audio local deja telecharge depuis le bucket.

    Leve ValueError si le fichier n'est pas un format audio reconnu ; le scanner
    transforme cela en erreur de scan sans interrompre le reste.
    """
    audio = mutagen.File(path, easy=True)
    if audio is None:
        raise ValueError("format audio non reconnu par mutagen")

    path_artist, path_album, path_title, path_track_no = _from_path(object_key)

    # L'albumartist porte l'identite de l'album : sans lui une compilation
    # eclaterait en autant d'artistes que de pistes.
    album_artist = (
        _tag(audio, "albumartist")
        or _tag(audio, "artist")
        or path_artist
        or "Artiste inconnu"
    )
    artist = _tag(audio, "artist") or album_artist

    info = audio.info
    return TrackTags(
        title=_tag(audio, "title") or path_title,
        artist=artist,
        album_artist=album_artist,
        album=_tag(audio, "album") or path_album or "Album inconnu",
        track_no=_to_int(_tag(audio, "tracknumber")) or path_track_no,
        disc_no=_to_int(_tag(audio, "discnumber")),
        year=_to_year(_tag(audio, "date") or _tag(audio, "originaldate")),
        duration_s=getattr(info, "length", None),
        fmt=(Path(object_key).suffix.lstrip(".").lower() or None),
        bitrate=getattr(info, "bitrate", None),
        sample_rate=getattr(info, "sample_rate", None),
        channels=getattr(info, "channels", None),
        album_mbid=_tag(audio, "musicbrainz_albumid"),
        artist_mbid=_tag(audio, "musicbrainz_albumartistid")
        or _tag(audio, "musicbrainz_artistid"),
        genre=(_tag(audio, "genre") or None),
        lyrics=read_lyrics(path),
    )
