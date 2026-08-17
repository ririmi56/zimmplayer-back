from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "mysql+pymysql://audioplayer:audioplayer@localhost:3306/audioplayer?charset=utf8mb4"

    # Endpoint interne, utilise pour lister et telecharger les objets.
    s3_endpoint: str = "http://localhost:9000"
    # Base URL par laquelle le NAVIGATEUR joint MinIO. En dev on tape MinIO
    # directement, en production nginx l'expose sous /s3 (ex. http://host/s3).
    s3_public_base_url: str = "http://localhost:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_region: str = "us-east-1"
    s3_bucket: str = "music"
    s3_prefix: str = ""

    presign_ttl_seconds: int = 3600
    cover_dir: Path = Path("./data/covers")
    scan_concurrency: int = 8

    cors_origins: list[str] = ["http://localhost:5173"]

    # --- Snapcast ---------------------------------------------------------
    # Valeurs par defaut seulement : l'ecran de configuration les surcharge et
    # les persiste en base (voir services/appsettings.py).
    snapcast_enabled: bool = False
    snapcast_host: str = "localhost"
    snapcast_port: int = 1705
    # Port HTTP/WebSocket, par lequel le navigateur recoit l'audio.
    snapcast_http_port: int = 1780
    # Adresse a laquelle snapserver joint CETTE API pour venir lire le PCM.
    # Resolue en IP avant d'etre transmise : snapserver refuse un nom d'hote.
    snapcast_advertise_host: str = "localhost"
    # Plage de ports ouverts par l'API, un par session en mode snapcast.
    snapcast_port_start: int = 4960
    snapcast_port_count: int = 20
    # Le port de controle de snapserver n'est pas chiffre : activer ceci
    # suppose un terminateur TLS (stunnel, nginx `stream`) devant lui, et
    # `snapcast_port` pointant sur ce terminateur.
    snapcast_tls: bool = False
    # Vide = magasin de certificats du systeme. En airgap, pointer ici le
    # certificat de l'autorite maison.
    snapcast_tls_ca_file: str = ""
    # Nom verifie contre le certificat (et envoye en SNI). Vide = snapcast_host,
    # a renseigner si l'on joint le serveur par IP.
    snapcast_tls_server_name: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()