"""
playoff_pipeline.py

Full pipeline for loading NST playoff per-game data into moneypuck_games.

Steps:
  1. collect  — fetch current playoff game IDs + player IDs from NHL API
  2. download — pull NST API data for each player (4 requests each)
  3. load     — parse responses and insert into moneypuck_games table
  4. rebuild  — trigger Flask season cache refresh

Single-game update (1 NST token, replaces delete+re-download for new games):
  update <game_id>  — fetch NST game page, upsert all skater stats, rebuild

Usage:
    python3 playoff_pipeline.py                        # run all steps
    python3 playoff_pipeline.py collect                # step 1 only
    python3 playoff_pipeline.py download               # step 2 only
    python3 playoff_pipeline.py load                   # step 3 only
    python3 playoff_pipeline.py rebuild                # step 4 only
    python3 playoff_pipeline.py update 2025030236      # single-game update
"""
from __future__ import annotations
import json
import re
import sqlite3
import sys
import time
from datetime import date
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
DATA_DIR     = Path(__file__).parent / 'NST Playoff Data' / f'{FROM_SEASON}'
PLAYERS_FILE = DATA_DIR / 'playoff_player_ids.json'

REQUEST_DELAY = 1.0          # seconds between requests within a batch
BATCH_SIZE    = 14            # requests per batch (~150 tokens = 1 standard refill window)
BATCH_PAUSE   = 305          # seconds to wait between batches (5 min for standard tokens to refill)

# Situations to download
SITUATIONS = ['all', '5v4', '4v5']

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
                    pos = 'C' if cat == 'forwards' else 'D'
                    for p in pbgs.get(side, {}).get(cat, []):
                        pid = str(p['playerId'])
                        if pid not in player_ids:
                            player_ids[pid] = {
                                'name': p.get('name', {}).get('default', ''),
                                'team': team_abbrev,
                                'position': pos,
                            }
                        elif 'position' not in player_ids[pid]:
                            player_ids[pid]['position'] = pos
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


def _raw_path(player_id: str, stdoi: str, sit: str, team: str = 'unknown') -> Path:
    return DATA_DIR / team / player_id / f'{stdoi}_{sit}.html'


def download():
    """Download NST game log HTML for each playoff skater."""
    print('── Step 2: Downloading NST data ──')
    if not PLAYERS_FILE.exists():
        print('  ERROR: Run collect first.')
        return

    player_ids = json.loads(PLAYERS_FILE.read_text())

    # We download: std/all, std/5v4, std/4v5, oi/all  = 4 per player
    downloads = [
        ('std', 'all'), ('std', '5v4'), ('std', '4v5'), ('oi', 'all')
    ]
    total = len(player_ids) * len(downloads)
    done  = sum(
        1 for pid, info in player_ids.items() for stdoi, sit in downloads
        if _raw_path(pid, stdoi, sit, info.get('team', 'unknown')).exists()
    )
    print(f'  {done}/{total} already downloaded')

    # Build list of all pending downloads
    pending = [
        (pid, info, stdoi, sit)
        for pid, info in player_ids.items()
        for stdoi, sit in downloads
        if not _raw_path(pid, stdoi, sit, info.get('team', 'unknown')).exists()
    ]
    total_pending = len(pending)
    print(f'  {total - total_pending}/{total} already downloaded, {total_pending} to go')

    batch_num = 0
    i = 0
    while i < len(pending):
        batch = pending[i:i + BATCH_SIZE]
        batch_num += 1
        print(f'\n  Batch {batch_num}: requests {i+1}–{i+len(batch)} of {total_pending}', flush=True)

        for pid, info, stdoi, sit in batch:
            name = info.get('name', pid)
            team = info.get('team', 'unknown')
            dest = _raw_path(pid, stdoi, sit, team)
            if dest.exists():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            url  = _nst_url(pid, stdoi, sit)
            try:
                r = requests.get(url, timeout=15)
                if 'Pending key' in r.text:
                    print('\n  ERROR: API key not yet approved. Try again later.')
                    return
                if 'burst tokens' in r.text or len(r.text) < 200:
                    print(f'  Rate limited mid-batch — pausing {BATCH_PAUSE}s then retrying batch...', flush=True)
                    time.sleep(BATCH_PAUSE)
                    break  # retry same batch
                dest.write_text(r.text, encoding='utf-8')
                print(f'  {name} {stdoi}/{sit} ✓', end='\r', flush=True)
            except Exception as e:
                print(f'\n  {name} {stdoi}/{sit} ✗ {e}')
            time.sleep(REQUEST_DELAY)
        else:
            # Full batch completed without rate limit — advance and pause before next batch
            i += len(batch)
            if i < len(pending):
                print(f'\n  Batch {batch_num} done. Pausing {BATCH_PAUSE}s for burst refill...', flush=True)
                time.sleep(BATCH_PAUSE)

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
        SIT_MAP = {'5v4': '5on4', '4v5': '4on5'}
        team = info.get('team', 'unknown')
        for sit in SITUATIONS:
            ind_path = _raw_path(pid, 'std', sit, team)
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
                        info.get('team', ''), SIT_MAP.get(sit, sit),
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
        oi_path = _raw_path(pid, 'oi', 'all', team)
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


# ── Single-game update (game page approach) ───────────────────────────────────

# NST uses different abbreviations for some teams than the NHL API.
_NST_TO_NHL = {'L.A': 'LAK', 'T.B': 'TBL', 'N.J': 'NJD', 'S.J': 'SJS'}


def _store_game_score(game_id: int):
    """Fetch the final score from the NHL boxscore API and persist it to cache.db."""
    try:
        data = requests.get(
            f'https://api-web.nhle.com/v1/gamecenter/{game_id}/boxscore', timeout=10
        ).json()
        home_score = data.get('homeTeam', {}).get('score')
        away_score = data.get('awayTeam', {}).get('score')
        if home_score is None or away_score is None:
            print(f'  Score not available yet for {game_id}')
            return
        conn = sqlite3.connect(DB_PATH)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS game_scores (
                game_id INTEGER PRIMARY KEY,
                home_score INTEGER NOT NULL,
                away_score INTEGER NOT NULL
            )
        ''')
        conn.execute(
            'INSERT OR REPLACE INTO game_scores (game_id, home_score, away_score) VALUES (?,?,?)',
            (game_id, home_score, away_score)
        )
        conn.commit()
        conn.close()
        print(f'  Score stored: {game_id} home={home_score} away={away_score}')
    except Exception as e:
        print(f'  Could not store score for {game_id}: {e}')


def _refresh_team_schedules(nhl_teams: list[str]):
    """Re-fetch NHL schedule data for the given teams and overwrite the DB cache.

    The bracket reads scores from the schedules table, which may have been
    cached before this game was played (no scores yet). Forcing a re-fetch
    here ensures the final score shows up in the bracket immediately.
    """
    conn = sqlite3.connect(DB_PATH)
    for team in nhl_teams:
        try:
            url = f'https://api-web.nhle.com/v1/club-schedule-season/{team}/{FROM_SEASON}'
            r = requests.get(url, timeout=10)
            games = r.json().get('games', [])
            conn.execute(
                'INSERT OR REPLACE INTO schedules (team, season, data, saved_at) VALUES (?,?,?,?)',
                (team, FROM_SEASON, json.dumps(games), time.time())
            )
            print(f'  Schedule refreshed: {team}')
        except Exception as e:
            print(f'  Schedule refresh failed for {team}: {e}')
    conn.commit()
    conn.close()


def _nst_game_id(game_id: int) -> int:
    """2025030235 → 30235 (last 5 digits used by NST game.php)."""
    return int(str(game_id)[-5:])


def _norm_name(name: str) -> str:
    """
    Normalize to 'F. Lastname' so abbreviated and full names match.
    'Nathan MacKinnon' → 'n. mackinnon'
    'N. MacKinnon'     → 'n. mackinnon'
    'Joel Eriksson Ek' → 'j. eriksson ek'
    """
    name = name.replace('\xa0', ' ').strip()
    parts = name.split()
    if len(parts) < 2:
        return name.lower()
    first = parts[0]
    last  = ' '.join(parts[1:])
    # Already abbreviated ("N." has len ≤ 2 or ends with '.')
    if len(first) <= 2 or first.endswith('.'):
        return f"{first.rstrip('.')}. {last}".lower()
    return f"{first[0]}. {last}".lower()


def _build_name_lookup(player_ids: dict) -> dict[str, str]:
    """Build normalized name → player_id lookup (handles abbreviated + full names)."""
    lookup = {}
    for pid, info in player_ids.items():
        key = _norm_name(info.get('name', ''))
        if key:
            lookup[key] = pid
    return lookup


def _extract_game_table(html: str, table_id: str) -> list[dict]:
    """Extract a named table from an NST game page and return rows as dicts."""
    import pandas as pd
    from io import StringIO
    m = re.search(rf'<table[^>]+id={re.escape(table_id)}[^>]*>(.*?)</table>', html, re.DOTALL)
    if not m:
        return []
    try:
        df = pd.read_html(StringIO(f'<table>{m.group(1)}</table>'))[0]
        if 'Player' in df.columns:
            df['Player'] = df['Player'].str.replace('\xa0', ' ', regex=False).str.strip()
        return df.to_dict('records')
    except Exception:
        return []


def _completed_playoff_games_on(check_date: str) -> list[int]:
    """Return NHL game IDs for completed playoff games on a given YYYY-MM-DD date."""
    print(f'  Checking NHL schedule for {check_date}...')
    try:
        data = requests.get(
            f'https://api-web.nhle.com/v1/schedule/{check_date}', timeout=10
        ).json()
    except Exception as e:
        print(f'  NHL API error: {e}')
        return []
    game_ids = []
    for day in data.get('gameWeek', []):
        if day.get('date') != check_date:
            continue
        for g in day.get('games', []):
            if g.get('gameType') == 3 and g.get('gameState') in ('OFF', 'FINAL', '7'):
                game_ids.append(g['id'])
    return game_ids


def update_game(game_id: int):
    """
    Fetch a single NST game page and upsert stats for all skaters.
    Costs 1 NST token vs ~172 for the delete+re-download approach.
    """
    print(f'── Updating game {game_id} via NST game page ──')

    if not PLAYERS_FILE.exists():
        print('  ERROR: Run collect first.')
        return

    player_ids  = json.loads(PLAYERS_FILE.read_text())
    name_to_pid = _build_name_lookup(player_ids)

    # Fetch game page (no Cloudflare on data.naturalstattrick.com)
    nst_gid = _nst_game_id(game_id)
    url = f'{NST_BASE}/game.php?season={FROM_SEASON}&game={nst_gid}&key={NST_KEY}'
    print(f'  GET {url}')
    r = requests.get(url, timeout=30)

    if 'burst tokens' in r.text or len(r.text) < 500:
        print('  Rate limited — try again in a few minutes.')
        return

    html = r.text

    # Auto-detect NST team abbreviations from table IDs embedded in the page
    nst_teams = re.findall(r'<table[^>]+id=tb([A-Z.]+)stall', html)
    if len(nst_teams) < 2:
        print(f'  ERROR: Could not find team tables (found: {nst_teams}).')
        return
    print(f'  NST teams: {nst_teams}')

    conn = sqlite3.connect(DB_PATH)
    inserted = oi_updated = skipped = 0

    for nst_team in nst_teams:
        # Pull the four tables we need for this team
        std_all = _extract_game_table(html, f'tb{nst_team}stall')
        std_pp  = _extract_game_table(html, f'tb{nst_team}stpp')
        std_pk  = _extract_game_table(html, f'tb{nst_team}stpk')
        oi_all  = _extract_game_table(html, f'tb{nst_team}oiall')

        # Insert standard stats for all three situation tables
        for rows, db_sit in [(std_all, 'all'), (std_pp, '5on4'), (std_pk, '4on5')]:
            for row in rows:
                name    = _norm_name(str(row.get('Player', '')))
                pid_str = name_to_pid.get(name)
                if not pid_str:
                    skipped += 1
                    continue
                pid      = int(pid_str)
                nhl_team = player_ids[pid_str].get('team', _NST_TO_NHL.get(nst_team, nst_team))
                try:
                    conn.execute('''
                        INSERT OR REPLACE INTO moneypuck_games
                        (player_id, game_id, season, team, situation,
                         icetime, goals, primary_assists, secondary_assists,
                         shots_on_goal, hits, takeaways, giveaways, pim,
                         ixg_adj, ixg_hd)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ''', (
                        pid, game_id, MP_SEASON, nhl_team, db_sit,
                        _toi_to_seconds(row.get('TOI', 0)),
                        float(row.get('Goals', 0) or 0),
                        float(row.get('First Assists', 0) or 0),
                        float(row.get('Second Assists', 0) or 0),
                        float(row.get('Shots', 0) or 0),
                        float(row.get('Hits', 0) or 0),
                        float(row.get('Takeaways', 0) or 0),
                        float(row.get('Giveaways', 0) or 0),
                        float(row.get('PIM', 0) or 0),
                        float(row.get('ixG', 0) or 0),
                        float(row.get('iHDCF', 0) or 0),
                    ))
                    inserted += 1
                except Exception as e:
                    print(f'  Insert error ({name}, {db_sit}): {e}')

        # Update on-ice stats into the already-inserted 'all' rows
        for row in oi_all:
            name    = _norm_name(str(row.get('Player', '')))
            pid_str = name_to_pid.get(name)
            if not pid_str:
                continue
            pid = int(pid_str)
            try:
                cf     = float(row.get('CF', 0) or 0)
                ca     = float(row.get('CA', 0) or 0)
                xgf    = float(row.get('xGF', 0) or 0)
                xga    = float(row.get('xGA', 0) or 0)
                cf_pct = cf / (cf + ca) if (cf + ca) > 0 else None
                xg_pct = xgf / (xgf + xga) if (xgf + xga) > 0 else None
                conn.execute('''
                    UPDATE moneypuck_games
                    SET onice_xgf_adj=?, onice_xga_adj=?,
                        onice_corsi_pct=?, onice_xg_pct=?
                    WHERE player_id=? AND game_id=? AND situation='all'
                ''', (xgf, xga, cf_pct, xg_pct, pid, game_id))
                oi_updated += 1
            except Exception as e:
                print(f'  OI update error ({name}): {e}')

    conn.commit()
    conn.close()
    print(f'  Inserted: {inserted}  OI updated: {oi_updated}  Skipped (name unmatched): {skipped}')
    if skipped:
        print('  Tip: run "collect" if new players appeared and re-run update.')
    nhl_teams = [_NST_TO_NHL.get(t, t) for t in nst_teams]
    _refresh_team_schedules(nhl_teams)
    _store_game_score(game_id)
    rebuild()


# ── Step 4: Rebuild ───────────────────────────────────────────────────────────

def rebuild():
    """Clear the season disk cache so Flask rebuilds grades on next page load."""
    print('── Step 4: Rebuilding season grades ──')
    cache_file = Path(__file__).parent / 'season_cache' / f'{FROM_SEASON}.json'
    if cache_file.exists():
        cache_file.unlink()
        print(f'  Cleared {cache_file.name} — grades will rebuild on next page load.')
    else:
        print(f'  Cache file not found, nothing to clear.')
    # Also try HTTP refresh in case Flask is reachable
    try:
        r = requests.get(f'http://localhost:5001/refresh?season={FROM_SEASON}', timeout=5)
        print(f'  Flask refresh: {r.status_code}')
    except Exception:
        pass


# ── Main ──────────────────────────────────────────────────────────────────────

STEPS = {'collect': collect, 'download': download, 'load': load, 'rebuild': rebuild}

if __name__ == '__main__':
    args = sys.argv[1:]

    # --season YYYY  — override constants for a prior playoff year
    if '--season' in args:
        _si = args.index('--season')
        _yr = int(args[_si + 1])
        args = args[:_si] + args[_si + 2:]
        MP_SEASON    = _yr
        PLAYOFF_YEAR = _yr + 1
        FROM_SEASON  = f'{_yr}{_yr + 1}'
        THRU_SEASON  = FROM_SEASON
        DATA_DIR     = Path(__file__).parent / 'NST Playoff Data' / FROM_SEASON
        PLAYERS_FILE = DATA_DIR / 'playoff_player_ids.json'
        print(f'  Season override: {FROM_SEASON} (playoff year {PLAYOFF_YEAR})')

    if args and args[0] == 'update':
        if len(args) >= 2 and args[1].isdigit():
            # Explicit game ID: python3 playoff_pipeline.py update 2025030236
            update_game(int(args[1]))
        else:
            # Date to check: today (default) or yesterday (for 1 AM run)
            from datetime import timedelta
            if len(args) >= 2 and args[1] == 'yesterday':
                check_date = (date.today() - timedelta(days=1)).isoformat()
            else:
                check_date = date.today().isoformat()
            game_ids = _completed_playoff_games_on(check_date)
            if not game_ids:
                print(f'  No completed playoff games found on {check_date}.')
            else:
                print(f'  Found {len(game_ids)} completed game(s): {game_ids}')
                for gid in game_ids:
                    update_game(gid)
    elif args:
        for arg in args:
            if arg in STEPS:
                STEPS[arg]()
            else:
                print(f'Unknown step: {arg}. Choose from: {list(STEPS)}, update <game_id>')
    else:
        collect()
        download()
        load()
        rebuild()
