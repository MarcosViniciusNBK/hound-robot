from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
MODELS_DIR = DATA_DIR / "models"
BETFAIR_HIST_DIR = DATA_DIR / "betfair_hist"
DB_PATH = DATA_DIR / "hound_robot.db"


@dataclass
class Settings:
    db_path: Path = DB_PATH
    models_dir: Path = MODELS_DIR
    betfair_hist_dir: Path = BETFAIR_HIST_DIR
    gbgb_base_url: str = "https://api.gbgb.org.uk/api"
    user_agent: str = "HoundRobotResearch/1.0 (+local-analytics-dashboard)"
    request_delay_sec: float = 0.25
    history_days_default: int = 30
    study_interval_sec: int = 60
    virtual_bankroll_start: float = 100.0
    virtual_lay_stake: float = 2.0
    exchange_commission: float = 0.05
    lay_spread_pct: float = 0.05
    lay_slippage_ticks: int = 1
    max_lay_odds: float = 12.0
    max_risk_pct_bankroll: float = 0.15
    min_prior_runs_for_bet: int = 2
    min_ev_per_unit: float = 0.01
    host: str = os.getenv("HOUND_HOST", "127.0.0.1")
    port: int = int(os.getenv("HOUND_PORT", "8000"))


settings = Settings()
settings.models_dir.mkdir(parents=True, exist_ok=True)
settings.betfair_hist_dir.mkdir(parents=True, exist_ok=True)
