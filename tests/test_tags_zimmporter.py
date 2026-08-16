"""Contrat avec zimmporter, l'outil qui alimente le bucket.

Reproduit exactement ce qu'ecrit `zimmporter/postprocessors.py` (EnrichMeta +
UploadToS3) : fichiers AAC/m4a, tags title/artist/album/date/tracknumber/genre,
pochette dans l'atome `covr`, cle `{artist}/{album}/{NN - titre}.m4a`.

Deux absences sont structurantes et volontairement figees ici : zimmporter
n'ecrit **ni albumartist ni discnumber**, et pour les playlists il force
l'artiste a la chaine litterale "playlists".
"""

import mutagen
import pytest
from mutagen.mp4 import MP4, MP4Cover

from app.services.covers import extract_embedded_cover
from app.services.tags import read_tags
from tests.conftest import ffmpeg_required


def write_zimmporter_tags(
    path, metadata: dict, cover: bytes | None = None, lyrics: str | None = None
) -> None:
    """Meme sequence d'ecriture que EnrichMeta.run()."""
    audio = mutagen.File(path, easy=True)
    for key, value in metadata.items():
        if value is not None:
            audio[key] = value
    audio.save()

    # EnrichMeta._write_lyrics : atome ©lyr en MP4, frame USLT sinon.
    if lyrics:
        mp4 = MP4(path)
        mp4["\xa9lyr"] = lyrics
        mp4.save()

    if cover:
        mp4 = MP4(path)
        mp4["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG)]
        mp4.save()


@ffmpeg_required
class TestFichiersZimmporter:
    def test_album_standard(self, make_audio, tiny_jpeg):
        path = make_audio("m4a")
        write_zimmporter_tags(
            path,
            {
                "title": "Around the World",
                "artist": "Daft Punk",
                "album": "Homework",
                "date": "1997",
                "tracknumber": "7",
                "genre": "Electronic",
            },
            cover=tiny_jpeg,
        )
        tags = read_tags(path, "Daft Punk/Homework/07 - Around the World.m4a")

        assert tags.title == "Around the World"
        assert tags.artist == "Daft Punk"
        assert tags.track_no == 7
        assert tags.year == 1997
        assert tags.fmt == "m4a"
        # Sans albumartist, l'identite de l'album retombe sur artist : c'est ce
        # qui regroupe correctement les albums importes par zimmporter.
        assert tags.album_artist == "Daft Punk"
        assert tags.disc_no is None
        assert tags.genre == "Electronic"
        assert extract_embedded_cover(path) == tiny_jpeg

    def test_genre_itunes_et_paroles_lrclib(self, make_audio):
        """zimmporter enrichit genre (iTunes) et paroles (LRCLIB) au download."""
        path = make_audio("m4a")
        paroles = "Premiere ligne\nDeuxieme ligne\nTroisieme ligne"
        write_zimmporter_tags(
            path,
            {"title": "T", "artist": "A", "album": "B", "genre": "Alternative"},
            lyrics=paroles,
        )
        tags = read_tags(path, "A/B/01 - T.m4a")

        assert tags.genre == "Alternative"
        assert tags.lyrics == paroles

    def test_genre_absent_quand_itunes_ne_repond_pas(self, make_audio):
        """_clear_genre supprime le tag : on doit lire None, pas une chaine vide."""
        path = make_audio("m4a")
        write_zimmporter_tags(path, {"title": "T", "artist": "A", "album": "B"})
        tags = read_tags(path, "A/B/01 - T.m4a")

        assert tags.genre is None
        assert tags.lyrics is None

    def test_playlist_artiste_litteral(self, make_audio):
        """zimmporter force artist="playlists" et n'ecrit aucun numero de piste."""
        path = make_audio("m4a")
        write_zimmporter_tags(
            path,
            {"title": "Some Song", "artist": "playlists", "album": "Ma playlist", "date": ""},
        )
        tags = read_tags(path, "playlists/Ma playlist/Some Song.m4a")

        assert tags.title == "Some Song"
        assert tags.album == "Ma playlist"
        assert tags.album_artist == "playlists"
        # Ni tag ni prefixe dans le nom de fichier : aucun numero a deduire.
        assert tags.track_no is None
        assert tags.year is None

    def test_slash_remplace_par_tiret_dans_la_cle(self, make_audio):
        """UploadToS3 remplace "/" par "-" : le chemin reste sur 3 segments."""
        path = make_audio("m4a")
        write_zimmporter_tags(
            path, {"title": "Sgt. Pepper", "artist": "AC-DC", "album": "Rock-Pop"}
        )
        tags = read_tags(path, "AC-DC/Rock-Pop/01 - Sgt. Pepper.m4a")

        assert tags.artist == "AC-DC"
        assert tags.album == "Rock-Pop"

    def test_date_vide_ne_produit_pas_d_annee(self, make_audio):
        """Les playlists ecrivent date="" quand l'annee est inconnue."""
        path = make_audio("m4a")
        write_zimmporter_tags(path, {"title": "T", "artist": "A", "album": "B", "date": ""})
        assert read_tags(path, "A/B/T.m4a").year is None

    def test_tags_absents_repli_sur_la_cle_zimmporter(self, make_audio):
        """Si l'enrichissement a echoue, la cle porte encore l'information."""
        path = make_audio("m4a")
        tags = read_tags(path, "Daft Punk/Homework/07 - Around the World.m4a")

        assert tags.title == "Around the World"
        assert tags.album == "Homework"
        assert tags.album_artist == "Daft Punk"
        assert tags.track_no == 7


@pytest.fixture(scope="session")
def tiny_jpeg() -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), (10, 120, 200)).save(buffer, format="JPEG")
    return buffer.getvalue()
