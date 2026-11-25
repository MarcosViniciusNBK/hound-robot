from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib
import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from app.adaptive_learner import load_past_error_race_ids
from app.config import settings
from app.db import get_conn
from app.features import FEATURE_LABELS_PT, FEATURE_NAMES, RunnerSample
from app.gbgb_client import utc_now_iso
from app.lay_engine import realistic_exchange_lay_odds, select_lay_candidate


@dataclass
class TrainedBundle:
    model_name: str
    estimator: Any
    model_run_id: int
    metrics: dict[str, Any]
    feature_importances: list[dict[str, Any]]


class BlendEnsemble:
    """Blends calibrated Logistic Regression and HistGBDT probabilities."""

    def __init__(self, models: list[Any], weights: list[float] | None = None) -> None:
        self.models = models
        self.weights = weights or [1.0 / len(models)] * len(models)

    def fit(
        self, X: np.ndarray, y: np.ndarray, sample_weight: np.ndarray | None = None
    ) -> "BlendEnsemble":
        for m in self.models:
            _fit_estimator(m, X, y, sample_weight=sample_weight)
        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        probs = np.zeros((X.shape[0], 2), dtype=np.float64)
        for m, w in zip(self.models, self.weights):
            probs += w * m.predict_proba(X)
        return probs


def _fit_estimator(
    est: Any, X: np.ndarray, y: np.ndarray, sample_weight: np.ndarray | None = None
) -> Any:
    if sample_weight is None:
        return est.fit(X, y)
    if isinstance(est, Pipeline):
        return est.fit(X, y, clf__sample_weight=sample_weight)
    return est.fit(X, y, sample_weight=sample_weight)


def compute_adaptive_sample_weights(
    samples: list[RunnerSample], error_race_ids: set[int]
) -> np.ndarray:
    """Recency decay + moderate 1.4x focus on prior Lay error races (strictly bounded)."""
    n = len(samples)
    if n == 0:
        return np.array([], dtype=np.float64)
    # Chronological recency ramp from 0.85 (oldest) to 1.15 (newest)
    recency = np.linspace(0.85, 1.15, n, dtype=np.float64)
    boost = np.array(
        [1.40 if s.race_id in error_race_ids else 1.0 for s in samples],
        dtype=np.float64,
    )
    return np.clip(recency * boost, 0.80, 1.60)


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
            ("clf", LogisticRegression(C=0.35, max_iter=600)),
        ]
    )


def _build_gbdt() -> HistGradientBoostingClassifier:
    return HistGradientBoostingClassifier(
        max_iter=150,
        max_depth=4,
        learning_rate=0.04,
        min_samples_leaf=35,
        l2_regularization=3.0,
        random_state=42,
    )


def _build_calibrated_gbdt() -> CalibratedClassifierCV:
    return CalibratedClassifierCV(estimator=_build_gbdt(), method="sigmoid", cv=3)


def _build_ensemble() -> BlendEnsemble:
    return BlendEnsemble([_build_logistic(), _build_calibrated_gbdt()], weights=[0.45, 0.55])


def _instantiate_model(model_name: str) -> Any:
    if model_name == "EnsembleBlend":
        return _build_ensemble()
    if model_name == "HistGBDTCalibrated":
        return _build_calibrated_gbdt()
    return _build_logistic()


def _compute_feature_importances(estimator: Any, X: np.ndarray, y: np.ndarray) -> list[dict[str, Any]]:
    if hasattr(estimator, "named_steps") and "clf" in estimator.named_steps:
        importances = np.abs(estimator.named_steps["clf"].coef_[0])
    elif isinstance(estimator, BlendEnsemble) and hasattr(estimator.models[0], "named_steps"):
        importances = np.abs(estimator.models[0].named_steps["clf"].coef_[0])
    else:
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


def compute_calibration_buckets(
    samples: list[RunnerSample], probs: np.ndarray
) -> list[dict[str, Any]]:
    """Group runners into predicted win-probability bands to audit calibration."""
    bands = [
        ("0% - 10%", 0.0, 0.10),
        ("10% - 18%", 0.10, 0.18),
        ("18% - 28%", 0.18, 0.28),
        ("28%+", 0.28, 1.01),
    ]
    out: list[dict[str, Any]] = []
    completed = [
        (s, float(p))
        for s, p in zip(samples, probs)
        if s.result_position is not None and s.result_position > 0
    ]
    for label, lo, hi in bands:
        bucket = [(s, p) for s, p in completed if lo <= p < hi]
        if not bucket:
            out.append(
                {
                    "band": label,
                    "count": 0,
                    "pred_win_pct": 0.0,
                    "actual_win_pct": 0.0,
                    "actual_lose_pct": 0.0,
                }
            )
            continue
        pred_avg = sum(p for _, p in bucket) / len(bucket)
        actual_avg = sum(s.is_winner for s, _ in bucket) / len(bucket)
        out.append(
            {
                "band": label,
                "count": len(bucket),
                "pred_win_pct": round(pred_avg * 100.0, 1),
                "actual_win_pct": round(actual_avg * 100.0, 1),
                "actual_lose_pct": round((1.0 - actual_avg) * 100.0, 1),
            }
        )
    return out


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

        lay_pick, lay_win_p = select_lay_candidate(ordered)
        lay_total += 1
        if lay_pick.is_winner == 0:
            lay_wins += 1

        if lay_pick.decimal_odds is not None:
            sim_odds, _ = realistic_exchange_lay_odds(lay_pick.decimal_odds, lay_win_p)
            if 1.05 <= sim_odds <= max_lay_odds:
                liability = stake * (sim_odds - 1.0)
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

    error_race_ids = load_past_error_race_ids(db_path)
    train_weights = compute_adaptive_sample_weights(train_samples, error_race_ids)

    candidates: list[tuple[str, Any, np.ndarray | None]] = [
        ("LogisticCalibrated", _build_logistic(), None),
    ]
    if error_race_ids:
        candidates.append(("LogisticErrorWeighted", _build_logistic(), train_weights))
    if len(train_samples) >= 350:
        candidates.append(("HistGBDTCalibrated", _build_calibrated_gbdt(), None))
        candidates.append(("EnsembleBlend", _build_ensemble(), None))
        if error_race_ids:
            candidates.append(("EnsembleErrorWeighted", _build_ensemble(), train_weights))

    best_name = ""
    best_metrics: dict[str, Any] = {}
    best_ll = float("inf")
    use_weighted_final = False

    for name, est, sw in candidates:
        _fit_estimator(est, X_train, y_train, sample_weight=sw)
        raw_val_p = est.predict_proba(X_val)[:, 1]
        norm_val_p = normalize_race_probabilities(val_samples, raw_val_p)
        metrics = evaluate_predictions(val_samples, norm_val_p)
        if metrics["log_loss"] < best_ll:
            best_ll = metrics["log_loss"]
            best_name = name
            best_metrics = metrics
            use_weighted_final = sw is not None

    X_all = np.vstack([s.features for s in completed])
    y_all = np.array([s.is_winner for s in completed], dtype=np.int32)
    base_arch = (
        "EnsembleBlend"
        if "Ensemble" in best_name
        else ("HistGBDTCalibrated" if "HistGBDT" in best_name else "LogisticCalibrated")
    )
    final_model = _instantiate_model(base_arch)
    final_weights = (
        compute_adaptive_sample_weights(completed, error_race_ids)
        if use_weighted_final
        else None
    )
    _fit_estimator(final_model, X_all, y_all, sample_weight=final_weights)

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
                "Validação temporal walk-forward com calibração e spread realista",
            ),
        )
        run_id = int(cur.lastrowid)

    model_path = settings.models_dir / "latest_model.joblib"
    joblib.dump({"model_name": base_arch, "display_name": best_name, "estimator": final_model, "run_id": run_id}, model_path)

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
    n_blocks: int = 8,
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
            use_name = model_name if len(train_idx) >= 350 else "LogisticCalibrated"
            est = _instantiate_model(use_name)
            est.fit(X_all[train_idx], y_all[train_idx])
            raw_probs[eval_idx] = est.predict_proba(X_all[eval_idx])[:, 1]
        else:
            for i in eval_idx:
                raw_probs[i] = 1.0 / max(len(race_to_indices[samples[i].race_id]), 1)

        start = end

    return normalize_race_probabilities(samples, raw_probs)
