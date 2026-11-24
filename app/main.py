from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import BASE_DIR, settings
from app.db import get_conn, init_db
from app.features import build_chronological_dataset
from app.lay_engine import compute_lay_ev, explain_runner, select_lay_candidate
from app.model import load_latest_model
from app.study_worker import coordinator


STATIC_DIR = BASE_DIR / "static"


class StudyTriggerRequest(BaseModel):
    days: int | None = Field(default=None, ge=1, le=30)
    target_date: str | None = None


class SimRunnerInput(BaseModel):
    trap_number: int = Field(ge=1, le=8)
    dog_name: str = Field(min_length=1)
    dog_id: int | None = None
    trainer_name: str | None = None
    decimal_odds: float | None = Field(default=None, gt=1.0)


class SimulateRaceRequest(BaseModel):
    track_name: str = "Nottingham"
    race_distance: float = Field(default=480.0, gt=100.0, le=1200.0)
    race_class: str = "A1"
    runners: list[SimRunnerInput] = Field(min_length=2, max_length=8)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(settings.db_path)
    coordinator.attach_loop(asyncio.get_running_loop())
    if os.getenv("HOUND_DISABLE_BG_LOOP") != "1":
        await coordinator.start_background_loop()
    try:
        yield
    finally:
        await coordinator.stop()


app = FastAPI(title="Hound Robot — Estudo & Lay Analytics", lifespan=lifespan)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data:; "
        "connect-src 'self' ws: wss:; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'"
    )
    return response


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def serve_index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/overview")
def get_overview(selected_date: str | None = Query(default=None, alias="date")) -> dict[str, Any]:
    today_iso = date.today().isoformat()
    with get_conn(settings.db_path) as conn:
        dates_rows = conn.execute(
            """
            SELECT iso_date, COUNT(*) AS race_count
            FROM races
            GROUP BY iso_date
            ORDER BY iso_date DESC
            LIMIT 30
            """
        ).fetchall()
        available_dates = [
            {"iso_date": r["iso_date"], "race_count": int(r["race_count"])}
            for r in dates_rows
        ]

        active_date = selected_date
        if not active_date:
            if any(d["iso_date"] == today_iso for d in available_dates):
                active_date = today_iso
            elif available_dates:
                active_date = available_dates[0]["iso_date"]
            else:
                active_date = today_iso

        totals = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM races) AS total_races,
                (SELECT COUNT(*) FROM races WHERE is_completed = 1) AS completed_races,
                (SELECT COUNT(DISTINCT dog_id) FROM entries WHERE dog_id IS NOT NULL) AS total_dogs,
                (SELECT COUNT(*) FROM entries) AS total_entries
            """
        ).fetchone()

        latest_model = conn.execute(
            "SELECT * FROM model_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        model_history_rows = conn.execute(
            "SELECT id, created_at, model_name, log_loss, brier_score, top1_accuracy, top3_accuracy, lay_accuracy, lay_roi_pct, val_races FROM model_runs ORDER BY id DESC LIMIT 15"
        ).fetchall()

        paper_stats = conn.execute(
            """
            SELECT
                COUNT(*) AS total_bets,
                SUM(CASE WHEN status = 'WON_LAY' THEN 1 ELSE 0 END) AS won_bets,
                SUM(CASE WHEN status = 'LOST_LAY' THEN 1 ELSE 0 END) AS lost_bets,
                COALESCE(SUM(CASE WHEN status = 'WON_LAY' THEN pnl ELSE 0.0 END), 0.0) AS total_won_amount,
                COALESCE(SUM(CASE WHEN status = 'LOST_LAY' THEN ABS(pnl) ELSE 0.0 END), 0.0) AS total_lost_amount,
                COALESCE(SUM(pnl), 0.0) AS total_pnl,
                COALESCE(SUM(liability), 0.0) AS total_risked
            FROM paper_bets
            WHERE status IN ('WON_LAY', 'LOST_LAY')
            """
        ).fetchone()

        bankroll_rows = conn.execute(
            """
            SELECT id, iso_date, track_name, race_number, dog_name, lay_odds, status, pnl, bankroll_after
            FROM paper_bets
            WHERE status IN ('WON_LAY', 'LOST_LAY')
            ORDER BY id ASC
            """
        ).fetchall()

        error_rows = conn.execute(
            """
            SELECT iso_date, track_name, race_number, trap_number, dog_name, win_prob, lose_prob, lay_odds, liability, pnl, error_reason
            FROM paper_bets
            WHERE status = 'LOST_LAY'
            ORDER BY id DESC
            LIMIT 25
            """
        ).fetchall()

        log_rows = conn.execute(
            "SELECT id, created_at, level, phase, message, meta_json FROM study_logs ORDER BY id DESC LIMIT 40"
        ).fetchall()

    total_bets = int(paper_stats["total_bets"] or 0)
    won_bets = int(paper_stats["won_bets"] or 0)
    lost_bets = int(paper_stats["lost_bets"] or 0)
    total_won_amount = round(float(paper_stats["total_won_amount"] or 0.0), 2)
    total_lost_amount = round(float(paper_stats["total_lost_amount"] or 0.0), 2)
    total_pnl = round(float(paper_stats["total_pnl"] or 0.0), 2)
    total_risked = float(paper_stats["total_risked"] or 0.0)
    current_bankroll = round(settings.virtual_bankroll_start + total_pnl, 2)

    latest_model_dict = None
    feature_importances: list[dict[str, Any]] = []
    if latest_model:
        latest_model_dict = dict(latest_model)
        try:
            feature_importances = json.loads(latest_model["feature_importances_json"] or "[]")
        except json.JSONDecodeError:
            feature_importances = []
        latest_model_dict.pop("feature_importances_json", None)

    logs = []
    for lr in reversed(log_rows):
        try:
            meta = json.loads(lr["meta_json"] or "{}")
        except json.JSONDecodeError:
            meta = {}
        logs.append(
            {
                "id": lr["id"],
                "created_at": lr["created_at"],
                "level": lr["level"],
                "phase": lr["phase"],
                "message": lr["message"],
                "meta": meta,
            }
        )

    return {
        "status": dict(coordinator.state),
        "active_date": active_date,
        "available_dates": available_dates,
        "totals": dict(totals) if totals else {},
        "latest_model": latest_model_dict,
        "feature_importances": feature_importances,
        "model_history": [dict(r) for r in reversed(model_history_rows)],
        "paper_summary": {
            "start_bankroll": settings.virtual_bankroll_start,
            "current_bankroll": current_bankroll,
            "total_pnl": total_pnl,
            "is_profit": total_pnl >= 0,
            "total_won_amount": total_won_amount,
            "total_lost_amount": total_lost_amount,
            "roi_pct": round((total_pnl / total_risked) * 100.0, 2) if total_risked > 0 else 0.0,
            "total_bets": total_bets,
            "won_bets": won_bets,
            "lost_bets": lost_bets,
            "win_rate_pct": round((won_bets / total_bets) * 100.0, 2) if total_bets > 0 else 0.0,
        },
        "bankroll_curve": [dict(r) for r in bankroll_rows[-120:]],
        "error_audit": [dict(r) for r in error_rows],
        "logs": logs,
    }


@app.get("/api/races")
def get_races(
    selected_date: str | None = Query(default=None, alias="date"),
    track: str | None = Query(default=None),
) -> dict[str, Any]:
    with get_conn(settings.db_path) as conn:
        if not selected_date:
            today_iso = date.today().isoformat()
            has_today = conn.execute(
                "SELECT 1 FROM races WHERE iso_date = ? LIMIT 1", (today_iso,)
            ).fetchone()
            if has_today:
                selected_date = today_iso
            else:
                row = conn.execute("SELECT MAX(iso_date) AS d FROM races").fetchone()
                selected_date = row["d"] if row and row["d"] else today_iso

        track_rows = conn.execute(
            "SELECT DISTINCT track_name FROM races WHERE iso_date = ? ORDER BY track_name ASC",
            (selected_date,),
        ).fetchall()
        tracks = [r["track_name"] for r in track_rows]

        params: list[Any] = [selected_date]
        query = """
            SELECT * FROM races
            WHERE iso_date = ?
        """
        if track and track != "ALL":
            query += " AND track_name = ?"
            params.append(track)
        query += " ORDER BY race_time ASC, track_name ASC, race_number ASC"
        race_rows = conn.execute(query, params).fetchall()

        race_ids = [int(r["race_id"]) for r in race_rows]
        if not race_ids:
            return {"date": selected_date, "tracks": tracks, "races": [], "day_summary": {}}

        placeholders = ",".join("?" for _ in race_ids)
        runner_rows = conn.execute(
            f"""
            SELECT
                e.*,
                p.win_prob, p.lose_prob, p.fair_odds, p.predicted_rank,
                p.is_lay_pick, p.lay_score, p.lay_ev, p.reasons_json
            FROM entries e
            LEFT JOIN predictions p
                ON p.race_id = e.race_id AND p.trap_number = e.trap_number
            WHERE e.race_id IN ({placeholders})
            ORDER BY e.race_id ASC, COALESCE(p.predicted_rank, e.trap_number) ASC, e.trap_number ASC
            """,
            race_ids,
        ).fetchall()

        paper_rows = conn.execute(
            f"""
            SELECT race_id, status, pnl, liability, lay_odds, stake, error_reason
            FROM paper_bets
            WHERE race_id IN ({placeholders})
            """,
            race_ids,
        ).fetchall()

    paper_by_race: dict[int, dict[str, Any]] = {
        int(pr["race_id"]): dict(pr) for pr in paper_rows
    }

    runners_by_race: dict[int, list[dict[str, Any]]] = {rid: [] for rid in race_ids}
    for rr in runner_rows:
        d = dict(rr)
        try:
            payload = json.loads(d.pop("reasons_json") or "{}")
        except json.JSONDecodeError:
            payload = {}
        d["reasons"] = payload.get("reasons", [])
        d["stats"] = payload.get("stats", {})
        runners_by_race[int(d["race_id"])].append(d)

    races_out: list[dict[str, Any]] = []
    day_completed = 0
    day_top1_hits = 0
    day_lay_hits = 0
    day_lay_total = 0
    day_won_amount = 0.0
    day_lost_amount = 0.0
    day_net_pnl = 0.0

    for r in race_rows:
        rd = dict(r)
        rid = int(rd["race_id"])
        runners = runners_by_race.get(rid, [])
        # Sort by predicted_rank if predictions exist
        if any(x.get("predicted_rank") is not None for x in runners):
            ordered = sorted(runners, key=lambda x: x.get("predicted_rank") or 99)
        else:
            ordered = sorted(runners, key=lambda x: x["trap_number"])

        probable_winner = ordered[0] if ordered else None
        podium = ordered[:3]
        lay_pick = next((x for x in ordered if x.get("is_lay_pick") == 1), ordered[-1] if ordered else None)
        actual_winner = next((x for x in runners if x.get("result_position") == 1), None)

        lay_outcome = "PENDENTE"
        top1_hit = None
        if rd["is_completed"] and actual_winner:
            day_completed += 1
            if probable_winner and probable_winner.get("result_position") == 1:
                day_top1_hits += 1
                top1_hit = True
            else:
                top1_hit = False

            if lay_pick:
                day_lay_total += 1
                if lay_pick.get("result_position") != 1:
                    lay_outcome = "ACERTOU_LAY"
                    day_lay_hits += 1
                else:
                    lay_outcome = "ERROU_LAY"

        pb = paper_by_race.get(rid)
        if pb:
            pnl = float(pb.get("pnl") or 0.0)
            if pb.get("status") == "WON_LAY":
                day_won_amount += pnl
                day_net_pnl += pnl
            elif pb.get("status") == "LOST_LAY":
                day_lost_amount += abs(pnl)
                day_net_pnl += pnl

        rd["runners"] = ordered
        rd["probable_winner"] = probable_winner
        rd["probable_podium"] = podium
        rd["lay_pick"] = lay_pick
        rd["actual_winner"] = actual_winner
        rd["lay_outcome"] = lay_outcome
        rd["top1_hit"] = top1_hit
        rd["paper_bet"] = pb
        races_out.append(rd)

    return {
        "date": selected_date,
        "tracks": tracks,
        "races": races_out,
        "day_summary": {
            "total_races": len(races_out),
            "completed_races": day_completed,
            "top1_hits": day_top1_hits,
            "top1_pct": round((day_top1_hits / day_completed) * 100.0, 1) if day_completed else 0.0,
            "lay_hits": day_lay_hits,
            "lay_total": day_lay_total,
            "lay_pct": round((day_lay_hits / day_lay_total) * 100.0, 1) if day_lay_total else 0.0,
            "day_won_amount": round(day_won_amount, 2),
            "day_lost_amount": round(day_lost_amount, 2),
            "day_net_pnl": round(day_net_pnl, 2),
        },
    }


@app.post("/api/study/run")
async def trigger_study(req: StudyTriggerRequest | None = None) -> dict[str, Any]:
    if coordinator.state.get("running"):
        return {"started": False, "message": "Ciclo de estudo já está em execução."}
    days = req.days if req else None
    target_date = req.target_date if req else None
    asyncio.create_task(coordinator.run_study_cycle(days=days, target_date=target_date))
    return {"started": True, "message": "Novo ciclo de estudo iniciado em tempo real."}


@app.get("/api/dogs/search")
def search_dogs(q: str = Query(default="", min_length=2)) -> list[dict[str, Any]]:
    like = f"%{q.strip()}%"
    with get_conn(settings.db_path) as conn:
        rows = conn.execute(
            """
            SELECT dog_id, dog_name, MAX(trainer_name) AS trainer_name, COUNT(*) AS runs
            FROM entries
            WHERE dog_id IS NOT NULL AND dog_name LIKE ?
            GROUP BY dog_id, dog_name
            ORDER BY runs DESC, dog_name ASC
            LIMIT 15
            """,
            (like,),
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/simulate-race")
def simulate_race(req: SimulateRaceRequest) -> dict[str, Any]:
    model_bundle = load_latest_model()
    _, state = build_chronological_dataset(settings.db_path)

    future_dt = "2099-12-31 23:59:59"
    race_meta = {
        "race_id": 999999999,
        "race_dt": future_dt,
        "iso_date": date.today().isoformat(),
        "track_name": req.track_name,
        "race_number": 1,
        "race_distance": req.race_distance,
        "race_class": req.race_class,
    }
    runners_payload = [
        {
            "trap_number": r.trap_number,
            "dog_id": r.dog_id,
            "dog_name": r.dog_name,
            "trainer_name": r.trainer_name,
            "decimal_odds": r.decimal_odds,
            "result_position": None,
        }
        for r in req.runners
    ]
    samples = state.extract_race_features(race_meta, runners_payload)
    X = np.vstack([s.features for s in samples])

    if model_bundle is not None:
        raw_p = model_bundle["estimator"].predict_proba(X)[:, 1]
        model_name = model_bundle["model_name"]
    else:
        raw_p = np.ones(len(samples), dtype=np.float64) / len(samples)
        model_name = "PriorUniforme"

    raw_p = np.clip(raw_p, 1e-4, 0.999)
    probs = raw_p / float(np.sum(raw_p))

    paired = sorted(zip(samples, probs), key=lambda x: float(x[1]), reverse=True)
    lay_sample, _ = select_lay_candidate([(s, float(p)) for s, p in paired])

    results = []
    for rank, (s, p) in enumerate(paired, start=1):
        wp = float(p)
        lp = 1.0 - wp
        is_lay = s.trap_number == lay_sample.trap_number
        results.append(
            {
                "predicted_rank": rank,
                "trap_number": s.trap_number,
                "dog_name": s.dog_name,
                "dog_id": s.dog_id,
                "win_prob_pct": round(wp * 100.0, 2),
                "lose_prob_pct": round(lp * 100.0, 2),
                "fair_odds": round(1.0 / max(wp, 0.005), 2),
                "market_odds": s.decimal_odds,
                "lay_ev": compute_lay_ev(wp, s.decimal_odds),
                "is_lay_pick": is_lay,
                "reasons": explain_runner(s, wp, is_lay),
                "stats": s.summary,
            }
        )

    lay_pick_result = next((r for r in results if r["is_lay_pick"]), results[-1])

    return {
        "model_name": model_name,
        "track_name": req.track_name,
        "race_distance": req.race_distance,
        "race_class": req.race_class,
        "probable_winner": results[0],
        "lay_pick": lay_pick_result,
        "runners": results,
    }


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await coordinator.ws_manager.connect(websocket)
    try:
        with get_conn(settings.db_path) as conn:
            log_rows = conn.execute(
                "SELECT id, created_at, level, phase, message, meta_json FROM study_logs ORDER BY id DESC LIMIT 25"
            ).fetchall()
        logs = []
        for lr in reversed(log_rows):
            try:
                meta = json.loads(lr["meta_json"] or "{}")
            except json.JSONDecodeError:
                meta = {}
            logs.append(
                {
                    "id": lr["id"],
                    "created_at": lr["created_at"],
                    "level": lr["level"],
                    "phase": lr["phase"],
                    "message": lr["message"],
                    "meta": meta,
                }
            )
        await websocket.send_json(
            {
                "type": "snapshot",
                "status": dict(coordinator.state),
                "logs": logs,
            }
        )
        while True:
            # Keep connection alive; ignore client pings
            await websocket.receive_text()
    except WebSocketDisconnect:
        coordinator.ws_manager.disconnect(websocket)
    except Exception:
        coordinator.ws_manager.disconnect(websocket)
