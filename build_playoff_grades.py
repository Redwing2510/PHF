"""
build_playoff_grades.py

Builds per-game grades for playoff games only, without touching regular season rows.
Run after playoff_pipeline.py load completes for a season.

Usage:
    python3 build_playoff_grades.py 2024      # 2024-25 playoffs
    python3 build_playoff_grades.py 2025      # 2025-26 playoffs
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace
import sqlite3

import requests

DB_PATH = Path(__file__).parent / 'cache.db'
LOGS_BASE = Path(__file__).parent / 'Manual Game Logs' / 'Playoffs'


def _season_label(mp_year: int) -> str:
    return f'{mp_year}-{mp_year + 1}'


def _get_tracked_playoff_game_ids(mp_year: int):
    """Return full NHL game IDs for tracked playoff xlsx files for this season."""
    folder = LOGS_BASE / _season_label(mp_year)
    if not folder.exists():
        return []
    ids = []
    for f in folder.glob('*.xlsx'):
        try:
            file_id = int(f.stem.split()[0])
            full_id = int(f'{mp_year}0{file_id}')
            ids.append(full_id)
        except (ValueError, IndexError):
            pass
    return ids


def _fetch_bio(pid: int, conn: sqlite3.Connection) -> dict:
    """Fetch player bio from NHL API and cache it."""
    try:
        d = requests.get(f'https://api-web.nhle.com/v1/player/{pid}/landing', timeout=8).json()
        bio = {
            'firstName':  d.get('firstName', {}).get('default', '') if isinstance(d.get('firstName'), dict) else d.get('firstName', ''),
            'lastName':   d.get('lastName', {}).get('default', '') if isinstance(d.get('lastName'), dict) else d.get('lastName', ''),
            'position':   d.get('position', 'C'),
            'teamAbbrev': d.get('currentTeamAbbrev', ''),
        }
        conn.execute('INSERT OR REPLACE INTO player_bios (player_id, data) VALUES (?,?)',
                     (pid, json.dumps(bio)))
        return bio
    except Exception:
        return {}


def build(mp_year: int):
    season_str = f'{mp_year}{mp_year + 1}'
    playoff_min = int(f'{mp_year}030000')

    conn = sqlite3.connect(DB_PATH)
    conn.execute('CREATE TABLE IF NOT EXISTS player_bios (player_id INTEGER PRIMARY KEY, data TEXT NOT NULL)')

    # Get all player IDs in playoff moneypuck_games
    pids = [r[0] for r in conn.execute(
        'SELECT DISTINCT player_id FROM moneypuck_games WHERE season=? AND game_id >= ?',
        (mp_year, playoff_min)
    ).fetchall()]

    if not pids:
        print(f'No playoff moneypuck_games found for season {mp_year}. Run playoff_pipeline.py load first.')
        conn.close()
        return

    print(f'Found {len(pids)} players in {mp_year}-{mp_year+1} playoff moneypuck_games.')

    # Build minimal season_acc from player_bios, fetching missing bios from NHL API
    missing_bios = [p for p in pids
                    if not conn.execute('SELECT 1 FROM player_bios WHERE player_id=?', (p,)).fetchone()]
    if missing_bios:
        print(f'Fetching {len(missing_bios)} missing player bios from NHL API...')
        fetched = 0
        for pid in missing_bios:
            bio = _fetch_bio(pid, conn)
            if bio.get('lastName'):
                fetched += 1
        conn.commit()
        print(f'  Fetched {fetched}/{len(missing_bios)} bios.')

    season_acc = {}
    for pid in pids:
        bio_row = conn.execute('SELECT data FROM player_bios WHERE player_id=?', (pid,)).fetchone()
        if bio_row:
            bio = json.loads(bio_row[0])
            pos = bio.get('position', 'C') or 'C'
            name = f"{bio.get('firstName', '')} {bio.get('lastName', '')}".strip()
            team = bio.get('teamAbbrev', '')
        else:
            pos, name, team = 'C', f'Player {pid}', ''
        if name:
            season_acc[pid] = SimpleNamespace(name=name, team=team, position=pos)

    conn.close()

    tracked_ids = _get_tracked_playoff_game_ids(mp_year)
    print(f'A3Z tracked playoff games: {len(tracked_ids)}')

    from game_grade_builder import build_player_game_grades
    print(f'Building playoff game grades (playoff_only=True)...')
    build_player_game_grades(season_str, season_acc, tracked_ids, playoff_only=True, norm_sd=21)

    # Clear season cache so Flask rebuilds
    cache_file = Path(__file__).parent / 'season_cache' / f'{season_str}.json'
    if cache_file.exists():
        cache_file.unlink()
        print(f'  Cleared {cache_file.name}')


if __name__ == '__main__':
    year = int(sys.argv[1]) if len(sys.argv) > 1 else 2024
    build(year)
