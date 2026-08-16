import shutil
import subprocess
from pathlib import Path

import pytest

ffmpeg_required = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg absent"
)


@pytest.fixture(scope="session")
def make_audio(tmp_path_factory):
    """Fabrique un fichier audio reel du format demande, sans aucun tag."""
    directory = tmp_path_factory.mktemp("audio")
    counter = iter(range(1000))

    def _make(extension: str) -> Path:
        path = directory / f"sample{next(counter)}.{extension}"
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                str(path),
            ],
            check=True,
        )
        return path

    return _make
