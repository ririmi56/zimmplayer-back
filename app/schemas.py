from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field


def _as_utc(value: datetime) -> datetime:
    """Les colonnes DATETIME sont naives et stockees en UTC ; on le dit
    explicitement pour que le navigateur ne les prenne pas pour de l'heure locale."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


UtcDatetime = Annotated[datetime, AfterValidator(_as_utc)]


class ArtistOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    album_count: int = 0


class AlbumOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    year: int | None
    artist_id: int
    artist_name: str
    has_cover: bool
    track_count: int = 0
    genre: str | None = None
    # Champs corriges a la main, pour que l'interface puisse les signaler et
    # proposer de revenir a la valeur du tag.
    overrides: dict[str, Any] = {}


class TrackOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    track_no: int | None
    disc_no: int | None
    duration_s: float | None
    format: str | None
    bitrate: int | None
    album_id: int
    album_title: str
    artist_id: int
    artist_name: str
    has_cover: bool
    genre: str | None = None
    # Les paroles ne sont pas embarquees ici : elles sont volumineuses et
    # servies a la demande par /api/tracks/{id}/lyrics.
    has_lyrics: bool = False
    overrides: dict[str, Any] = {}


class GenreOut(BaseModel):
    name: str
    album_count: int
    track_count: int


class LyricsOut(BaseModel):
    track_id: int
    lyrics: str | None


class ArtistDetail(ArtistOut):
    albums: list[AlbumOut]
    # Pistes ou l'artiste intervient sans signer l'album (compilations) : sans
    # cela, sa page serait vide alors qu'il est bien present au catalogue.
    appears_on: list[TrackOut]


class AlbumDetail(AlbumOut):
    tracks: list[TrackOut]


class SearchResults(BaseModel):
    artists: list[ArtistOut]
    albums: list[AlbumOut]
    tracks: list[TrackOut]


class Page[T](BaseModel):
    items: list[T]
    total: int
    limit: int
    offset: int


class TrackUpdate(BaseModel):
    """Champs editables d'une piste. Absent = inchange, null = correction retiree."""

    title: str | None = Field(default=None, min_length=1, max_length=255)
    track_no: int | None = Field(default=None, ge=0)
    disc_no: int | None = Field(default=None, ge=0)


class AlbumUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    year: int | None = Field(default=None, ge=1000, le=2999)


class ScanRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: str
    started_at: UtcDatetime
    finished_at: UtcDatetime | None
    files_seen: int
    files_indexed: int
    files_removed: int
    files_failed: int
    error: str | None


class ScanErrorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    object_key: str
    message: str
    created_at: UtcDatetime


class QueueItemOut(BaseModel):
    id: int
    position: int
    added_by: str
    added_at: UtcDatetime
    track: TrackOut


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    created_by: str
    created_at: UtcDatetime
    snapcast_stream_id: str | None
    item_count: int = 0


class SessionDetail(SessionOut):
    items: list[QueueItemOut]
    current_item_id: int | None
    is_playing: bool
    # Position extrapolee au moment de la reponse : l'interface peut continuer
    # a la faire avancer localement entre deux interrogations.
    position_s: float
    # N'avance que sur un ordre explicite (play/pause/seek/next/previous), jamais
    # sur une simple interrogation : permet au front de distinguer les deux sans
    # resynchroniser un <audio> a chaque poll.
    command_seq: int


class SessionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class QueueAdd(BaseModel):
    """Ajout de pistes explicites et/ou de tous les titres d'un album."""

    track_ids: list[int] = []
    album_id: int | None = None


class MoveItem(BaseModel):
    to_index: int = Field(ge=0)


class SeekRequest(BaseModel):
    position_s: float = Field(ge=0)


class AuthStatus(BaseModel):
    """Ce que l'interface doit savoir pour afficher, ou non, une connexion."""

    oidc_enabled: bool
    authenticated: bool
    subject: str
    name: str
    email: str
    groups: list[str]
    role: str
    #: Nomme dans la configuration : son role ne se revoque pas a l'ecran.
    is_super_admin: bool


class UserOut(BaseModel):
    """Une personne deja connectee, telle que la page Administration l'affiche."""

    id: int
    subject: str
    name: str
    email: str
    is_admin: bool
    #: Nomme dans la configuration : sa bascule est desactivee a l'ecran.
    is_super_admin: bool
    last_seen_at: UtcDatetime

    model_config = ConfigDict(from_attributes=True)


class AdminUpdate(BaseModel):
    is_admin: bool


class PersonOut(BaseModel):
    """Une personne connue, telle qu'un selecteur de partage l'affiche."""

    id: int
    name: str
    email: str


class PlaylistCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class PlaylistTracksAdd(BaseModel):
    """Des titres nommes un par un, ou tout un album."""

    track_ids: list[int] | None = None
    album_id: int | None = None


class ShareUpdate(BaseModel):
    #: Faux = lecture seule ; vrai = peut aussi ajouter et retirer des titres.
    can_edit: bool = False


class PlaylistShareOut(BaseModel):
    user_id: int
    name: str
    can_edit: bool


class PlaylistItemOut(BaseModel):
    id: int
    track: TrackOut
    #: Qui a ajoute ce titre, sur une playlist partagee en ecriture.
    added_by: str | None


class PlaylistOut(BaseModel):
    id: int
    name: str
    owner_name: str
    is_owner: bool
    can_edit: bool
    track_count: int
    updated_at: UtcDatetime


class PlaylistDetail(PlaylistOut):
    items: list[PlaylistItemOut]
    #: Vide pour qui n'est pas proprietaire : lui seul gere les partages.
    shares: list[PlaylistShareOut]
