from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests

from app.config import settings
from app.db import get_conn


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def gbgb_date_to_iso(date_str: str) -> str:
    """Convert 'DD/MM/YYYY' to 'YYYY-MM-DD'."""
    date_str = (date_str or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        return date_str
    parts = date_str.split("/")
    if len(parts) == 3:
        d, m, y = parts
        return f"{y}-{m.zfill(2)}-{d.zfill(2)}"
    return date_str


def make_race_dt(iso_date: str, race_time: str) -> str:
    rt = (race_time or "00:00:00").strip()
    if len(rt) == 5:
        rt += ":00"
    return f"{iso_date} {rt}"


def safe_float(val: Any) -> float | None:
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def safe_int(val: Any) -> int | None:
    if val is None or val == "":
        return None
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return None


def parse_sp_to_decimal(
    sp_str: str | None,
    num: Any = None,
    den: Any = None,
) -> float | None:
    """Convert UK fractional SP (e.g. '5/2', '6/4F', 'EVS') to decimal odds."""
    n = safe_float(num)
    d = safe_float(den)
    if n is not None and d is not None and d > 0:
        return round(1.0 + n / d, 3)

    if not sp_str:
        return None
    cleaned = sp_str.strip().upper()
    cleaned = re.sub(r"[FJCT]+$", "", cleaned).strip()
    if cleaned in {"EVS", "EVENS", "1/1"}:
        return 2.0
    if "/" in cleaned:
        parts = cleaned.split("/", 1)
        a = safe_float(parts[0])
        b = safe_float(parts[1])
        if a is not None and b is not None and b > 0:
            return round(1.0 + a / b, 3)
    val = safe_float(cleaned)
    if val is not None and val >= 1.01:
        return round(val, 3)
    return None


class GBGBClient:
    """Polite client for public GBGB JSON endpoints."""

    def __init__(
        self,
        base_url: str = settings.gbgb_base_url,
        delay_sec: float = settings.request_delay_sec,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.delay_sec = delay_sec
        self._last_req_ts = 0.0
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": settings.user_agent,
                "Accept": "application/json",
            }
        )

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        elapsed = time.monotonic() - self._last_req_ts
        if elapsed < self.delay_sec:
            time.sleep(self.delay_sec - elapsed)
        url = f"{self.base_url}/{path.lstrip('/')}"
        resp = self.session.get(url, params=params, timeout=(10, 20))
        self._last_req_ts = time.monotonic()
        resp.raise_for_status()
        return resp.json()

    def fetch_meeting_ids_for_date(self, iso_date: str) -> list[dict[str, Any]]:
        """Return unique meetings (meetingId, trackName, meetingDate) for an ISO date."""
        meetings: dict[int, dict[str, Any]] = {}
        page = 1
        while page <= 20:
            payload = self._get_json(
                "results",
                params={
                    "page": page,
                    "itemsPerPage": 200,
                    "date": iso_date,
                    "race_type": "race",
                },
            )
            items = payload.get("items") or []
            if not items:
                break
            for item in items:
                mid = safe_int(item.get("meetingId"))
                if mid and mid not in meetings:
                    meetings[mid] = {
                        "meeting_id": mid,
                        "track_name": item.get("trackName") or "Unknown",
                        "meeting_date": item.get("raceDate") or iso_date,
                        "iso_date": gbgb_date_to_iso(item.get("raceDate") or iso_date),
                    }
            meta = payload.get("meta") or {}
            page_count = safe_int(meta.get("pageCount")) or 1
            if page >= page_count:
                break
            page += 1
        return list(meetings.values())

    def fetch_meeting_detail(self, meeting_id: int) -> dict[str, Any] | None:
        data = self._get_json(
            f"results/meeting/{meeting_id}",
            params={"meeting": meeting_id},
        )
        if isinstance(data, list) and data:
            return data[0]
        if isinstance(data, dict):
            return data
        return None

    def fetch_dog_history(self, dog_id: int, items_per_page: int = 30) -> list[dict[str, Any]]:
        payload = self._get_json(
            f"results/dog/{dog_id}",
            params={"page": 1, "itemsPerPage": items_per_page},
        )
        return payload.get("items") or []


def store_meeting_payload(db_path: Path, meeting_payload: dict[str, Any]) -> tuple[int, int]:
    """Persist one GBGB meeting payload into SQLite. Returns (races_saved, entries_saved)."""
    mid = safe_int(meeting_payload.get("meetingId"))
    if not mid:
        return 0, 0
    meeting_date = meeting_payload.get("meetingDate") or ""
    iso_date = gbgb_date_to_iso(meeting_date)
    track_name = meeting_payload.get("trackName") or "Unknown"
    now = utc_now_iso()

    races_saved = 0
    entries_saved = 0

    with get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO meetings (meeting_id, meeting_date, iso_date, track_name, fetched_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(meeting_id) DO UPDATE SET
                fetched_at = excluded.fetched_at,
                track_name = excluded.track_name
            """,
            (mid, meeting_date, iso_date, track_name, now),
        )

        for race in meeting_payload.get("races") or []:
            race_id = safe_int(race.get("raceId"))
            if not race_id:
                continue
            r_date = race.get("raceDate") or meeting_date
            r_iso = gbgb_date_to_iso(r_date)
            r_time = race.get("raceTime") or "00:00:00"
            race_dt = make_race_dt(r_iso, r_time)
            race_number = safe_int(race.get("raceNumber")) or 0
            traps = race.get("traps") or []
            valid_traps = [t for t in traps if safe_int(t.get("dogId")) or t.get("dogName")]
            # Count active runners (exclude explicit non-runners where resultPosition == 0 & NoRace)
            active_traps = [
                t
                for t in valid_traps
                if not (
                    safe_int(t.get("resultPosition")) == 0
                    and "NORACE" in (t.get("resultComment") or "").upper()
                )
            ]
            has_winner = any(safe_int(t.get("resultPosition")) == 1 for t in active_traps)

            conn.execute(
                """
                INSERT INTO races (
                    race_id, meeting_id, race_date, iso_date, race_time, race_dt,
                    track_name, race_number, race_title, race_type, race_class,
                    race_distance, race_going, race_handicap, race_forecast,
                    race_tricast, is_completed, active_runners
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(race_id) DO UPDATE SET
                    race_going = excluded.race_going,
                    race_forecast = excluded.race_forecast,
                    race_tricast = excluded.race_tricast,
                    is_completed = excluded.is_completed,
                    active_runners = excluded.active_runners
                """,
                (
                    race_id,
                    mid,
                    r_date,
                    r_iso,
                    r_time,
                    race_dt,
                    track_name,
                    race_number,
                    race.get("raceTitle"),
                    race.get("raceType"),
                    race.get("raceClass"),
                    safe_float(race.get("raceDistance")),
                    safe_float(race.get("raceGoing")),
                    1 if race.get("raceHandicap") else 0,
                    race.get("raceForecast"),
                    race.get("raceTricast"),
                    1 if has_winner else 0,
                    len(active_traps),
                ),
            )
            races_saved += 1

            for t in valid_traps:
                trap_num = safe_int(t.get("trapNumber"))
                if not trap_num:
                    continue
                pos = safe_int(t.get("resultPosition"))
                comment = t.get("resultComment") or ""
                if pos == 0 and "NORACE" in comment.upper():
                    continue
                dec_odds = parse_sp_to_decimal(
                    t.get("SP"),
                    t.get("resultPriceNumerator"),
                    t.get("resultPriceDenominator"),
                )
                conn.execute(
                    """
                    INSERT INTO entries (
                        race_id, trap_number, dog_id, dog_name, dog_sire, dog_dam,
                        dog_born, dog_sex, trainer_name, sp_str, decimal_odds,
                        market_pos, result_position, is_winner, btn_distance,
                        sectional_time, run_time, adjusted_time, dog_weight, comment
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(race_id, trap_number) DO UPDATE SET
                        dog_id = excluded.dog_id,
                        dog_name = excluded.dog_name,
                        sp_str = excluded.sp_str,
                        decimal_odds = excluded.decimal_odds,
                        market_pos = excluded.market_pos,
                        result_position = excluded.result_position,
                        is_winner = excluded.is_winner,
                        btn_distance = excluded.btn_distance,
                        sectional_time = excluded.sectional_time,
                        run_time = excluded.run_time,
                        adjusted_time = excluded.adjusted_time,
                        dog_weight = excluded.dog_weight,
                        comment = excluded.comment
                    """,
                    (
                        race_id,
                        trap_num,
                        safe_int(t.get("dogId")),
                        t.get("dogName") or f"Trap {trap_num}",
                        t.get("dogSire"),
                        t.get("dogDam"),
                        t.get("dogBorn"),
                        t.get("dogSex"),
                        t.get("trainerName"),
                        t.get("SP"),
                        dec_odds,
                        safe_int(t.get("resultMarketPos")),
                        pos,
                        1 if pos == 1 else 0,
                        t.get("resultBtnDistance"),
                        safe_float(t.get("resultSectionalTime")),
                        safe_float(t.get("resultRunTime")),
                        safe_float(t.get("resultAdjustedTime")),
                        safe_float(t.get("resultDogWeight")),
                        comment,
                    ),
                )
                entries_saved += 1

    return races_saved, entries_saved


def store_dog_history(db_path: Path, dog_id: int, items: list[dict[str, Any]]) -> int:
    saved = 0
    with get_conn(db_path) as conn:
        for item in items:
            race_id = safe_int(item.get("raceId"))
            if not race_id:
                continue
            pos = safe_int(item.get("resultPosition"))
            comment = item.get("resultComment") or ""
            if pos == 0 and "NORACE" in comment.upper():
                continue
            iso_date = gbgb_date_to_iso(item.get("raceDate") or "")
            race_dt = make_race_dt(iso_date, item.get("raceTime") or "00:00:00")
            conn.execute(
                """
                INSERT INTO dog_history_cache (
                    dog_id, race_id, race_dt, iso_date, track_name, race_class,
                    race_distance, race_going, trap_number, result_position,
                    sectional_time, run_time, adjusted_time, race_win_time,
                    dog_weight, decimal_odds, comment
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(dog_id, race_id) DO UPDATE SET
                    result_position = excluded.result_position,
                    adjusted_time = excluded.adjusted_time,
                    sectional_time = excluded.sectional_time,
                    comment = excluded.comment
                """,
                (
                    dog_id,
                    race_id,
                    race_dt,
                    iso_date,
                    item.get("trackName") or "Unknown",
                    item.get("raceClass"),
                    safe_float(item.get("raceDistance")),
                    safe_float(item.get("raceGoing")),
                    safe_int(item.get("trapNumber")),
                    pos,
                    safe_float(item.get("resultSectionalTime")),
                    safe_float(item.get("resultRunTime")),
                    safe_float(item.get("resultAdjustedTime")),
                    safe_float(item.get("raceWinTime")),
                    safe_float(item.get("resultDogWeight")),
                    parse_sp_to_decimal(item.get("SP")),
                    comment,
                ),
            )
            saved += 1
    return saved


def ingest_date_from_gbgb(
    db_path: Path,
    client: GBGBClient,
    iso_date: str,
    skip_existing_meetings: bool = True,
    progress_cb: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """Fetch all meetings and races for a given ISO date from GBGB."""
    meetings = client.fetch_meeting_ids_for_date(iso_date)
    with get_conn(db_path) as conn:
        existing_rows = conn.execute(
            "SELECT meeting_id FROM meetings WHERE iso_date = ?", (iso_date,)
        ).fetchall()
        existing_ids = {int(r["meeting_id"]) for r in existing_rows}

    total_races = 0
    total_entries = 0
    fetched_meetings = 0

    for m in meetings:
        mid = m["meeting_id"]
        if skip_existing_meetings and mid in existing_ids:
            continue
        if progress_cb:
            progress_cb(f"Coletando reunião {m['track_name']} ({iso_date})...")
        detail = client.fetch_meeting_detail(mid)
        if detail:
            r_cnt, e_cnt = store_meeting_payload(db_path, detail)
            total_races += r_cnt
            total_entries += e_cnt
            fetched_meetings += 1

    return {
        "meetings_found": len(meetings),
        "meetings_fetched": fetched_meetings,
        "races_saved": total_races,
        "entries_saved": total_entries,
    }


def enrich_dogs_for_date(
    db_path: Path,
    client: GBGBClient,
    iso_date: str,
    max_dogs: int = 80,
    progress_cb: Callable[[str], None] | None = None,
) -> int:
    """Fetch individual past runs for dogs racing on `iso_date` if not cached yet."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT e.dog_id, e.dog_name, MIN(r.is_completed) AS completed_flag
            FROM entries e
            JOIN races r ON r.race_id = e.race_id
            WHERE r.iso_date = ?
              AND e.dog_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM dog_history_cache dh
                  WHERE dh.dog_id = e.dog_id
                  GROUP BY dh.dog_id
                  HAVING COUNT(*) >= 3
              )
            GROUP BY e.dog_id, e.dog_name
            ORDER BY completed_flag ASC, e.dog_name ASC
            LIMIT ?
            """,
            (iso_date, max_dogs),
        ).fetchall()

    enriched = 0
    for idx, row in enumerate(rows, start=1):
        dog_id = int(row["dog_id"])
        try:
            items = client.fetch_dog_history(dog_id, items_per_page=25)
            store_dog_history(db_path, dog_id, items)
            enriched += 1
            if progress_cb and idx % 10 == 0:
                progress_cb(f"Histórico detalhado enriquecido: {idx}/{len(rows)} galgos ({row['dog_name']})")
        except Exception:
            continue
    return enriched
