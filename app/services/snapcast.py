"""Client de controle Snapcast (JSON-RPC 2.0 en ndjson sur le port 1705).

Une connexion par appel, volontairement. Le controle est peu sollicite
(quelques appels par seconde au plus, sur un reseau local) et cela evite tout
un etage de complexite : pas de thread lecteur, pas de machine a etats de
reconnexion, pas de cache a invalider, et c'est naturellement sur vis-a-vis des
threads.

Attention : snapserver entrelace ses notifications avec les reponses sur la
meme socket. Il faut donc lire ligne a ligne jusqu'a trouver celle qui porte
l'`id` de la requete, et ignorer le reste.

TLS : snapserver ne chiffre pas lui-meme le port de controle. `snapcast_tls`
suppose donc un terminateur TLS (stunnel, nginx en mode `stream`...) place
devant snapserver, sur un port distinct : c'est vers CE port que pointe
`snapcast_port` quand l'option est active.
"""

import json
import logging
import socket
import ssl
import time
from functools import lru_cache
from itertools import count
from typing import Any

from ..config import get_settings

logger = logging.getLogger(__name__)

_ids = count(1)
DEFAULT_TIMEOUT = 5.0


class SnapcastError(RuntimeError):
    """Snapserver injoignable ou ayant refuse la requete."""


@lru_cache(maxsize=8)
def _ssl_context(ca_file: str) -> ssl.SSLContext:
    """Contexte TLS pour joindre le terminateur place devant snapserver.

    Mis en cache : `call()` ouvre une connexion par appel, et construire un
    contexte relit et parse tout le magasin de certificats a chaque fois.

    `ca_file` vide signifie « magasin systeme ». Sur un reseau airgap avec un
    certificat auto-signe, pointer ici le certificat de VOTRE autorite plutot
    que de desactiver la verification : un controle non authentifie laisse
    passer n'importe quel serveur qui repond sur le port.
    """
    return ssl.create_default_context(cafile=ca_file or None)


def call(
    host: str,
    port: int,
    method: str,
    params: dict[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    tls: bool | None = None,
    ca_file: str | None = None,
    server_name: str | None = None,
) -> Any:
    """Un appel JSON-RPC, une connexion.

    `tls`, `ca_file` et `server_name` valent par defaut la configuration
    (`SNAPCAST_TLS...`) : les appelants n'ont rien a transmettre, seuls les
    tests ont besoin de les forcer.
    """
    settings = get_settings()
    if tls is None:
        tls = settings.snapcast_tls
    if ca_file is None:
        ca_file = settings.snapcast_tls_ca_file
    if server_name is None:
        server_name = settings.snapcast_tls_server_name

    request_id = next(_ids)
    payload = {"id": request_id, "jsonrpc": "2.0", "method": method}
    if params:
        payload["params"] = params

    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        if tls:
            # server_hostname porte le SNI *et* le nom verifie contre le
            # certificat : il doit correspondre au CN/SAN, qui n'est pas
            # forcement l'adresse par laquelle on joint le serveur (cas d'une
            # IP en airgap, avec un certificat emis pour un nom).
            sock = _ssl_context(ca_file).wrap_socket(
                sock, server_hostname=server_name or host
            )

        with sock:
            sock.settimeout(timeout)
            sock.sendall((json.dumps(payload) + "\r\n").encode())

            buffer = b""
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                chunk = sock.recv(65536)
                if not chunk:
                    raise SnapcastError("connexion fermee par snapserver")
                buffer += chunk

                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    message = json.loads(line)
                    # Les notifications n'ont pas d'`id` : on les laisse passer.
                    if message.get("id") != request_id:
                        continue
                    if "error" in message:
                        detail = message["error"]
                        raise SnapcastError(
                            f"{detail.get('message')}: {detail.get('data', '')}".strip(": ")
                        )
                    return message.get("result")
            raise SnapcastError("pas de reponse de snapserver")

    # Avant OSError : SSLCertVerificationError en herite, et son message brut
    # ("[SSL: CERTIFICATE_VERIFY_FAILED] ...") n'aide personne dans l'UI.
    except ssl.SSLCertVerificationError as exc:
        raise SnapcastError(
            f"certificat refuse par {server_name or host} : {exc.verify_message}"
        ) from exc
    except ssl.SSLError as exc:
        raise SnapcastError(f"echec de la negociation TLS : {exc}") from exc
    except (OSError, json.JSONDecodeError) as exc:
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
    d'avoir a declarer un port par session dans snapserver.conf.

    L'hote doit imperativement etre une adresse IP : snapserver rejette un nom
    d'hote avec « Invalid argument » dans ce mode.

    `codec=pcm` est impose : le front est lui-meme un snapclient et lit le flux
    dans le navigateur. En PCM il n'a aucun decodeur a embarquer, la ou flac ou
    opus exigeraient un decodeur WASM — inutile ici, ou l'on est sur un reseau
    local et ou 1,5 Mbit/s par flux ne pose aucun probleme.

    Ce transport reste en clair meme avec `snapcast_tls` : snapserver ne sait
    lire qu'en `tcp://`, il n'existe pas de variante chiffree.
    """
    return (
        f"tcp://{advertise_ip}:{port}"
        f"?name={name}&mode=client&sampleformat=48000:16:2&codec=pcm"
    )
