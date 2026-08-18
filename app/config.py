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

    # Adresse publique de l'application, telle que tapee par les utilisateurs.
    # Sert a construire l'URI de redirection OIDC, qui doit correspondre au
    # caractere pres a celle declaree chez le fournisseur.
    public_base_url: str = "http://localhost:5173"

    # --- TLS sortant ------------------------------------------------------
    # Autorite de certification a laquelle faire confiance pour TOUTES les
    # connexions sortantes de l'API : stockage S3 (boto3 et ffmpeg) et
    # snapserver. Vide = magasin de certificats du systeme.
    #
    # Sur un reseau airgap les certificats sont signes par une autorite maison,
    # absente de tout magasin livre avec les images : sans ce reglage, chaque
    # connexion chiffree echoue a la verification.
    #
    # Attention : ce fichier REMPLACE le magasin systeme, il ne s'y ajoute pas.
    # S'il faut joindre a la fois des serveurs internes et des serveurs a
    # certificat public, y concatener les deux jeux d'autorites.
    tls_ca_file: str = ""

    # --- OIDC -------------------------------------------------------------
    # Desactive, l'identite se resume au pseudo saisi dans l'ecran de
    # configuration, comme avant. Active, ce pseudo disparait : l'identite
    # vient du fournisseur, et l'en-tete X-User-Name cesse d'etre lue.
    oidc_enabled: bool = False
    # URL de l'emetteur, sans le /.well-known : tout le reste est decouvert.
    # C'est ce qui rend le branchement independant du fournisseur.
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    # `openid` est obligatoire. `groups` est la facon dont Authentik expose
    # l'appartenance aux groupes ; d'autres fournisseurs la placent ailleurs,
    # d'ou oidc_groups_claim ci-dessous.
    oidc_scopes: str = "openid profile email groups"
    oidc_groups_claim: str = "groups"
    # Autorite propre au fournisseur, si son certificat n'est pas signe par la
    # meme que le reste. Vide = tls_ca_file, puis magasin systeme.
    oidc_ca_file: str = ""
    # Duree de la session applicative, independante de celle du jeton : on ne
    # conserve ni access token ni refresh token, seulement l'identite validee.
    session_max_age_s: int = 8 * 3600
    # --- Administrateurs --------------------------------------------------
    # Comptes toujours administrateurs, quoi qu'il arrive : c'est par eux que
    # l'on entre la premiere fois, et ils ne peuvent pas etre retrogrades
    # depuis l'interface. Tout le reste se gere ensuite a l'ecran.
    #
    # Liste separee par des virgules. Chaque entree est comparee au `sub` ET a
    # l'adresse de courriel du jeton (comparaison insensible a la casse pour
    # le courriel) : le `sub` d'un fournisseur est opaque et penible a
    # recopier, le courriel se configure a la main.
    #
    # Le nom affiche n'est deliberement PAS compare : chez beaucoup de
    # fournisseurs, chacun peut le modifier lui-meme — il suffirait de se
    # renommer pour devenir administrateur.
    super_admins: str = ""

    # Cle de signature du cookie de session. Obligatoire des que OIDC est
    # active : une valeur connue laisserait forger une session.
    session_secret: str = ""

    # --- Snapcast ---------------------------------------------------------
    # Valeurs par defaut seulement : l'ecran de configuration les surcharge et
    # les persiste en base (voir services/appsettings.py).
    snapcast_enabled: bool = False
    snapcast_host: str = "localhost"
    # Port HTTP/WebSocket de snapserver : il porte a la fois le controle
    # JSON-RPC (/jsonrpc) et l'audio (/stream). C'est le seul port de
    # snapserver que l'API utilise ; le port de controle TCP 1705 ne sert plus.
    snapcast_http_port: int = 1780
    # Adresse a laquelle snapserver joint CETTE API pour venir lire le PCM.
    # Resolue en IP avant d'etre transmise : snapserver refuse un nom d'hote.
    snapcast_advertise_host: str = "localhost"
    # Plage de ports ouverts par l'API, un par session en mode snapcast.
    snapcast_port_start: int = 4960
    snapcast_port_count: int = 20
    # Snapserver ne chiffre pas son serveur HTTP : activer ceci suppose un
    # reverse proxy TLS devant lui, et `snapcast_http_port` pointant sur ce
    # proxy. Vaut pour le controle comme pour l'audio, qui partagent le port.
    snapcast_tls: bool = False
    # Autorite propre a snapserver, quand son proxy TLS n'est pas signe par
    # la meme que le reste. Vide = `tls_ca_file`, puis magasin systeme.
    snapcast_tls_ca_file: str = ""
    # Nom verifie contre le certificat (et envoye en SNI). Vide = snapcast_host,
    # a renseigner si l'on joint le serveur par IP.
    snapcast_tls_server_name: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
