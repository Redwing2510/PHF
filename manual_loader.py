"""
manual_loader.py

Reads AllThreeZones-style xlsx game logs from 'Manual Game Logs/' and produces
PFF-style -2 to +2 microstat grades per player per game.

Grades are computed by:
  1. Per-60 normalizing each raw count stat
  2. Z-scoring each metric relative to the same position group (F or D)
     across ALL tracked games in the dataset
  3. Weighting z-scores into 5 category grades: Offense, Entries, Exits,
     Entry Defense, Forechecking
  4. Weighting categories (F/D have different weights) into an overall grade
  5. Clamping all grades to [-2, +2] and converting to 0-100 for display
Should
Usage:
    from manual_loader import get_microstat_grade
    ms = get_microstat_grade(game_id=2025030131, name="Sebastian Aho")
    if ms:
        print(ms.overall, ms.offense, ms.entries)  # -2 to +2
        print(ms.overall_100)                        # 0-100

Call invalidate_cache() after dropping new xlsx files into Manual Game Logs/.
"""

import os
import re
import pickle
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import openpyxl
    _OPENPYXL_AVAILABLE = True
except ImportError:
    _OPENPYXL_AVAILABLE = False

LOGS_DIR   = Path(__file__).parent / "Manual Game Logs"
_CACHE_FILE = Path(__file__).parent / ".ms_grades_cache.pkl"

# ---------------------------------------------------------------------------
# Column indices in the Player List sheet (0-indexed)
# ---------------------------------------------------------------------------
C_JERSEY    = 0
C_NAME      = 1
C_TEAM      = 2
C_POS       = 3
C_TOI       = 4   # 5v5 TOI in minutes
C_YEAR      = 5
C_GAME      = 6
C_SHOTS     = 7   # Shot attempts
C_SOG       = 8   # Shots On Goal
C_CHANCES   = 9   # Scoring Chances
# C_PASSES  = 10
C_PA1       = 11  # Primary Shot Assists
# C_PA2     = 12
# C_PA3     = 13
C_CA        = 14  # Chance Assists
# C_HP      = 15
# ...
C_ENTRIES   = 28  # Zone Entries (total)
C_CARRIES   = 29  # Carries (controlled entries)
C_FAILED_E  = 30  # Failed Entry attempts
# C_PASS_E  = 31
# C_RECOVERIES = 32
C_CWC       = 33  # Carries with Scoring Chances
C_DIC       = 34  # Dump-in Chances
C_FC_PRESS  = 35  # Forecheck Pressures
# C_DZ_TOUCH = 36
C_DZ_RET    = 37  # DZ Retrievals
C_EXITS     = 38  # Zone Exits (total)
C_EWP       = 39  # Exits with Possession
# C_CARRY_EXIT = 40
# C_PASS_EXIT  = 41
# C_CLEARS     = 42
# C_MISSED_PASS = 43
C_RET_EXIT  = 44  # Retrievals Leading to Exits
C_BOTCH     = 45  # Botched Retrievals (negative)
C_EXCHANGE  = 46  # Exchanges (contested entries, partial denial credit)
C_FAIL_EXIT = 47  # Failed Exits (negative)
C_CARRY_EXIT = 40  # Carry-outs (controlled exits by skating)
C_PASS_EXIT  = 41  # Pass-exits (controlled exits by passing)
C_CLEARS     = 42  # Clears (dump-outs, any exit under pressure)
# C_RUSHED    = 48
# C_2ND_TOUCH = 49
C_TARGETS   = 50  # Entry Targets (entries against)
# C_CARRIES_AG = 51
C_DENIALS   = 52  # Carry Denials
C_PASSES_AG = 53  # Passes Allowed (entries against)
C_CCA       = 54  # Carries with Chance Against (negative)
C_DICA      = 55  # Dump-in with Chance Against (negative)
C_PK_DENY   = 60  # 4v5 Carry Denials (PK entry defense)


@dataclass
class MicrostatRecord:
    """Raw per-player stats extracted from one game log xlsx."""
    game_file_id: int         # short ID from filename, e.g. 30131
    name: str                 # normalized lowercase with spaces
    team: str
    position: str             # 'F' or 'D'
    toi_min: float            # 5v5 TOI in minutes

    # Offense
    shots: int = 0
    sog: int = 0
    chances: int = 0
    primary_assists: int = 0
    chance_assists: int = 0

    # Zone entries
    zone_entries: int = 0
    carries: int = 0
    failed_entries: int = 0
    carries_w_chances: int = 0
    dump_in_chances: int = 0

    # Zone exits
    zone_exits: int = 0
    exits_w_possession: int = 0
    carry_exits: int = 0
    pass_exits: int = 0
    clears: int = 0
    retrievals_leading_to_exits: int = 0
    botched_retrievals: int = 0
    failed_exits: int = 0

    # Entry defense
    targets: int = 0
    denials: int = 0
    pk_denials: int = 0
    exchanges: int = 0
    passes_allowed: int = 0
    carries_chance_against: int = 0
    dump_in_chance_against: int = 0

    # Forechecking
    fc_pressures: int = 0
    dz_retrievals: int = 0


@dataclass
class MicrostatGrade:
    """PFF-style grades for a single player in a single game."""
    # -2 to +2 category grades
    offense: float = 0.0
    entries: float = 0.0
    exits: float = 0.0
    entry_defense: float = 0.0
    forechecking: float = 0.0
    overall: float = 0.0

    # 0-100 equivalents for display alongside API grades
    overall_100: float = 50.0
    offense_100: float = 50.0
    entries_100: float = 50.0
    exits_100: float = 50.0
    defense_100: float = 50.0
    forechecking_100: float = 50.0

    position: str = 'F'
    toi_min: float = 0.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _norm_name(name: str) -> str:
    """Normalize player name: strip non-breaking spaces, lowercase."""
    return name.replace('\xa0', ' ').strip().lower()


def _safe_int(v) -> int:
    try:
        return int(v) if v is not None else 0
    except (TypeError, ValueError):
        return 0


def _safe_float(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _pos_group(pos: str) -> str:
    if pos in ('D',):
        return 'D'
    return 'F'


def _per60(count: int, toi_min: float) -> float:
    if toi_min <= 0:
        return 0.0
    return count / toi_min * 60.0


def _rate(num: int, denom: int) -> float:
    if denom <= 0:
        return 0.5  # regress to 50% when no opportunity
    return num / denom


# Cache mean/std per pool list (keyed by list id) for the duration of _compute_grades.
# This avoids re-scanning the same pool thousands of times.
_POOL_STATS_CACHE: dict = {}


def _pool_stats(vals: list):
    """Return (mean, std) for vals, memoised by list identity."""
    k = id(vals)
    if k not in _POOL_STATS_CACHE:
        n = len(vals)
        mean = sum(vals) / n
        var = sum((x - mean) ** 2 for x in vals) / (n - 1)
        _POOL_STATS_CACHE[k] = (mean, math.sqrt(var) if var > 1e-18 else None)
    return _POOL_STATS_CACHE[k]


def _zscore(value: float, vals: list) -> float:
    if len(vals) < 3:
        return 0.0
    mean, std = _pool_stats(vals)
    if std is None:
        return 0.0
    return (value - mean) / std


def _clamp(z: float) -> float:
    """Clamp z-score to -2 … +2."""
    return max(-2.0, min(2.0, z))


def _to_100(grade: float) -> float:
    """Convert -2…+2 grade to 0–100."""
    return round((grade + 2.0) / 4.0 * 100.0, 1)


# ---------------------------------------------------------------------------
# xlsx loading
# ---------------------------------------------------------------------------

def _load_records_from_file(path: Path, file_game_id: int) -> list:
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    if 'Player List' not in wb.sheetnames:
        return []
    ws = wb['Player List']
    records = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        name = row[C_NAME]
        pos  = row[C_POS]
        toi  = row[C_TOI]

        if not name or not isinstance(name, str):
            continue
        if not pos or pos == 'G':
            continue
        toi_f = _safe_float(toi)
        if toi_f <= 0:
            continue

        rec = MicrostatRecord(
            game_file_id=file_game_id,
            name=_norm_name(name),
            team=str(row[C_TEAM]) if row[C_TEAM] else '',
            position=_pos_group(str(pos)),
            toi_min=toi_f,
            shots=_safe_int(row[C_SHOTS]),
            sog=_safe_int(row[C_SOG]),
            chances=_safe_int(row[C_CHANCES]),
            primary_assists=_safe_int(row[C_PA1]),
            chance_assists=_safe_int(row[C_CA]),
            zone_entries=_safe_int(row[C_ENTRIES]),
            carries=_safe_int(row[C_CARRIES]),
            failed_entries=_safe_int(row[C_FAILED_E]),
            carries_w_chances=_safe_int(row[C_CWC]),
            dump_in_chances=_safe_int(row[C_DIC]),
            zone_exits=_safe_int(row[C_EXITS]),
            exits_w_possession=_safe_int(row[C_EWP]),
            carry_exits=_safe_int(row[C_CARRY_EXIT]),
            pass_exits=_safe_int(row[C_PASS_EXIT]),
            clears=_safe_int(row[C_CLEARS]),
            retrievals_leading_to_exits=_safe_int(row[C_RET_EXIT]),
            botched_retrievals=_safe_int(row[C_BOTCH]),
            failed_exits=_safe_int(row[C_FAIL_EXIT]),
            targets=_safe_int(row[C_TARGETS]),
            denials=_safe_int(row[C_DENIALS]),
            pk_denials=_safe_int(row[C_PK_DENY]),
            exchanges=_safe_int(row[C_EXCHANGE]),
            passes_allowed=_safe_int(row[C_PASSES_AG]),
            carries_chance_against=_safe_int(row[C_CCA]),
            dump_in_chance_against=_safe_int(row[C_DICA]),
            fc_pressures=_safe_int(row[C_FC_PRESS]),
            dz_retrievals=_safe_int(row[C_DZ_RET]),
        )
        records.append(rec)
    return records


def _load_all_records() -> list:
    if not _OPENPYXL_AVAILABLE or not LOGS_DIR.exists():
        return []
    all_records = []
    files = sorted(LOGS_DIR.rglob('*.xlsx'))
    total = len(files)
    for i, fname in enumerate(files, 1):
        m = re.match(r'^(\d+)', fname.name)
        if not m:
            continue
        file_game_id = int(m.group(1))
        try:
            recs = _load_records_from_file(fname, file_game_id)
            all_records.extend(recs)
        except Exception:
            pass
        if i % 25 == 0 or i == total:
            print(f"  Loading xlsx files... {i}/{total}", flush=True)
    return all_records


# ---------------------------------------------------------------------------
# Grade engine
# ---------------------------------------------------------------------------

# Category weights per position group
# Must sum to 1.0 for each position
_CAT_WEIGHTS = {
    'F': {
        'offense':      0.45,
        'entries':      0.20,
        'defense':      0.35,
    },
    'D': {
        'offense':       0.20,
        'entries':       0.15,
        'exits':         0.30,
        'entry_defense': 0.35,
    },
}


def _compute_grades(records: list) -> dict:
    """
    Grade all records relative to position-group baselines.
    Returns dict keyed by (game_file_id, name_normalized) -> MicrostatGrade.
    """
    _POOL_STATS_CACHE.clear()
    fwd_recs = [r for r in records if r.position == 'F']
    def_recs = [r for r in records if r.position == 'D']

    result = {}

    for pos_recs in [fwd_recs, def_recs]:
        if not pos_recs:
            continue

        pos = pos_recs[0].position

        # Build per-60 and rate vectors for the entire position pool
        shots_p60   = [_per60(r.shots,             r.toi_min) for r in pos_recs]
        chances_p60 = [_per60(r.chances,           r.toi_min) for r in pos_recs]
        pa1_p60     = [_per60(r.primary_assists,   r.toi_min) for r in pos_recs]
        ca_p60      = [_per60(r.chance_assists,    r.toi_min) for r in pos_recs]

        carry_p60   = [_per60(r.carries,           r.toi_min) for r in pos_recs]
        entry_eff   = [_rate(r.carries, r.carries + r.failed_entries) for r in pos_recs]
        cwc_p60     = [_per60(r.carries_w_chances, r.toi_min) for r in pos_recs]

        exit_ctrl      = [_rate(r.exits_w_possession, r.zone_exits) for r in pos_recs]
        exit_p60       = [_per60(r.zone_exits,         r.toi_min) for r in pos_recs]
        carry_exit_p60 = [_per60(r.carry_exits,         r.toi_min) for r in pos_recs]
        pass_exit_p60  = [_per60(r.pass_exits,          r.toi_min) for r in pos_recs]
        clears_p60     = [_per60(r.clears,              r.toi_min) for r in pos_recs]
        ret_exit_p60   = [_per60(r.retrievals_leading_to_exits, r.toi_min) for r in pos_recs]
        botch_p60      = [_per60(r.botched_retrievals,  r.toi_min) for r in pos_recs]

        denial_p60  = [_per60(r.denials, r.toi_min) for r in pos_recs]
        denial_rate = [_rate(r.denials, r.targets) for r in pos_recs]
        pkdeny_p60  = [_per60(r.pk_denials,  r.toi_min) for r in pos_recs]
        exchange_p60= [_per60(r.exchanges,   r.toi_min) for r in pos_recs]
        cca_p60     = [_per60(r.carries_chance_against, r.toi_min) for r in pos_recs]
        dica_p60    = [_per60(r.dump_in_chance_against, r.toi_min) for r in pos_recs]

        fc_p60      = [_per60(r.fc_pressures,  r.toi_min) for r in pos_recs]
        dzret_p60   = [_per60(r.dz_retrievals, r.toi_min) for r in pos_recs]

        cw = _CAT_WEIGHTS[pos]

        for i, r in enumerate(pos_recs):
            # ── Offense ──────────────────────────────────────────────────
            if pos == 'D':
                # D-men: FC pressures count as offensive contribution
                z_off = (
                    0.30 * _zscore(chances_p60[i], chances_p60) +
                    0.20 * _zscore(shots_p60[i],   shots_p60)   +
                    0.25 * _zscore(pa1_p60[i],     pa1_p60)     +
                    0.15 * _zscore(ca_p60[i],      ca_p60)      +
                    0.10 * _zscore(fc_p60[i],      fc_p60)
                )
            else:
                # Forwards: FC pressures count as offensive contribution
                z_off = (
                    0.30 * _zscore(chances_p60[i], chances_p60) +
                    0.22 * _zscore(shots_p60[i],   shots_p60)   +
                    0.23 * _zscore(pa1_p60[i],     pa1_p60)     +
                    0.15 * _zscore(ca_p60[i],      ca_p60)      +
                    0.10 * _zscore(fc_p60[i],      fc_p60)
                )
            off_g = _clamp(z_off)

            # ── Zone Entries ──────────────────────────────────────────────
            # Efficiency (not getting stuffed) + volume + danger
            z_ent = (
                0.40 * _zscore(entry_eff[i],   entry_eff)   +
                0.35 * _zscore(carry_p60[i],   carry_p60)   +
                0.25 * _zscore(cwc_p60[i],     cwc_p60)
            )
            ent_g = _clamp(z_ent)

            # ── Zone Exits ───────────────────────────────────────────────
            # Emphasise rate/quality over volume — OZ-heavy players have fewer
            # exit opportunities so per-60 volume metrics unfairly penalise them.
            z_ext = (
                0.25 * _zscore(carry_exit_p60[i], carry_exit_p60) +
                0.35 * _zscore(exit_ctrl[i],      exit_ctrl)      +
                0.15 * _zscore(pass_exit_p60[i],  pass_exit_p60)  +
                0.10 * _zscore(ret_exit_p60[i],   ret_exit_p60)   +
                0.05 * _zscore(clears_p60[i],     clears_p60)     -
                0.10 * _zscore(botch_p60[i],      botch_p60)
            )
            ext_g = _clamp(z_ext)

            # ── Entry Defense ─────────────────────────────────────────────
            # Denials (5v5 + PK) + exchanges (partial denial credit) +
            # denial rate - dangerous carries allowed
            all_denials_p60 = denial_p60[i] + pkdeny_p60[i]
            all_denials_pool = [d + pk for d, pk in zip(denial_p60, pkdeny_p60)]
            # Clamp CCA/DICA z-scores before combining — their per-60 rates are
            # extremely sparse (pool mean ~0.1), so one event in short ice time
            # produces z-scores of 8-10 that would dominate the entire grade.
            z_cca_c  = _clamp(_zscore(cca_p60[i],  cca_p60))
            z_dica_c = _clamp(_zscore(dica_p60[i], dica_p60))
            # Shift weight from volume penalties toward rate metrics —
            # OZ-heavy players face fewer entries so each CCA carries
            # disproportionate weight without this adjustment.
            if pos == 'D':
                # D-men: DZ retrievals count as defensive contribution
                z_def = (
                    0.25 * _zscore(denial_rate[i],   denial_rate)      +
                    0.20 * _zscore(all_denials_p60,  all_denials_pool) +
                    0.15 * _zscore(exchange_p60[i],  exchange_p60)     +
                    0.20 * _zscore(dzret_p60[i],     dzret_p60)        -
                    0.15 * z_cca_c                                      -
                    0.05 * z_dica_c
                )
            else:
                z_def = (
                    0.30 * _zscore(denial_rate[i],   denial_rate)      +
                    0.25 * _zscore(all_denials_p60,  all_denials_pool) +
                    0.15 * _zscore(exchange_p60[i],  exchange_p60)     -
                    0.20 * z_cca_c                                      -
                    0.10 * z_dica_c
                )
            def_g = _clamp(z_def)

            # ── Forechecking ──────────────────────────────────────────────
            z_fc = (
                0.50 * _zscore(fc_p60[i],    fc_p60)    +
                0.50 * _zscore(dzret_p60[i], dzret_p60)
            )
            fc_g = _clamp(z_fc)

            # ── Overall ───────────────────────────────────────────────────
            if pos == 'F':
                # Exits (75%) + entry defense (15%) + DZ retrievals (10%)
                dzret_g = _clamp(_zscore(dzret_p60[i], dzret_p60))
                combined_def_g = _clamp(0.75 * ext_g + 0.15 * def_g + 0.10 * dzret_g)
                overall_g = (
                    cw['offense']  * off_g          +
                    cw['entries']  * ent_g          +
                    cw['defense']  * combined_def_g
                )
                display_def_g = combined_def_g
            else:
                overall_g = (
                    cw['offense']       * off_g +
                    cw['entries']       * ent_g +
                    cw['exits']         * ext_g +
                    cw['entry_defense'] * def_g
                )
                display_def_g = def_g
            overall_g = _clamp(overall_g)

            grade = MicrostatGrade(
                offense=round(off_g,          2),
                entries=round(ent_g,          2),
                exits=round(ext_g,            2),
                entry_defense=round(def_g,    2),
                forechecking=round(fc_g,      2),
                overall=round(overall_g,      2),
                overall_100=_to_100(overall_g),
                offense_100=_to_100(off_g),
                entries_100=_to_100(ent_g),
                exits_100=_to_100(ext_g),
                defense_100=_to_100(display_def_g),
                forechecking_100=_to_100(fc_g),
                position=r.position,
                toi_min=r.toi_min,
            )
            result[(r.game_file_id, r.name)] = grade

    return result


def _xlsx_fingerprint() -> float:
    """Return the most recent mtime across all xlsx files."""
    if not LOGS_DIR.exists():
        return 0.0
    mtimes = [f.stat().st_mtime for f in LOGS_DIR.rglob('*.xlsx')]
    return max(mtimes) if mtimes else 0.0


# ---------------------------------------------------------------------------
# Module-level cache (loaded once per server process)
# ---------------------------------------------------------------------------
_CACHE: Optional[dict] = None


def load_microstat_grades() -> dict:
    """
    Load and grade all xlsx files.
    Results are persisted to a pickle cache on disk so subsequent restarts
    are near-instant. The cache is invalidated whenever any xlsx file is
    newer than the cache file.
    Returns dict keyed by (game_file_id: int, name_normalized: str).
    """
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    # Check if disk cache is still valid
    if _CACHE_FILE.exists():
        cache_mtime = _CACHE_FILE.stat().st_mtime
        xlsx_mtime  = _xlsx_fingerprint()
        if xlsx_mtime <= cache_mtime:
            print("  Loading microstat grades from cache...", flush=True)
            with open(_CACHE_FILE, 'rb') as f:
                _CACHE = pickle.load(f)
            print(f"  Cache loaded ({len(_CACHE)} player-game records).", flush=True)
            return _CACHE

    # Cache miss — parse all xlsx files
    records = _load_all_records()
    _CACHE = _compute_grades(records)

    # Save to disk
    with open(_CACHE_FILE, 'wb') as f:
        pickle.dump(_CACHE, f)
    print(f"  Microstat cache saved ({len(_CACHE)} player-game records).", flush=True)
    return _CACHE


def get_microstat_grade(game_id: int, name: str) -> Optional[MicrostatGrade]:
    """
    Look up a microstat grade for a player in a game.

    game_id: full NHL game ID (e.g. 2025030131)
    name:    player name (any casing, non-breaking spaces OK)
    """
    grades = load_microstat_grades()
    norm = _norm_name(name)
    # The xlsx filename uses the last 5 digits of the NHL game ID
    # e.g. 2025030131 → 30131
    short = int(str(game_id)[-5:])
    return grades.get((short, norm))


def invalidate_cache() -> None:
    """Call this after adding new xlsx files so grades are recomputed."""
    global _CACHE
    _CACHE = None
    if _CACHE_FILE.exists():
        _CACHE_FILE.unlink()
