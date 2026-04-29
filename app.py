import json
import sqlite3
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for
from pipeline import process_game, grade_game
from season import build_season_grades, fetch_schedule, TEAMS, SEASON
from grader import score_to_letter
import manual_loader

app = Flask(__name__)

# Simple in-memory cache so the season data is only computed once per run
_season_cache = None


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


@app.route('/')
def index():
    global _season_cache
    if _season_cache is None:
        print('Building season grades...')
        _season_cache = build_season_grades()
        print(f"Done — {len(_season_cache['players'])} players across {_season_cache['total_games']} games.")
    return render_template('season.html', data=_season_cache)


@app.route('/refresh')
def refresh():
    global _season_cache
    _season_cache = None
    return redirect(url_for('index'))


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

        games.append({'id': gid, 'label': label, 'group': group,
                      'round': round_num, 'series': series_num, 'game_num': game_num,
                      'away': away, 'home': home,
                      'away_logo': away_logo, 'home_logo': home_logo})
    return games


@app.route('/game-lookup')
def game_lookup():
    return render_template('index.html', games=_get_cached_game_list())


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


if __name__ == '__main__':
    print('Warming microstat cache...', flush=True)
    manual_loader.load_microstat_grades()
    print('Building season grades...', flush=True)
    _season_cache = build_season_grades()
    print(f"Done — {len(_season_cache['players'])} players across {_season_cache['total_games']} games.", flush=True)
    print('Ensuring game dates...', flush=True)
    _ensure_game_dates()
    app.run(debug=True, port=5001, use_reloader=False)
