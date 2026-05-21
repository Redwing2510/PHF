"""
game_grade_builder.py

Computes per-game off/dfn/overall grades for all player-games in a season.
Normalizes each metric across all player-games (by position group) so that
72 means "better than ~70% of all player-games that season."

Writes to player_game_grades table in cache.db.
"""
from __future__ import annotations
import sqlite3
import statistics as _stats
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from grader import normalize_by_position_group
from tracking_grader import compute_tracking_split
from manual_loader import get_microstat_record

DB_PATH = Path(__file__).parent / 'cache.db'

_FWD = {'C', 'L', 'R'}


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute('''
        CREATE TABLE IF NOT EXISTS player_game_grades (
            player_id   INTEGER NOT NULL,
            game_id     INTEGER NOT NULL,
            season      INTEGER NOT NULL,
            name        TEXT,
            team        TEXT,
            opponent    TEXT,
            game_date   TEXT,
            position    TEXT,
            toi_min     REAL,
            off         REAL,
            dfn         REAL,
            overall     REAL,
            has_tracking INTEGER DEFAULT 0,
            PRIMARY KEY (player_id, game_id)
        )
    ''')
    conn.commit()


def _norm_pool(raw_vals: List[float], positions: List[str], norm_sd: float = None) -> List[float]:
    """z-score normalize a list of (val, position) pairs → mean=NORM_MEAN, clamp 0-100."""
    pairs = list(zip(raw_vals, positions))
    normed = normalize_by_position_group(pairs, norm_sd=norm_sd)
    return [max(0.0, min(100.0, v)) for v in normed]


def _toi_h(toi_seconds: float) -> float:
    return max(toi_seconds, 60.0) / 3600.0


def build_player_game_grades(
    season_str: str,
    season_acc: dict,          # pid → SeasonEntry
    game_ids: List[int],       # tracked game IDs (xlsx files) — used for tracking blend only
    playoff_only: bool = False, # if True, only build/replace playoff game rows (game_id >= {year}030000)
    norm_sd: float = None,     # override SD for normalization (e.g. 9 for playoffs)
) -> None:
    """
    Build and store per-game grades for ALL player-games in the season (full MP data).
    game_ids determines which games have manual tracking available to blend in.
    Clears existing rows for this season before writing (or just playoff rows if playoff_only=True).
    """
    mp_year = int(season_str[:4])
    tracked_gids = set(game_ids)
    playoff_min_gid = int(f'{mp_year}030000')

    # ── 1. Load MP per-game data ──────────────────────────────────────────────
    conn = sqlite3.connect(DB_PATH)
    _ensure_table(conn)

    # Clear stale rows — playoff_only mode only removes playoff game rows
    if playoff_only:
        conn.execute('DELETE FROM player_game_grades WHERE season=? AND game_id >= ?',
                     (mp_year, playoff_min_gid))
    else:
        conn.execute('DELETE FROM player_game_grades WHERE season=?', (mp_year,))
    conn.commit()

    # all-situation rows — scoped to playoff games if playoff_only
    gid_filter = f' AND game_id >= {playoff_min_gid}' if playoff_only else ''
    all_rows: Dict[Tuple[int,int], dict] = {}
    for row in conn.execute(
        "SELECT player_id, game_id, team, icetime, "
        "ixg_adj, ixg_hd, onice_xgf_adj, "
        "onice_xga_adj, takeaways, giveaways, "
        "goals, primary_assists "
        f"FROM moneypuck_games "
        f"WHERE situation='all' AND season=?{gid_filter}",
        [mp_year]
    ).fetchall():
        pid, gid = row[0], row[1]
        all_rows[(pid, gid)] = {
            'team': row[2], 'toi_s': row[3] or 0.0,
            'ixg_adj': row[4] or 0.0, 'ixg_hd': row[5] or 0.0,
            'xgf_adj': row[6] or 0.0,
            'xga_adj': row[7] or 0.0,
            'tka': row[8] or 0.0, 'gva': row[9] or 0.0,
            'goals': row[10] or 0.0, 'primary_assists': row[11] or 0.0,
        }

    # PP rows
    pp_rows: Dict[Tuple[int,int], dict] = {}
    for row in conn.execute(
        f"SELECT player_id, game_id, ixg_adj, icetime "
        f"FROM moneypuck_games "
        f"WHERE situation='5on4' AND season=?{gid_filter}",
        [mp_year]
    ).fetchall():
        pp_rows[(row[0], row[1])] = {'pp_ixg': row[2] or 0.0, 'pp_toi_s': row[3] or 0.0}

    # PK rows
    pk_rows: Dict[Tuple[int,int], dict] = {}
    for row in conn.execute(
        f"SELECT player_id, game_id, onice_xga_adj, icetime "
        f"FROM moneypuck_games "
        f"WHERE situation='4on5' AND season=?{gid_filter}",
        [mp_year]
    ).fetchall():
        pk_rows[(row[0], row[1])] = {'pk_xga': row[2] or 0.0, 'pk_toi_s': row[3] or 0.0}

    # game_dates lookup
    date_map: Dict[int, str] = {
        r[0]: r[1]
        for r in conn.execute('SELECT game_id, game_date FROM game_dates').fetchall()
    }

    # Derive opponents: for each game, find both teams from MP data
    game_teams: Dict[int, List[str]] = {}
    for row in conn.execute(
        f"SELECT DISTINCT game_id, team FROM moneypuck_games "
        f"WHERE situation='all' AND season=?{gid_filter}",
        [mp_year]
    ).fetchall():
        game_teams.setdefault(row[0], []).append(row[1])

    # ── 2. Build pid → (name, team, position) map for tracking lookups ───────
    pid_to_info: Dict[int, tuple] = {
        pid: (e.name, e.team, e.position) for pid, e in season_acc.items()
    }

    # ── 3. Compute raw scores for every player-game ───────────────────────────
    records: List[dict] = []

    all_pids_in_games = {pid for (pid, _) in all_rows}

    for (pid, gid), mp in all_rows.items():
        if pid not in season_acc:
            continue
        e = season_acc[pid]
        pos = e.position
        if pos == 'G':
            continue

        toi_s = mp['toi_s']
        if toi_s < 60:
            continue

        toi_h = _toi_h(toi_s)
        teams_in_game = game_teams.get(gid, [])
        opponent = next((t for t in teams_in_game if t != mp['team']), '')

        # per-60 rates
        ixg_p60 = mp['ixg_adj'] / toi_h
        ixg_hd_p60 = mp['ixg_hd'] / toi_h
        xgf_p60 = mp['xgf_adj'] / toi_h

        pp = pp_rows.get((pid, gid), {})
        pp_toi_h = _toi_h(pp.get('pp_toi_s', 0.0)) if pp.get('pp_toi_s', 0.0) > 0 else None
        pp_ixg_p60 = (pp['pp_ixg'] / pp_toi_h) if pp_toi_h else 0.0

        toi_min = toi_s / 60
        g_pa_p60 = (toi_min / (toi_min + 10)) * ((mp['goals'] + mp['primary_assists']) / toi_h)

        # Dfn: xGA/60 (negated) + net puck/60
        xga_p60 = mp['xga_adj'] / toi_h
        pk = pk_rows.get((pid, gid), {})
        pk_xga = pk.get('pk_xga', 0.0)
        pk_toi_s = pk.get('pk_toi_s', 0.0)
        npk_toi_h = _toi_h(toi_s - pk_toi_s)
        npk_xga_p60 = (mp['xga_adj'] - pk_xga) / npk_toi_h
        net_puck_p60 = (mp['tka'] - mp['gva']) / toi_h

        # Tracking for this game (only attempt lookup for tracked game IDs)
        name, team, position = pid_to_info.get(pid, (e.name, e.team, e.position))
        trk = get_microstat_record(gid, name, team, position) if gid in tracked_gids else None
        has_tracking = trk is not None
        trk_off_pts = trk_dz_pts = trk_ed_pts = 0.0
        if trk:
            trk_off_pts, trk_dz_pts, trk_ed_pts = compute_tracking_split(trk, position)

        records.append({
            'pid': pid, 'gid': gid, 'season': mp_year,
            'name': name,
            'team': mp['team'], 'opponent': opponent,
            'date': date_map.get(gid, ''),
            'pos': pos,
            'toi_min': toi_s / 60.0,
            # raw off components
            'ixg_p60': ixg_p60, 'ixg_hd_p60': ixg_hd_p60,
            'xgf_p60': xgf_p60, 'pp_ixg_p60': pp_ixg_p60,
            'g_pa_p60': g_pa_p60,
            # raw dfn components
            'npk_xga_p60': npk_xga_p60,
            'net_puck_p60': net_puck_p60,
            'pk_xga_p60': (pk_xga / (pk_toi_s / 3600.0)) if pk_toi_s >= 60 else None,
            # tracking
            'has_tracking': has_tracking,
            'trk_off_pts': trk_off_pts,
            'trk_dz_pts': trk_dz_pts,
            'trk_ed_pts': trk_ed_pts,
        })

    if not records:
        conn.close()
        return

    # ── 4. Normalize each raw component across all player-games ──────────────
    positions = [r['pos'] for r in records]

    def _norm(vals):
        return _norm_pool(vals, positions, norm_sd=norm_sd)

    ixg_n      = _norm([r['ixg_p60']      for r in records])
    ixg_hd_n   = _norm([r['ixg_hd_p60']   for r in records])
    xgf_n      = _norm([r['xgf_p60']      for r in records])
    pp_ixg_n   = _norm([r['pp_ixg_p60']   for r in records])
    g_pa_n     = _norm([r['g_pa_p60']      for r in records])
    npk_xga_n  = _norm([-r['npk_xga_p60'] for r in records])   # negated
    net_puck_n = _norm([r['net_puck_p60']  for r in records])

    # PK performance: D only, >= 1 min PK time; neutral 75 for all others
    pk_perf_n = [75.0] * len(records)
    pk_indices = [i for i, r in enumerate(records)
                  if r['pos'] == 'D' and r['pk_xga_p60'] is not None]
    if pk_indices:
        pk_vals = [-records[i]['pk_xga_p60'] for i in pk_indices]  # negated: lower xGA = better
        pk_pos  = [records[i]['pos'] for i in pk_indices]
        pk_normed = normalize_by_position_group(list(zip(pk_vals, pk_pos)), norm_sd=norm_sd)
        for j, idx in enumerate(pk_indices):
            pk_perf_n[idx] = max(0.0, min(100.0, pk_normed[j]))

    # Tracking off/dfn: normalize raw point totals across games that have tracking
    trk_off_n = trk_dfn_n = None
    trk_indices = [i for i, r in enumerate(records) if r['has_tracking']]
    if trk_indices:
        trk_off_raw = [records[i]['trk_off_pts']
                       for i in trk_indices]
        trk_dfn_raw = [
            (records[i]['trk_dz_pts'] * 0.45 + records[i]['trk_ed_pts'] * 0.55)
            / (max(records[i]['toi_min'], 5) if records[i]['pos'] == 'D' else 1.0)
            for i in trk_indices
        ]
        trk_positions = [records[i]['pos'] for i in trk_indices]
        trk_off_normed  = normalize_by_position_group(list(zip(trk_off_raw, trk_positions)), norm_sd=norm_sd)
        trk_dfn_normed  = normalize_by_position_group(list(zip(trk_dfn_raw, trk_positions)), norm_sd=norm_sd)
        trk_off_n  = {trk_indices[j]: max(0.0, min(100.0, v)) for j, v in enumerate(trk_off_normed)}
        trk_dfn_n  = {trk_indices[j]: max(0.0, min(100.0, v)) for j, v in enumerate(trk_dfn_normed)}

    # ── 5. Blend and write rows ───────────────────────────────────────────────
    out_rows = []
    for i, r in enumerate(records):
        pos = r['pos']
        is_fwd = pos in _FWD
        is_d   = pos == 'D'

        # MP off blend
        if is_fwd:
            mp_off = (0.20 * ixg_n[i] + 0.15 * ixg_hd_n[i] +
                      0.15 * xgf_n[i] + 0.20 * pp_ixg_n[i] + 0.30 * g_pa_n[i])
        else:
            mp_off = (0.20 * ixg_n[i] + 0.15 * ixg_hd_n[i] +
                      0.45 * xgf_n[i] + 0.20 * g_pa_n[i])

        # MP dfn blend — D gets PK performance component; tracked games reduce
        # net_puck weight since giveaways are captured in tracking exit score
        if is_d:
            if r['has_tracking']:
                mp_dfn = (0.65 * npk_xga_n[i] + 0.15 * net_puck_n[i]
                          + 0.20 * pk_perf_n[i])
            else:
                mp_dfn = (0.50 * npk_xga_n[i] + 0.30 * net_puck_n[i]
                          + 0.20 * pk_perf_n[i])
        elif r['has_tracking']:
            mp_dfn = 0.80 * npk_xga_n[i] + 0.20 * net_puck_n[i]
        else:
            mp_dfn = 0.60 * npk_xga_n[i] + 0.40 * net_puck_n[i]

        # Tracking blend: fwd off=50%, fwd dfn=30%, D off=30%, D dfn=50%
        trk_off_w = (0.50 if is_fwd else 0.30) if r['has_tracking'] else 0.0
        trk_dfn_w = (0.50 if is_d   else 0.30) if r['has_tracking'] else 0.0

        if r['has_tracking'] and trk_off_n and i in trk_off_n:
            off_final = (1 - trk_off_w) * mp_off + trk_off_w * trk_off_n[i]
            dfn_final = (1 - trk_dfn_w) * mp_dfn + trk_dfn_w * trk_dfn_n[i]
        else:
            off_final = mp_off
            dfn_final = mp_dfn

        off_final = max(0.0, min(100.0, off_final))
        dfn_final = max(0.0, min(100.0, dfn_final))

        off_w, dfn_w = (0.80, 0.20) if is_fwd else (0.20, 0.80)
        overall = max(0.0, min(100.0, off_w * off_final + dfn_w * dfn_final))

        out_rows.append((
            r['pid'], r['gid'], r['season'],
            r['name'],
            r['team'], r['opponent'], r['date'], r['pos'],
            round(r['toi_min'], 1),
            round(off_final, 1), round(dfn_final, 1), round(overall, 1),
            int(r['has_tracking']),
        ))

    conn.executemany(
        '''INSERT OR REPLACE INTO player_game_grades
           (player_id, game_id, season, name, team, opponent, game_date, position,
            toi_min, off, dfn, overall, has_tracking)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        out_rows
    )
    conn.commit()
    conn.close()
    print(f'  Game grades stored ({len(out_rows)} player-game rows).', flush=True)
