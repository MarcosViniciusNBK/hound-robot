from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from app.config import settings
from app.db import get_conn
from app.features import FEATURE_LABELS_PT, FEATURE_NAMES, RunnerSample
from app.gbgb_client import utc_now_iso
from app.lay_engine import select_lay_candidate


@dataclass
class TrainedBundle:
    model_name: str
    estimator: Any
    model_run_id: int
    metrics: dict[str, Any]
    feature_importances: list[dict[str, Any]]


def normalize_race_probabilities(samples: list[RunnerSample], raw_probs: np.ndarray) -> np.ndarray:
    """Normalize runner probabilities within each race so probabilities sum to 1.0."""
    out = np.copy(raw_probs)
    by_race: dict[int, list[int]] = defaultdict(list)
    for idx, s in enumerate(samples):
        by_race[s.race_id].append(idx)

    for idxs in by_race.values():
        p = np.clip(out[idxs], 1e-4, 0.999)
        total = float(np.sum(p))
        if total > 0:
            out[idxs] = p / total
        else:
            out[idxs] = 1.0 / len(idxs)
    return out


def _build_logistic() -> Pipeline:
    return Pipeline(
        [
            ("scaler", StandardScaler()),
            ("clf", LogisticRegression(C=0.4, max_iter=500)),
        ]
    )


def _build_gbdt() -> HistGradientBoostingClassifier:
    return HistGradientBoostingClassifier(
        max_iter=120,
        max_depth=4,
        learning_rate=0.05,
        min_samples_leaf=30,
        l2_regularization=2.0,
        random_state=42,
    )


def _compute_feature_importances(estimator: Any, X: np.ndarray, y: np.ndarray) -> list[dict[str, Any]]:
    importances: np.ndarray
    if hasattr(estimator, "named_steps") and "clf" in estimator.named_steps:
        coef = np.abs(estimator.named_steps["clf"].coef_[0])
        importances = coef
    else:
        # Correlation-based importance fallback for HistGBDT without slow permutation
        importances = np.array(
            [
                abs(float(np.corrcoef(X[:, i], y)[0, 1]))
                if np.std(X[:, i]) > 1e-9
                else 0.0
                for i in range(X.shape[1])
            ],
            dtype=np.float64,
        )
        importances = np.nan_to_num(importances, nan=0.0)

    total = float(np.sum(importances)) or 1.0
    items = [
        {
            "feature": name,
            "label": FEATURE_LABELS_PT.get(name, name),
            "weight": round(float(val / total) * 100.0, 2),
        }
        for name, val in zip(FEATURE_NAMES, importances)
    ]
    items.sort(key=lambda x: x["weight"], reverse=True)
    return items[:10]


def evaluate_predictions(
    samples: list[RunnerSample],
    probs: np.ndarray,
    commission: float = settings.exchange_commission,
    stake: float = settings.virtual_lay_stake,
    max_lay_odds: float = settings.max_lay_odds,
) -> dict[str, Any]:
    y_true = np.array([s.is_winner for s in samples], dtype=np.int32)
    clipped = np.clip(probs, 1e-5, 1.0 - 1e-5)

    ll = float(log_loss(y_true, clipped, labels=[0, 1]))
    bs = float(brier_score_loss(y_true, clipped))

    by_race: dict[int, list[tuple[RunnerSample, float]]] = defaultdict(list)
    for s, p in zip(samples, clipped):
        by_race[s.race_id].append((s, float(p)))

    top1_hits = 0
    top3_hits = 0
    completed_races = 0
    lay_wins = 0
    lay_total = 0
    total_pnl = 0.0
    total_risked = 0.0

    for race_items in by_race.values():
        if not any(s.is_winner == 1 for s, _ in race_items):
            continue
        completed_races += 1
        ordered = sorted(race_items, key=lambda x: x[1], reverse=True)
        if ordered[0][0].is_winner == 1:
            top1_hits += 1
        if any(s.is_winner == 1 for s, _ in ordered[:3]):
            top3_hits += 1

        # Pick Lay candidate (excluding 1st and last ranked when field_size >= 3)
        lay_pick, lay_win_p = select_lay_candidate(ordered)
        lay_total += 1
        if lay_pick.is_winner == 0:
            lay_wins += 1

        odds = lay_pick.decimal_odds
        if odds is not None and 1.05 <= odds <= max_lay_odds:
            liability = stake * (odds - 1.0)
            total_risked += liability
            if lay_pick.is_winner == 0:
                total_pnl += stake * (1.0 - commission)
            else:
                total_pnl -= liability

    return {
        "completed_races": completed_races,
        "log_loss": round(ll, 4),
        "brier_score": round(bs, 4),
        "top1_accuracy": round((top1_hits / completed_races) * 100.0, 2) if completed_races else 0.0,
        "top3_accuracy": round((top3_hits / completed_races) * 100.0, 2) if completed_races else 0.0,
        "lay_accuracy": round((lay_wins / lay_total) * 100.0, 2) if lay_total else 0.0,
        "lay_bets_count": lay_total,
        "lay_roi_pct": round((total_pnl / total_risked) * 100.0, 2) if total_risked > 0 else 0.0,
    }


def train_and_evaluate(db_path: Path, samples: list[RunnerSample]) -> TrainedBundle:
    """Walk-forward time split training to minimize calibration error (log-loss)."""
    completed = [s for s in samples if s.result_position is not None and s.result_position > 0]
    if len(completed) < 30:
        raise ValueError("Dados insuficientes para treinar o modelo (mínimo 30 entradas completas).")

    # Chronological 75% train / 25% validation split by race
    race_order: list[int] = []
    seen = set()
    for s in completed:
        if s.race_id not in seen:
            seen.add(s.race_id)
            race_order.append(s.race_id)

    split_idx = max(1, int(len(race_order) * 0.75))
    train_race_ids = set(race_order[:split_idx])
    val_race_ids = set(race_order[split_idx:]) or set(race_order[-1:])

    train_samples = [s for s in completed if s.race_id in train_race_ids]
    val_samples = [s for s in completed if s.race_id in val_race_ids]

    X_train = np.vstack([s.features for s in train_samples])
    y_train = np.array([s.is_winner for s in train_samples], dtype=np.int32)
    X_val = np.vstack([s.features for s in val_samples])

    candidates: list[tuple[str, Any]] = [("LogisticCalibrated", _build_logistic())]
    if len(train_samples) >= 240:
        candidates.append(("HistGBDT", _build_gbdt()))

    best_name = ""
    best_model: Any = None
    best_metrics: dict[str, Any] = {}
    best_ll = float("inf")

    for name, est in candidates:
        est.fit(X_train, y_train)
        raw_val_p = est.predict_proba(X_val)[:, 1]
        norm_val_p = normalize_race_probabilities(val_samples, raw_val_p)
        metrics = evaluate_predictions(val_samples, norm_val_p)
        if metrics["log_loss"] < best_ll:
            best_ll = metrics["log_loss"]
            best_name = name
            best_model = est
            best_metrics = metrics

    # Refit winning architecture on 100% of completed data for future/live inference
    X_all = np.vstack([s.features for s in completed])
    y_all = np.array([s.is_winner for s in completed], dtype=np.int32)
    final_model = _build_logistic() if best_name == "LogisticCalibrated" else _build_gbdt()
    final_model.fit(X_all, y_all)

    importances = _compute_feature_importances(final_model, X_all, y_all)
    now = utc_now_iso()

    with get_conn(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO model_runs (
                created_at, model_name, train_races, val_races, train_entries,
                val_entries, log_loss, brier_score, top1_accuracy, top3_accuracy,
                lay_accuracy, lay_roi_pct, lay_bets_count, feature_importances_json, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                best_name,
                len(train_race_ids),
                len(val_race_ids),
                len(train_samples),
                len(val_samples),
                best_metrics["log_loss"],
                best_metrics["brier_score"],
                best_metrics["top1_accuracy"],
                best_metrics["top3_accuracy"],
                best_metrics["lay_accuracy"],
                best_metrics["lay_roi_pct"],
                best_metrics["lay_bets_count"],
                json.dumps(importances, ensure_ascii=False),
                "Validação temporal walk-forward (sem vazamento futuro)",
            ),
        )
        run_id = int(cur.lastrowid)

    model_path = settings.models_dir / "latest_model.joblib"
    joblib.dump({"model_name": best_name, "estimator": final_model, "run_id": run_id}, model_path)

    return TrainedBundle(
        model_name=best_name,
        estimator=final_model,
        model_run_id=run_id,
        metrics=best_metrics,
        feature_importances=importances,
    )


def load_latest_model() -> dict[str, Any] | None:
    model_path = settings.models_dir / "latest_model.joblib"
    if not model_path.exists():
        return None
    return joblib.load(model_path)


def walk_forward_out_of_sample_probs(
    samples: list[RunnerSample],
    model_name: str = "LogisticCalibrated",
    n_blocks: int = 5,
) -> np.ndarray:
    """Compute strictly out-of-sample probabilities via expanding chronological blocks."""
    n = len(samples)
    if n == 0:
        return np.array([], dtype=np.float64)

    race_order: list[int] = []
    seen: set[int] = set()
    for s in samples:
        if s.race_id not in seen:
            seen.add(s.race_id)
            race_order.append(s.race_id)

    if len(race_order) < 6:
        # Too few races for blocks: uniform prior fallback
        by_race_cnt: dict[int, int] = defaultdict(int)
        for s in samples:
            by_race_cnt[s.race_id] += 1
        return np.array([1.0 / max(by_race_cnt[s.race_id], 1) for s in samples], dtype=np.float64)

    raw_probs = np.zeros(n, dtype=np.float64)
    X_all = np.vstack([s.features for s in samples])
    y_all = np.array([s.is_winner for s in samples], dtype=np.int32)
    completed_mask = np.array(
        [s.result_position is not None and s.result_position > 0 for s in samples],
        dtype=bool,
    )

    block_size = max(1, len(race_order) // n_blocks)
    first_eval_idx = max(2, block_size)

    # Initial warmup races use simple empirical baseline (faster speed + win rate)
    warmup_race_ids = set(race_order[:first_eval_idx])
    for idx, s in enumerate(samples):
        if s.race_id in warmup_race_ids:
            score = 0.5 + 0.15 * float(s.features[FEATURE_NAMES.index("rel_speed_field")]) + 0.8 * float(
                s.features[FEATURE_NAMES.index("rel_win_rate_field")]
            )
            raw_probs[idx] = float(np.clip(1.0 / (1.0 + np.exp(-score)), 0.02, 0.95))

    race_to_indices: dict[int, list[int]] = defaultdict(list)
    for idx, s in enumerate(samples):
        race_to_indices[s.race_id].append(idx)

    start = first_eval_idx
    while start < len(race_order):
        end = min(len(race_order), start + block_size)
        train_races = set(race_order[:start])
        eval_races = set(race_order[start:end])

        train_idx = [
            i for i, s in enumerate(samples) if s.race_id in train_races and completed_mask[i]
        ]
        eval_idx = [i for i, s in enumerate(samples) if s.race_id in eval_races]

        if len(train_idx) >= 25 and len(np.unique(y_all[train_idx])) >= 2 and eval_idx:
            use_gbdt = model_name == "HistGBDT" and len(train_idx) >= 240
            est = _build_gbdt() if use_gbdt else _build_logistic()
            est.fit(X_all[train_idx], y_all[train_idx])
            raw_probs[eval_idx] = est.predict_proba(X_all[eval_idx])[:, 1]
        else:
            for i in eval_idx:
                raw_probs[i] = 1.0 / max(len(race_to_indices[samples[i].race_id]), 1)

        start = end

    return normalize_race_probabilities(samples, raw_probs)

