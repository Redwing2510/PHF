"""
fo_grade_loader.py

Computes per-player zone×strength faceoff grades from cached play-by-play.
Stores results in cache.db so season.py can replace the plain win-rate FO
grade with a data-derived zone-weighted version.

Weight derivation: net shot-differential swing per faceoff type relative to
ES neutral zone (= 1.0), computed from 289 tracked NHL games.
"""

import json
import sqlite3
from typing import Dict, Tuple, Optional

DB_PATH = 'cache.db'

# ---------------------------------------------------------------------------
# Data-derived delta weights  (symmetric: win = +delta, loss = -delta)
# Baseline: ES neutral zone = ±0.30
# ---------------------------------------------------------------------------
_FO_DELTA: Dict[Tuple[str, str], float] = {
    ('ES', 'O'): 0.74,
    ('ES', 'N'): 0.30,
    ('ES', 'D'): 0.74,
    ('PP', 'O'): 0.62,
    ('PP', 'N'): 0.13,
    ('PP', 'D'): 0.36,
    ('PK', 'O'): 0.36,
    ('PK', 'N'): 0.13,
    ('PK', 'D'): 0.62,
}

_CACHE: Optional[Dict[int, Dict[int, Tuple[float, int]]]] = None


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute('''
        CREATE TABLE IF NOT EXISTS fo_grades (
            game_id     INTEGER NOT NULL,
            player_id   INTEGER NOT NULL,
            weighted    REAL    NOT NULL,
            total_fo    INTEGER NOT NULL,
            PRIMARY KEY (game_id, player_id)
        )
    ''')
    conn.commit()


def _strength(situation: str, winner_id: int, home_id: int) -> str:
    if len(situation) < 4:
        return 'ES'
    away_sk = int(situation[1])
    home_sk = int(situation[2])
    if away_sk == home_sk:
        return 'ES'
    home_has_pp    = home_sk > away_sk
    winner_is_home = winner_id == home_id
    if home_has_pp:
        return 'PP' if winner_is_home else 'PK'
    return 'PP' if not winner_is_home else 'PK'


def _build_fo_grades(conn: sqlite3.Connection) -> None:
    """Parse PBP cache and populate fo_grades table for all uncached games."""
    cached_games = {r[0] for r in conn.execute('SELECT DISTINCT game_id FROM fo_grades').fetchall()}
    pbp_rows     = conn.execute('SELECT game_id, data FROM pbp').fetchall()
    missing      = [(gid, d) for gid, d in pbp_rows if gid not in cached_games]

    if not missing:
        return

    print(f'  Building zone-FO grades for {len(missing)} games...', flush=True)
    rows_to_insert = []

    for gid, data_str in missing:
        data  = json.loads(data_str)
        plays = data.get('plays', [])
        home_id = data.get('homeTeam', {}).get('id')
        if not home_id:
            continue

        # Build player_id -> team_id lookup from rosterSpots
        pid_team: Dict[int, int] = {}
        for spot in data.get('rosterSpots', []):
            pid_team[spot['playerId']] = spot['teamId']

        # Accumulate: player_id -> [weighted_sum, total_fo]
        acc: Dict[int, list] = {}

        for play in plays:
            if play.get('typeDescKey') != 'faceoff':
                continue
            det       = play.get('details', {})
            winner_id = det.get('winningPlayerId')
            loser_id  = det.get('losingPlayerId')
            zone      = det.get('zoneCode', 'N')
            owner_id  = det.get('eventOwnerTeamId')
            situation = play.get('situationCode', '1551')

            if zone not in ('O', 'N', 'D'):
                continue

            strength = _strength(situation, owner_id, home_id)

            for pid, is_win in ((winner_id, True), (loser_id, False)):
                if pid is None:
                    continue
                team_id = pid_team.get(pid, owner_id)

                # Zone is from eventOwnerTeamId (winner) perspective; flip for loser
                if is_win:
                    pzone = zone
                else:
                    pzone = {'O': 'D', 'D': 'O'}.get(zone, zone)

                delta = _FO_DELTA.get((strength, pzone), 0.30)
                sign  = 1 if is_win else -1

                if pid not in acc:
                    acc[pid] = [0.0, 0]
                acc[pid][0] += sign * delta
                acc[pid][1] += 1

        for pid, (ws, n) in acc.items():
            rows_to_insert.append((gid, pid, ws, n))

    if rows_to_insert:
        conn.executemany(
            'INSERT OR IGNORE INTO fo_grades (game_id, player_id, weighted, total_fo) VALUES (?,?,?,?)',
            rows_to_insert
        )
        conn.commit()
        print(f'  Stored FO grades for {len(missing)} games ({len(rows_to_insert)} player-game records).', flush=True)


def load_fo_grades() -> Dict[int, Dict[int, Tuple[float, int]]]:
    """
    Return {game_id: {player_id: (weighted_sum, total_fo)}} for all cached games.
    Builds the fo_grades table from PBP on first call if needed.
    """
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    conn = sqlite3.connect(DB_PATH)
    _ensure_table(conn)
    _build_fo_grades(conn)

    result: Dict[int, Dict[int, Tuple[float, int]]] = {}
    for game_id, player_id, weighted, total_fo in conn.execute(
        'SELECT game_id, player_id, weighted, total_fo FROM fo_grades'
    ).fetchall():
        result.setdefault(game_id, {})[player_id] = (weighted, total_fo)

    conn.close()
    _CACHE = result
    print(f'  FO grade cache loaded ({sum(len(v) for v in result.values())} player-game records across {len(result)} games).', flush=True)
    return _CACHE


def get_fo_grade(game_id: int, player_id: int) -> Optional[Tuple[float, int]]:
    """Return (weighted_sum, total_fo) for a player in a game, or None."""
    return load_fo_grades().get(game_id, {}).get(player_id)
