from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from app.adaptive_learner import AdaptiveContextTracker, save_adaptive_summary
from app.config import settings
from app.db import get_bankroll_config, get_conn
from app.features import FEATURE_NAMES, RunnerSample
from app.gbgb_client import utc_now_iso


_IDX = {name: i for i, name in enumerate(FEATURE_NAMES)}

_TICK_BANDS: list[tuple[float, float, float]] = [
    (1.01, 2.0, 0.01),
    (2.0, 3.0, 0.02),
    (3.0, 4.0, 0.05),
    (4.0, 6.0, 0.10),
    (6.0, 10.0, 0.20),
    (10.0, 20.0, 0.50),
    (20.0, 30.0, 1.00),
    (30.0, 50.0, 2.00),
    (50.0, 100.0, 5.00),
    (100.0, 1000.0, 10.00),
]


def _tick_step(price: float) -> float:
    for lo, hi, step in _TICK_BANDS:
        if lo <= price < hi:
            return step
    return 10.0


def snap_to_exchange_tick(odds: float, extra_ticks: int = 0) -> float:
    """Snap a decimal price conservatively (ceil) to valid Exchange tick increments."""
    price = max(1.01, min(1000.0, float(odds)))
    step = _tick_step(price)
    snapped = round(math.ceil(price / step - 1e-9) * step, 2)
    for _ in range(max(0, extra_ticks)):
        snapped = round(snapped + _tick_step(snapped), 2)
    return min(1000.0, max(1.01, snapped))


def realistic_exchange_lay_odds(
    sp_decimal: float | None,
    win_prob: float,
    exchange_lay_odds: float | None = None,
) -> tuple[float, str]:
    """Return realistic Exchange Lay price + source tag."""
    if exchange_lay_odds is not None and 1.01 <= exchange_lay_odds <= 1000.0:
        return snap_to_exchange_tick(exchange_lay_odds, extra_ticks=0), "BETFAIR_EXCHANGE"

    if sp_decimal is not None and sp_decimal >= 1.01:
        with_spread = sp_decimal * (1.0 + settings.lay_spread_pct)
        return (
            snap_to_exchange_tick(with_spread, extra_ticks=settings.lay_slippage_ticks),
            "SP_CONSERVADOR",
        )

    fair = 1.0 / max(win_prob, 0.03)
    est = fair * (1.0 + settings.lay_spread_pct)
    return (
        snap_to_exchange_tick(min(settings.max_lay_odds, max(1.40, est)), extra_ticks=0),
        "ESTIMADA_CONSERVADORA",
    )


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


def evaluate_lay_entry(
    sample: RunnerSample,
    win_prob: float,
    lay_odds: float,
    desired_stake: float,
    bankroll_ref: float,
    require_market_ev: bool = True,
    min_ev_override: float | None = None,
    max_risk_pct_override: float | None = None,
) -> tuple[bool, float, float, float, str | None]:
    """
    Apply disciplined +EV, experience, and bankroll risk management filters.
    Returns (should_enter, final_stake, liability, ev_brl, skip_reason).
    """
    prior_runs = int(sample.features[_IDX["prior_runs"]])
    if prior_runs < settings.min_prior_runs_for_bet:
        return False, 0.0, 0.0, 0.0, f"Histórico curto (<{settings.min_prior_runs_for_bet} corridas)"

    if lay_odds > settings.max_lay_odds or lay_odds < 1.05:
        return False, 0.0, 0.0, 0.0, f"Odd {lay_odds:.2f} fora do teto ({settings.max_lay_odds:.1f})"

    min_ev = settings.min_ev_per_unit if min_ev_override is None else min_ev_override
    max_risk_pct = (
        settings.max_risk_pct_bankroll
        if max_risk_pct_override is None
        else max_risk_pct_override
    )

    ev_per_unit = compute_lay_ev(
        win_prob, lay_odds, stake=1.0, commission=settings.exchange_commission
    )
    if require_market_ev:
        if ev_per_unit is None or ev_per_unit < min_ev:
            return False, 0.0, 0.0, 0.0, "Sem valor esperado positivo (+EV) após comissão/spread"
    elif (1.0 - win_prob) < 0.80:
        return False, 0.0, 0.0, 0.0, "Confiança de derrota abaixo de 80% para entrada pré-corrida"

    max_risk = max(1.0, bankroll_ref * max_risk_pct)
    raw_liability = desired_stake * (lay_odds - 1.0)
    final_stake = desired_stake
    if raw_liability > max_risk:
        scaled = max_risk / max(lay_odds - 1.0, 0.05)
        final_stake = math.floor(scaled * 2.0) / 2.0  # round down to R$ 0.50
        if final_stake < 1.0:
            return False, 0.0, 0.0, 0.0, "Risco excede 15% da banca"

    liability = round(final_stake * (lay_odds - 1.0), 2)
    ev_brl = round((ev_per_unit or 0.0) * final_stake, 2)
    return True, final_stake, liability, ev_brl, None


def select_lay_candidate(
    ordered_desc: Sequence[tuple[RunnerSample, float]],
) -> tuple[RunnerSample, float]:
    """
    Select the greyhound to Lay (bet it will NOT win).

    Business rule: when a race has 3+ runners, never pick the 1st ranked
    (probable winner) nor the last ranked (extreme longshot with huge liability).
    """
    n = len(ordered_desc)
    if n == 0:
        raise ValueError("Cannot select Lay candidate from empty race.")
    if n <= 2:
        return ordered_desc[-1]

    middle_pool = list(ordered_desc[1:-1])
    preferred_pool = list(ordered_desc[2:-1]) if n >= 4 else middle_pool

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


def compute_portfolio_metrics(pnls: list[float], start_bankroll: float) -> dict[str, Any]:
    if not pnls:
        return {
            "max_drawdown_brl": 0.0,
            "max_drawdown_pct": 0.0,
            "profit_factor": 0.0,
            "expectancy_brl": 0.0,
            "max_loss_streak": 0,
            "max_win_streak": 0,
        }

    eq = start_bankroll
    peak = start_bankroll
    max_dd = 0.0
    max_dd_pct = 0.0
    won = 0.0
    lost = 0.0
    cur_win_s = 0
    cur_loss_s = 0
    max_win_s = 0
    max_loss_s = 0

    for p in pnls:
        eq += p
        if eq > peak:
            peak = eq
        dd = peak - eq
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = (dd / peak) * 100.0 if peak > 0 else 0.0
        if p >= 0:
            won += p
            cur_win_s += 1
            cur_loss_s = 0
            max_win_s = max(max_win_s, cur_win_s)
        else:
            lost += abs(p)
            cur_loss_s += 1
            cur_win_s = 0
            max_loss_s = max(max_loss_s, cur_loss_s)

    pf = round(won / lost, 2) if lost > 0 else (99.0 if won > 0 else 0.0)
    return {
        "max_drawdown_brl": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "profit_factor": pf,
        "expectancy_brl": round(sum(pnls) / len(pnls), 2),
        "max_loss_streak": max_loss_s,
        "max_win_streak": max_win_s,
    }


def persist_predictions_and_paper_bets(
    db_path: Path,
    samples: list[RunnerSample],
    probs: np.ndarray,
    model_run_id: int | None,
) -> dict[str, Any]:
    """
    Save normalized race predictions, rebuild conservative backtest_bets (5% comm,
    spread + slippage ticks, +EV filter, 15% max risk cap), and manage the live
    R$ test bankroll (paper_bets).
    """
    bankroll_start, stake = get_bankroll_config(
        db_path, settings.virtual_bankroll_start, settings.virtual_lay_stake
    )
    commission = settings.exchange_commission

    with get_conn(db_path) as conn:
        locked_rows = conn.execute(
            "SELECT * FROM paper_bets WHERE placed_before_race = 1 ORDER BY race_dt ASC"
        ).fetchall()
        ex_rows = conn.execute(
            "SELECT race_id, trap_number, exchange_lay_odds FROM entries WHERE exchange_lay_odds IS NOT NULL"
        ).fetchall()
    locked_by_race: dict[int, dict[str, Any]] = {
        int(r["race_id"]): dict(r) for r in locked_rows
    }
    ex_odds_map: dict[tuple[int, int], float] = {
        (int(r["race_id"]), int(r["trap_number"])): float(r["exchange_lay_odds"])
        for r in ex_rows
    }

    by_race: dict[int, list[tuple[RunnerSample, float]]] = defaultdict(list)
    for s, p in zip(samples, probs):
        by_race[s.race_id].append((s, float(p)))

    now = utc_now_iso()
    cutoff_dt = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")

    pred_rows: list[tuple[Any, ...]] = []
    backtest_rows: list[tuple[Any, ...]] = []
    live_settled_rows: list[tuple[Any, ...]] = []
    live_pending_candidates: list[tuple[int, RunnerSample, float, dict[str, Any] | None]] = []

    live_bankroll = bankroll_start
    backtest_bankroll = bankroll_start
    tracker = AdaptiveContextTracker()

    sorted_races = sorted(
        by_race.items(),
        key=lambda kv: (kv[1][0][0].race_dt, kv[0]),
    )

    lay_correct = 0
    lay_total = 0
    top1_correct = 0
    completed_races = 0
    upcoming_races = 0
    live_won_amount = 0.0
    live_lost_amount = 0.0
    bt_won_amount = 0.0
    bt_lost_amount = 0.0
    bt_pnls: list[float] = []
    bt_skipped = 0

    warmup_cutoff = min(15, len(sorted_races) // 4) if len(sorted_races) > 8 else 0

    for race_idx, (race_id, items) in enumerate(sorted_races):
        ordered = sorted(items, key=lambda x: x[1], reverse=True)
        locked = locked_by_race.get(race_id)

        if locked is not None:
            matched = next(
                ((s, p) for s, p in ordered if s.trap_number == int(locked["trap_number"])),
                None,
            )
            lay_candidate, wp_lay = matched if matched is not None else select_lay_candidate(ordered)
        else:
            lay_candidate, wp_lay = select_lay_candidate(ordered)

        has_winner = any(s.is_winner == 1 for s, _ in ordered)
        if has_winner:
            completed_races += 1
            if ordered[0][0].is_winner == 1:
                top1_correct += 1
            lay_total += 1
            if lay_candidate.is_winner == 0:
                lay_correct += 1
        else:
            upcoming_races += 1

        for rank_idx, (s, wp) in enumerate(ordered, start=1):
            lp = max(0.001, min(0.999, 1.0 - wp))
            fair = round(1.0 / max(wp, 0.005), 2)
            is_lay = 1 if s.trap_number == lay_candidate.trap_number else 0
            ex_odds = ex_odds_map.get((s.race_id, s.trap_number))
            lay_price, _ = realistic_exchange_lay_odds(
                s.decimal_odds, wp, exchange_lay_odds=ex_odds
            )
            ev = compute_lay_ev(wp, lay_price, stake=stake, commission=commission)
            reasons = explain_runner(s, wp, is_lay == 1)
            reasons_payload = {"reasons": reasons, "stats": s.summary, "sim_lay_odds": lay_price}
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

        # 1) Conservative historical out-of-sample backtest (post-warmup)
        if has_winner and race_idx >= warmup_cutoff:
            ex_odds = ex_odds_map.get((race_id, lay_candidate.trap_number))
            if ex_odds is not None or lay_candidate.decimal_odds is not None:
                sim_odds, _ = realistic_exchange_lay_odds(
                    lay_candidate.decimal_odds, wp_lay, exchange_lay_odds=ex_odds
                )
                ctx_ok, _ = tracker.check_context_allowed(lay_candidate, sim_odds)
                dyn_min_ev, dyn_max_risk, _ = tracker.current_thresholds(
                    max(backtest_bankroll, bankroll_start)
                )
                if ctx_ok:
                    should_enter, adj_stake, liability, _, _ = evaluate_lay_entry(
                        lay_candidate,
                        wp_lay,
                        sim_odds,
                        stake,
                        max(backtest_bankroll, bankroll_start),
                        min_ev_override=dyn_min_ev,
                        max_risk_pct_override=dyn_max_risk,
                    )
                else:
                    should_enter, adj_stake, liability = False, 0.0, 0.0

                if should_enter:
                    if lay_candidate.is_winner == 0:
                        bt_status = "WON_LAY"
                        bt_pnl = round(adj_stake * (1.0 - commission), 2)
                        bt_won_amount += bt_pnl
                        bt_err = None
                    else:
                        bt_status = "LOST_LAY"
                        bt_pnl = -liability
                        bt_lost_amount += liability
                        bt_err = diagnose_lay_error(lay_candidate, wp_lay)
                    backtest_bankroll = round(backtest_bankroll + bt_pnl, 2)
                    bt_pnls.append(bt_pnl)
                    tracker.record_outcome(
                        lay_candidate,
                        sim_odds,
                        wp_lay,
                        liability,
                        bt_pnl,
                        is_lay_win=(lay_candidate.is_winner == 0),
                    )
                    backtest_rows.append(
                        (
                            race_id,
                            lay_candidate.iso_date,
                            lay_candidate.race_dt,
                            lay_candidate.track_name,
                            lay_candidate.race_number,
                            lay_candidate.trap_number,
                            lay_candidate.dog_name,
                            round(wp_lay, 5),
                            round(1.0 - wp_lay, 5),
                            round(sim_odds, 2),
                            adj_stake,
                            liability,
                            lay_candidate.result_position,
                            bt_status,
                            bt_pnl,
                            backtest_bankroll,
                            bt_err,
                            now,
                        )
                    )
                else:
                    bt_skipped += 1

        # 2) Live R$ Test Bankroll (ONLY bets locked before race ran!)
        if has_winner and locked is not None:
            ex_odds = ex_odds_map.get((race_id, lay_candidate.trap_number))
            if ex_odds is not None:
                used_odds, odds_src = realistic_exchange_lay_odds(
                    None, float(locked["win_prob"]), exchange_lay_odds=ex_odds
                )
            elif lay_candidate.decimal_odds is not None and 1.05 <= lay_candidate.decimal_odds <= settings.max_lay_odds:
                used_odds, odds_src = realistic_exchange_lay_odds(
                    lay_candidate.decimal_odds, float(locked["win_prob"])
                )
            else:
                used_odds = float(locked["lay_odds"])
                odds_src = locked.get("odds_source") or "ESTIMADA_CONSERVADORA"

            bet_stake = float(locked["stake"])
            liability = round(bet_stake * (used_odds - 1.0), 2)
            if lay_candidate.is_winner == 0:
                status = "WON_LAY"
                pnl = round(bet_stake * (1.0 - commission), 2)
                live_won_amount += pnl
                err = None
            else:
                status = "LOST_LAY"
                pnl = -liability
                live_lost_amount += liability
                err = diagnose_lay_error(lay_candidate, float(locked["win_prob"]))
            live_bankroll = round(live_bankroll + pnl, 2)
            tracker.record_outcome(
                lay_candidate,
                used_odds,
                float(locked["win_prob"]),
                liability,
                pnl,
                is_lay_win=(lay_candidate.is_winner == 0),
            )
            live_settled_rows.append(
                (
                    race_id,
                    lay_candidate.iso_date,
                    lay_candidate.race_dt,
                    lay_candidate.track_name,
                    lay_candidate.race_number,
                    lay_candidate.trap_number,
                    lay_candidate.dog_name,
                    float(locked["win_prob"]),
                    float(locked["lose_prob"]),
                    round(used_odds, 2),
                    odds_src,
                    1,
                    bet_stake,
                    liability,
                    lay_candidate.result_position,
                    status,
                    pnl,
                    live_bankroll,
                    err,
                    locked["created_at"],
                )
            )
        elif not has_winner and lay_candidate.race_dt >= cutoff_dt:
            live_pending_candidates.append((race_id, lay_candidate, wp_lay, locked))

    available_balance = max(0.0, live_bankroll)
    live_pending_rows: list[tuple[Any, ...]] = []
    committed_liability = 0.0

    live_pending_candidates.sort(key=lambda item: (0 if item[3] is not None else 1, item[1].race_dt))

    for race_id, lay_candidate, wp_lay, locked in live_pending_candidates:
        if locked is not None:
            est_odds = float(locked["lay_odds"])
            odds_src = locked.get("odds_source") or "ESTIMADA_CONSERVADORA"
            bet_stake = float(locked["stake"])
            liability = round(bet_stake * (est_odds - 1.0), 2)
        else:
            ex_odds = ex_odds_map.get((race_id, lay_candidate.trap_number))
            est_odds, odds_src = realistic_exchange_lay_odds(
                None, wp_lay, exchange_lay_odds=ex_odds
            )
            ctx_ok, _ = tracker.check_context_allowed(lay_candidate, est_odds)
            if not ctx_ok:
                continue
            dyn_min_ev, dyn_max_risk, _ = tracker.current_thresholds(max(live_bankroll, 10.0))
            should_enter, bet_stake, liability, _, _ = evaluate_lay_entry(
                lay_candidate,
                wp_lay,
                est_odds,
                stake,
                max(live_bankroll, 10.0),
                require_market_ev=(ex_odds is not None),
                min_ev_override=dyn_min_ev,
                max_risk_pct_override=dyn_max_risk,
            )
            if not should_enter:
                continue

        if liability <= 0 or available_balance < liability:
            continue

        available_balance = round(available_balance - liability, 2)
        committed_liability = round(committed_liability + liability, 2)
        live_pending_rows.append(
            (
                race_id,
                lay_candidate.iso_date,
                lay_candidate.race_dt,
                lay_candidate.track_name,
                lay_candidate.race_number,
                lay_candidate.trap_number,
                lay_candidate.dog_name,
                round(float(locked["win_prob"]) if locked else wp_lay, 5),
                round(float(locked["lose_prob"]) if locked else (1.0 - wp_lay), 5),
                round(est_odds, 2),
                odds_src,
                1,
                bet_stake,
                liability,
                None,
                "PENDING",
                0.0,
                live_bankroll,
                None,
                locked["created_at"] if locked else now,
            )
        )

    all_paper_rows = live_settled_rows + live_pending_rows
    bt_risk_metrics = compute_portfolio_metrics(bt_pnls, bankroll_start)
    adaptive_summary = tracker.export_summary(live_bankroll)
    save_adaptive_summary(db_path, adaptive_summary)

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
                odds_source, placed_before_race, stake, liability, actual_position,
                status, pnl, bankroll_after, error_reason, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            all_paper_rows,
        )

        conn.execute("DELETE FROM backtest_bets")
        conn.executemany(
            """
            INSERT INTO backtest_bets (
                race_id, iso_date, race_dt, track_name, race_number,
                trap_number, dog_name, win_prob, lose_prob, lay_odds,
                stake, liability, actual_position, status, pnl,
                bankroll_after, error_reason, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            backtest_rows,
        )

    return {
        "races_predicted": len(sorted_races),
        "completed_races": completed_races,
        "upcoming_races": upcoming_races,
        "pending_bets": len(live_pending_rows),
        "live_settled_bets": len(live_settled_rows),
        "committed_liability": committed_liability,
        "available_balance": available_balance,
        "top1_accuracy_pct": round((top1_correct / completed_races) * 100.0, 2) if completed_races else 0.0,
        "lay_accuracy_pct": round((lay_correct / lay_total) * 100.0, 2) if lay_total else 0.0,
        "paper_bets_count": len(all_paper_rows),
        "bankroll_start": bankroll_start,
        "stake": stake,
        "virtual_bankroll": live_bankroll,
        "virtual_pnl": round(live_bankroll - bankroll_start, 2),
        "total_won_amount": round(live_won_amount, 2),
        "total_lost_amount": round(live_lost_amount, 2),
        "backtest_bets_count": len(backtest_rows),
        "backtest_skipped_count": bt_skipped,
        "backtest_pnl": round(backtest_bankroll - bankroll_start, 2),
        "backtest_won_amount": round(bt_won_amount, 2),
        "backtest_lost_amount": round(bt_lost_amount, 2),
        "backtest_risk_metrics": bt_risk_metrics,
        "adaptive_learning": adaptive_summary,
    }
