"""
playoff_pipeline.py

Full pipeline for loading NST playoff per-game data into moneypuck_games.

Steps:
  1. collect  — fetch current playoff game IDs + player IDs from NHL API
  2. download — pull NST API data for each player (4 requests each)
  3. load     — parse responses and insert into moneypuck_games table
  4. rebuild  — trigger Flask season cache refresh

Usage:
    python3 playoff_pipeline.py              # run all steps
    python3 playoff_pipeline.py collect      # step 1 only
    python3 playoff_pipeline.py download     # step 2 only
    python3 playoff_pipeline.py load         # step 3 only
    python3 playoff_pipeline.py rebuild      # step 4 only
"""
from __future__ import annotations
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

import requests

# ── Config ────────────────────────────────────────────────────────────────────
NST_KEY      = '4a7401913b91d093db07f57fdc38ae29'
NST_BASE     = 'https://data.naturalstattrick.com'
FROM_SEASON  = '20252026'
THRU_SEASON  = '20252026'
STYPE        = '3'           # playoffs
PLAYOFF_YEAR = 2026          # calendar year playoffs are played
MP_SEASON    = 2025          # moneypuck season key (year season starts)

DB_PATH      = Path(__file__).parent / 'cache.db'
DATA_DIR     = Path(__file__).parent / 'NST Playoff Data'
PLAYERS_FILE = DATA_DIR / 'playoff_player_ids.json'

REQUEST_DELAY = 0.5          # seconds between NST API calls

# Situations to download
SITUATIONS = ['all', '5on4', '4on5']

# ── Step 1: Collect ───────────────────────────────────────────────────────────

def collect():
    """Fetch all current playoff game IDs and skater IDs from NHL API."""
    print('── Step 1: Collecting playoff players from NHL API ──')
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Get playoff teams from bracket
    r = requests.get(
        f'https://api-web.nhle.com/v1/playoff-bracket/{PLAYOFF_YEAR}', timeout=10
    ).json()
    teams = set()
    for series in r.get('series', []):
        for key in ['topSeedTeam', 'bottomSeedTeam']:
            abbrev = series.get(key, {}).get('abbrev', '')
            if abbrev and abbrev != 'TBD':
                teams.add(abbrev)
    print(f'  Playoff teams ({len(teams)}): {sorted(teams)}')

    # Get all playoff game IDs
    all_game_ids = set()
    for team in teams:
        sched = requests.get(
            f'https://api-web.nhle.com/v1/club-schedule-season/{team}/{FROM_SEASON}',
            timeout=10
        ).json()
        for g in sched.get('games', []):
            if g.get('gameType') == 3:
                all_game_ids.add(g['id'])
    print(f'  Total playoff games: {len(all_game_ids)}')

    # Build game_id → {date, home, away} lookup
    game_meta: dict[int, dict] = {}
    player_ids: dict[str, dict] = {}

    # Load existing players so we don't lose any
    if PLAYERS_FILE.exists():
        existing = json.loads(PLAYERS_FILE.read_text())
        player_ids.update(existing)

    for i, gid in enumerate(sorted(all_game_ids), 1):
        print(f'  Boxscore {i}/{len(all_game_ids)}: {gid}', end='\r', flush=True)
        try:
            box = requests.get(
                f'https://api-web.nhle.com/v1/gamecenter/{gid}/boxscore', timeout=10
            ).json()
            home = box.get('homeTeam', {}).get('abbrev', '')
            away = box.get('awayTeam', {}).get('abbrev', '')
            date = box.get('gameDate', '')
            game_meta[gid] = {'date': date, 'home': home, 'away': away}

            pbgs = box.get('playerByGameStats', {})
            for side in ['homeTeam', 'awayTeam']:
                team_abbrev = box.get(side, {}).get('abbrev', '')
                for cat in ['forwards', 'defense']:
                    for p in pbgs.get(side, {}).get(cat, []):
                        pid = str(p['playerId'])
                        if pid not in player_ids:
                            player_ids[pid] = {
                                'name': p.get('name', {}).get('default', ''),
                                'team': team_abbrev,
                            }
        except Exception as e:
            print(f'\n  Error on {gid}: {e}')

    print(f'\n  Total unique skaters: {len(player_ids)}')

    # Save players
    PLAYERS_FILE.write_text(json.dumps(player_ids, indent=2))
    print(f'  Saved {PLAYERS_FILE}')

    # Save game meta for date→game_id mapping in loader
    meta_file = DATA_DIR / 'playoff_game_meta.json'
    meta_file.write_text(json.dumps(
        {str(k): v for k, v in game_meta.items()}, indent=2
    ))
    print(f'  Saved {meta_file}')


# ── Step 2: Download ──────────────────────────────────────────────────────────

def _nst_url(player_id: str, stdoi: str, sit: str) -> str:
    return (
        f'{NST_BASE}/playerreport.php'
        f'?fromseason={FROM_SEASON}&thruseason={THRU_SEASON}'
        f'&stype={STYPE}&sit={sit}&stdoi={stdoi}&rate=n&v=g'
        f'&playerid={player_id}&key={NST_KEY}'
    )


def _raw_path(player_id: str, stdoi: str, sit: str) -> Path:
    return DATA_DIR / 'raw' / f'{player_id}_{stdoi}_{sit}.html'


def download():
    """Download NST game log HTML for each playoff skater."""
    print('── Step 2: Downloading NST data ──')
    if not PLAYERS_FILE.exists():
        print('  ERROR: Run collect first.')
        return

    player_ids = json.loads(PLAYERS_FILE.read_text())
    (DATA_DIR / 'raw').mkdir(parents=True, exist_ok=True)

    total = len(player_ids) * (len(SITUATIONS) * 2 - 2)
    # We download: ind/all, ind/5on4, ind/4on5, oi/all  = 4 per player
    downloads = [
        ('std', 'all'), ('std', '5on4'), ('std', '4on5'), ('oi', 'all')
    ]
    total = len(player_ids) * len(downloads)
    done  = sum(
        1 for pid in player_ids for stdoi, sit in downloads
        if _raw_path(pid, stdoi, sit).exists()
    )
    print(f'  {done}/{total} already downloaded')

    for i, (pid, info) in enumerate(player_ids.items(), 1):
        name = info.get('name', pid)
        for stdoi, sit in downloads:
            dest = _raw_path(pid, stdoi, sit)
            if dest.exists():
                continue
            url = _nst_url(pid, stdoi, sit)
            try:
                r = requests.get(url, timeout=15)
                if 'Pending key' in r.text:
                    print('\n  ERROR: API key not yet approved. Try again later.')
                    return
                dest.write_text(r.text, encoding='utf-8')
                print(f'  [{i}/{len(player_ids)}] {name} {stdoi}/{sit} ✓', end='\r', flush=True)
            except Exception as e:
                print(f'\n  [{i}/{len(player_ids)}] {name} {stdoi}/{sit} ✗ {e}')
            time.sleep(REQUEST_DELAY)

    print(f'\n  Download complete.')


# ── Step 3: Load ──────────────────────────────────────────────────────────────

def _toi_to_seconds(toi_str: str) -> float:
    """Convert 'MM:SS.ss' or 'MM.ss' to seconds float."""
    toi_str = str(toi_str).strip()
    if ':' in toi_str:
        parts = toi_str.split(':')
        return float(parts[0]) * 60 + float(parts[1])
    return float(toi_str) * 60  # already decimal minutes


def _parse_game_str(game_str: str, game_meta: dict) -> int | None:
    """Map NST game string like '2026-04-19 L.A at COL' to NHL game_id."""
    # Extract date
    m = re.match(r'(\d{4}-\d{2}-\d{2})', game_str)
    if not m:
        return None
    date = m.group(1)

    # Find matching game by date + teams in game string
    for gid_str, meta in game_meta.items():
        if meta['date'] == date:
            home = meta['home']
            away = meta['away']
            if home in game_str or away in game_str:
                return int(gid_str)
    return None


def _parse_html_table(html: str) -> list[dict]:
    """Parse the DataTable HTML from NST into a list of row dicts."""
    import pandas as pd
    from io import StringIO
    try:
        tables = pd.read_html(StringIO(html))
        if not tables:
            return []
        df = tables[0]
        return df.to_dict('records')
    except Exception:
        return []


def load():
    """Parse downloaded HTML files and insert rows into moneypuck_games."""
    print('── Step 3: Loading into moneypuck_games ──')

    meta_file = DATA_DIR / 'playoff_game_meta.json'
    if not meta_file.exists():
        print('  ERROR: Run collect first.')
        return

    game_meta = json.loads(meta_file.read_text())
    player_ids = json.loads(PLAYERS_FILE.read_text())

    conn = sqlite3.connect(DB_PATH)
    conn.execute('''CREATE TABLE IF NOT EXISTS moneypuck_games (
        player_id INTEGER NOT NULL, game_id INTEGER NOT NULL,
        season INTEGER NOT NULL, team TEXT NOT NULL, situation TEXT NOT NULL,
        icetime REAL, goals REAL, primary_assists REAL, secondary_assists REAL,
        shots_on_goal REAL, shot_attempts REAL, hits REAL,
        takeaways REAL, giveaways REAL, pim REAL, game_score REAL,
        ixg REAL, ixg_adj REAL, ixg_hd REAL, hd_shots REAL, rebounds REAL,
        dzone_shift_starts REAL, ozone_shift_starts REAL,
        nzone_shift_starts REAL, fly_shift_starts REAL,
        onice_xga REAL, onice_xga_adj REAL, onice_xga_hd REAL,
        onice_xgf REAL, onice_xgf_adj REAL,
        onice_xg_pct REAL, office_xg_pct REAL,
        onice_corsi_pct REAL, office_corsi_pct REAL,
        xga_after_shifts REAL, xgf_after_shifts REAL, dzone_giveaways REAL,
        PRIMARY KEY (player_id, game_id, situation)
    )''')

    inserted = skipped = errors = 0

    for pid, info in player_ids.items():
        # ── Individual (all situations) ───────────────────────────────────────
        for sit in SITUATIONS:
            ind_path = _raw_path(pid, 'std', sit)
            if not ind_path.exists():
                continue
            rows = _parse_html_table(ind_path.read_text(encoding='utf-8'))
            for row in rows:
                game_str = str(row.get('Game', ''))
                gid = _parse_game_str(game_str, game_meta)
                if not gid:
                    skipped += 1
                    continue

                try:
                    toi_s = _toi_to_seconds(row.get('TOI', 0))
                    conn.execute('''
                        INSERT OR REPLACE INTO moneypuck_games
                        (player_id, game_id, season, team, situation,
                         icetime, goals, primary_assists, secondary_assists,
                         shots_on_goal, hits, takeaways, giveaways, pim,
                         ixg_adj, ixg_hd)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ''', (
                        int(pid), gid, MP_SEASON,
                        info.get('team', ''), sit,
                        toi_s,
                        float(row.get('Goals', 0) or 0),
                        float(row.get('First Assists', 0) or 0),
                        float(row.get('Second Assists', 0) or 0),
                        float(row.get('Shots', 0) or 0),
                        float(row.get('Hits', 0) or 0),
                        float(row.get('Takeaways', 0) or 0),
                        float(row.get('Giveaways', 0) or 0),
                        float(row.get('PIM', 0) or 0),
                        float(row.get('ixG', 0) or 0),   # maps to ixg_adj
                        float(row.get('iHDCF', 0) or 0), # proxy for ixg_hd
                    ))
                    inserted += 1
                except Exception as e:
                    errors += 1

        # ── On-ice (all situations only) ──────────────────────────────────────
        oi_path = _raw_path(pid, 'oi', 'all')
        if oi_path.exists():
            rows = _parse_html_table(oi_path.read_text(encoding='utf-8'))
            for row in rows:
                game_str = str(row.get('Game', ''))
                gid = _parse_game_str(game_str, game_meta)
                if not gid:
                    continue
                try:
                    cf  = float(row.get('CF', 0) or 0)
                    ca  = float(row.get('CA', 0) or 0)
                    xgf = float(row.get('xGF', 0) or 0)
                    xga = float(row.get('xGA', 0) or 0)
                    cf_pct  = cf / (cf + ca) if (cf + ca) > 0 else None
                    xg_pct  = xgf / (xgf + xga) if (xgf + xga) > 0 else None
                    conn.execute('''
                        UPDATE moneypuck_games
                        SET onice_xgf_adj=?, onice_xga_adj=?,
                            onice_corsi_pct=?, onice_xg_pct=?
                        WHERE player_id=? AND game_id=? AND situation='all'
                    ''', (xgf, xga, cf_pct, xg_pct, int(pid), gid))
                except Exception as e:
                    errors += 1

    conn.commit()
    conn.close()
    print(f'  Inserted: {inserted}  Skipped: {skipped}  Errors: {errors}')


# ── Step 4: Rebuild ───────────────────────────────────────────────────────────

def rebuild():
    """Trigger Flask season cache refresh to rebuild per-game grades."""
    print('── Step 4: Rebuilding season grades ──')
    try:
        r = requests.get('http://localhost:5001/refresh?season=20252026', timeout=120)
        print(f'  2025-26 rebuild: {r.status_code}')
    except Exception as e:
        print(f'  Could not reach Flask server: {e}')


# ── Main ──────────────────────────────────────────────────────────────────────

STEPS = {'collect': collect, 'download': download, 'load': load, 'rebuild': rebuild}

if __name__ == '__main__':
    args = sys.argv[1:]
    if args:
        for arg in args:
            if arg in STEPS:
                STEPS[arg]()
            else:
                print(f'Unknown step: {arg}. Choose from: {list(STEPS)}')
    else:
        collect()
        download()
        load()
        rebuild()
