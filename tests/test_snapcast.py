"""Fonctions pures du sous-systeme Snapcast.

Le reste (flux TCP, ffmpeg, snapserver) est verifie de bout en bout par
scripts/check_snapcast.sh contre un vrai snapserver.
"""

from datetime import timedelta

import pytest

from app.models import Session, utcnow
from app.services.queue import effective_position
from app.services.snapcast import normalise_status, stream_uri
from app.services.snapoutput import BYTES_PER_SECOND, stream_name


class TestStreamUri:
    def test_mode_client_et_format_impose(self):
        uri = stream_uri("192.168.1.10", 4960, "Salon")
        assert uri.startswith("tcp://192.168.1.10:4960?")
        # mode=client : c'est snapserver qui vient se connecter a nous.
        assert "mode=client" in uri
        assert "sampleformat=48000:16:2" in uri
        assert "name=Salon" in uri

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Salon", "Salon"),
            ("Salle a manger", "Salle-a-manger"),
            ("Fete des voisins !", "Fete-des-voisins"),
            ("???", "session"),
            ("", "session"),
        ],
    )
    def test_nom_de_flux_utilisable_dans_une_uri(self, raw, expected):
        assert stream_name(raw) == expected


class TestNormaliseStatus:
    def test_extrait_pieces_appareils_et_volumes(self):
        raw = {
            "server": {
                "groups": [
                    {
                        "id": "g1",
                        "name": "Salon",
                        "muted": False,
                        "stream_id": "Cuisine",
                        "clients": [
                            {
                                "id": "aa:bb",
                                "connected": True,
                                "config": {
                                    "name": "Enceinte",
                                    "latency": 12,
                                    "volume": {"percent": 42, "muted": False},
                                },
                                "host": {"name": "pi", "ip": "10.0.0.5", "os": "Linux"},
                            }
                        ],
                    }
                ],
                "streams": [{"id": "Cuisine", "status": "playing"}],
            }
        }
        result = normalise_status(raw)

        assert result["streams"] == [{"id": "Cuisine", "status": "playing"}]
        client = result["groups"][0]["clients"][0]
        assert (client["name"], client["volume"], client["ip"]) == ("Enceinte", 42, "10.0.0.5")

    def test_nom_vide_retombe_sur_le_nom_de_machine(self):
        """Snapcast laisse souvent `config.name` vide ; afficher un champ vide
        dans la liste des membres serait inutilisable."""
        raw = {
            "server": {
                "groups": [
                    {
                        "id": "g1",
                        "clients": [
                            {
                                "id": "aa:bb",
                                "connected": True,
                                "config": {"name": "", "volume": {"percent": 100}},
                                "host": {"name": "salon-pi"},
                            }
                        ],
                    }
                ],
                "streams": [],
            }
        }
        assert normalise_status(raw)["groups"][0]["clients"][0]["name"] == "salon-pi"

    def test_serveur_sans_groupe(self):
        assert normalise_status({"server": {}}) == {"groups": [], "streams": []}


class TestEffectivePosition:
    def test_en_pause_la_position_est_figee(self):
        session = Session(
            is_playing=False, position_s=42.0, updated_at=utcnow() - timedelta(seconds=30)
        )
        assert effective_position(session) == 42.0

    def test_en_lecture_la_position_est_extrapolee(self):
        now = utcnow()
        session = Session(is_playing=True, position_s=10.0, updated_at=now - timedelta(seconds=5))
        assert effective_position(session, now) == pytest.approx(15.0)

    def test_jamais_negative(self):
        now = utcnow()
        session = Session(is_playing=True, position_s=0.0, updated_at=now + timedelta(seconds=5))
        assert effective_position(session, now) == 0.0


def test_debit_pcm_attendu_par_snapcast():
    """48 kHz, 16 bits, stereo : la cadence sur laquelle repose le calcul de position."""
    assert BYTES_PER_SECOND == 48000 * 2 * 2
