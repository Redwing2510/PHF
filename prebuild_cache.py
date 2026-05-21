"""
prebuild_cache.py — build the season cache before Flask starts.

Run this before `systemctl restart phf` in cronjobs so all gunicorn workers
find a warm cache on startup instead of each rebuilding independently.

Usage:
    python3 prebuild_cache.py            # builds current season (20252026)
    python3 prebuild_cache.py 20242025   # builds a specific season
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

CACHE_DIR = Path(__file__).parent / 'season_cache'

def _game_count(season_str: str) -> int:
    prefix = season_str[:4]
    conn = sqlite3.connect(Path(__file__).parent / 'cache.db')
    n = conn.execute(
        "SELECT COUNT(*) FROM games WHERE CAST(game_id AS TEXT) LIKE ?",
        (f'{prefix}%',)
    ).fetchone()[0]
    conn.close()
    return n

def main():
    from season import build_season_grades, SEASON
    season_str = sys.argv[1] if len(sys.argv) > 1 else SEASON
    print(f'Pre-building season cache for {season_str}...', flush=True)
    data = build_season_grades(season_str)
    CACHE_DIR.mkdir(exist_ok=True)
    path = CACHE_DIR / f'{season_str}.json'
    with open(path, 'w') as f:
        json.dump(dict(data, _game_count=_game_count(season_str)), f)
    print(f'Cache saved to {path}', flush=True)

if __name__ == '__main__':
    main()
