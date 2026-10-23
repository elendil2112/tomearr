"""Runtime settings, read from the environment on every call so tests can override them."""
import os
from dataclasses import dataclass
from pathlib import Path

DUMP_URL = "https://openlibrary.org/data/ol_dump_{kind}_latest.txt.gz"
KINDS = ("authors", "works")


@dataclass(frozen=True)
class Settings:
    config_dir: Path
    data_dir: Path
    static_dir: Path

    @property
    def db_path(self) -> Path:
        return self.config_dir / "tomearr.db"

    @property
    def dump_dir(self) -> Path:
        return self.data_dir / "dumps"

    def dump_path(self, kind: str) -> Path:
        return self.dump_dir / f"ol_dump_{kind}.txt.gz"


def get_settings() -> Settings:
    here = Path(__file__).resolve().parent
    s = Settings(
        config_dir=Path(os.environ.get("TOMEARR_CONFIG", "./config")),
        data_dir=Path(os.environ.get("TOMEARR_DATA", "./data")),
        static_dir=Path(os.environ.get("TOMEARR_STATIC", here / "static")),
    )
    s.config_dir.mkdir(parents=True, exist_ok=True)
    s.dump_dir.mkdir(parents=True, exist_ok=True)
    return s
