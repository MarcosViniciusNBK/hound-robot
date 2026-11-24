from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Sequence, TypeVar

import numpy as np

from app.config import settings
from app.db import get_conn
from app.features import FEATURE_NAMES, RunnerSample
from app.gbgb_client import utc_now_iso


_IDX = {name: i for i, name in enumerate(FEATURE_NAMES)}
T = TypeVar("T")


def compute_lay_ev(
    win_prob: float,
    lay_odds: float | None,
    stake: float = settings.virtual_lay_stake,
    commission: float = settings.exchange_commission,
) -> float | None:
    if lay_odds is None or lay_odds <= 1.01:
        return None
    lose_prob = 1.0 - win_prob
    liability = stake * (lay_odds - 1.0)
    ev = lose_prob * stake * (1.0 - commission) - win_prob * liability
    return round(ev, 2)


def select_lay_candidate(
    ordered_desc: Sequence[tuple[RunnerSample, float]],
) -> tuple[RunnerSample, float]:
    """
    Select the greyhound to Lay (bet it will NOT win).

    Business rule: when a race has 3+ runners, never pick the 1st ranked
    (probable winner) nor the last ranked (extreme longshot with huge liability).
    Among ranks 2..(N-1), prefer runners from rank 3..(N-1) if N >= 4, selecting
    the candidate with best Lay EV (within max_lay_odds) or lowest win probability.
    """
    n = len(ordered_desc)
    if n == 0:
        raise ValueError("Cannot select Lay candidate from empty race.")
    if n <= 2:
        return ordered_desc[-1]

    # Exclude 1st (index 0) and last (index n - 1)
    middle_pool = list(ordered_desc[1:-1])
    if n >= 4:
        # Also skip 2nd favorite when enough runners exist so we focus on mid-to-low ranks
        preferred_pool = list(ordered_desc[2:-1])
    else:
        preferred_pool = middle_pool

    # Prefer candidates with known reasonable market odds and best Lay EV
    valid_odds_pool = [
        (s, p)
        for s, p in preferred_pool
        if s.decimal_odds is not None and 1.05 <= s.decimal_odds <= settings.max_lay_odds
    ]
    if valid_odds_pool:
        return max(
            valid_odds_pool,
            key=lambda item: (
                compute_lay_ev(item[1], item[0].decimal_odds) or -1e9,
                1.0 - item[1],
            ),
        )

    # Fallback without market odds: lowest win probability inside preferred_pool
    return min(preferred_pool, key=lambda item: item[1])


def explain_runner(sample: RunnerSample, win_prob: float, is_lay_pick: bool) -> list[str]:
    f = sample.features
    reasons: list[str] = []
    rel_speed = float(f[_IDX["rel_speed_field"]])
    rel_wr = float(f[_IDX["rel_win_rate_field"]])
    rel_sec = float(f[_IDX["rel_sectional_field"]])
    trouble = float(f[_IDX["trouble_rate"]])
    fast_break = float(f[_IDX["fast_break_rate"]])
    prior_runs = int(f[_IDX["prior_runs"]])
    ewm_pos = float(f[_IDX["ewm_pos"]])

    if is_lay_pick:
        if rel_speed < -0.04:
            reasons.append("Velocidade média abaixo dos favoritos do páreo")
        if rel_wr < -0.01:
            reasons.append("Aproveitamento inferior aos principais concorrentes")
        if rel_sec > 0.04:
            reasons.append("Split de largada mais lento que a média")
        if trouble >= 0.25:
            reasons.append(f"Histórico de incidentes/atrasos ({round(trouble * 100)}%)")
        if ewm_pos >= 3.5 and prior_runs >= 2:
            reasons.append(f"Forma recente irregular (média {ewm_pos:.1f}º)")
        if not reasons:
            reasons.append("Melhor relação risco/probabilidade de derrota fora das pontas")
    else:
        if rel_speed > 0.05:
            reasons.append("Ritmo/velocidade ajustada superior ao páreo")
        if rel_wr > 0.03:
            reasons.append("Taxa de vitórias acima da média dos rivais")
        if rel_sec < -0.04:
            reasons.append("Largada explosiva (split mais rápido)")
        if fast_break >= 0.35:
            reasons.append(f"Boa frequência de saída limpa ({round(fast_break * 100)}%)")
        if ewm_pos <= 2.4 and prior_runs >= 2:
            reasons.append(f"Regularidade no pódio (média {ewm_pos:.1f}º)")
        if not reasons:
            reasons.append("Equilíbrio positivo nos indicadores do páreo")

    return reasons[:3]


def diagnose_lay_error(sample: RunnerSample, win_prob: float) -> str:
    f = sample.features
    prior_runs = int(f[_IDX["prior_runs"]])
    rel_sec = float(f[_IDX["rel_sectional_field"]])
    odds = sample.decimal_odds

    notes: list[str] = []
    if prior_runs < 2:
        notes.append("Poucas corridas prévias na base para calibrar o galgo")
    if odds and odds >= 6.0:
        notes.append(f"Vitória inesperada (odd {odds:.2f})")
    if rel_sec <= 0.02:
        notes.append("Largou limpo e evitou tráfego")
    if not notes:
        notes.append("Variância natural / melhora súbita de performance")
    return "; ".join(notes)


def persist_predictions_and_paper_bets(
    db_path: Path,
    samples: list[RunnerSample],
    probs: np.ndarray,
    model_run_id: int | None,
) -> dict[str, Any]:
    """Save normalized race predictions and rebuild simulated Lay ledger."""
    by_race: dict[int, list[tuple[RunnerSample, float]]] = defaultdict(list)
    for s, p in zip(samples, probs):
        by_race[s.race_id].append((s, float(p)))

    now = utc_now_iso()
    pred_rows: list[tuple[Any, ...]] = []
    paper_rows: list[tuple[Any, ...]] = []

    bankroll = settings.virtual_bankroll_start
    stake = settings.virtual_lay_stake
    commission = settings.exchange_commission

    sorted_races = sorted(
        by_race.items(),
        key=lambda kv: (kv[1][0][0].race_dt, kv[0]),
    )

    lay_correct = 0
    lay_total = 0
    top1_correct = 0
    completed_races = 0
    total_won_amount = 0.0
    total_lost_amount = 0.0

    warmup_cutoff = min(15, len(sorted_races) // 4) if len(sorted_races) > 8 else 0

    for race_idx, (race_id, items) in enumerate(sorted_races):
        ordered = sorted(items, key=lambda x: x[1], reverse=True)
        lay_candidate, wp_lay = select_lay_candidate(ordered)

        has_winner = any(s.is_winner == 1 for s, _ in ordered)
        if has_winner:
            completed_races += 1
            if ordered[0][0].is_winner == 1:
                top1_correct += 1
            lay_total += 1
            if lay_candidate.is_winner == 0:
                lay_correct += 1

        for rank_idx, (s, wp) in enumerate(ordered, start=1):
            lp = max(0.001, min(0.999, 1.0 - wp))
            fair = round(1.0 / max(wp, 0.005), 2)
            is_lay = 1 if s.trap_number == lay_candidate.trap_number else 0
            ev = compute_lay_ev(wp, s.decimal_odds)
            reasons = explain_runner(s, wp, is_lay == 1)
            reasons_payload = {
                "reasons": reasons,
                "stats": s.summary,
            }
            pred_rows.append(
                (
                    s.race_id,
                    s.trap_number,
                    s.dog_id,
                    s.dog_name,
                    model_run_id,
                    round(wp, 5),
                    round(lp, 5),
                    fair,
                    rank_idx,
                    is_lay,
                    round(lp * 100.0, 2),
                    ev,
                    json.dumps(reasons_payload, ensure_ascii=False),
                    now,
                )
            )

        if race_idx >= warmup_cutoff:
            lp_lay = 1.0 - wp_lay
            odds = lay_candidate.decimal_odds
            if odds is not None and 1.05 <= odds <= settings.max_lay_odds:
                liability = round(stake * (odds - 1.0), 2)
                if has_winner:
                    if lay_candidate.is_winner == 0:
                        status = "WON_LAY"
                        pnl = round(stake * (1.0 - commission), 2)
                        total_won_amount += pnl
                        err = None
                    else:
                        status = "LOST_LAY"
                        pnl = -liability
                        total_lost_amount += liability
                        err = diagnose_lay_error(lay_candidate, wp_lay)
                    bankroll = round(bankroll + pnl, 2)
                else:
                    status = "PENDING"
                    pnl = 0.0
                    err = None

                paper_rows.append(
                    (
                        race_id,
                        lay_candidate.iso_date,
                        lay_candidate.race_dt,
                        lay_candidate.track_name,
                        lay_candidate.race_number,
                        lay_candidate.trap_number,
                        lay_candidate.dog_name,
                        round(wp_lay, 5),
                        round(lp_lay, 5),
                        round(odds, 2),
                        stake,
                        liability,
                        lay_candidate.result_position,
                        status,
                        pnl,
                        bankroll,
                        err,
                        now,
                    )
                )

    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM predictions")
        conn.executemany(
            """
            INSERT INTO predictions (
                race_id, trap_number, dog_id, dog_name, model_run_id,
                win_prob, lose_prob, fair_odds, predicted_rank,
                is_lay_pick, lay_score, lay_ev, reasons_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            pred_rows,
        )

        conn.execute("DELETE FROM paper_bets")
        conn.executemany(
            """
            INSERT INTO paper_bets (
                race_id, iso_date, race_dt, track_name, race_number,
                trap_number, dog_name, win_prob, lose_prob, lay_odds,
                stake, liability, actual_position, status, pnl,
                bankroll_after, error_reason, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            paper_rows,
        )

    return {
        "races_predicted": len(sorted_races),
        "completed_races": completed_races,
        "top1_accuracy_pct": round((top1_correct / completed_races) * 100.0, 2) if completed_races else 0.0,
        "lay_accuracy_pct": round((lay_correct / lay_total) * 100.0, 2) if lay_total else 0.0,
        "paper_bets_count": len(paper_rows),
        "virtual_bankroll": bankroll,
        "virtual_pnl": round(bankroll - settings.virtual_bankroll_start, 2),
        "total_won_amount": round(total_won_amount, 2),
        "total_lost_amount": round(total_lost_amount, 2),
    }
