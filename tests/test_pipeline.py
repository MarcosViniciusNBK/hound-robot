from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import settings
from app.db import init_db
from app.features import build_chronological_dataset
from app.gbgb_client import parse_sp_to_decimal, store_meeting_payload
from app.lay_engine import compute_lay_ev, persist_predictions_and_paper_bets
from app.model import train_and_evaluate, walk_forward_out_of_sample_probs


def _make_synthetic_meeting(meeting_id: int, date_uk: str, n_races: int = 10) -> dict:
    races = []
    for r in range(1, n_races + 1):
        traps = []
        # Trap 1 is consistently fast, Trap 6 is consistently slow
        for trap in range(1, 7):
            dog_id = 1000 + trap
            is_win = 1 if trap == 1 else trap
            adj_time = 28.5 + trap * 0.28 + (r % 3) * 0.03
            sec_time = 4.30 + trap * 0.08
            traps.append(
                {
                    "trapNumber": str(trap),
                    "dogId": dog_id,
                    "dogName": f"Hound_{trap}",
                    "trainerName": f"Trainer_{trap}",
                    "SP": f"{trap + 1}/1",
                    "resultPriceNumerator": trap + 1,
                    "resultPriceDenominator": 1,
                    "resultPosition": is_win,
                    "resultSectionalTime": f"{sec_time:.2f}",
                    "resultRunTime": f"{adj_time:.2f}",
                    "resultAdjustedTime": f"{adj_time:.2f}",
                    "resultDogWeight": "31.5",
                    "resultComment": "QAw,ALd" if trap == 1 else "Bmp,SAw" if trap == 6 else "ClrRun",
                }
            )
        races.append(
            {
                "raceId": meeting_id * 100 + r,
                "raceTime": f"{18 + (r // 4):02d}:{(r * 5) % 60:02d}:00",
                "raceDate": date_uk,
                "raceNumber": str(r),
                "raceTitle": f"Test Stakes {r}",
                "raceType": "Flat",
                "raceClass": "A1",
                "raceDistance": 480.0,
                "raceGoing": "0",
                "traps": traps,
            }
        )
    return {
        "meetingId": meeting_id,
        "meetingDate": date_uk,
        "trackName": "Nottingham",
        "races": races,
    }


def test_parse_sp() -> None:
    assert parse_sp_to_decimal("5/2") == 3.5
    assert parse_sp_to_decimal("EVS") == 2.0
    assert parse_sp_to_decimal("6/4F") == 2.5
    assert parse_sp_to_decimal("100/30") == 4.333


def test_lay_ev_math() -> None:
    # 90% chance to lose at odds 5.0 -> 0.9*20*0.98 - 0.1*80 = 17.64 - 8.0 = +9.64
    ev = compute_lay_ev(win_prob=0.10, lay_odds=5.0, stake=20.0, commission=0.02)
    assert ev == 9.64


def test_end_to_end_pipeline_and_api(tmp_path: Path, monkeypatch) -> None:
    db_file = tmp_path / "test_hound.db"
    models_dir = tmp_path / "models"
    models_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(settings, "db_path", db_file)
    monkeypatch.setattr(settings, "models_dir", models_dir)
    os.environ["HOUND_DISABLE_BG_LOOP"] = "1"

    init_db(db_file)
    for idx, day in enumerate(["18/11/2026", "19/11/2026", "20/11/2026", "21/11/2026"], start=1):
        store_meeting_payload(db_file, _make_synthetic_meeting(500 + idx, day, n_races=8))

    samples, _ = build_chronological_dataset(db_file)
    assert len(samples) == 4 * 8 * 6

    # First race must have zero prior runs (no leakage!)
    first_race_samples = [s for s in samples if s.race_id == 50101]
    assert all(s.summary["prior_runs"] == 0 for s in first_race_samples)

    bundle = train_and_evaluate(db_file, samples)
    assert bundle.metrics["log_loss"] > 0.0

    oos_probs = walk_forward_out_of_sample_probs(samples, model_name=bundle.model_name)
    # Check each race probabilities sum to 1.0
    last_race_probs = [p for s, p in zip(samples, oos_probs) if s.race_id == 50408]
    assert abs(sum(last_race_probs) - 1.0) < 1e-4

    summary = persist_predictions_and_paper_bets(db_file, samples, oos_probs, bundle.model_run_id)
    assert summary["races_predicted"] == 32
    assert summary["lay_accuracy_pct"] >= 80.0

    from app.main import app

    with TestClient(app) as client:
        idx = client.get("/")
        assert idx.status_code == 200
        assert idx.headers.get("x-content-type-options") == "nosniff"
        assert idx.headers.get("x-frame-options") == "DENY"
        assert "default-src 'self'" in (idx.headers.get("content-security-policy") or "")

        ov = client.get("/api/overview?date=2026-11-21")
        assert ov.status_code == 200
        ov_json = ov.json()
        assert ov_json["totals"]["total_races"] == 32
        assert "total_won_amount" in ov_json["paper_summary"]
        assert "total_lost_amount" in ov_json["paper_summary"]
        assert "is_profit" in ov_json["paper_summary"]

        rc = client.get("/api/races?date=2026-11-21")
        assert rc.status_code == 200
        races = rc.json()["races"]
        assert len(races) == 8
        assert races[0]["probable_winner"]["dog_name"] == "Hound_1"
        # Rule: Lay pick must NOT be 1st (rank 1) and must NOT be last (rank 6)
        assert 1 < races[0]["lay_pick"]["predicted_rank"] < 6
        assert races[0]["lay_pick"]["dog_name"] not in {"Hound_1", "Hound_6"}
        assert races[0]["lay_outcome"] == "ACERTOU_LAY"

        sim = client.post(
            "/api/simulate-race",
            json={
                "track_name": "Nottingham",
                "race_distance": 480,
                "race_class": "A1",
                "runners": [
                    {"trap_number": 1, "dog_name": "Hound_1", "dog_id": 1001, "decimal_odds": 2.2},
                    {"trap_number": 2, "dog_name": "Hound_2", "dog_id": 1002, "decimal_odds": 3.5},
                    {"trap_number": 4, "dog_name": "Hound_4", "dog_id": 1004, "decimal_odds": 6.0},
                    {"trap_number": 6, "dog_name": "Hound_6", "dog_id": 1006, "decimal_odds": 12.0},
                ],
            },
        )
        assert sim.status_code == 200
        sim_json = sim.json()
        assert sim_json["probable_winner"]["dog_name"] == "Hound_1"
        assert 1 < sim_json["lay_pick"]["predicted_rank"] < 4
