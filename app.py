import json
import os
import sqlite3
import threading
import requests
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, jsonify
from pipeline import process_game, grade_game
from season import build_season_grades, fetch_schedule, TEAMS, SEASON, GRADE_DESCRIPTIONS
from grader import score_to_letter
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


@app.route('/refresh')
def refresh():
    season_str = request.args.get('season', '20252026')
    _season_cache.pop(season_str, None)
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

    # Find game IDs that are missing dates
    all_ids = {r[0] for r in conn.execute('SELECT game_id FROM games').fetchall()}
    dated   = {r[0] for r in conn.execute('SELECT game_id FROM game_dates').fetchall()}
    missing = all_ids - dated
    if not missing:
        conn.close()
        return

    print(f'  Fetching dates for {len(missing)} games from NHL schedules...', flush=True)

    # Build game_id -> date from all team schedules
    found = {}
    for team in TEAMS:
        try:
            games = fetch_schedule(team, SEASON)
            for g in games:
                gid = g.get('id')
                date = g.get('gameDate', '')
                if gid and date and gid in missing:
                    found[gid] = date
        except Exception:
            pass

    if found:
        conn.executemany('INSERT OR IGNORE INTO game_dates (game_id, game_date) VALUES (?,?)',
                         [(gid, date) for gid, date in found.items()])
        conn.commit()
        print(f'  Stored dates for {len(found)} games.', flush=True)
    conn.close()


def _get_cached_game_list():
    """Return list of {id, label, group} for all cached games, sorted by game_id."""
    conn = sqlite3.connect('cache.db')
    game_ids = [r[0] for r in conn.execute('SELECT game_id FROM games ORDER BY game_id').fetchall()]

    # Build lookup: game_id -> schedule entry (playoff games fetched via API)
    sched_map = {}
    for (data,) in conn.execute('SELECT data FROM schedules').fetchall():
        for g in json.loads(data):
            gid = g.get('id')
            if gid and gid not in sched_map:
                sched_map[gid] = g

    # Build lookup: game_id -> date from game_dates table
    date_map = {r[0]: r[1] for r in conn.execute('SELECT game_id, game_date FROM game_dates').fetchall()}
    conn.close()

    # Build lookup: short file_id (5 digits) -> (away, home) from xlsx filenames
    import re as _re
    from manual_loader import LOGS_DIR as _ML_LOGS_DIR
    _rs_dir = _ML_LOGS_DIR / 'Regular Season'
    _xlsx_teams: dict = {}
    if _rs_dir.exists():
        for _f in _rs_dir.glob('*.xlsx'):
            m = _re.match(r'^(\d+)\s+([A-Z]{2,3})\s+vs\.\s+([A-Z]{2,3})', _f.stem)
            if m:
                _xlsx_teams[int(m.group(1))] = (m.group(2), m.group(3))

    games = []
    for gid in game_ids:
        gid_s = str(gid)
        game_type = int(gid_s[4:6]) if len(gid_s) >= 6 else 0

        g = sched_map.get(gid, {})
        away = g.get('awayTeam', {}).get('abbrev', '')
        home = g.get('homeTeam', {}).get('abbrev', '')
        date_str = g.get('gameDate', '') or date_map.get(gid, '')
        away_logo = g.get('awayTeam', {}).get('darkLogo', '')
        home_logo = g.get('homeTeam', {}).get('darkLogo', '')

        # For regular season games not in schedules table, use xlsx filename
        if not away and game_type == 2:
            file_id = int(gid_s[-5:])
            if file_id in _xlsx_teams:
                away, home = _xlsx_teams[file_id]

        if not away:
            away, home = '???', '???'

        # Fill missing logos from the standard NHL CDN pattern
        if away != '???' and not away_logo:
            away_logo = f'https://assets.nhle.com/logos/nhl/svg/{away}_dark.svg'
        if home != '???' and not home_logo:
            home_logo = f'https://assets.nhle.com/logos/nhl/svg/{home}_dark.svg'

        date_label = datetime.strptime(date_str, '%Y-%m-%d').strftime('%b %d') if date_str else ''

        if game_type == 3:
            # Playoff: YYYY 03 R S G
            round_num  = int(gid_s[7]) if len(gid_s) == 10 else 0
            series_num = int(gid_s[8]) if len(gid_s) == 10 else 0
            game_num   = int(gid_s[9]) if len(gid_s) == 10 else 0

            # Sort teams alphabetically so same series always groups together
            if away != '???' and away > home:
                away, home = home, away
                away_logo, home_logo = home_logo, away_logo

            pair  = f'{away} vs {home}' if away != '???' else f'Series {series_num}'
            group = f'Round {round_num} \u2014 {pair}'
            label = f'Game {game_num}  ({date_label})' if date_label else f'Game {game_num}'
        else:
            # Regular season
            game_num   = int(gid_s[-5:])
            series_num = 0
            round_num  = 0
            group = f'Regular Season \u2014 {away} vs {home}'
            label = f'{away} vs {home}  ({date_label})' if date_label else f'{away} vs {home}'

        season_year = int(gid_s[:4])
        season_str  = f"{season_year}{season_year + 1}"
        games.append({'id': gid, 'label': label, 'group': group,
                      'round': round_num, 'series': series_num, 'game_num': game_num,
                      'away': away, 'home': home,
                      'away_logo': away_logo, 'home_logo': home_logo,
                      'season': season_str})
    return games


@app.route('/methodology')
def methodology():
    return render_template('methodology.html', desc=GRADE_DESCRIPTIONS)


@app.route('/game-lookup')
def game_lookup():
    active_season = request.args.get('season', '20252026')
    if active_season not in dict(_SEASONS):
        active_season = '20252026'
    return render_template('index.html', games=_get_cached_game_list(),
                           active_season=active_season, seasons=_SEASONS)


@app.route('/game/<int:game_id>')
def game(game_id):
    season = request.args.get('season', '20252026')
    player_stats, all_players, ctx, game_data, play_log = process_game(
        game_id=game_id, season=season, verbose=False
    )
    grades = grade_game(player_stats, all_players, game_id=game_id)

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
                'overall':        player.get('overall'),
                'overall_letter': player.get('overall_letter'),
                'off':            player.get('off'),
                'off_letter':     player.get('off_letter'),
                'dfn':            player.get('dfn'),
                'dfn_letter':     player.get('dfn_letter'),
                'rank':           player.get('rank'),
                'pos_group':      pos_group,
                'pos_total':      pos_total,
                'off_rank':       _rank('off'),
                'dfn_rank':       _rank('dfn'),
                'ms_gp':          ms_gp,
                'gp':             player.get('gp'),
                'toi_per_game':   player.get('toi_per_game'),
                'goals':          player.get('goals'),
                'assists':        player.get('assists'),
                'points':         player.get('points'),
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
