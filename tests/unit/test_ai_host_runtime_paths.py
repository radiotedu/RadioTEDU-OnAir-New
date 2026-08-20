import os
from pathlib import Path
import subprocess
import sys


def test_ai_cache_follows_selected_runtime_data_root(tmp_path: Path) -> None:
    database_path = tmp_path / "cleanroom.db"
    environment = os.environ.copy()
    environment["CLEANROOM_DB_PATH"] = str(database_path)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.services.ai_host import CACHE_DIR; print(CACHE_DIR)",
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        check=True,
        text=True,
    )

    assert Path(result.stdout.strip()) == tmp_path / "ai_cache"
