import json
import time
import requests
from typing import Dict, List, Optional
from models import Shift, PlayerInfo, GameContext

# ─── Roster cache — same team+season fetched once per process ────────────────
_roster_cache: Dict[tuple, Dict[int, PlayerInfo]] = {}


def _get(url: str, retries: int = 5) -> requests.Response:
    """GET with exponential backoff on 429 / 5xx errors."""
    delay = 2.0
    for attempt in range(retries):
        r = requests.get(url, timeout=20)
        if r.status_code == 429 or r.status_code >= 500:
            if attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
        r.raise_for_status()
        return r
    r.raise_for_status()  # final raise
    return r


def time_to_seconds(time_str: str) -> int:
    m, s = map(int, time_str.split(':'))
    return m * 60 + s


def load_game_from_file(filepath: str) -> dict:
    with open(filepath) as f:
        return json.load(f)


def fetch_game(game_id: int) -> dict:
    url = f"https://api-web.nhle.com/v1/gamecenter/{game_id}/play-by-play"
    r = _get(url)
    return r.json()


def fetch_shifts(game_id: int) -> List[Shift]:
    url = f"https://api.nhle.com/stats/rest/en/shiftcharts?cayenneExp=gameId={game_id}"
    r = _get(url)
    data = r.json()

    shifts = []
    for s in data['data']:
        try:
            shift = Shift(
                player_id=s['playerId'],
                team_id=s['teamId'],
                period=s['period'],
                start_sec=time_to_seconds(s['startTime']),
                end_sec=time_to_seconds(s['endTime'])
            )
            shifts.append(shift)
        except Exception:
            continue

    return shifts


def fetch_toi_from_boxscore(game_id: int) -> Dict[int, int]:
    """Fallback: return {player_id: toi_seconds} from boxscore when shift chart is unavailable."""
    url = f"https://api-web.nhle.com/v1/gamecenter/{game_id}/boxscore"
    r = _get(url)
    data = r.json()

    toi_map: Dict[int, int] = {}
    for side in ('homeTeam', 'awayTeam'):
        team_stats = data.get('playerByGameStats', {}).get(side, {})
        for group in ('forwards', 'defense', 'goalies'):
            for player in team_stats.get(group, []):
                pid = player.get('playerId')
                toi_str = player.get('toi', '0:00')
                if pid:
                    toi_map[pid] = time_to_seconds(toi_str)

    return toi_map


def fetch_roster(team_abbrev: str, season: str, team_id: int, team_name: str) -> Dict[int, PlayerInfo]:
    cache_key = (team_abbrev, season)
    if cache_key in _roster_cache:
        return _roster_cache[cache_key]

    url = f"https://api-web.nhle.com/v1/roster/{team_abbrev}/{season}"
    r = _get(url)
    data = r.json()

    players = {}
    for group in ['forwards', 'defensemen', 'goalies']:
        for player in data.get(group, []):
            pid = player['id']
            name = f"{player['firstName']['default']} {player['lastName']['default']}"
            pos = player.get('positionCode', 'F')
            players[pid] = PlayerInfo(
                player_id=pid,
                name=name,
                position=pos,
                team=team_abbrev,
                team_id=team_id
            )

    _roster_cache[cache_key] = players
    return players


def build_game_context(game_data: dict) -> GameContext:
    return GameContext(
        game_id=game_data['id'],
        season=game_data['season'],
        home_team_id=game_data['homeTeam']['id'],
        away_team_id=game_data['awayTeam']['id'],
        home_team_name=game_data['homeTeam']['commonName']['default'],
        away_team_name=game_data['awayTeam']['commonName']['default'],
        home_team_abbrev=game_data['homeTeam']['abbrev'],
        away_team_abbrev=game_data['awayTeam']['abbrev']
    )


def build_roster_from_pbp(game_data: dict, ctx) -> Dict[int, PlayerInfo]:
    """Build player map from rosterSpots in the PBP — only players who actually dressed."""
    home_id = game_data['homeTeam']['id']
    players = {}
    for spot in game_data.get('rosterSpots', []):
        pid = spot['playerId']
        name = f"{spot['firstName']['default']} {spot['lastName']['default']}"
        pos = spot.get('positionCode', 'F')
        team_id = spot['teamId']
        if team_id == home_id:
            team_abbrev = ctx.home_team_abbrev
            team_name = ctx.home_team_name
        else:
            team_abbrev = ctx.away_team_abbrev
            team_name = ctx.away_team_name
        players[pid] = PlayerInfo(
            player_id=pid,
            name=name,
            position=pos,
            team=team_abbrev,
            team_name=team_name
        )
    return players


def is_on_ice(player_id: int, period: int, time_sec: int, shifts: List[Shift]) -> bool:
    for shift in shifts:
        if (shift.player_id == player_id and
                shift.period == period and
                shift.start_sec <= time_sec <= shift.end_sec):
            return True
    return False


def load_all(game_id: int, season: str = '20252026', from_file: Optional[str] = None, verbose: bool = True) -> tuple:
    game_data = load_game_from_file(from_file) if from_file else fetch_game(game_id)

    ctx = build_game_context(game_data)
    if verbose:
        print(f"  {ctx.away_team_name} @ {ctx.home_team_name}")

    all_players = build_roster_from_pbp(game_data, ctx)

    shifts = fetch_shifts(game_id)

    boxscore_toi: Dict[int, int] = {}
    if not shifts:
        boxscore_toi = fetch_toi_from_boxscore(game_id)

    return game_data, ctx, all_players, shifts, boxscore_toi