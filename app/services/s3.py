from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import boto3
from botocore.client import BaseClient
from botocore.config import Config

from app.config import get_settings

_CONFIG = Config(
    signature_version="s3v4",
    s3={"addressing_style": "path"},
    retries={"max_attempts": 3, "mode": "standard"},
)


def _client(endpoint: str) -> BaseClient:
    settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
        config=_CONFIG,
    )


@lru_cache
def get_internal_client() -> BaseClient:
    """Client utilise par le backend pour lister et telecharger."""
    return _client(get_settings().s3_endpoint)


@lru_cache
def _signing_client(origin: str) -> BaseClient:
    return _client(origin)


def public_stream_url(key: str, content_type: str | None = None) -> str:
    """URL presignee telle que le navigateur doit l'appeler.

    En SigV4 le Host et le chemin sont couverts par la signature. Quand MinIO
    est expose derriere nginx sous un prefixe (`http://host/s3`), on signe donc
    sur l'origine seule puis on reinjecte le prefixe dans le chemin : nginx le
    retire avant de transmettre a MinIO, qui retrouve exactement le chemin
    signe. Voir nginx/default.conf.
    """
    settings = get_settings()
    base = urlsplit(settings.s3_public_base_url.rstrip("/"))
    origin = urlunsplit((base.scheme, base.netloc, "", "", ""))

    params: dict[str, str] = {"Bucket": settings.s3_bucket, "Key": key}
    if content_type:
        # Les objets d'un bucket existant sont souvent stockes en
        # binary/octet-stream. On impose le type a la reponse plutot que de
        # dependre de la facon dont le bucket a ete rempli.
        params["ResponseContentType"] = content_type

    signed = _signing_client(origin).generate_presigned_url(
        "get_object",
        Params=params,
        ExpiresIn=settings.presign_ttl_seconds,
    )

    if not base.path:
        return signed
    parts = urlsplit(signed)
    return urlunsplit(
        (parts.scheme, parts.netloc, base.path + parts.path, parts.query, "")
    )


def internal_stream_url(key: str) -> str:
    """URL presignee sur l'endpoint interne, pour ffmpeg cote serveur.

    Evite de telecharger le fichier avant de le decoder : ffmpeg lit l'objet en
    HTTP et n'en tire que ce dont il a besoin.
    """
    settings = get_settings()
    return get_internal_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.s3_bucket, "Key": key},
        ExpiresIn=settings.presign_ttl_seconds,
    )


def iter_objects(prefix: str = "") -> Iterator[dict[str, Any]]:
    settings = get_settings()
    paginator = get_internal_client().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=settings.s3_bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            yield {
                "key": obj["Key"],
                "etag": obj["ETag"].strip('"'),
                "size": obj["Size"],
                "last_modified": obj["LastModified"].replace(tzinfo=None),
            }


def download_to(key: str, destination: Path) -> None:
    get_internal_client().download_file(get_settings().s3_bucket, key, str(destination))


def get_object_bytes(key: str) -> bytes:
    settings = get_settings()
    response = get_internal_client().get_object(Bucket=settings.s3_bucket, Key=key)
    return response["Body"].read()


def object_exists(key: str) -> bool:
    from botocore.exceptions import ClientError

    try:
        get_internal_client().head_object(Bucket=get_settings().s3_bucket, Key=key)
    except ClientError:
        return False
    return True
