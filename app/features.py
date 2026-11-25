from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from app.db import get_conn


FEATURE_NAMES: list[str] = [
    "prior_runs",
    "win_rate_bayes",
    "top2_rate_bayes",
    "ewm_pos",
    "last_pos",
    "speed_vs_par",
    "best_speed_vs_par",
    "last_speed_vs_par",
    "sectional_vs_par",
    "trouble_rate",
    "fast_break_rate",
    "same_track_win_rate",
    "trap_win_rate",
    "trainer_win_rate",
    "days_since_last",
    "field_size",
    "rel_speed_field",
    "rel_win_rate_field",
    "rel_sectional_field",
    "rel_ewm_pos_field",
]

FEATURE_LABELS_PT: dict[str, str] = {
    "prior_runs": "Experiência (corridas prévias)",
    "win_rate_bayes": "Taxa de vitória histórica",
    "top2_rate_bayes": "Taxa de pódio (Top-2)",
    "ewm_pos": "Forma recente (posição ponderada)",
    "last_pos": "Última colocação",
    "speed_vs_par": "Velocidade média vs pista/distância",
    "best_speed_vs_par": "Melhor velocidade vs pista/distância",
    "last_speed_vs_par": "Última velocidade vs pista/distância",
    "sectional_vs_par": "Split de largada vs pista",
    "trouble_rate": "Índice de colisões/atrasos",
    "fast_break_rate": "Índice de largada rápida",
    "same_track_win_rate": "Aproveitamento nesta pista",
    "trap_win_rate": "Vantagem da Trap/Raia",
    "trainer_win_rate": "Taxa do treinador",
    "days_since_last": "Dias desde a última corrida",
    "field_size": "Tamanho do páreo",
    "rel_speed_field": "Velocidade relativa ao páreo",
    "rel_win_rate_field": "Win rate relativo ao páreo",
    "rel_sectional_field": "Largada relativa ao páreo",
    "rel_ewm_pos_field": "Forma relativa ao páreo",
}

TROUBLE_RE = re.compile(r"\b(BMP|BLK|CRD|SAW|FCK|FCD|STUMB|BUMP|CHECK|SLOW)\b", re.IGNORECASE)
FAST_BREAK_RE = re.compile(r"\b(QAW|EP|ALD|LD|VQAW|SNLD)\b", re.IGNORECASE)


def _dist_bucket(dist: float | None) -> int:
    if not dist or dist <= 0:
        return 450
    return int(round(dist / 25.0) * 25)


def _parse_dt(dt_str: str) -> datetime | None:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(dt_str.strip(), fmt)
        except ValueError:
            continue
    return None


@dataclass
class RunnerSample:
    race_id: int
    race_dt: str
    iso_date: str
    track_name: str
    race_number: int
    trap_number: int
    dog_id: int | None
    dog_name: str
    decimal_odds: float | None
    result_position: int | None
    is_winner: int
    features: np.ndarray
    summary: dict[str, Any]
    race_distance: float | None = None
    race_class: str | None = None


class RollingState:
    """Chronological state accumulator ensuring strict zero-leakage feature computation."""

    def __init__(self) -> None:
        self.dog_runs: dict[int, list[dict[str, Any]]] = defaultdict(list)
        self.dog_seen_races: dict[int, set[int]] = defaultdict(set)
        self.trainer_stats: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
        self.trap_stats: dict[tuple[str, int, int], list[float]] = defaultdict(lambda: [0.0, 0.0])
        self.track_dist_speed: dict[tuple[str, int], list[float]] = defaultdict(lambda: [0.0, 0.0])
        self.track_dist_sect: dict[tuple[str, int], list[float]] = defaultdict(lambda: [0.0, 0.0])

    def _par_speed(self, track: str, dist: float | None) -> float:
        key = (track, _dist_bucket(dist))
        s, c = self.track_dist_speed[key]
        if c >= 5:
            return s / c
        # Global track fallback
        ts, tc = 0.0, 0.0
        for (trk, _), (ss, cc) in self.track_dist_speed.items():
            if trk == track:
                ts += ss
                tc += cc
        if tc >= 5:
            return ts / tc
        return 16.5  # typical greyhound m/s baseline

    def _par_sectional(self, track: str, dist: float | None) -> float:
        key = (track, _dist_bucket(dist))
        s, c = self.track_dist_sect[key]
        if c >= 5:
            return s / c
        return 4.5

    def add_historical_run(
        self,
        dog_id: int | None,
        race_id: int,
        race_dt: str,
        track_name: str,
        race_distance: float | None,
        trap_number: int | None,
        result_position: int | None,
        adjusted_time: float | None,
        run_time: float | None,
        sectional_time: float | None,
        comment: str | None,
        trainer_name: str | None = None,
    ) -> None:
        if result_position is None or result_position <= 0:
            return
        t_use = adjusted_time if (adjusted_time and adjusted_time > 5.0) else run_time
        speed_mps = None
        if t_use and t_use > 5.0 and race_distance and race_distance > 100:
            speed_mps = race_distance / t_use

        if speed_mps and 10.0 <= speed_mps <= 22.0:
            key = (track_name, _dist_bucket(race_distance))
            self.track_dist_speed[key][0] += speed_mps
            self.track_dist_speed[key][1] += 1.0

        if sectional_time and 1.0 <= sectional_time <= 15.0:
            key = (track_name, _dist_bucket(race_distance))
            self.track_dist_sect[key][0] += sectional_time
            self.track_dist_sect[key][1] += 1.0

        is_win = 1.0 if result_position == 1 else 0.0
        if trap_number and 1 <= trap_number <= 8:
            tkey = (track_name, _dist_bucket(race_distance), trap_number)
            self.trap_stats[tkey][0] += is_win
            self.trap_stats[tkey][1] += 1.0

        if trainer_name:
            self.trainer_stats[trainer_name][0] += is_win
            self.trainer_stats[trainer_name][1] += 1.0

        if dog_id is not None and race_id not in self.dog_seen_races[dog_id]:
            self.dog_seen_races[dog_id].add(race_id)
            par_spd = self._par_speed(track_name, race_distance)
            par_sec = self._par_sectional(track_name, race_distance)
            comment_str = comment or ""
            self.dog_runs[dog_id].append(
                {
                    "race_dt": race_dt,
                    "track_name": track_name,
                    "dist_bucket": _dist_bucket(race_distance),
                    "pos": float(result_position),
                    "speed_vs_par": (speed_mps - par_spd) if speed_mps is not None else 0.0,
                    "has_speed": speed_mps is not None,
                    "sect_vs_par": (sectional_time - par_sec) if sectional_time is not None else 0.0,
                    "has_sect": sectional_time is not None,
                    "trouble": 1.0 if TROUBLE_RE.search(comment_str) else 0.0,
                    "fast_break": 1.0 if FAST_BREAK_RE.search(comment_str) else 0.0,
                }
            )

    def extract_race_features(
        self,
        race_meta: dict[str, Any],
        runners: list[dict[str, Any]],
    ) -> list[RunnerSample]:
        track_name = race_meta.get("track_name") or "Unknown"
        race_distance = race_meta.get("race_distance")
        dist_b = _dist_bucket(race_distance)
        race_dt_str = race_meta.get("race_dt") or ""
        race_dt_obj = _parse_dt(race_dt_str)
        field_size = float(max(len(runners), 1))

        raw_vectors: list[dict[str, float]] = []
        summaries: list[dict[str, Any]] = []

        for r in runners:
            dog_id = r.get("dog_id")
            trap_num = int(r.get("trap_number") or 1)
            trainer = r.get("trainer_name") or ""
            runs = [
                x
                for x in (self.dog_runs.get(dog_id, []) if dog_id is not None else [])
                if x["race_dt"] < race_dt_str
            ]
            runs = runs[-12:]  # focus on most recent 12 runs

            n_runs = len(runs)
            if n_runs > 0:
                wins = sum(1.0 for x in runs if x["pos"] == 1.0)
                top2 = sum(1.0 for x in runs if x["pos"] <= 2.0)
                win_rate_bayes = (wins + 0.5) / (n_runs + 3.0)
                top2_rate_bayes = (top2 + 1.0) / (n_runs + 3.0)

                # Exponentially weighted position (most recent run weighs highest)
                weights = [math.exp(0.35 * i) for i in range(n_runs)]
                ewm_pos = sum(w * x["pos"] for w, x in zip(weights, runs)) / sum(weights)
                last_pos = runs[-1]["pos"]

                spd_vals = [x["speed_vs_par"] for x in runs if x["has_speed"]]
                speed_vs_par = sum(spd_vals) / len(spd_vals) if spd_vals else 0.0
                best_speed_vs_par = max(spd_vals) if spd_vals else 0.0
                last_speed_vs_par = spd_vals[-1] if spd_vals else 0.0

                sec_vals = [x["sect_vs_par"] for x in runs if x["has_sect"]]
                sectional_vs_par = sum(sec_vals) / len(sec_vals) if sec_vals else 0.0

                trouble_rate = sum(x["trouble"] for x in runs) / n_runs
                fast_break_rate = sum(x["fast_break"] for x in runs) / n_runs

                same_trk = [x for x in runs if x["track_name"] == track_name]
                if same_trk:
                    same_track_win_rate = (
                        sum(1.0 for x in same_trk if x["pos"] == 1.0) + 0.35
                    ) / (len(same_trk) + 2.0)
                else:
                    same_track_win_rate = win_rate_bayes

                last_dt = _parse_dt(runs[-1]["race_dt"])
                if race_dt_obj and last_dt:
                    days_since_last = float(max(0, min(60, (race_dt_obj - last_dt).days)))
                else:
                    days_since_last = 7.0
                recent_form = "".join(str(int(x["pos"])) for x in runs[-5:])
            else:
                win_rate_bayes = 1.0 / max(field_size, 6.0)
                top2_rate_bayes = 2.0 / max(field_size, 6.0)
                ewm_pos = (field_size + 1.0) / 2.0
                last_pos = ewm_pos
                speed_vs_par = 0.0
                best_speed_vs_par = 0.0
                last_speed_vs_par = 0.0
                sectional_vs_par = 0.0
                trouble_rate = 0.15
                fast_break_rate = 0.15
                same_track_win_rate = win_rate_bayes
                days_since_last = 14.0
                recent_form = "-"

            tw, tc = self.trap_stats[(track_name, dist_b, trap_num)]
            trap_win_rate = (tw + 1.0) / (tc + 6.0)

            tr_w, tr_c = self.trainer_stats[trainer] if trainer else (0.0, 0.0)
            trainer_win_rate = (tr_w + 1.0) / (tr_c + 6.0)

            raw_vectors.append(
                {
                    "prior_runs": float(n_runs),
                    "win_rate_bayes": win_rate_bayes,
                    "top2_rate_bayes": top2_rate_bayes,
                    "ewm_pos": ewm_pos,
                    "last_pos": last_pos,
                    "speed_vs_par": speed_vs_par,
                    "best_speed_vs_par": best_speed_vs_par,
                    "last_speed_vs_par": last_speed_vs_par,
                    "sectional_vs_par": sectional_vs_par,
                    "trouble_rate": trouble_rate,
                    "fast_break_rate": fast_break_rate,
                    "same_track_win_rate": same_track_win_rate,
                    "trap_win_rate": trap_win_rate,
                    "trainer_win_rate": trainer_win_rate,
                    "days_since_last": days_since_last,
                    "field_size": field_size,
                }
            )
            summaries.append(
                {
                    "prior_runs": n_runs,
                    "recent_form": recent_form,
                    "win_rate_pct": round(win_rate_bayes * 100.0, 1),
                    "speed_vs_par": round(speed_vs_par, 3),
                    "sectional_vs_par": round(sectional_vs_par, 3),
                    "trouble_rate_pct": round(trouble_rate * 100.0, 1),
                    "fast_break_pct": round(fast_break_rate * 100.0, 1),
                    "trap_win_pct": round(trap_win_rate * 100.0, 1),
                    "trainer_win_pct": round(trainer_win_rate * 100.0, 1),
                }
            )

        # Relative-to-field statistics
        mean_speed = sum(v["speed_vs_par"] for v in raw_vectors) / len(raw_vectors)
        mean_wr = sum(v["win_rate_bayes"] for v in raw_vectors) / len(raw_vectors)
        mean_sec = sum(v["sectional_vs_par"] for v in raw_vectors) / len(raw_vectors)
        mean_ewm = sum(v["ewm_pos"] for v in raw_vectors) / len(raw_vectors)

        samples: list[RunnerSample] = []
        for r, v, summ in zip(runners, raw_vectors, summaries):
            v["rel_speed_field"] = v["speed_vs_par"] - mean_speed
            v["rel_win_rate_field"] = v["win_rate_bayes"] - mean_wr
            v["rel_sectional_field"] = v["sectional_vs_par"] - mean_sec
            v["rel_ewm_pos_field"] = v["ewm_pos"] - mean_ewm

            vec = np.array([v[name] for name in FEATURE_NAMES], dtype=np.float64)
            pos = r.get("result_position")
            samples.append(
                RunnerSample(
                    race_id=int(race_meta["race_id"]),
                    race_dt=race_dt_str,
                    iso_date=race_meta.get("iso_date") or "",
                    track_name=track_name,
                    race_number=int(race_meta.get("race_number") or 0),
                    trap_number=int(r.get("trap_number") or 0),
                    dog_id=r.get("dog_id"),
                    dog_name=r.get("dog_name") or f"Trap {r.get('trap_number')}",
                    decimal_odds=r.get("decimal_odds"),
                    result_position=pos,
                    is_winner=1 if pos == 1 else 0,
                    features=vec,
                    summary=summ,
                    race_distance=race_meta.get("race_distance"),
                    race_class=race_meta.get("race_class"),
                )
            )
        return samples


def build_chronological_dataset(db_path: Path) -> tuple[list[RunnerSample], RollingState]:
    """Build full zero-leakage chronological feature dataset from SQLite."""
    state = RollingState()
    samples: list[RunnerSample] = []

    with get_conn(db_path) as conn:
        hist_rows = conn.execute(
            """
            SELECT dog_id, race_id, race_dt, track_name, race_distance, trap_number,
                   result_position, adjusted_time, run_time, sectional_time, comment
            FROM dog_history_cache
            ORDER BY race_dt ASC, race_id ASC
            """
        ).fetchall()

        race_rows = conn.execute(
            """
            SELECT race_id, race_dt, iso_date, track_name, race_number,
                   race_distance, race_class, is_completed
            FROM races
            ORDER BY race_dt ASC, race_id ASC
            """
        ).fetchall()

        entry_rows = conn.execute(
            """
            SELECT race_id, trap_number, dog_id, dog_name, trainer_name,
                   decimal_odds, result_position, adjusted_time, run_time,
                   sectional_time, comment
            FROM entries
            ORDER BY race_id ASC, trap_number ASC
            """
        ).fetchall()

    entries_by_race: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for er in entry_rows:
        entries_by_race[int(er["race_id"])].append(dict(er))

    race_ids_in_races = {int(r["race_id"]) for r in race_rows}
    # Index cached dog history runs that happened BEFORE or OUTSIDE the races table
    # Wait: to preserve strict chronological order, merge historical runs by race_dt pointer!
    hist_list = [dict(hr) for hr in hist_rows if int(hr["race_id"]) not in race_ids_in_races]
    hist_idx = 0

    for rr in race_rows:
        race_meta = dict(rr)
        rid = int(race_meta["race_id"])
        r_dt = race_meta["race_dt"]

        # Advance external dog_history_cache pointer strictly before r_dt
        while hist_idx < len(hist_list) and hist_list[hist_idx]["race_dt"] < r_dt:
            h = hist_list[hist_idx]
            state.add_historical_run(
                dog_id=h.get("dog_id"),
                race_id=int(h["race_id"]),
                race_dt=h["race_dt"],
                track_name=h["track_name"],
                race_distance=h.get("race_distance"),
                trap_number=h.get("trap_number"),
                result_position=h.get("result_position"),
                adjusted_time=h.get("adjusted_time"),
                run_time=h.get("run_time"),
                sectional_time=h.get("sectional_time"),
                comment=h.get("comment"),
            )
            hist_idx += 1

        runners = entries_by_race.get(rid, [])
        if len(runners) < 2:
            continue

        # 1. Extract features BEFORE updating state with this race's outcome
        race_samples = state.extract_race_features(race_meta, runners)
        samples.extend(race_samples)

        # 2. Update state AFTER race completion
        if race_meta.get("is_completed"):
            for r in runners:
                state.add_historical_run(
                    dog_id=r.get("dog_id"),
                    race_id=rid,
                    race_dt=r_dt,
                    track_name=race_meta["track_name"],
                    race_distance=race_meta.get("race_distance"),
                    trap_number=r.get("trap_number"),
                    result_position=r.get("result_position"),
                    adjusted_time=r.get("adjusted_time"),
                    run_time=r.get("run_time"),
                    sectional_time=r.get("sectional_time"),
                    comment=r.get("comment"),
                    trainer_name=r.get("trainer_name"),
                )

    # Flush any remaining dog_history_cache rows for future predictions
    while hist_idx < len(hist_list):
        h = hist_list[hist_idx]
        state.add_historical_run(
            dog_id=h.get("dog_id"),
            race_id=int(h["race_id"]),
            race_dt=h["race_dt"],
            track_name=h["track_name"],
            race_distance=h.get("race_distance"),
            trap_number=h.get("trap_number"),
            result_position=h.get("result_position"),
            adjusted_time=h.get("adjusted_time"),
            run_time=h.get("run_time"),
            sectional_time=h.get("sectional_time"),
            comment=h.get("comment"),
        )
        hist_idx += 1

    return samples, state
