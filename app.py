import json
import os
import sqlite3
import threading
import requests
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, jsonify
from pipeline import process_game, grade_game
from season import build_season_grades, fetch_schedule, TEAMS, SEASON, GRADE_DESCRIPTIONS, _fetch_standings
from grader import score_to_letter, normalize_by_position_group
import manual_loader
import play_grader
import fo_grade_loader

app = Flask(__name__)


@app.route('/apple-touch-icon.png')
def apple_touch_icon():
    from flask import send_from_directory
    return send_from_directory('static', 'apple-touch-icon.png')

# Per-season cache: {'20252026': {...}, '20242025': {...}}
_season_cache: dict = {}
_season_build_lock = threading.Lock()   # single global lock guards all builds
_extended_cache: dict = {}             # season_str -> list of extended grade dicts

_CACHE_DIR = os.path.join(os.path.dirname(__file__), 'season_cache')


def _disk_cache_path(season_str: str) -> str:
    os.makedirs(_CACHE_DIR, exist_ok=True)
    return os.path.join(_CACHE_DIR, f'{season_str}.json')


def _game_count(season_str: str) -> int:
    prefix = season_str[:4]
    conn = sqlite3.connect('cache.db')
    n = conn.execute(
        "SELECT COUNT(*) FROM games WHERE CAST(game_id AS TEXT) LIKE ?",
        (f'{prefix}%',)
    ).fetchone()[0]
    conn.close()
    return n


def _load_disk_cache(season_str: str) -> dict | None:
    path = _disk_cache_path(season_str)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            cached = json.load(f)
        if cached.get('_game_count') != _game_count(season_str):
            return None
        return cached
    except Exception:
        return None


def _save_disk_cache(season_str: str, data: dict) -> None:
    path = _disk_cache_path(season_str)
    try:
        with open(path, 'w') as f:
            json.dump(dict(data, _game_count=_game_count(season_str)), f)
    except Exception as e:
        print(f'  Warning: could not save season cache ({e})', flush=True)


def _grade_bg(score) -> str:
    if score is None:
        return '#374151'
    score = float(score)
    if score >= 90: return '#0d9488'
    if score >= 80: return '#16a34a'
    if score >= 70: return '#4d7c0f'
    if score >= 60: return '#b45309'
    if score >= 50: return '#c2410c'
    return '#b91c1c'


def _delta_bg(delta) -> str:
    if delta > 0:  return 'rgba(74,222,128,.15)'
    if delta < 0:  return 'rgba(248,113,113,.12)'
    return 'transparent'


def _delta_color(delta) -> str:
    if delta > 0:  return '#4ade80'
    if delta < 0:  return '#f87171'
    return '#9ca3af'


app.jinja_env.filters['grade_bg']    = _grade_bg
app.jinja_env.filters['delta_bg']    = _delta_bg
app.jinja_env.filters['delta_color'] = _delta_color


_SEASONS = [('20252026', '2025–26'), ('20242025', '2024–25')]


def _get_season_data(season_str: str) -> dict:
    if season_str in _season_cache:
        return _season_cache[season_str]
    with _season_build_lock:
        if season_str not in _season_cache:
            cached = _load_disk_cache(season_str)
            if cached:
                print(f'  Loaded {season_str} from disk cache ({len(cached["players"])} players).', flush=True)
                _season_cache[season_str] = cached
            else:
                print(f'Building season grades for {season_str}...', flush=True)
                data = build_season_grades(season_str)
                _season_cache[season_str] = data
                print(f"Done — {len(data['players'])} players across {data['total_games']} games.", flush=True)
                _save_disk_cache(season_str, data)
    return _season_cache[season_str]


@app.route('/')
def index():
    season_str = request.args.get('season', '20252026')
    if season_str not in dict(_SEASONS):
        season_str = '20252026'
    data = _get_season_data(season_str)
    return render_template('season.html', data=data, active_season=season_str, seasons=_SEASONS, grade_desc=GRADE_DESCRIPTIONS)


@app.route('/api/player/<int:player_id>/games')
def player_games(player_id: int):
    season_str = request.args.get('season', '20252026')
    mp_year = int(season_str[:4])
    conn = sqlite3.connect('cache.db')
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        '''SELECT game_id, team, opponent, game_date, position, toi_min,
                  off, dfn, overall, has_tracking
           FROM player_game_grades
           WHERE player_id=? AND season=? AND CAST(game_id AS TEXT) LIKE ?
           ORDER BY game_date ASC, game_id ASC''',
        (player_id, mp_year, f'{mp_year}02%')
    ).fetchall()
    conn.close()
    from flask import jsonify
    return jsonify([dict(r) for r in rows])


@app.route('/api/season-extended/<season_str>')
def season_extended_api(season_str):
    if season_str not in dict(_SEASONS):
        return jsonify([])
    if season_str in _extended_cache:
        return jsonify(_extended_cache[season_str])
    mp_year = int(season_str[:4])
    conn = sqlite3.connect('cache.db')
    rows = conn.execute(
        "SELECT player_id, toi_min, off, dfn, overall FROM player_game_grades "
        "WHERE season=? AND CAST(game_id AS TEXT) LIKE ?",
        [mp_year, f'{mp_year}02%']
    ).fetchall()
    conn.close()
    acc = {}
    for pid, toi_min, off, dfn, overall in rows:
        if pid not in acc:
            acc[pid] = {'gp': 0, 'toi': 0.0, 'off_w': 0.0, 'dfn_w': 0.0, 'overall_w': 0.0}
        d = acc[pid]
        toi = toi_min or 0.0
        d['gp'] += 1
        d['toi'] += toi
        d['off_w'] += (off or 0.0) * toi
        d['dfn_w'] += (dfn or 0.0) * toi
        d['overall_w'] += (overall or 0.0) * toi
    raw = []
    for pid, d in acc.items():
        if d['toi'] < 60:
            continue
        raw.append({
            'player_id': pid,
            'gp': d['gp'],
            'off': d['off_w'] / d['toi'],
            'dfn': d['dfn_w'] / d['toi'],
            'overall': d['overall_w'] / d['toi'],
        })

    def _renorm(vals, target_mean=60.0, target_sd=12.0):
        import statistics
        if len(vals) < 2:
            return vals
        m = statistics.mean(vals)
        s = statistics.stdev(vals) or 1.0
        return [max(0.0, min(100.0, (v - m) / s * target_sd + target_mean)) for v in vals]

    for col in ('off', 'dfn', 'overall'):
        normed = _renorm([r[col] for r in raw])
        for r, v in zip(raw, normed):
            r[col] = round(v, 1)

    result = raw
    _extended_cache[season_str] = result
    return jsonify(result)


@app.route('/refresh')
def refresh():
    season_str = request.args.get('season', '20252026')
    _season_cache.pop(season_str, None)
    _extended_cache.pop(season_str, None)
    path = _disk_cache_path(season_str)
    if os.path.exists(path):
        os.remove(path)
    manual_loader.invalidate_cache()
    play_grader.invalidate_cache()
    return redirect(url_for('index', season=season_str))


def _ensure_game_dates():
    """Populate game_dates table in cache.db from all team schedules (regular season + playoff)."""
    conn = sqlite3.connect('cache.db')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS game_dates (
            game_id INTEGER PRIMARY KEY,
            game_date TEXT NOT NULL
        )
    ''')
    conn.commit()

    # Find game IDs that are missing dates (from both play-grade cache and MP data)
    all_ids = {r[0] for r in conn.execute('SELECT game_id FROM games').fetchall()}
    try:
        all_ids |= {r[0] for r in conn.execute('SELECT DISTINCT game_id FROM moneypuck_games').fetchall()}
    except Exception:
        pass
    dated   = {r[0] for r in conn.execute('SELECT game_id FROM game_dates').fetchall()}
    missing = all_ids - dated
    if not missing:
        conn.close()
        return

    print(f'  Fetching dates for {len(missing)} games from NHL schedules...', flush=True)

    # First try to resolve from already-cached schedules (covers all seasons)
    found = {}
    for (data,) in conn.execute('SELECT data FROM schedules').fetchall():
        try:
            for g in json.loads(data):
                gid = g.get('id')
                date = g.get('gameDate', '')
                if gid and date and gid in missing:
                    found[gid] = date
        except Exception:
            pass

    # Fall back to live API for any still-missing games in the current season
    still_missing = missing - set(found)
    for team in TEAMS:
        if not still_missing:
            break
        try:
            games = fetch_schedule(team, SEASON)
            for g in games:
                gid = g.get('id')
                date = g.get('gameDate', '')
                if gid and date and gid in still_missing:
                    found[gid] = date
                    still_missing.discard(gid)
        except Exception:
            pass

    if found:
        conn.executemany('INSERT OR IGNORE INTO game_dates (game_id, game_date) VALUES (?,?)',
                         [(gid, date) for gid, date in found.items()])
        conn.commit()
        print(f'  Stored dates for {len(found)} games.', flush=True)
    conn.close()


def _get_cached_game_list():
    """Return list of game dicts for all RS + playoff games from schedule data."""
    conn = sqlite3.connect('cache.db')

    # Build full game map from all 32 team schedules (has home/away/date for every game)
    sched_map = {}
    for (data,) in conn.execute('SELECT data FROM schedules').fetchall():
        for g in json.loads(data):
            gid = g.get('id')
            if gid and gid not in sched_map:
                sched_map[gid] = g

    # Also include game_dates for any games not in schedules
    date_map = {r[0]: r[1] for r in conn.execute('SELECT game_id, game_date FROM game_dates').fetchall()}

    # Override scores from boxscore cache (schedule endpoint omits scores for recent playoff games)
    try:
        score_map = {r[0]: (r[1], r[2]) for r in conn.execute(
            'SELECT game_id, home_score, away_score FROM game_scores'
        ).fetchall()}
    except Exception:
        score_map = {}

    # Set of game_ids that have full play-grade data cached
    cached_gids = {r[0] for r in conn.execute('SELECT game_id FROM games').fetchall()}

    # Set of game_ids that have per-game grades
    try:
        graded_gids = {r[0] for r in conn.execute('SELECT DISTINCT game_id FROM player_game_grades').fetchall()}
    except Exception:
        graded_gids = set()

    conn.close()

    # Use all game IDs from schedules (covers RS + playoffs for both seasons)
    all_gids = sorted(sched_map.keys())

    games = []
    for gid in all_gids:
        gid_s = str(gid)
        game_type = int(gid_s[4:6]) if len(gid_s) >= 6 else 0
        if game_type == 1:   # skip preseason only
            continue

        g = sched_map.get(gid, {})
        away = g.get('awayTeam', {}).get('abbrev', '')
        home = g.get('homeTeam', {}).get('abbrev', '')
        date_str = g.get('gameDate', '') or date_map.get(gid, '')
        away_logo = g.get('awayTeam', {}).get('darkLogo', '')
        home_logo = g.get('homeTeam', {}).get('darkLogo', '')

        if not away:
            away, home = '???', '???'

        if away != '???' and not away_logo:
            away_logo = f'https://assets.nhle.com/logos/nhl/svg/{away}_dark.svg'
        if home != '???' and not home_logo:
            home_logo = f'https://assets.nhle.com/logos/nhl/svg/{home}_dark.svg'

        date_label = datetime.strptime(date_str, '%Y-%m-%d').strftime('%b %d') if date_str else ''
        home_score = g.get('homeTeam', {}).get('score')
        away_score = g.get('awayTeam', {}).get('score')
        if gid in score_map:
            home_score, away_score = score_map[gid]

        if game_type == 3:
            # Playoff: YYYY03RRSGN \u2192 [6:8]=round, [8]=series, [9]=game_num
            round_num  = int(gid_s[6:8]) if len(gid_s) >= 8  else 0
            series_num = int(gid_s[8])   if len(gid_s) >= 9  else 0
            game_num   = int(gid_s[9])   if len(gid_s) >= 10 else 0
            group      = 'Playoffs'
        else:
            round_num  = 0
            series_num = 0
            game_num   = int(gid_s[6:10]) if len(gid_s) >= 10 else 0
            group      = f'Regular Season \u2014 {away} vs {home}'
        label = f'{away} vs {home}  ({date_label})' if date_label else f'{away} vs {home}'

        season_year = int(gid_s[:4])
        season_str  = f"{season_year}{season_year + 1}"
        games.append({'id': gid, 'label': label, 'group': group,
                      'round': round_num, 'series': series_num, 'game_num': game_num,
                      'away': away, 'home': home,
                      'away_logo': away_logo, 'home_logo': home_logo,
                      'season': season_str,
                      'home_score': home_score, 'away_score': away_score,
                      'has_grades': gid in graded_gids,
                      'has_play_grades': gid in cached_gids})
    return games


@app.route('/methodology')
def methodology():
    return render_template('methodology.html', desc=GRADE_DESCRIPTIONS)


@app.route('/game-lookup')
def game_lookup():
    active_season = request.args.get('season', '20252026')
    if active_season not in dict(_SEASONS):
        active_season = '20252026'
    standings = _fetch_standings(active_season)
    team_pts = {abbrev: d['pts'] for abbrev, d in standings.items()}
    return render_template('index.html', games=_get_cached_game_list(),
                           active_season=active_season, seasons=_SEASONS,
                           team_pts=team_pts)


@app.route('/game-grades/<int:game_id>')
def game_grades(game_id):
    season = request.args.get('season', '20252026')
    mp_year = int(season[:4])
    conn = sqlite3.connect('cache.db')
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        '''SELECT player_id, name, team, opponent, game_date, position, toi_min,
                  off, dfn, overall, has_tracking
           FROM player_game_grades
           WHERE game_id=? AND season=?
           ORDER BY team, overall DESC''',
        (game_id, mp_year)
    ).fetchall()
    date_row = conn.execute('SELECT game_date FROM game_dates WHERE game_id=?', (game_id,)).fetchone()

    # MP per-game stats (all situations) for stat columns
    mp_stat_map = {}
    for r in conn.execute(
        '''SELECT player_id, goals, primary_assists, secondary_assists,
                  shots_on_goal, hits, giveaways, takeaways, pim,
                  onice_corsi_pct, onice_xg_pct, icetime
           FROM moneypuck_games
           WHERE game_id=? AND season=? AND situation='all' ''',
        (game_id, mp_year)
    ).fetchall():
        cf = r[9]
        xg = r[10]
        mp_stat_map[r[0]] = {
            'goals': int(r[1] or 0), 'assists': int((r[2] or 0) + (r[3] or 0)),
            'sog': int(r[4] or 0), 'hits': int(r[5] or 0),
            'gva': int(r[6] or 0), 'tka': int(r[7] or 0), 'pim': int(r[8] or 0),
            'cf_pct': f'{cf*100:.1f}' if cf else '—',
            'xg_pct': f'{xg*100:.1f}' if xg else '—',
        }

    # Blocks and FO% from tracked-game cache (266 games only)
    tracked_stat_map = {}
    games_row = conn.execute('SELECT player_stats FROM games WHERE game_id=?', (game_id,)).fetchone()
    if games_row:
        for pid_str, ps in json.loads(games_row[0]).items():
            fo_won  = (ps.get('es_fo_won', 0) or 0) + (ps.get('pp_fo_won', 0) or 0) + (ps.get('pk_fo_won', 0) or 0)
            fo_lost = (ps.get('es_fo_lost', 0) or 0) + (ps.get('pp_fo_lost', 0) or 0) + (ps.get('pk_fo_lost', 0) or 0)
            fo_tot  = fo_won + fo_lost
            tracked_stat_map[int(pid_str)] = {
                'blocks': int(ps.get('blocked_shots', 0) or 0),
                'fo_pct': f'{fo_won/fo_tot*100:.1f}' if fo_tot > 0 else None,
            }

    # Zone-weighted FO grades from fo_grades table (PBP-derived, tracked games only)
    fo_grade_raw = {
        r[0]: (r[1], r[2])  # pid -> (weighted_sum, total_fo)
        for r in conn.execute(
            'SELECT player_id, weighted, total_fo FROM fo_grades WHERE game_id=?', (game_id,)
        ).fetchall()
    }

    conn.close()

    game_date = date_row[0] if date_row else ''
    sched_map = {}
    conn2 = sqlite3.connect('cache.db')
    for (data,) in conn2.execute('SELECT data FROM schedules').fetchall():
        for g in json.loads(data):
            if g.get('id') == game_id:
                sched_map = g
                break
        if sched_map:
            break
    conn2.close()

    away = sched_map.get('awayTeam', {}).get('abbrev', '')
    home = sched_map.get('homeTeam', {}).get('abbrev', '')
    away_score = sched_map.get('awayTeam', {}).get('score', '')
    home_score = sched_map.get('homeTeam', {}).get('score', '')
    away_logo = sched_map.get('awayTeam', {}).get('darkLogo', '') or (f'https://assets.nhle.com/logos/nhl/svg/{away}_dark.svg' if away else '')
    home_logo = sched_map.get('homeTeam', {}).get('darkLogo', '') or (f'https://assets.nhle.com/logos/nhl/svg/{home}_dark.svg' if home else '')

    has_play_grades = bool(tracked_stat_map)

    grades = []
    for r in rows:
        d = dict(r)
        d['overall_letter'] = score_to_letter(d['overall'])
        pid = d['player_id']
        mp = mp_stat_map.get(pid, {})
        trk = tracked_stat_map.get(pid, {})
        d['goals']   = mp.get('goals', 0)
        d['assists'] = mp.get('assists', 0)
        d['sog']     = mp.get('sog', 0)
        d['hits']    = mp.get('hits', 0)
        d['gva']     = mp.get('gva', 0)
        d['tka']     = mp.get('tka', 0)
        d['pim']     = mp.get('pim', 0)
        d['cf_pct']  = mp.get('cf_pct', '—')
        d['xg_pct']  = mp.get('xg_pct', '—')
        d['blocks']  = trk.get('blocks', '—') if trk else '—'
        d['fo_pct']  = trk.get('fo_pct') if trk else None
        d['_fo_raw'] = fo_grade_raw.get(pid)  # (weighted_sum, total_fo) or None
        grades.append(d)

    # Normalize zone-weighted FO scores across players who took faceoffs this game
    fo_eligible = [d for d in grades if d['_fo_raw'] and d['_fo_raw'][1] > 0]
    if fo_eligible:
        fo_vals = [d['_fo_raw'][0] for d in fo_eligible]
        fo_pos  = [d['position']   for d in fo_eligible]
        fo_normed = normalize_by_position_group(list(zip(fo_vals, fo_pos)))
        for d, normed in zip(fo_eligible, fo_normed):
            score = max(0.0, min(100.0, normed))
            d['fo_grade']  = round(score, 1)
            d['fo_letter'] = score_to_letter(score)
    for d in grades:
        d.pop('_fo_raw', None)
        if 'fo_grade' not in d:
            d['fo_grade']  = None
            d['fo_letter'] = None

    return render_template('game_grades.html',
        game_id=game_id, season=season,
        game_date=game_date,
        away=away, home=home,
        away_score=away_score, home_score=home_score,
        away_logo=away_logo, home_logo=home_logo,
        grades=grades,
        has_play_grades=has_play_grades,
    )


@app.route('/game/<int:game_id>')
def game(game_id):
    from flask import abort
    abort(404)
    season = request.args.get('season', '20252026')  # unreachable
    season = request.args.get('season', '20252026')
    player_stats, all_players, ctx, game_data, play_log = process_game(
        game_id=game_id, season=season, verbose=False
    )
    grades = grade_game(player_stats, all_players, game_id=game_id)

    # When PBP Corsi/xG parsing fails entirely, all players show cf_pct=0.0.
    # Fall back to moneypuck_games for display and substitute stable MP-based grades.
    pbp_ok = any(float(r['cf_pct']) > 0.0 for r in grades)
    if not pbp_ok:
        mp_year = int(season[:4])
        conn = sqlite3.connect('cache.db')
        mp_map = {
            r[0]: {
                'cf_pct': f'{r[1]*100:.1f}' if r[1] else '—',
                'xg_pct': f'{r[2]*100:.1f}' if r[2] else '—',
            }
            for r in conn.execute(
                "SELECT player_id, onice_corsi_pct, onice_xg_pct "
                "FROM moneypuck_games WHERE game_id=? AND season=? AND situation='all'",
                (game_id, mp_year)
            ).fetchall()
        }
        pg_map = {
            r[0]: {
                'overall': r[1], 'overall_letter': score_to_letter(r[1]),
                'off': r[2], 'off_letter': score_to_letter(r[2]),
                'dfn': r[3], 'dfn_letter': score_to_letter(r[3]),
            }
            for r in conn.execute(
                "SELECT player_id, overall, off, dfn "
                "FROM player_game_grades WHERE game_id=? AND season=?",
                (game_id, mp_year)
            ).fetchall()
        }
        conn.close()
        for r in grades:
            pid = r['player_id']
            if pid in mp_map:
                r['cf_pct'] = mp_map[pid]['cf_pct']
                r['xg_pct'] = mp_map[pid]['xg_pct']
            if pid in pg_map:
                r.update(pg_map[pid])

    play_logs: dict[int, list] = {}
    for row in grades:
        pid     = row['player_id']
        running = 0.0
        entries = []
        for p, t, desc, d in play_log.get(pid, []):
            running += d
            entries.append({'period': p, 'time': t, 'desc': desc,
                            'delta': round(d, 2), 'running': round(running, 2)})
        play_logs[pid] = entries

    teams = sorted({r['team'] for r in grades})

    return render_template('game.html',
        grades         = grades,
        away           = ctx.away_team_name,
        away_abbrev    = ctx.away_team_abbrev,
        away_logo      = game_data['awayTeam'].get('darkLogo', ''),
        home           = ctx.home_team_name,
        home_abbrev    = ctx.home_team_abbrev,
        home_logo      = game_data['homeTeam'].get('darkLogo', ''),
        away_score     = game_data['awayTeam']['score'],
        home_score     = game_data['homeTeam']['score'],
        game_id        = game_id,
        season         = season,
        teams          = teams,
        play_logs_json = json.dumps(play_logs),
    )


def _fetch_player_bio(player_id: int) -> dict:
    """Fetch player bio from NHL API and cache in player_bios table."""
    conn = sqlite3.connect('cache.db')
    conn.execute('''CREATE TABLE IF NOT EXISTS player_bios
                    (player_id INTEGER PRIMARY KEY, data TEXT NOT NULL)''')
    conn.commit()
    row = conn.execute('SELECT data FROM player_bios WHERE player_id=?', (player_id,)).fetchone()
    if row:
        conn.close()
        return json.loads(row[0])
    try:
        url = f'https://api-web.nhle.com/v1/player/{player_id}/landing'
        d = requests.get(url, timeout=8).json()
        bio = {
            'headshot':    d.get('headshot', ''),
            'firstName':   d.get('firstName', {}).get('default', ''),
            'lastName':    d.get('lastName', {}).get('default', ''),
            'sweater':     d.get('sweaterNumber', ''),
            'position':    d.get('position', ''),
            'teamAbbrev':  d.get('currentTeamAbbrev', ''),
            'teamName':    d.get('fullTeamName', {}).get('default', '') if isinstance(d.get('fullTeamName'), dict) else d.get('fullTeamName', ''),
            'teamLogo':    f"https://assets.nhle.com/logos/nhl/svg/{d.get('currentTeamAbbrev', '')}_dark.svg" if d.get('currentTeamAbbrev') else '',
            'heightIn':    d.get('heightInInches'),
            'weightLbs':   d.get('weightInPounds'),
            'birthDate':   d.get('birthDate', ''),
            'birthCity':   d.get('birthCity', {}).get('default', '') if isinstance(d.get('birthCity'), dict) else d.get('birthCity', ''),
            'birthProv':   d.get('birthStateProvince', {}).get('default', '') if isinstance(d.get('birthStateProvince'), dict) else d.get('birthStateProvince', ''),
            'birthCountry':d.get('birthCountry', ''),
            'shoots':      d.get('shootsCatches', ''),
            'draft':       d.get('draftDetails'),
        }
        conn.execute('INSERT OR REPLACE INTO player_bios (player_id, data) VALUES (?,?)',
                     (player_id, json.dumps(bio)))
        conn.commit()
    except Exception:
        bio = {}
    conn.close()
    return bio


@app.route('/api/headshot/<int:player_id>')
def api_headshot(player_id):
    bio = _fetch_player_bio(player_id)
    url = bio.get('headshot', '')
    if not url:
        return '', 404
    try:
        resp = requests.get(url, timeout=8)
        from flask import Response
        return Response(resp.content, content_type=resp.headers.get('Content-Type', 'image/png'))
    except Exception:
        return '', 404


@app.route('/api/player/<int:player_id>')
def api_player(player_id):
    season_str = request.args.get('season', '20252026')
    bio = _fetch_player_bio(player_id)

    # Pull grade data from cached season
    grades = {}
    if season_str in _season_cache:
        all_players = _season_cache[season_str]['players']
        player = next((p for p in all_players if p['player_id'] == player_id), None)
        if player:
            pos_group = player.get('pos_group', '')
            ms_gp     = player.get('ms_gp', 0) or 0
            qualified = [p for p in all_players if p.get('qualified') and p.get('pos_group') == pos_group]
            pos_total = len(qualified)
            def _rank(key):
                ranked = sorted(qualified, key=lambda p: p.get(key) or 0, reverse=True)
                return next((i + 1 for i, p in enumerate(ranked) if p['player_id'] == player_id), None)
            grades = {
                'overall':           player.get('overall'),
                'overall_letter':    player.get('overall_letter'),
                'off':               player.get('off'),
                'off_letter':        player.get('off_letter'),
                'dfn':               player.get('dfn'),
                'dfn_letter':        player.get('dfn_letter'),
                'overall_mp':        player.get('overall_mp'),
                'overall_mp_letter': player.get('overall_mp_letter'),
                'off_mp':            player.get('off_mp'),
                'off_mp_letter':     player.get('off_mp_letter'),
                'dfn_mp':            player.get('dfn_mp'),
                'dfn_mp_letter':     player.get('dfn_mp_letter'),
                'overall_blend':     player.get('overall_blend'),
                'off_blend':         player.get('off_blend'),
                'off_blend_letter':  player.get('off_blend_letter'),
                'rank':              player.get('rank'),
                'blend_rank':        _rank('overall_blend'),
                'overall_mp_rank':   _rank('overall_mp'),
                'pos_group':         pos_group,
                'pos_total':         pos_total,
                'off_rank':          _rank('off'),
                'dfn_rank':          _rank('dfn'),
                'off_blend_rank':    _rank('off_blend'),
                'off_mp_rank':       _rank('off_mp'),
                'dfn_mp_rank':       _rank('dfn_mp'),
                'ms_gp':              ms_gp,
                'gp':                 player.get('gp'),
                'toi_per_game':       player.get('toi_per_game'),
                'goals':              player.get('goals'),
                'assists':            player.get('assists'),
                'points':             player.get('points'),
                'sub_toi_per_game':   player.get('sub_toi_per_game'),
                'sub_goals':          player.get('sub_goals'),
                'sub_assists':        player.get('sub_assists'),
                'sub_points':         player.get('sub_points'),
            }
    return jsonify({'player_id': player_id, 'bio': bio, 'grades': grades})


if __name__ == '__main__':
    print('Warming microstat cache...', flush=True)
    manual_loader.load_microstat_grades()
    print('Warming play grade cache...', flush=True)
    play_grader.load_play_grades()
    print('Warming FO grade cache...', flush=True)
    fo_grade_loader.load_fo_grades()
    for s_str, s_label in _SEASONS:
        print(f'Building season grades ({s_label})...', flush=True)
        _get_season_data(s_str)
    print('Ensuring game dates...', flush=True)
    _ensure_game_dates()
    app.run(debug=True, port=5001, use_reloader=False)
