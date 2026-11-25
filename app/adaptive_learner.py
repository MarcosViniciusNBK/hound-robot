from __future__ import annotations

import json
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from app.config import settings
from app.db import get_conn
from app.features import RunnerSample


def _dist_band(distance: float | None) -> str:
    if not distance or distance <= 0:
        return "Distância: Padrão"
    if distance < 350:
        return "Distância: Sprint (<350m)"
    if distance < 550:
        return "Distância: Padrão (350-549m)"
    return "Distância: Stayers (550m+)"


def _class_band(race_class: str | None) -> str:
    c = (race_class or "").strip().upper()
    if not c:
        return "Classe: Outras"
    if c.startswith("OR"):
        return "Classe: Open (OR)"
    if c.startswith("A") and c[1:2].isdigit():
        num = int(c[1:2])
        return "Classe: A1-A4" if num <= 4 else "Classe: A5-A10"
    if c.startswith("D"):
        return "Classe: Sprint (D)"
    if c.startswith("HP") or "HANDICAP" in c:
        return "Classe: Handicap"
    return f"Classe: {c[:4]}"


def extract_context_keys(sample: RunnerSample, lay_odds: float | None = None) -> list[str]:
    """Monitor track-specific performance without blocking global race classes."""
    return [
        f"Pista: {sample.track_name}",
        f"{sample.track_name} | {_dist_band(sample.race_distance).replace('Distância: ', '')}",
    ]


class AdaptiveContextTracker:
    """
    Leak-free adaptive learner that monitors out-of-sample ROI across track
    contexts using a rolling window + cooldown recovery.
    """

    def __init__(
        self,
        min_samples: int = 45,
        min_roi_pct: float = -12.0,
        window_size: int = 75,
        cooldown_skips: int = 20,
    ) -> None:
        self.min_samples = min_samples
        self.min_roi_pct = min_roi_pct
        self.window_size = window_size
        self.cooldown_skips = cooldown_skips
        self.history: dict[str, deque[dict[str, float]]] = defaultdict(
            lambda: deque(maxlen=self.window_size)
        )
        self.skips_since_block: dict[str, int] = defaultdict(int)
        self.recent_pnls: deque[float] = deque(maxlen=30)
        self.equity = 0.0
        self.peak_equity = 0.0
        self.blocked_hits = 0

    def is_drawdown_defensive(self, bankroll_ref: float) -> bool:
        dd = self.peak_equity - self.equity
        dd_pct = (dd / max(bankroll_ref, 10.0)) if bankroll_ref > 0 else 0.0
        recent_sum = sum(self.recent_pnls) if len(self.recent_pnls) >= 15 else 0.0
        return dd_pct >= 0.12 or (len(self.recent_pnls) >= 15 and recent_sum < -18.0)

    def current_thresholds(self, bankroll_ref: float) -> tuple[float, float, bool]:
        """Return (min_ev_per_unit, max_risk_pct, defensive_mode)."""
        defensive = self.is_drawdown_defensive(bankroll_ref)
        if defensive:
            return (
                max(settings.min_ev_per_unit, 0.03),
                min(settings.max_risk_pct_bankroll, 0.10),
                True,
            )
        return settings.min_ev_per_unit, settings.max_risk_pct_bankroll, False

    def _summarize_key(self, key: str) -> dict[str, float]:
        entries = self.history.get(key)
        if not entries:
            return {"bets": 0, "wins": 0, "pnl": 0.0, "risked": 0.0, "roi_pct": 0.0, "pred_win": 0.0, "actual_win": 0.0}
        bets = len(entries)
        wins = sum(e["win"] for e in entries)
        pnl = sum(e["pnl"] for e in entries)
        risked = sum(e["risked"] for e in entries)
        roi_pct = (pnl / risked) * 100.0 if risked > 0 else 0.0
        pred_win = sum(e["pred_win"] for e in entries) / bets
        actual_win = sum(e["actual_win"] for e in entries) / bets
        return {
            "bets": float(bets),
            "wins": float(wins),
            "pnl": pnl,
            "risked": risked,
            "roi_pct": roi_pct,
            "pred_win": pred_win,
            "actual_win": actual_win,
        }

    def check_context_allowed(
        self, sample: RunnerSample, lay_odds: float | None = None
    ) -> tuple[bool, str | None]:
        """Check whether any granular context of this race is currently blocked by negative ROI."""
        for key in extract_context_keys(sample, lay_odds):
            st = self._summarize_key(key)
            if st["bets"] < self.min_samples:
                continue
            if st["roi_pct"] < self.min_roi_pct:
                # Allow 1 probe entry after cooldown_skips so recovering contexts can prove themselves
                if self.skips_since_block[key] >= self.cooldown_skips:
                    self.skips_since_block[key] = 0
                    continue
                self.skips_since_block[key] += 1
                self.blocked_hits += 1
                return (
                    False,
                    f"Autoaprendizado bloqueou ({key}: ROI {st['roi_pct']:.1f}% em {int(st['bets'])} entradas)",
                )
        return True, None

    def record_outcome(
        self,
        sample: RunnerSample,
        lay_odds: float,
        win_prob: float,
        liability: float,
        pnl: float,
        is_lay_win: bool,
    ) -> None:
        self.recent_pnls.append(pnl)
        self.equity += pnl
        if self.equity > self.peak_equity:
            self.peak_equity = self.equity

        item = {
            "win": 1.0 if is_lay_win else 0.0,
            "pnl": float(pnl),
            "risked": float(liability),
            "pred_win": float(win_prob),
            "actual_win": float(sample.is_winner),
        }
        for key in extract_context_keys(sample, lay_odds):
            self.history[key].append(item)

    def export_summary(self, bankroll_ref: float) -> dict[str, Any]:
        min_ev, max_risk, defensive = self.current_thresholds(bankroll_ref)
        contexts: list[dict[str, Any]] = []
        for key in list(self.history.keys()):
            st = self._summarize_key(key)
            bets = int(st["bets"])
            if bets < 10:
                continue
            is_blocked = bets >= self.min_samples and st["roi_pct"] < self.min_roi_pct
            contexts.append(
                {
                    "context": key,
                    "bets": bets,
                    "win_pct": round((st["wins"] / bets) * 100.0, 1),
                    "roi_pct": round(st["roi_pct"], 2),
                    "pnl": round(st["pnl"], 2),
                    "pred_win_pct": round(st["pred_win"] * 100.0, 1),
                    "actual_win_pct": round(st["actual_win"] * 100.0, 1),
                    "status": "BLOQUEADO" if is_blocked else "ATIVO",
                }
            )

        contexts.sort(key=lambda c: (0 if c["status"] == "BLOQUEADO" else 1, c["roi_pct"]))
        blocked_count = sum(1 for c in contexts if c["status"] == "BLOQUEADO")
        return {
            "defensive_mode": defensive,
            "active_min_ev": round(min_ev, 3),
            "active_max_risk_pct": round(max_risk * 100.0, 1),
            "blocked_contexts_count": blocked_count,
            "races_protected_count": self.blocked_hits,
            "contexts": contexts[:30],
        }


def load_past_error_race_ids(db_path: Path) -> set[int]:
    """Return race_ids where the robot previously had a LOST_LAY mistake."""
    with get_conn(db_path) as conn:
        rows_live = conn.execute(
            "SELECT race_id FROM paper_bets WHERE status = 'LOST_LAY'"
        ).fetchall()
        rows_bt = conn.execute(
            "SELECT race_id FROM backtest_bets WHERE status = 'LOST_LAY'"
        ).fetchall()
    return {int(r["race_id"]) for r in rows_live} | {int(r["race_id"]) for r in rows_bt}


def save_adaptive_summary(db_path: Path, summary: dict[str, Any]) -> None:
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES ('adaptive_learning', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (json.dumps(summary, ensure_ascii=False),),
        )
