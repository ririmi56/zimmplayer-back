"""Reglages persistes, avec repli sur la configuration d'environnement."""

from sqlalchemy.orm import Session as DbSession

from app.config import get_settings
from app.models import AppSetting, utcnow

SNAPCAST_HOST = "snapcast.host"
SNAPCAST_HTTP_PORT = "snapcast.http_port"
SNAPCAST_ENABLED = "snapcast.enabled"
SNAPCAST_ADVERTISE_HOST = "snapcast.advertise_host"


def get(db: DbSession, key: str, default: str = "") -> str:
    row = db.get(AppSetting, key)
    return row.value if row is not None else default


def set_value(db: DbSession, key: str, value: str) -> None:
    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=value))
    else:
        row.value = value
        row.updated_at = utcnow()


def snapcast_config(db: DbSession) -> dict[str, object]:
    settings = get_settings()
    return {
        "host": get(db, SNAPCAST_HOST, settings.snapcast_host),
        # Port unique de snapserver : controle JSON-RPC et audio. L'ancienne
        # cle `snapcast.port` (1705) peut subsister en base, elle n'est plus lue.
        "http_port": int(
            get(db, SNAPCAST_HTTP_PORT, str(settings.snapcast_http_port))
        ),
        "enabled": get(db, SNAPCAST_ENABLED, str(settings.snapcast_enabled)).lower()
        in ("1", "true", "yes"),
        "advertise_host": get(
            db, SNAPCAST_ADVERTISE_HOST, settings.snapcast_advertise_host
        ),
    }
