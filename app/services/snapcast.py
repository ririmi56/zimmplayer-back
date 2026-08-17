"""Client de controle Snapcast (JSON-RPC 2.0 sur la WebSocket /jsonrpc).

Une connexion par appel, volontairement. Le controle est peu sollicite
(quelques appels par seconde au plus, sur un reseau local) et cela evite tout
un etage de complexite : pas de thread lecteur, pas de machine a etats de
reconnexion, pas de cache a invalider, et c'est naturellement sur vis-a-vis des
threads.

On passe par le serveur HTTP integre de snapserver (port 1780 par defaut, celui
qui sert aussi Snapweb et le flux audio `/stream`) plutot que par le port de
controle TCP 1705 : c'est le seul port expose en production, et il porte
exactement le meme JSON-RPC. Consequence : l'API ne connait plus qu'un seul
port de snapserver (`snapcast_http_port`) ; 1705 ne sert plus a rien, et le
reglage correspondant a ete retire.

Attention : snapserver entrelace ses notifications avec les reponses sur la
meme connexion. Il faut donc lire message par message jusqu'a trouver celui qui
porte l'`id` de la requete, et ignorer le reste.

TLS : snapserver ne chiffre pas lui-meme. `snapcast_tls` suppose un reverse
proxy devant lui, et `snapcast_http_port` pointant sur ce proxy.
"""

import json
import logging
import ssl
import time
from functools import lru_cache
from itertools import count
from typing import Any

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from app.config import get_settings

logger = logging.getLogger(__name__)

_ids = count(1)
DEFAULT_TIMEOUT = 5.0


class SnapcastError(RuntimeError):
    """Snapserver injoignable ou ayant refuse la requete."""


@lru_cache(maxsize=8)
def _ssl_context(ca_file: str) -> ssl.SSLContext:
    """Contexte TLS pour joindre le proxy place devant snapserver.

    Mis en cache : `call()` ouvre une connexion par appel, et construire un
    contexte relit et parse tout le magasin de certificats a chaque fois.

    `ca_file` vide signifie « magasin systeme ». Sur un reseau airgap avec un
    certificat auto-signe, pointer ici le certificat de VOTRE autorite plutot
    que de desactiver la verification.
    """
    return ssl.create_default_context(cafile=ca_file or None)


def ws_target(host: str, port: int, path: str) -> tuple[str, dict[str, Any]]:
    """URI et options TLS pour joindre snapserver en WebSocket.

    Partage par le controle JSON-RPC et par le relais audio `/stream` : les
    deux passent par le meme hote et le meme port, donc par le meme schema.
    Coder `ws://` en dur d'un cote et `wss://` de l'autre est le bug qu'on
    evite ici.
    """
    settings = get_settings()
    tls = settings.snapcast_tls
    server_name = settings.snapcast_tls_server_name

    uri = f"{'wss' if tls else 'ws'}://{host}:{port}{path}"
    options: dict[str, Any] = {}
    if tls:
        options["ssl"] = _ssl_context(settings.snapcast_tls_ca_file)
        # server_hostname porte le SNI *et* le nom verifie contre le
        # certificat : il doit correspondre au CN/SAN, qui n'est pas forcement
        # l'adresse par laquelle on joint le serveur.
        if server_name:
            options["server_hostname"] = server_name
    return uri, options


def call(
    host: str,
    port: int,
    method: str,
    params: dict[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Any:
    """Un appel JSON-RPC, une connexion.

    `port` est le port HTTP de snapserver (`http_port` dans la configuration),
    pas l'ancien port de controle TCP.
    """
    request_id = next(_ids)
    payload: dict[str, Any] = {"id": request_id, "jsonrpc": "2.0", "method": method}
    if params:
        payload["params"] = params

    uri, options = ws_target(host, port, "/jsonrpc")

    try:
        with connect(
            uri, open_timeout=timeout, close_timeout=timeout, **options
        ) as socket:
            socket.send(json.dumps(payload))

            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SnapcastError("pas de reponse de snapserver")

                raw = socket.recv(timeout=remaining)
                if isinstance(raw, bytes):
                    raw = raw.decode()
                message = json.loads(raw)

                # snapserver emet aussi des notifications (pas d'`id`) et peut
                # repondre a plusieurs requetes en vol : on ne retient que la
                # notre.
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    detail = message["error"]
                    raise SnapcastError(
                        f"{detail.get('message')}: {detail.get('data', '')}".strip(": ")
                    )
                return message.get("result")

    # Avant WebSocketException/OSError : SSLCertVerificationError en herite, et
    # son message brut ("[SSL: CERTIFICATE_VERIFY_FAILED] ...") n'aide personne
    # dans l'interface.
    except ssl.SSLCertVerificationError as exc:
        verified = get_settings().snapcast_tls_server_name or host
        raise SnapcastError(
            f"certificat refuse par {verified} : {exc.verify_message}"
        ) from exc
    except ssl.SSLError as exc:
        raise SnapcastError(f"echec de la negociation TLS : {exc}") from exc
    except TimeoutError as exc:
        raise SnapcastError(f"snapserver ne repond pas ({uri})") from exc
    except (WebSocketException, OSError, json.JSONDecodeError) as exc:
        raise SnapcastError(f"{type(exc).__name__}: {exc}") from exc


def normalise_status(raw: dict[str, Any]) -> dict[str, Any]:
    """Applatit la reponse Server.GetStatus en une forme utilisable par l'UI."""
    server = raw.get("server", {})
    groups = []
    for group in server.get("groups", []):
        clients = []
        for client in group.get("clients", []):
            config = client.get("config", {})
            host = client.get("host", {})
            volume = config.get("volume", {})
            clients.append(
                {
                    "id": client.get("id"),
                    # Le nom configure est souvent vide : on retombe alors sur
                    # le nom de machine, comme le fait l'interface Snapcast.
                    "name": config.get("name") or host.get("name") or client.get("id"),
                    "connected": bool(client.get("connected")),
                    "volume": volume.get("percent", 0),
                    "muted": bool(volume.get("muted")),
                    "latency": config.get("latency", 0),
                    "host_name": host.get("name"),
                    "ip": host.get("ip"),
                    "os": host.get("os"),
                }
            )
        groups.append(
            {
                "id": group.get("id"),
                "name": group.get("name") or "",
                "muted": bool(group.get("muted")),
                "stream_id": group.get("stream_id"),
                "clients": clients,
            }
        )

    streams = [
        {"id": stream.get("id"), "status": stream.get("status")}
        for stream in server.get("streams", [])
    ]
    return {"groups": groups, "streams": streams}


def stream_uri(advertise_ip: str, port: int, name: str) -> str:
    """URI du flux que snapserver viendra lire chez nous.

    `mode=client` : c'est snapserver qui se connecte a notre port, ce qui evite
    d'avoir a declarer un port par session dans snapserver.conf. Corollaire a
    ne pas perdre de vue : snapserver doit pouvoir SORTIR vers l'API sur la
    plage `snapcast_port_start`, independamment des ports qu'il expose.

    L'hote doit imperativement etre une adresse IP : snapserver rejette un nom
    d'hote avec « Invalid argument » dans ce mode.

    `codec=pcm` est impose : le front est lui-meme un snapclient et lit le flux
    dans le navigateur. En PCM il n'a aucun decodeur a embarquer, la ou flac ou
    opus exigeraient un decodeur WASM — inutile ici, ou l'on est sur un reseau
    local et ou 1,5 Mbit/s par flux ne pose aucun probleme.
    """
    return (
        f"tcp://{advertise_ip}:{port}"
        f"?name={name}&mode=client&sampleformat=48000:16:2&codec=pcm"
    )
