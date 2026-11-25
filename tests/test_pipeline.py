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

        # Test SQLite WAL checkpoint endpoint used by daily backup scripts
        chk = client.post("/api/admin/checkpoint")
        assert chk.status_code == 200
        assert chk.json() == {"ok": True}

        # Test bankroll config update in BRL
        cfg = client.post(
            "/api/settings/bankroll",
            json={"bankroll_start": 2500.0, "lay_stake": 50.0},
        )
        assert cfg.status_code == 200
        assert cfg.json()["bankroll_start"] == 2500.0
        assert cfg.json()["lay_stake"] == 50.0


def test_upcoming_bet_locks_and_settles_with_true_result(tmp_path: Path, monkeypatch) -> None:
    from datetime import datetime, timedelta, timezone

    future_dt = datetime.now(timezone.utc) + timedelta(hours=2)
    future_date_uk = future_dt.strftime("%d/%m/%Y")
    future_time_str = future_dt.strftime("%H:%M:%S")

    db_file = tmp_path / "test_live.db"
    models_dir = tmp_path / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "db_path", db_file)
    monkeypatch.setattr(settings, "models_dir", models_dir)

    init_db(db_file)
    for idx, day in enumerate(["20/11/2026", "21/11/2026", "22/11/2026"], start=1):
        store_meeting_payload(db_file, _make_synthetic_meeting(600 + idx, day, n_races=6))

    # Add an upcoming race (no results yet!)
    upcoming_meeting = {
        "meetingId": 699,
        "meetingDate": future_date_uk,
        "trackName": "Nottingham",
        "races": [
            {
                "raceId": 69901,
                "raceTime": future_time_str,
                "raceDate": future_date_uk,
                "raceNumber": "1",
                "raceType": "Flat",
                "raceClass": "A1",
                "raceDistance": 480.0,
                "traps": [
                    {"trapNumber": str(t), "dogId": 1000 + t, "dogName": f"Hound_{t}", "trainerName": f"Trainer_{t}"}
                    for t in range(1, 7)
                ],
            }
        ],
    }
    store_meeting_payload(db_file, upcoming_meeting)

    samples, _ = build_chronological_dataset(db_file)
    bundle = train_and_evaluate(db_file, samples)
    probs = walk_forward_out_of_sample_probs(samples, model_name=bundle.model_name)
    res1 = persist_predictions_and_paper_bets(db_file, samples, probs, bundle.model_run_id)
    assert res1["upcoming_races"] == 1
    assert res1["pending_bets"] == 1

    # Now simulate that the true result arrives where the locked dog unexpectedly WON (pos=1)!
    from app.db import get_conn

    with get_conn(db_file) as conn:
        pending = conn.execute("SELECT trap_number, dog_name FROM paper_bets WHERE race_id = 69901").fetchone()
    locked_trap = int(pending["trap_number"])

    completed_traps = []
    for t in range(1, 7):
        pos = 1 if t == locked_trap else (2 if t == 1 else t)
        completed_traps.append(
            {
                "trapNumber": str(t),
                "dogId": 1000 + t,
                "dogName": f"Hound_{t}",
                "trainerName": f"Trainer_{t}",
                "SP": "4/1",
                "resultPriceNumerator": 4,
                "resultPriceDenominator": 1,
                "resultPosition": pos,
                "resultSectionalTime": "4.50",
                "resultRunTime": "29.00",
                "resultAdjustedTime": "29.00",
            }
        )
    upcoming_meeting["races"][0]["traps"] = completed_traps
    store_meeting_payload(db_file, upcoming_meeting)

    samples2, _ = build_chronological_dataset(db_file)
    probs2 = walk_forward_out_of_sample_probs(samples2, model_name=bundle.model_name)
    res2 = persist_predictions_and_paper_bets(db_file, samples2, probs2, bundle.model_run_id)
    assert res2["pending_bets"] == 0
    assert res2["live_settled_bets"] == 1

    with get_conn(db_file) as conn:
        settled = conn.execute("SELECT trap_number, status, placed_before_race, pnl FROM paper_bets WHERE race_id = 69901").fetchone()
    assert int(settled["trap_number"]) == locked_trap
    assert int(settled["placed_before_race"]) == 1
    assert settled["status"] == "LOST_LAY"
    assert float(settled["pnl"]) < 0.0


def test_live_bankroll_respects_available_balance(tmp_path: Path, monkeypatch) -> None:
    from datetime import datetime, timedelta, timezone
    from app.db import save_bankroll_config

    db_file = tmp_path / "test_cap.db"
    models_dir = tmp_path / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "db_path", db_file)
    monkeypatch.setattr(settings, "models_dir", models_dir)

    init_db(db_file)
    save_bankroll_config(db_file, bankroll_start=100.0, lay_stake=4.0)
    for idx, day in enumerate(["20/11/2026", "21/11/2026", "22/11/2026"], start=1):
        store_meeting_payload(db_file, _make_synthetic_meeting(700 + idx, day, n_races=6))

    # Create 12 upcoming races; with R$ 100 bankroll & 15% risk cap, not all 12 fit simultaneously
    future_dt = datetime.now(timezone.utc) + timedelta(hours=2)
    upcoming_meeting = {
        "meetingId": 799,
        "meetingDate": future_dt.strftime("%d/%m/%Y"),
        "trackName": "Nottingham",
        "races": [
            {
                "raceId": 79900 + r,
                "raceTime": (future_dt + timedelta(minutes=8 * r)).strftime("%H:%M:%S"),
                "raceDate": future_dt.strftime("%d/%m/%Y"),
                "raceNumber": str(r),
                "raceType": "Flat",
                "raceClass": "A1",
                "raceDistance": 480.0,
                "traps": [
                    {"trapNumber": str(t), "dogId": 1000 + t, "dogName": f"Hound_{t}", "trainerName": f"Trainer_{t}"}
                    for t in range(1, 7)
                ],
            }
            for r in range(1, 13)
        ],
    }
    store_meeting_payload(db_file, upcoming_meeting)

    samples, _ = build_chronological_dataset(db_file)
    bundle = train_and_evaluate(db_file, samples)
    probs = walk_forward_out_of_sample_probs(samples, model_name=bundle.model_name)
    res = persist_predictions_and_paper_bets(db_file, samples, probs, bundle.model_run_id)

    assert res["bankroll_start"] == 100.0
    assert res["virtual_bankroll"] == 100.0
    assert res["committed_liability"] <= 100.0
    assert res["available_balance"] >= 0.0
    assert 1 <= res["pending_bets"] < 12


def test_exchange_ticks_and_betfair_stream_parser() -> None:
    from app.betfair_hist import normalize_dog_name, parse_betfair_stream_lines
    from app.lay_engine import compute_portfolio_metrics, snap_to_exchange_tick

    assert snap_to_exchange_tick(2.03) == 2.04
    assert snap_to_exchange_tick(4.12) == 4.2
    assert snap_to_exchange_tick(6.35, extra_ticks=1) == 6.6

    metrics = compute_portfolio_metrics([1.9, 1.9, -8.0, 1.9], 100.0)
    assert metrics["max_drawdown_brl"] == 8.0
    assert metrics["max_win_streak"] == 2
    assert metrics["max_loss_streak"] == 1

    assert normalize_dog_name("4. Lisnakill Donnie") == "lisnakilldonnie"
    sample_lines = [
        '{"op":"mcm","mc":[{"id":"1.2345","marketDefinition":{"eventTypeId":"4339","marketType":"WIN","venue":"Nottingham","marketTime":"2026-11-23T18:37:00.000Z","inPlay":false,"runners":[{"id":101,"name":"4. Lisnakill Donnie"},{"id":102,"name":"1. Blastoff Tony"}]},"rc":[{"id":101,"ltp":5.5},{"id":102,"batl":[[0,4.2,50]]}]}]}'
    ]
    parsed = parse_betfair_stream_lines(sample_lines)
    assert len(parsed) == 2
    by_dog = {x["dog_norm"]: x["lay_price"] for x in parsed}
    assert by_dog["lisnakilldonnie"] == 5.6  # 5.5 + 1 tick (0.1)
    assert by_dog["blastofftony"] == 4.2


def test_adaptive_context_tracker_blocks_losing_context() -> None:
    import numpy as np
    from app.adaptive_learner import AdaptiveContextTracker
    from app.features import FEATURE_NAMES, RunnerSample

    tracker = AdaptiveContextTracker(min_samples=25, min_roi_pct=-10.0)
    sample = RunnerSample(
        race_id=1,
        race_dt="2026-11-25 18:00:00",
        iso_date="2026-11-25",
        track_name="BadTrack",
        race_number=1,
        trap_number=4,
        dog_id=999,
        dog_name="TestDog",
        decimal_odds=5.0,
        result_position=1,
        is_winner=1,
        features=np.zeros(len(FEATURE_NAMES)),
        summary={},
        race_distance=280.0,
        race_class="D2",
    )

    # Record 25 losing outcomes at BadTrack
    for _ in range(25):
        tracker.record_outcome(
            sample=sample,
            lay_odds=5.0,
            win_prob=0.15,
            liability=8.0,
            pnl=-8.0,
            is_lay_win=False,
        )

    allowed, reason = tracker.check_context_allowed(sample, 5.0)
    assert allowed is False
    assert reason is not None and "BadTrack" in reason
    summary = tracker.export_summary(100.0)
    assert summary["blocked_contexts_count"] >= 1
    assert summary["defensive_mode"] is True
