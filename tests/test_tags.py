import mutagen
import pytest

from app.services.tags import _from_path, _to_int, _to_year, read_tags
from tests.conftest import ffmpeg_required


def write(path, **tags):
    audio = mutagen.File(path, easy=True)
    if audio.tags is None:
        audio.add_tags()
    for key, value in tags.items():
        audio[key] = value
    audio.save()


class TestParsingHelpers:
    @pytest.mark.parametrize(
        "raw,expected",
        [("3", 3), ("3/12", 3), ("03", 3), ("", None), (None, None), ("abc", None)],
    )
    def test_to_int(self, raw, expected):
        assert _to_int(raw) == expected

    @pytest.mark.parametrize(
        "raw,expected",
        [("2001", 2001), ("2001-05-01", 2001), ("", None), ("99", None)],
    )
    def test_to_year(self, raw, expected):
        assert _to_year(raw) == expected

    def test_from_path_extrait_artiste_album_titre_et_numero(self):
        artist, album, title, track_no = _from_path("Daft Punk/Discovery/02 - Aerodynamic.mp3")
        assert (artist, album, title, track_no) == ("Daft Punk", "Discovery", "Aerodynamic", 2)

    def test_from_path_sans_numero_en_tete(self):
        _, _, title, track_no = _from_path("A/B/Aerodynamic.mp3")
        assert (title, track_no) == ("Aerodynamic", None)

    def test_from_path_arborescence_plate(self):
        artist, album, title, _ = _from_path("morceau.mp3")
        assert (artist, album, title) == (None, None, "morceau")


@ffmpeg_required
class TestReadTags:
    def test_tags_complets_priment_sur_le_chemin(self, make_audio):
        path = make_audio("mp3")
        write(
            path,
            title="Aerodynamic",
            artist="Daft Punk",
            album="Discovery",
            tracknumber="2/14",
            discnumber="1",
            date="2001",
        )
        tags = read_tags(path, "Autre Artiste/Autre Album/99 - Autre titre.mp3")

        assert tags.title == "Aerodynamic"
        assert tags.album == "Discovery"
        assert tags.track_no == 2
        assert tags.year == 2001
        assert tags.fmt == "mp3"
        assert tags.duration_s == pytest.approx(1.0, abs=0.2)

    def test_fichier_sans_tag_retombe_sur_le_chemin(self, make_audio):
        path = make_audio("mp3")
        tags = read_tags(path, "Untagged Band/Nameless Sessions/02 - Second Thought.mp3")

        assert tags.title == "Second Thought"
        assert tags.album == "Nameless Sessions"
        assert tags.album_artist == "Untagged Band"
        assert tags.track_no == 2

    def test_albumartist_porte_l_identite_de_l_album(self, make_audio):
        """Sur une compilation, l'album doit rester groupe malgre des artistes differents."""
        path = make_audio("mp3")
        write(path, title="Premier morceau", artist="Groupe A", albumartist="Various Artists", album="Compil")
        tags = read_tags(path, "Various Artists/Compil/01 - Premier morceau.mp3")

        assert tags.artist == "Groupe A"
        assert tags.album_artist == "Various Artists"

    def test_sans_albumartist_on_retombe_sur_artist(self, make_audio):
        path = make_audio("mp3")
        write(path, title="T", artist="Daft Punk", album="Discovery")
        assert read_tags(path, "x/y/z.mp3").album_artist == "Daft Punk"

    def test_flac_avec_accents(self, make_audio):
        path = make_audio("flac")
        write(path, title="La Vie en rose", artist="Édith Piaf", album="Éternelle", tracknumber="2")
        tags = read_tags(path, "Édith Piaf/Éternelle/02 - La Vie en rose.flac")

        assert tags.artist == "Édith Piaf"
        assert tags.album == "Éternelle"
        assert tags.fmt == "flac"
        assert tags.sample_rate == 44100

    def test_m4a(self, make_audio):
        path = make_audio("m4a")
        write(path, title="Pocket Calculator", artist="Kraftwerk", album="Computer World")
        tags = read_tags(path, "Kraftwerk/Computer World/02 - Pocket Calculator.m4a")

        assert tags.title == "Pocket Calculator"
        assert tags.fmt == "m4a"

    def test_tags_vides_traites_comme_absents(self, make_audio):
        path = make_audio("mp3")
        write(path, title="   ", album="")
        tags = read_tags(path, "Artiste/Album/01 - Depuis le chemin.mp3")

        assert tags.title == "Depuis le chemin"
        assert tags.album == "Album"

    def test_paroles_selon_le_conteneur(self, make_audio):
        """Chaque format range les paroles ailleurs : USLT, ©lyr, LYRICS."""
        from mutagen.flac import FLAC
        from mutagen.id3 import ID3, USLT
        from mutagen.mp4 import MP4

        paroles = "Ligne une\nLigne deux"

        mp3 = make_audio("mp3")
        write(mp3, title="T")
        tags = ID3(mp3)
        tags.add(USLT(encoding=3, lang="fra", desc="", text=paroles))
        tags.save(mp3)
        assert read_tags(mp3, "a/b/c.mp3").lyrics == paroles

        m4a = make_audio("m4a")
        write(m4a, title="T")
        mp4 = MP4(m4a)
        mp4["\xa9lyr"] = paroles
        mp4.save()
        assert read_tags(m4a, "a/b/c.m4a").lyrics == paroles

        flac = make_audio("flac")
        write(flac, title="T")
        audio = FLAC(flac)
        audio["lyrics"] = paroles
        audio.save()
        assert read_tags(flac, "a/b/c.flac").lyrics == paroles

    def test_paroles_absentes(self, make_audio):
        path = make_audio("mp3")
        write(path, title="T")
        assert read_tags(path, "a/b/c.mp3").lyrics is None

    def test_paroles_tres_longues_sont_tronquees(self, make_audio):
        """Garde-fou : un tag aberrant ne doit pas gonfler la base."""
        from mutagen.id3 import ID3, USLT

        from app.services.tags import MAX_LYRICS_CHARS

        path = make_audio("mp3")
        write(path, title="T")
        tags = ID3(path)
        tags.add(USLT(encoding=3, lang="fra", desc="", text="a" * (MAX_LYRICS_CHARS + 5000)))
        tags.save(path)
        assert len(read_tags(path, "a/b/c.mp3").lyrics) == MAX_LYRICS_CHARS

    def test_genre(self, make_audio):
        path = make_audio("mp3")
        write(path, title="T", genre="Electronic")
        assert read_tags(path, "a/b/c.mp3").genre == "Electronic"

    def test_fichier_non_audio_leve_valueerror(self, tmp_path):
        path = tmp_path / "notes.txt"
        path.write_text("ceci n'est pas de la musique")
        with pytest.raises(ValueError):
            read_tags(path, "x/y/notes.txt")
