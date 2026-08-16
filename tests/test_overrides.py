"""Corrections manuelles : ce sont elles qui rendent une bibliotheque mal taggee
utilisable en airgap, il faut donc qu'elles resistent aux rescans."""

from app.models import Album, Track, refresh_effective


def make_track(**overrides_kwargs) -> Track:
    return Track(
        source_title="Aerodynamic",
        source_track_no=2,
        source_disc_no=1,
        **overrides_kwargs,
    )


class TestRefreshEffective:
    def test_sans_correction_les_valeurs_des_tags_sont_utilisees(self):
        track = make_track(overrides={})
        refresh_effective(track)
        assert (track.title, track.track_no, track.disc_no) == ("Aerodynamic", 2, 1)

    def test_la_correction_prime_sur_le_tag(self):
        track = make_track(overrides={"title": "Aerodynamic (Remaster)"})
        refresh_effective(track)
        assert track.title == "Aerodynamic (Remaster)"
        # Les champs non corriges suivent toujours le tag.
        assert track.track_no == 2

    def test_relire_les_tags_n_ecrase_pas_la_correction(self):
        """Simule un rescan : la source change, la correction doit tenir."""
        track = make_track(overrides={"title": "Titre corrige"})
        refresh_effective(track)

        track.source_title = "Titre relu depuis le fichier"
        track.source_track_no = 9
        refresh_effective(track)

        assert track.title == "Titre corrige"
        assert track.track_no == 9

    def test_retirer_la_correction_restaure_la_valeur_du_tag(self):
        track = make_track(overrides={"title": "Titre corrige"})
        refresh_effective(track)
        assert track.title == "Titre corrige"

        track.overrides = {}
        refresh_effective(track)
        assert track.title == "Aerodynamic"

    def test_une_correction_a_zero_est_conservee(self):
        """0 est une valeur legitime : elle ne doit pas etre confondue avec 'absent'."""
        track = make_track(overrides={"track_no": 0})
        refresh_effective(track)
        assert track.track_no == 0

    def test_un_champ_non_editable_est_ignore(self):
        track = make_track(overrides={"object_key": "piratage/tentative.mp3"})
        track.object_key = "Artiste/Album/01.mp3"
        refresh_effective(track)
        assert track.object_key == "Artiste/Album/01.mp3"

    def test_album_titre_affiche_et_titre_d_identite_sont_distincts(self):
        """Renommer un album ne doit pas changer la cle de rapprochement des scans."""
        album = Album(source_title="Discovery", source_year=2001, overrides={"title": "Découverte"})
        refresh_effective(album)

        assert album.title == "Découverte"
        assert album.source_title == "Discovery"
        assert album.year == 2001
