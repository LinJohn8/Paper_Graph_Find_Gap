from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
load_dotenv(ROOT / '.env', override=False)
CONFIG_PATH = Path(os.environ.get("PAPER_CONFIG", ROOT / "venues.yaml"))
DATA_ROOT = Path(os.environ.get("PAPER_DATA_DIR", REPO_ROOT / "Data")).resolve()
DB_PATH = Path(os.environ.get("PAPER_DB_PATH", DATA_ROOT / "database" / "papers.sqlite")).resolve()


def venues() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)["venues"]
