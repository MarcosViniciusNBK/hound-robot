from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
MODELS_DIR = DATA_DIR / "models"
DB_PATH = DATA_DIR / "hound_robot.db"


@dataclass
class Settings:
    db_path: Path = DB_PATH
    models_dir: Path = MODELS_DIR
    gbgb_base_url: str = "https://api.gbgb.org.uk/api"
    user_agent: str = "HoundRobotResearch/1.0 (+local-analytics-dashboard)"
    request_delay_sec: float = 0.25
    history_days_default: int = 10
    study_interval_sec: int = 180
    virtual_bankroll_start: float = 1000.0
    virtual_lay_stake: float = 20.0
    exchange_commission: float = 0.02
    max_lay_odds: float = 12.0
    host: str = os.getenv("HOUND_HOST", "127.0.0.1")
    port: int = int(os.getenv("HOUND_PORT", "8000"))


settings = Settings()
settings.models_dir.mkdir(parents=True, exist_ok=True)
