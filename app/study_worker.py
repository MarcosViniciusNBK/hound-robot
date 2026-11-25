from __future__ import annotations

import asyncio
import sys
from datetime import date, timedelta
from typing import Any

from fastapi import WebSocket

from app.betfair_hist import import_betfair_directory
from app.config import settings
from app.db import get_conn, log_study_event
from app.features import build_chronological_dataset
from app.gbgb_client import (
    GBGBClient,
    enrich_dogs_for_date,
    ingest_date_from_gbgb,
    utc_now_iso,
)
from app.lay_engine import persist_predictions_and_paper_bets
from app.model import compute_calibration_buckets, train_and_evaluate, walk_forward_out_of_sample_probs


class ConnectionManager:
    """Broadcasts live bot study events to connected dashboard clients."""

    def __init__(self) -> None:
        self.active: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self.active.discard(ws)

    async def broadcast(self, event: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for ws in list(self.active):
            try:
                await ws.send_json(event)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.active.discard(ws)


class StudyCoordinator:
    """Runs background GBGB ingestion, dog enrichment, ML training, and Lay scoring."""

    def __init__(self) -> None:
        self.ws_manager = ConnectionManager()
        self.loop: asyncio.AbstractEventLoop | None = None
        self._lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._bg_task: asyncio.Task[None] | None = None
        self.state: dict[str, Any] = {
            "running": False,
            "phase": "idle",
            "progress": 0,
            "message": "Aguardando início do estudo",
            "last_run_at": None,
            "cycle_count": 0,
            "last_summary": {},
        }

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def _emit_sync(self, event: dict[str, Any]) -> None:
        if self.loop is None or self.loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(self.ws_manager.broadcast(event), self.loop)

    def _update_status_sync(self, phase: str, progress: int, message: str, running: bool = True) -> None:
        self.state.update(
            {
                "running": running,
                "phase": phase,
                "progress": progress,
                "message": message,
            }
        )
        self._emit_sync({"type": "status", "status": dict(self.state)})

    def _log_sync(self, level: str, phase: str, message: str, meta: dict[str, Any] | None = None) -> None:
        try:
            entry = log_study_event(
                settings.db_path,
                utc_now_iso(),
                level,
                phase,
                message,
                meta=meta,
            )
            self._emit_sync({"type": "log", "log": entry})
        except Exception as exc:
            print(f"[study_worker] failed to persist study log ({phase}): {exc}", file=sys.stderr, flush=True)

    def _run_cycle_blocking(self, days: int, target_date: str | None) -> dict[str, Any]:
        client = GBGBClient()
        today = date.today()
        focus_date = target_date or today.isoformat()

        self._update_status_sync("coleta", 10, f"Conectando à API pública da GBGB ({focus_date})...")
        self._log_sync("INFO", "coleta", f"Iniciando ciclo de estudo para {focus_date} (janela {days} dias).")

        # Determine dates to sync (most recent days + target_date)
        date_list = [(today - timedelta(days=offset)).isoformat() for offset in range(days - 1, -1, -1)]
        if focus_date not in date_list:
            date_list.append(focus_date)

        total_races_new = 0
        total_entries_new = 0

        for idx, d_iso in enumerate(date_list):
            pct = 10 + int((idx / max(len(date_list), 1)) * 30)
            is_recent = d_iso >= (today - timedelta(days=1)).isoformat() or d_iso == focus_date
            self._update_status_sync("coleta", pct, f"Verificando corridas de {d_iso}...")
            try:
                res = ingest_date_from_gbgb(
                    settings.db_path,
                    client,
                    d_iso,
                    skip_existing_meetings=not is_recent,
                    progress_cb=lambda msg: self._log_sync("INFO", "coleta", msg),
                )
                total_races_new += res["races_saved"]
                total_entries_new += res["entries_saved"]
                if res["races_saved"] > 0:
                    self._log_sync(
                        "SUCCESS",
                        "coleta",
                        f"{d_iso}: {res['races_saved']} corridas e {res['entries_saved']} registros sincronizados.",
                    )
            except Exception as exc:
                self._log_sync("WARN", "coleta", f"Falha parcial ao buscar {d_iso}: {exc}")

        # Pick active date to enrich (focus_date if it has races, otherwise latest date in DB)
        with get_conn(settings.db_path) as conn:
            cnt_row = conn.execute(
                "SELECT COUNT(*) AS c FROM races WHERE iso_date = ?", (focus_date,)
            ).fetchone()
            if cnt_row and int(cnt_row["c"]) > 0:
                enrich_date = focus_date
            else:
                latest_row = conn.execute(
                    "SELECT MAX(iso_date) AS d FROM races"
                ).fetchone()
                enrich_date = (latest_row["d"] if latest_row and latest_row["d"] else focus_date)

        self._update_status_sync("enriquecimento", 45, f"Buscando histórico individual dos galgos ({enrich_date})...")
        try:
            bf_res = import_betfair_directory(settings.db_path, settings.betfair_hist_dir)
            if bf_res["entries_updated"] > 0:
                self._log_sync(
                    "SUCCESS",
                    "betfair_hist",
                    f"Betfair Historical Data: {bf_res['entries_updated']} odds reais de Exchange vinculadas.",
                )
        except Exception as exc:
            self._log_sync("WARN", "betfair_hist", f"Falha ao importar Betfair Historical: {exc}")

        try:
            enriched = enrich_dogs_for_date(
                settings.db_path,
                client,
                enrich_date,
                max_dogs=60,
                progress_cb=lambda msg: self._log_sync("INFO", "enriquecimento", msg),
            )
            if enriched > 0:
                self._log_sync(
                    "SUCCESS",
                    "enriquecimento",
                    f"Histórico individual atualizado para {enriched} galgos de {enrich_date}.",
                )
        except Exception as exc:
            self._log_sync("WARN", "enriquecimento", f"Enriquecimento parcial: {exc}")

        self._update_status_sync("features", 62, "Calculando indicadores sem vazamento temporal...")
        samples, _ = build_chronological_dataset(settings.db_path)
        self._log_sync(
            "INFO",
            "features",
            f"Base processada: {len(samples)} participações cronológicas com 20 indicadores cada.",
        )

        if len(samples) < 30:
            self._update_status_sync("idle", 100, "Aguardando mais corridas para treinar.", running=False)
            return {"status": "insufficient_data", "samples": len(samples)}

        self._update_status_sync("treino", 76, "Treinando modelo probabilístico e minimizando Log-Loss...")
        bundle = train_and_evaluate(settings.db_path, samples)
        self._log_sync(
            "SUCCESS",
            "treino",
            (
                f"Modelo {bundle.model_name} (#{bundle.model_run_id}) calibrado: "
                f"Log-Loss={bundle.metrics['log_loss']} | "
                f"Acerto Lay={bundle.metrics['lay_accuracy']}% | "
                f"ROI Val={bundle.metrics['lay_roi_pct']}%"
            ),
            meta=bundle.metrics,
        )

        self._update_status_sync("previsao", 90, "Gerando previsões honestas (walk-forward) e seleção Lay...")
        oos_probs = walk_forward_out_of_sample_probs(samples, model_name=bundle.model_name)
        summary = persist_predictions_and_paper_bets(
            settings.db_path,
            samples,
            oos_probs,
            model_run_id=bundle.model_run_id,
        )
        cal_buckets = compute_calibration_buckets(samples, oos_probs)
        with get_conn(settings.db_path) as conn:
            import json as _json

            conn.execute(
                "INSERT INTO app_settings (key, value) VALUES ('calibration_buckets', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (_json.dumps(cal_buckets),),
            )
        summary["model_name"] = bundle.model_name
        summary["model_run_id"] = bundle.model_run_id
        summary["log_loss"] = bundle.metrics["log_loss"]
        summary["brier_score"] = bundle.metrics["brier_score"]
        summary["active_date"] = enrich_date
        summary["races_synced_now"] = total_races_new
        summary["entries_synced_now"] = total_entries_new

        al = summary.get("adaptive_learning") or {}
        al_msg = (
            f" | Autoaprendizado: {al.get('blocked_contexts_count', 0)} perfis bloqueados, "
            f"{al.get('races_protected_count', 0)} corridas evitadas"
        )
        self._log_sync(
            "SUCCESS",
            "previsao",
            (
                f"Estudo concluído ({summary['races_predicted']} corridas, "
                f"{summary['upcoming_races']} futuras). "
                f"Lay acertou {summary['lay_accuracy_pct']}% | "
                f"Banca: R$ {summary['virtual_bankroll']:.2f}{al_msg}"
            ),
            meta=summary,
        )
        return summary

    async def run_study_cycle(self, days: int | None = None, target_date: str | None = None) -> dict[str, Any]:
        if self._lock.locked():
            return {"status": "already_running"}

        async with self._lock:
            use_days = days or settings.history_days_default
            try:
                summary = await asyncio.to_thread(self._run_cycle_blocking, use_days, target_date)
                self.state["cycle_count"] += 1
                self.state["last_run_at"] = utc_now_iso()
                self.state["last_summary"] = summary
                self._update_status_sync("idle", 100, "Estudo atualizado. Monitorando próximo ciclo.", running=False)
                await self.ws_manager.broadcast({"type": "study_complete", "summary": summary, "status": dict(self.state)})
                return summary
            except Exception as exc:
                self._log_sync("ERROR", "erro", f"Erro no ciclo de estudo: {exc}")
                self._update_status_sync("error", 0, f"Erro: {exc}", running=False)
                return {"status": "error", "detail": str(exc)}

    async def start_background_loop(self) -> None:
        self._stop_event.clear()

        async def _runner() -> None:
            # Immediate first study cycle on startup; guard loop so transient errors never kill scheduler
            while not self._stop_event.is_set():
                try:
                    await self.run_study_cycle()
                except Exception as exc:
                    print(f"[study_worker] unhandled cycle error: {exc}", file=sys.stderr, flush=True)
                    self._update_status_sync("error", 0, f"Erro inesperado: {exc}", running=False)
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=settings.study_interval_sec,
                    )
                except asyncio.TimeoutError:
                    continue

        self._bg_task = asyncio.create_task(_runner())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._bg_task:
            self._bg_task.cancel()
            try:
                await self._bg_task
            except asyncio.CancelledError:
                pass


coordinator = StudyCoordinator()
