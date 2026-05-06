"""
qoc_loader.py

Computes Quality of Competition (QoC) using shift chart overlap.
For each player-game, records how many seconds they shared ice against each opponent.
Stores results in cache.db so season.py can apply a QoC adjustment to overall grades.
"""
from __future__ import annotations
import sqlite3
import time
from collections import defaultdict
from typing import Dict, List

from loader import fetch_shifts

DB_PATH = 'cache.db'


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute('''
        CREATE TABLE IF NOT EXISTS matchup_time (
            game_id     INTEGER NOT NULL,
            player_id   INTEGER NOT NULL,
            opponent_id INTEGER NOT NULL,
            seconds     INTEGER NOT NULL,
            PRIMARY KEY (game_id, player_id, opponent_id)
        )
    ''')
    conn.commit()


def _build_matchup_for_games(conn: sqlite3.Connection, game_ids: List[int]) -> None:
    """Fetch shift charts and compute matchup seconds for uncached games."""
    cached = {r[0] for r in conn.execute('SELECT DISTINCT game_id FROM matchup_time').fetchall()}
    missing = [g for g in game_ids if g not in cached]

    if not missing:
        return

    print(f'  Building QoC shift matchup data for {len(missing)} games...', flush=True)

    for i, gid in enumerate(missing):
        try:
            shifts = fetch_shifts(gid)
        except Exception:
            shifts = []

        if not shifts:
            # Insert a sentinel so we don't retry this game
            conn.execute(
                'INSERT OR IGNORE INTO matchup_time (game_id, player_id, opponent_id, seconds) VALUES (?,0,0,0)',
                (gid,)
            )
            conn.commit()
            continue

        # Split shifts into two teams
        teams = list({s.team_id for s in shifts})
        if len(teams) != 2:
            continue

        team_a = [s for s in shifts if s.team_id == teams[0]]
        team_b = [s for s in shifts if s.team_id == teams[1]]

        # Group each team's shifts by period for efficiency
        def by_period(shift_list):
            d = defaultdict(list)
            for s in shift_list:
                d[s.period].append(s)
            return d

        a_by_p = by_period(team_a)
        b_by_p = by_period(team_b)

        matchup: Dict[tuple, int] = defaultdict(int)
        for period in set(a_by_p) | set(b_by_p):
            for sa in a_by_p.get(period, []):
                for sb in b_by_p.get(period, []):
                    overlap = max(0, min(sa.end_sec, sb.end_sec) - max(sa.start_sec, sb.start_sec))
                    if overlap > 0:
                        matchup[(sa.player_id, sb.player_id)] += overlap

        rows = []
        for (pid_a, pid_b), secs in matchup.items():
            rows.append((gid, pid_a, pid_b, secs))
            rows.append((gid, pid_b, pid_a, secs))

        if rows:
            conn.executemany(
                'INSERT OR IGNORE INTO matchup_time (game_id, player_id, opponent_id, seconds) VALUES (?,?,?,?)',
                rows
            )
        conn.commit()

        if (i + 1) % 25 == 0:
            print(f'  QoC matchup data... {i + 1}/{len(missing)}', flush=True)

        time.sleep(0.3)

    print(f'  QoC matchup data built ({len(missing)} games).', flush=True)


def load_matchup_totals(game_ids: List[int]) -> Dict[int, Dict[int, int]]:
    """
    Return {player_id: {opponent_id: total_seconds}} summed across all provided game_ids.
    Builds the matchup_time table from shift charts on first call if needed.
    """
    conn = sqlite3.connect(DB_PATH)
    _ensure_table(conn)
    _build_matchup_for_games(conn, game_ids)

    placeholders = ','.join('?' * len(game_ids))
    result: Dict[int, Dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for player_id, opponent_id, seconds in conn.execute(
        f'SELECT player_id, opponent_id, seconds FROM matchup_time WHERE game_id IN ({placeholders}) AND player_id != 0',
        game_ids
    ).fetchall():
        result[player_id][opponent_id] += seconds

    conn.close()
    return result
