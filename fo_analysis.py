"""
fo_analysis.py

Computes shot-differential weights for faceoffs by zone × strength.
Fetches NHL play-by-play for all cached games (one-time ~3 min, then
stored in cache.db) and measures shot attempts by each team in the
15 seconds following every faceoff.

Weight formula:
  shot_diff(type) = (shots_for - shots_against) per faceoff win
  weight(type)    = shot_diff(type) / shot_diff(ES, N)

Run standalone:  python3 fo_analysis.py
"""

import json
import sqlite3
import time
from collections import defaultdict

from loader import _get

DB_PATH    = 'cache.db'
WINDOW_SEC = 15
SHOT_TYPES = {'shot-on-goal', 'goal', 'missed-shot'}


# ---------------------------------------------------------------------------
# PBP fetch + cache
# ---------------------------------------------------------------------------

def _ensure_pbp_table(conn: sqlite3.Connection) -> None:
    conn.execute('''
        CREATE TABLE IF NOT EXISTS pbp (
            game_id  INTEGER PRIMARY KEY,
            data     TEXT    NOT NULL,
            saved_at TEXT    NOT NULL
        )
    ''')
    conn.commit()


def fetch_and_cache_pbp() -> None:
    conn = sqlite3.connect(DB_PATH)
    _ensure_pbp_table(conn)

    all_ids  = [r[0] for r in conn.execute('SELECT game_id FROM games').fetchall()]
    cached   = {r[0] for r in conn.execute('SELECT game_id FROM pbp').fetchall()}
    missing  = [g for g in all_ids if g not in cached]

    if not missing:
        print(f'  PBP already cached for all {len(all_ids)} games.')
        conn.close()
        return

    print(f'  Fetching PBP for {len(missing)} games (cached: {len(all_ids) - len(missing)})...')
    saved = 0
    for i, gid in enumerate(missing, 1):
        url = f'https://api-web.nhle.com/v1/gamecenter/{gid}/play-by-play'
        try:
            data = _get(url).json()
            conn.execute(
                'INSERT OR IGNORE INTO pbp (game_id, data, saved_at) VALUES (?, ?, datetime("now"))',
                (gid, json.dumps(data))
            )
            saved += 1
        except Exception as e:
            print(f'  Warning: {gid} failed — {e}')
        if i % 25 == 0 or i == len(missing):
            conn.commit()
            print(f'  ... {i}/{len(missing)}', flush=True)
        time.sleep(0.25)

    conn.commit()
    conn.close()
    print(f'  Stored {saved} new PBP records.')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _t2s(t: str) -> int:
    """'MM:SS' -> elapsed seconds in period."""
    m, s = t.split(':')
    return int(m) * 60 + int(s)


def _strength(situation: str, winner_team_id: int, home_team_id: int) -> str:
    """Return 'ES', 'PP', or 'PK' from the winner's perspective."""
    if len(situation) < 4:
        return 'ES'
    away_sk = int(situation[1])
    home_sk = int(situation[2])
    if away_sk == home_sk:
        return 'ES'
    home_has_pp   = home_sk > away_sk
    winner_is_home = winner_team_id == home_team_id
    if home_has_pp:
        return 'PP' if winner_is_home else 'PK'
    else:
        return 'PP' if not winner_is_home else 'PK'


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------

def analyze() -> dict:
    """
    Returns dict keyed by (strength, zone) ->
        {'sf': shots_for_total, 'sa': shots_against_total, 'n': faceoff_count}
    """
    conn   = sqlite3.connect(DB_PATH)
    rows   = conn.execute('SELECT game_id, data FROM pbp').fetchall()
    conn.close()

    stats: dict = defaultdict(lambda: {'sf': 0, 'sa': 0, 'n': 0})

    for gid, data_str in rows:
        data  = json.loads(data_str)
        plays = data.get('plays', [])
        if not plays:
            continue

        home_id = data.get('homeTeam', {}).get('id')
        if not home_id:
            continue

        for i, play in enumerate(plays):
            if play.get('typeDescKey') != 'faceoff':
                continue

            det      = play.get('details', {})
            owner_id = det.get('eventOwnerTeamId')
            zone     = det.get('zoneCode', 'N')
            if zone not in ('O', 'N', 'D'):
                continue

            situation = play.get('situationCode', '1551')
            strength  = _strength(situation, owner_id, home_id)
            period    = play['periodDescriptor']['number']
            fo_sec    = _t2s(play.get('timeInPeriod', '0:00'))

            sf = sa = 0

            for j in range(i + 1, len(plays)):
                p2       = plays[j]
                p2_type  = p2.get('typeDescKey', '')

                # Stop at next faceoff (possession reset)
                if p2_type == 'faceoff':
                    break

                if p2_type not in SHOT_TYPES:
                    continue

                # Stay within same period and time window
                if p2['periodDescriptor']['number'] != period:
                    break
                p2_sec = _t2s(p2.get('timeInPeriod', '0:00'))
                if p2_sec - fo_sec > WINDOW_SEC:
                    break
                if p2_sec < fo_sec:
                    continue

                p2_owner = p2.get('details', {}).get('eventOwnerTeamId')
                if p2_owner == owner_id:
                    sf += 1
                else:
                    sa += 1

            key = (strength, zone)
            stats[key]['sf'] += sf
            stats[key]['sa'] += sa
            stats[key]['n']  += 1

    return dict(stats)


# ---------------------------------------------------------------------------
# Weight table
# ---------------------------------------------------------------------------

def print_weights(stats: dict) -> None:
    bl     = stats.get(('ES', 'N'), {'sf': 1, 'sa': 0, 'n': 1})
    bl_n   = bl['n'] or 1
    bl_sf  = bl['sf'] / bl_n
    bl_sa  = bl['sa'] / bl_n
    bl_diff = bl_sf - bl_sa

    label_map = {
        'O': 'OZ (off)',
        'N': 'NZ (neu)',
        'D': 'DZ (def)',
    }

    rows = []
    for (strength, zone), v in stats.items():
        n    = v['n'] or 1
        sf_r = v['sf'] / n
        sa_r = v['sa'] / n
        diff = sf_r - sa_r
        w    = diff / bl_diff if bl_diff else 1.0
        rows.append((strength, zone, v['n'], sf_r, sa_r, diff, w))

    rows.sort(key=lambda x: x[6], reverse=True)

    print()
    print('Faceoff shot-differential weights  (baseline = ES neutral zone = 1.00×)')
    print(f'{"Strength":8} {"Zone":10} {"FOs":6}  {"SF/FO":7} {"SA/FO":7} {"Diff":7}  {"Weight":7}')
    print('─' * 60)
    for strength, zone, n, sf, sa, diff, w in rows:
        print(f'{strength:8} {label_map.get(zone, zone):10} {n:6}  {sf:7.4f} {sa:7.4f} {diff:+7.4f}  {w:6.2f}×')
    print()
    print('Suggested implementation weights:')
    for strength, zone, n, sf, sa, diff, w in rows:
        print(f'  ("{strength}", "{zone}"): {round(max(0.1, w), 2)},')


if __name__ == '__main__':
    print('Ensuring PBP cache...')
    fetch_and_cache_pbp()
    print('Analyzing faceoff → shot differentials...')
    stats = analyze()
    print_weights(stats)
