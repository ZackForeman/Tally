"""The haulier's set-up: fleet, rate cards and tolerances, entered once.

Lives in ``data/setup.json`` (not committed). The first run copies the
fictional demo set-up so the app works out of the box.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from tally.engine.models import Setup
from tally.store import DATA

DEMO_SETUP = Path(__file__).resolve().parent / "demo" / "setup.json"


def setup_path(root: Path = DATA) -> Path:
    path = root / "setup.json"
    if not path.exists():
        root.mkdir(parents=True, exist_ok=True)
        shutil.copy(DEMO_SETUP, path)
    return path


def load_setup(root: Path = DATA) -> Setup:
    return Setup.model_validate_json(setup_path(root).read_text())


def save_setup(setup: Setup, root: Path = DATA) -> None:
    setup_path(root).write_text(setup.model_dump_json(indent=2))
