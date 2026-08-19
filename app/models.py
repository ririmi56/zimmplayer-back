from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Float,
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    """Horodatage naif en UTC.

    MySQL/MariaDB ne stocke pas de fuseau : melanger `func.now()` (heure du
    serveur SQL) et `datetime.now()` (heure de l'hote) donnait des durees de
    scan fantaisistes. Tout est donc produit ici, en UTC.
    """
    return datetime.now(UTC).replace(tzinfo=None)


class Artist(Base):
    __tablename__ = "artists"
    __table_args__ = (
        Index("ft_artists_name", "name", mysql_prefix="FULLTEXT"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    mbid: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    albums: Mapped[list["Album"]] = relationship(
        back_populates="artist", cascade="all, delete-orphan"
    )


class Album(Base):
    __tablename__ = "albums"
    __table_args__ = (
        Index("uq_albums_artist_source_title", "artist_id", "source_title", unique=True),
        Index("ft_albums_title", "title", mysql_prefix="FULLTEXT"),
    )

    EDITABLE_FIELDS = frozenset({"title", "year"})

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    artist_id: Mapped[int] = mapped_column(ForeignKey("artists.id", ondelete="CASCADE"))
    # `source_title` porte en plus l'identite de l'album : c'est sur lui que
    # porte la contrainte d'unicite, si bien qu'un album renomme a la main reste
    # reconnu lors des rescans au lieu d'etre duplique.
    source_title: Mapped[str] = mapped_column(String(255))
    source_year: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(255))
    year: Mapped[int | None] = mapped_column(Integer)
    mbid: Mapped[str | None] = mapped_column(String(36))
    # Nom de fichier de la pochette dans COVER_DIR, None si aucune trouvee.
    cover_file: Mapped[str | None] = mapped_column(String(255))
    overrides: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    artist: Mapped[Artist] = relationship(back_populates="albums")
    tracks: Mapped[list["Track"]] = relationship(
        back_populates="album", cascade="all, delete-orphan"
    )


class Track(Base):
    __tablename__ = "tracks"
    __table_args__ = (
        Index("ft_tracks_title", "title", mysql_prefix="FULLTEXT"),
        Index("ix_tracks_genre", "genre"),
    )

    EDITABLE_FIELDS = frozenset({"title", "track_no", "disc_no"})

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    album_id: Mapped[int] = mapped_column(ForeignKey("albums.id", ondelete="CASCADE"))
    artist_id: Mapped[int] = mapped_column(ForeignKey("artists.id", ondelete="CASCADE"))
    # Valeurs telles que lues dans les tags, conservees pour pouvoir revenir en
    # arriere quand une correction manuelle est retiree.
    source_title: Mapped[str] = mapped_column(String(255))
    source_track_no: Mapped[int | None] = mapped_column(Integer)
    source_disc_no: Mapped[int | None] = mapped_column(Integer)

    title: Mapped[str] = mapped_column(String(255))
    track_no: Mapped[int | None] = mapped_column(Integer)
    disc_no: Mapped[int | None] = mapped_column(Integer)
    duration_s: Mapped[float | None]
    format: Mapped[str | None] = mapped_column(String(16))
    bitrate: Mapped[int | None] = mapped_column(Integer)
    sample_rate: Mapped[int | None] = mapped_column(Integer)
    channels: Mapped[int | None] = mapped_column(Integer)

    genre: Mapped[str | None] = mapped_column(String(100))
    # Les paroles pesent quelques kilo-octets : chargees en differe pour ne pas
    # les trainer dans chaque liste de pistes. Servies par un endpoint dedie.
    lyrics: Mapped[str | None] = mapped_column(Text, deferred=True)

    # Une cle S3 peut aller jusqu'a 1024 octets, trop pour un index unique
    # InnoDB en utf8mb4 (limite 3072 octets). L'unicite porte donc sur un
    # sha256 de la cle, la cle elle-meme restant en Text.
    object_key: Mapped[str] = mapped_column(Text)
    object_key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    etag: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    last_modified: Mapped[datetime | None] = mapped_column(DateTime)

    overrides: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    indexed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    album: Mapped[Album] = relationship(back_populates="tracks")
    artist: Mapped[Artist] = relationship()


class AppSetting(Base):
    """Reglages modifiables depuis l'interface, sans redeploiement.

    Les variables d'environnement ne servent plus que de valeurs par defaut :
    pointer l'application vers un autre snapserver ne doit pas demander de
    reconstruire l'image.
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Session(Base):
    """Session d'ecoute : une file d'attente partagee et son etat de lecture.

    Chacun cree ou rejoint librement une session ; rejoindre, c'est se
    synchroniser via Snapcast avec tous les autres membres. Une ecoute
    purement locale, elle, n'implique aucune session : c'est simplement ce
    navigateur qui lit sa propre file (voir `player/store.ts` cote front).
    """

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    created_by: Mapped[str] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    # Flux enregistre aupres de snapserver et port TCP que le backend ouvre
    # pour que snapserver vienne y lire le PCM.
    snapcast_stream_id: Mapped[str | None] = mapped_column(String(100))
    snapcast_port: Mapped[int | None] = mapped_column(Integer)

    # Element en cours de lecture. Volontairement sans contrainte de cle
    # etrangere : `queue_items` reference deja `sessions`, et une FK croisee
    # compliquerait inutilement migrations et suppressions. Le code remet la
    # valeur a NULL quand l'element disparait.
    current_item_id: Mapped[int | None] = mapped_column(Integer)
    is_playing: Mapped[bool] = mapped_column(Boolean, default=False)
    # Position dans la piste courante, figee au dernier changement d'etat.
    # L'interface extrapole avec `updated_at` pour afficher une progression
    # fluide sans interroger le serveur en continu.
    position_s: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # Incremente uniquement par une action utilisateur (lecture, pause, saut,
    # changement de piste). La sortie audio serveur s'en sert pour distinguer
    # un ordre a appliquer de ses propres remontees de position, qui elles
    # rafraichissent `position_s` sans rien commander.
    command_seq: Mapped[int] = mapped_column(Integer, default=0)

    items: Mapped[list["QueueItem"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class QueueItem(Base):
    __tablename__ = "queue_items"
    __table_args__ = (Index("ix_queue_items_session_position", "session_id", "position"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE")
    )
    track_id: Mapped[int] = mapped_column(ForeignKey("tracks.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)
    added_by: Mapped[str] = mapped_column(String(60))
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    session: Mapped[Session] = relationship(back_populates="items")
    track: Mapped[Track] = relationship()


class User(Base):
    """Une personne qui s'est deja connectee.

    La table n'existe QUE pour porter `is_admin` : tout le reste de l'identite
    vient du jeton a chaque requete, le fournisseur restant la source de
    verite pour le nom, le courriel et les groupes. Les colonnes `name` et
    `email` n'en sont qu'une copie, rafraichie a chaque connexion, pour que la
    page Administration puisse afficher une liste lisible sans interroger le
    fournisseur — ce qu'aucune API OIDC standard ne permettrait de toute facon.

    Consequence assumee : on ne peut promouvoir que quelqu'un qui s'est deja
    connecte au moins une fois.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Identifiant du fournisseur : stable et unique, meme si la personne change
    # de nom ou d'adresse. C'est lui qui fait l'identite, jamais le courriel.
    subject: Mapped[str] = mapped_column(String(255), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(255), default="")
    # Promotion accordee depuis la page Administration. Les super-admins de la
    # configuration sont admin sans etre marques ici : leur role ne se
    # revoque pas depuis l'interface.
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Playlist(Base):
    """Une selection de titres, personnelle puis partageable.

    Distincte de la file d'une session : la file est ce qui joue maintenant et
    se vide en avancant, une playlist se garde. Elles ne se rejoignent qu'au
    moment ou l'on envoie l'une dans l'autre.
    """

    __tablename__ = "playlists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    owner: Mapped["User"] = relationship()
    items: Mapped[list["PlaylistTrack"]] = relationship(
        back_populates="playlist",
        cascade="all, delete-orphan",
        order_by="PlaylistTrack.position",
    )
    shares: Mapped[list["PlaylistShare"]] = relationship(
        back_populates="playlist", cascade="all, delete-orphan"
    )


class PlaylistTrack(Base):
    """Un titre dans une playlist.

    Le meme titre peut y figurer plusieurs fois — c'est parfois voulu, et
    l'interdire compliquerait sans rendre service. D'ou une cle propre plutot
    qu'un couple (playlist, piste) unique.
    """

    __tablename__ = "playlist_tracks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    playlist_id: Mapped[int] = mapped_column(
        ForeignKey("playlists.id", ondelete="CASCADE"), index=True
    )
    track_id: Mapped[int] = mapped_column(ForeignKey("tracks.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)
    # Qui l'a ajoute : sur une playlist partagee en ecriture, c'est la seule
    # facon de savoir d'ou vient un titre qu'on n'a pas mis soi-meme.
    added_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    playlist: Mapped[Playlist] = relationship(back_populates="items")
    track: Mapped["Track"] = relationship()
    added_by: Mapped["User | None"] = relationship()


class PlaylistShare(Base):
    """Partage d'une playlist avec une personne, en lecture ou en ecriture."""

    __tablename__ = "playlist_shares"
    __table_args__ = (
        Index("uq_playlist_shares", "playlist_id", "user_id", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    playlist_id: Mapped[int] = mapped_column(
        ForeignKey("playlists.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    #: Faux = lecture seule ; vrai = peut aussi ajouter et retirer des titres.
    can_edit: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    playlist: Mapped[Playlist] = relationship(back_populates="shares")
    user: Mapped["User"] = relationship()


class Listen(Base):
    """Une ecoute effective, par une personne, d'un titre.

    N'est enregistree qu'au-dela du seuil de comptage (voir services/stats.py) :
    parcourir un album en sautant de titre en titre ne doit pas gonfler les
    compteurs.

    `session_name` est une copie volontaire : les sessions sont supprimees
    couramment, et une statistique par session qui disparait avec elle ne
    servirait a rien. `session_id` sert aux jointures tant que la session vit,
    le nom lui survit.
    """

    __tablename__ = "listens"
    __table_args__ = (
        Index("ix_listens_user_at", "user_id", "listened_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    track_id: Mapped[int] = mapped_column(ForeignKey("tracks.id", ondelete="CASCADE"))
    #: Session d'ecoute, ou None pour une ecoute solo dans le navigateur.
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="SET NULL")
    )
    session_name: Mapped[str | None] = mapped_column(String(120))
    #: Duree reellement ecoutee, pas la duree du titre.
    seconds: Mapped[float] = mapped_column(Float, default=0.0)
    listened_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    user: Mapped["User"] = relationship()
    track: Mapped["Track"] = relationship()


class QueueAddition(Base):
    """Un titre ajoute a la file d'une session.

    Table a part parce que `queue_items` est ephemere : la ligne disparait des
    qu'on retire le titre ou qu'on vide la file, et l'information serait perdue.
    """

    __tablename__ = "queue_additions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    track_id: Mapped[int] = mapped_column(ForeignKey("tracks.id", ondelete="CASCADE"))
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="SET NULL")
    )
    session_name: Mapped[str | None] = mapped_column(String(120))
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SessionPresence(Base):
    """Qui est dans quelle session, et depuis quand on l'a vu.

    Le serveur ne le savait pas : l'appartenance ne vivait que dans le
    navigateur. On la deduit de l'interrogation periodique du detail de la
    session, seul signal existant — approximation assumee, qui ne se trompe que
    sur quelqu'un gardant la page ouverte sans ecouter.
    """

    __tablename__ = "session_presence"
    __table_args__ = (
        Index("uq_session_presence", "session_id", "user_id", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped["User"] = relationship()


class ScanRun(Base):
    __tablename__ = "scan_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(16))  # running | completed | failed
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    files_seen: Mapped[int] = mapped_column(Integer, default=0)
    files_indexed: Mapped[int] = mapped_column(Integer, default=0)
    files_removed: Mapped[int] = mapped_column(Integer, default=0)
    files_failed: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)

    errors: Mapped[list["ScanError"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class ScanError(Base):
    __tablename__ = "scan_errors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scan_run_id: Mapped[int] = mapped_column(
        ForeignKey("scan_runs.id", ondelete="CASCADE")
    )
    object_key: Mapped[str] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    run: Mapped[ScanRun] = relationship(back_populates="errors")


def refresh_effective(entity: Album | Track) -> None:
    """Recalcule les champs affiches : valeur du tag, puis correction manuelle.

    Chaque champ editable existe en deux exemplaires : `source_<champ>` (ce que
    disent les tags) et `<champ>` (ce qu'on affiche et sur quoi portent tri et
    recherche). Repartir systematiquement de la source permet aussi bien de
    faire survivre une correction a un rescan que de revenir en arriere quand
    l'utilisateur la retire.
    """
    overrides = entity.overrides or {}
    for field in entity.EDITABLE_FIELDS:
        setattr(entity, field, getattr(entity, f"source_{field}"))
        if field in overrides:
            setattr(entity, field, overrides[field])
