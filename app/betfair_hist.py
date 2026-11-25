from __future__ import annotations

import bz2
import json
import re
import tarfile
from pathlib import Path
from typing import Any, Iterable

from app.db import get_conn
from app.lay_engine import snap_to_exchange_tick


TRAP_PREFIX_RE = re.compile(r"^\d+\.\s*")


def normalize_dog_name(name: str) -> str:
    cleaned = TRAP_PREFIX_RE.sub("", name or "").strip().lower()
    return re.sub(r"[^a-z0-9]", "", cleaned)


def _iter_stream_lines_from_file(path: Path) -> Iterable[str]:
    lower = path.name.lower()
    if lower.endswith((".tar.bz2", ".tbz2", ".tar")):
        mode = "r:bz2" if lower.endswith(("bz2", "tbz2")) else "r:"
        with tarfile.open(path, mode) as tf:
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                f = tf.extractfile(member)
                if f is None:
                    continue
                data = f.read()
                if member.name.lower().endswith(".bz2"):
                    data = bz2.decompress(data)
                for line in data.decode("utf-8", errors="ignore").splitlines():
                    if line.strip():
                        yield line
    elif lower.endswith(".bz2"):
        with bz2.open(path, "rt", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.strip():
                    yield line
    elif lower.endswith((".json", ".jsonl")):
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.strip():
                    yield line


def parse_betfair_stream_lines(lines: Iterable[str]) -> list[dict[str, Any]]:
    """
    Parse Betfair Stream API market snapshots (Basic/Advanced/Pro) and extract
    pre-off prices for Greyhound WIN markets (eventTypeId == '4339').
    """
    markets: dict[str, dict[str, Any]] = {}

    for raw_line in lines:
        try:
            msg = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        for mc in msg.get("mc") or []:
            mid = mc.get("id")
            if not mid:
                continue
            state = markets.setdefault(
                mid,
                {
                    "market_id": mid,
                    "event_type_id": None,
                    "market_type": None,
                    "venue": None,
                    "market_time": None,
                    "runners": {},
                    "pre_off_ltp": {},
                    "pre_off_lay": {},
                    "in_play": False,
                },
            )

            mdef = mc.get("marketDefinition")
            if mdef:
                state["event_type_id"] = str(mdef.get("eventTypeId") or state["event_type_id"] or "")
                state["market_type"] = mdef.get("marketType") or state["market_type"]
                state["venue"] = mdef.get("venue") or state["venue"]
                state["market_time"] = mdef.get("marketTime") or state["market_time"]
                if mdef.get("inPlay"):
                    state["in_play"] = True
                for r in mdef.get("runners") or []:
                    sid = r.get("id")
                    if sid is not None:
                        state["runners"][sid] = r.get("name") or ""

            if state["in_play"]:
                continue

            for rc in mc.get("rc") or []:
                sid = rc.get("id")
                if sid is None:
                    continue
                if "ltp" in rc and rc["ltp"] is not None:
                    state["pre_off_ltp"][sid] = float(rc["ltp"])
                # Advanced/Pro plans include batl (best available to lay)
                batl = rc.get("batl")
                if isinstance(batl, list) and batl:
                    best = batl[0]
                    if isinstance(best, list) and len(best) >= 2 and best[1]:
                        state["pre_off_lay"][sid] = float(best[1])

    extracted: list[dict[str, Any]] = []
    for m in markets.values():
        if m["event_type_id"] != "4339" or m["market_type"] != "WIN":
            continue
        if not m["market_time"] or not m["runners"]:
            continue
        iso_date = str(m["market_time"])[:10]
        venue = (m["venue"] or "").strip()
        for sid, rname in m["runners"].items():
            lay_price = m["pre_off_lay"].get(sid)
            if lay_price is None and sid in m["pre_off_ltp"]:
                # Basic plan has LTP 1 min pre-off: add 1 conservative Lay tick
                lay_price = snap_to_exchange_tick(m["pre_off_ltp"][sid], extra_ticks=1)
            if lay_price is None or lay_price < 1.01:
                continue
            extracted.append(
                {
                    "iso_date": iso_date,
                    "venue": venue.lower(),
                    "dog_norm": normalize_dog_name(rname),
                    "lay_price": round(float(lay_price), 2),
                }
            )
    return extracted


def import_betfair_directory(db_path: Path, hist_dir: Path) -> dict[str, int]:
    """Scan hist_dir for Betfair historical files and match Exchange Lay odds to DB entries."""
    if not hist_dir.exists():
        return {"files_scanned": 0, "prices_parsed": 0, "entries_updated": 0}

    files = [
        p
        for p in sorted(hist_dir.rglob("*"))
        if p.is_file() and p.name.lower().endswith((".tar.bz2", ".tbz2", ".tar", ".bz2", ".json", ".jsonl"))
    ]
    if not files:
        return {"files_scanned": 0, "prices_parsed": 0, "entries_updated": 0}

    all_quotes: list[dict[str, Any]] = []
    for f in files:
        all_quotes.extend(parse_betfair_stream_lines(_iter_stream_lines_from_file(f)))

    if not all_quotes:
        return {"files_scanned": len(files), "prices_parsed": 0, "entries_updated": 0}

    lookup: dict[tuple[str, str], float] = {}
    for q in all_quotes:
        lookup[(q["iso_date"], q["dog_norm"])] = q["lay_price"]

    updated = 0
    with get_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT e.id, r.iso_date, e.dog_name
            FROM entries e
            JOIN races r ON r.race_id = e.race_id
            """
        ).fetchall()
        updates: list[tuple[float, int]] = []
        for row in rows:
            price = lookup.get((row["iso_date"], normalize_dog_name(row["dog_name"])))
            if price is not None:
                updates.append((price, int(row["id"])))
        if updates:
            conn.executemany(
                "UPDATE entries SET exchange_lay_odds = ? WHERE id = ?",
                updates,
            )
            updated = len(updates)

    return {
        "files_scanned": len(files),
        "prices_parsed": len(all_quotes),
        "entries_updated": updated,
    }
