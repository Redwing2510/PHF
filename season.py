import json
import os
import sqlite3
import time
import statistics as _stats
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date as _date
from typing import Dict, List

from models import PlayerStats
from pipeline import process_game
from grader import normalize_by_position_group, score_to_letter
from loader import _get
from manual_loader import get_microstat_grade, get_microstat_record, _norm_name as _ml_norm_name
from play_grader import get_play_grade, get_season_def_aggregates
from fo_grade_loader import load_fo_grades, load_dz_fo_counts
from qoc_loader import load_matchup_totals
from tracking_grader import compute_tracking_grade as _compute_tracking_grade, compute_tracking_split as _compute_tracking_split

DB_PATH       = os.path.join(os.path.dirname(__file__), 'cache.db')
SCHEDULE_TTL  = 6 * 3600   # schedules re-fetched after 6 hours


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS schedules (
            team   TEXT NOT NULL,
            season TEXT NOT NULL,
            data   TEXT NOT NULL,
            saved_at REAL NOT NULL,
            PRIMARY KEY (team, season)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS games (
            game_id     INTEGER PRIMARY KEY,
            player_stats TEXT NOT NULL,
            all_players  TEXT NOT NULL,
            saved_at     REAL NOT NULL
        )
    """)
    con.commit()
    return con


def schedule_load(team: str, season: str):
    with _db() as con:
        row = con.execute(
            "SELECT data, saved_at FROM schedules WHERE team=? AND season=?",
            (team, season)
        ).fetchone()
    if row and (time.time() - row[1]) < SCHEDULE_TTL:
        return json.loads(row[0])
    return None


def schedule_save(team: str, season: str, games: list) -> None:
    with _db() as con:
        con.execute(
            "INSERT OR REPLACE INTO schedules VALUES (?,?,?,?)",
            (team, season, json.dumps(games), time.time())
        )


def game_load(game_id: int):
    with _db() as con:
        row = con.execute(
            "SELECT player_stats, all_players FROM games WHERE game_id=?",
            (game_id,)
        ).fetchone()
    if row:
        return json.loads(row[0]), json.loads(row[1])
    return None


def game_save(game_id: int, player_stats: dict, all_players: dict) -> None:
    with _db() as con:
        con.execute(
            "INSERT OR REPLACE INTO games VALUES (?,?,?,?)",
            (game_id, json.dumps(player_stats), json.dumps(all_players), time.time())
        )

PLAYOFF_TEAMS = ['CAR', 'OTT', 'MIN', 'DAL', 'COL', 'LAK', 'MTL', 'TBL',
                 'BUF', 'BOS', 'PHI', 'PIT', 'EDM', 'ANA', 'VGK', 'UTA']
TEAMS  = ['ANA', 'BOS', 'BUF', 'CAR', 'CBJ', 'CGY', 'CHI', 'COL',
          'DAL', 'DET', 'EDM', 'FLA', 'LAK', 'MIN', 'MTL', 'NSH',
          'NJD', 'NYI', 'NYR', 'OTT', 'PHI', 'PIT', 'SEA', 'SJS',
          'STL', 'TBL', 'TOR', 'UTA', 'VAN', 'VGK', 'WPG', 'WSH']
SEASON = '20252026'
PRIOR_MINUTES = 12.0   # Bayesian shrinkage prior (same as display_game)


def fetch_schedule(team_abbrev: str, season: str) -> list:
    cached = schedule_load(team_abbrev, season)
    if cached is not None:
        return cached
    print(f"Fetching {team_abbrev} playoff schedule...")
    url = f"https://api-web.nhle.com/v1/club-schedule-season/{team_abbrev}/{season}"
    r = _get(url)
    games = r.json().get('games', [])
    schedule_save(team_abbrev, season, games)
    return games


# ─── Stage 1: within-game normalization by position ──────────────────────────
def compute_per_game_scores(player_stats, all_players) -> Dict[int, float]:
    """
    Scale raw grades per-60 → Bayesian shrinkage → normalize by position
    group within this game (forwards vs D separately, mean=60 each).
    Returns {player_id: normalized_score}.
    """
    graded = [
        (pid, all_players[pid], player_stats[pid])
        for pid in player_stats
        if all_players.get(pid) and all_players[pid].position != 'G'
    ]
    if not graded:
        return {}

    per_60 = []
    for pid, info, stats in graded:
        toi_min = stats.toi_seconds / 60
        per_60.append(stats.raw_grade * (60 / toi_min) if toi_min > 0 else 0.0)

    mean_p60 = _stats.mean(per_60)

    scaled = []
    for (pid, info, stats), p60 in zip(graded, per_60):
        toi_min = stats.toi_seconds / 60
        w = toi_min / (toi_min + PRIOR_MINUTES)
        scaled.append(w * p60 + (1 - w) * mean_p60)

    grades_with_pos = [(g, info.position) for (_, info, _), g in zip(graded, scaled)]
    normalized = normalize_by_position_group(grades_with_pos)

    return {pid: score for (pid, _, _), score in zip(graded, normalized)}


# ─── Season accumulator ───────────────────────────────────────────────────────
@dataclass
class SeasonEntry:
    name: str
    team: str
    position: str
    gp: int = 0
    per_game_scores: List[float] = field(default_factory=list)
    raw_grade_total: float = 0.0
    goals: int = 0
    primary_assists: int = 0
    secondary_assists: int = 0
    shots_on_goal: int = 0
    hits: int = 0
    blocked_shots: int = 0
    giveaways: int = 0
    takeaways: int = 0
    toi_seconds: int = 0
    cf: int = 0
    ca: int = 0
    xgf: float = 0.0
    xga: float = 0.0
    ixg: float = 0.0
    pk_xga: float = 0.0
    pk_kills: int = 0
    es_fo_won: int = 0
    es_fo_lost: int = 0
    pp_fo_won: int = 0
    pp_fo_lost: int = 0
    pk_fo_won: int = 0
    pk_fo_lost: int = 0
    pim: int = 0
    fo_weighted_sum: float = 0.0
    fo_total: int = 0

    # Tracking sub-grade totals (accumulated from xlsx data across all games)
    raw_tracking_off_total:       float = 0.0
    raw_tracking_dz_exit_total:   float = 0.0
    raw_tracking_entry_dfn_total: float = 0.0
    ms_gp:          int = 0   # games with tracking data (used for MS mode filter)
    ms_toi_seconds: int = 0

    # DZ faceoff counts (from PBP, all games)
    dz_fo_won:  int = 0
    dz_fo_lost: int = 0

    # Block tracking
    pk_blocked_shots: int = 0

    # PK raw accumulator: pk_kills * 0.50 + pk_xga * -0.40 + pk_blocks * 0.40
    raw_pk_total: float = 0.0

    @property
    def assists(self):   return self.primary_assists + self.secondary_assists
    @property
    def points(self):    return self.goals + self.assists
    @property
    def cf_pct(self):
        t = self.cf + self.ca
        return f"{100*self.cf/t:.1f}" if t else '—'
    @property
    def xg_pct(self):
        t = self.xgf + self.xga
        return f"{100*self.xgf/t:.1f}" if t else '—'
    @property
    def toi_per_game(self):
        if not self.gp: return '—'
        s = self.toi_seconds / self.gp
        return f"{int(s//60)}:{int(s%60):02d}"
    @property
    def avg_score(self):
        """Overall score as raw_grade per hour, consistent with sub-score methodology."""
        toi_h = self.toi_seconds / 3600.0
        return (self.raw_grade_total / toi_h) if toi_h > 0 else 0.0


def main():
    # Collect unique playoff game IDs across both teams
    all_game_ids: set[int] = set()
    for team in TEAMS:
        print(f"Fetching {team} playoff schedule...")
        games = fetch_schedule(team, SEASON)
        for g in games:
            if g.get('gameType') == 3 and g.get('gameState') in ('OFF', 'FINAL'):
                all_game_ids.add(g['id'])

    game_ids = sorted(all_game_ids)
    print(f"\nFound {len(game_ids)} playoff games.\n")

    # Season accumulators: {player_id: SeasonEntry}
    season: Dict[int, SeasonEntry] = {}
    fo_grades_by_game = load_fo_grades()

    for i, game_id in enumerate(game_ids, 1):
        cached = game_load(game_id)
        if cached is not None:
            player_stats_raw, all_players_raw = cached
            from models import PlayerStats, PlayerInfo
            all_players = {int(k): PlayerInfo(**v) for k, v in all_players_raw.items()}
            player_stats = {}
            for k, v in player_stats_raw.items():
                s = PlayerStats(player_id=int(k))
                for attr, val in v.items():
                    if attr != 'player_id':
                        setattr(s, attr, val)
                player_stats[int(k)] = s
            print(f"  [{i}/{len(game_ids)}] {game_id} (cached)")
        else:
            try:
                player_stats, all_players, ctx, game_data, _ = process_game(
                    game_id=game_id,
                    season=SEASON,
                    verbose=False
                )
                game_save(
                    game_id,
                    {str(k): {f: getattr(v, f) for f in v.__dataclass_fields__} for k, v in player_stats.items()},
                    {str(k): {f: getattr(v, f) for f in v.__dataclass_fields__} for k, v in all_players.items()}
                )
                print(f"  [{i}/{len(game_ids)}] {game_id} ✓")
            except Exception as e:
                print(f"  [{i}/{len(game_ids)}] {game_id} ERROR: {e}")
                time.sleep(2)
                continue
            time.sleep(1.0)

        # Stage 1 — normalize within this game by position group
        per_game_scores = compute_per_game_scores(player_stats, all_players)

        for pid, score in per_game_scores.items():
            info  = all_players.get(pid)
            stats = player_stats.get(pid)
            if not info or not stats or info.team not in TEAMS or info.position == 'G':
                continue

            if pid not in season:
                season[pid] = SeasonEntry(
                    name=info.name, team=info.team, position=info.position
                )

            e = season[pid]
            e.gp                += 1
            e.per_game_scores.append(score)
            e.raw_grade_total   += stats.raw_grade
            e.goals             += stats.goals
            e.primary_assists   += stats.primary_assists
            e.secondary_assists += stats.secondary_assists
            e.shots_on_goal     += stats.shots_on_goal
            e.hits              += stats.hits
            e.blocked_shots     += stats.blocked_shots
            e.giveaways         += stats.giveaways
            e.takeaways         += stats.takeaways
            e.toi_seconds       += stats.toi_seconds
            e.cf                += stats.cf
            e.ca                += stats.ca
            e.xgf               += stats.xgf
            e.xga               += stats.xga
            e.ixg               += stats.ixg
            e.pk_xga            += stats.pk_xga
            e.es_fo_won         += stats.es_fo_won
            e.es_fo_lost        += stats.es_fo_lost
            e.pp_fo_won         += stats.pp_fo_won
            e.pp_fo_lost        += stats.pp_fo_lost
            e.pk_fo_won         += stats.pk_fo_won
            e.pk_fo_lost        += stats.pk_fo_lost
            e.pim               += stats.pim
            e.pk_kills          += stats.pk_kills
            fo_entry = fo_grades_by_game.get(game_id, {}).get(pid)
            if fo_entry:
                e.fo_weighted_sum += fo_entry[0]
                e.fo_total        += fo_entry[1]


    print(f"\nProcessed {len(game_ids)} games. Building leaderboards...\n")

    # ── Stage 2: normalize qualifying players only, project limited onto same curve ──
    # Qualifying threshold is per-team: must play ≥50% of that team's max GP
    team_max_gp: Dict[str, int] = {}
    for pid, e in season.items():
        team_max_gp[e.team] = max(team_max_gp.get(e.team, 0), e.gp)

    def min_gp_for(pid: int) -> int:
        return max(1, round(team_max_gp[season[pid].team] * 0.50))

    qual_pids = [pid for pid in season if season[pid].gp >= min_gp_for(pid)]
    lim_pids  = [pid for pid in season if season[pid].gp <  min_gp_for(pid)]

    # Normalize qualifying players by position group
    qual_scores = [season[pid].avg_score for pid in qual_pids]
    qual_pos    = [season[pid].position   for pid in qual_pids]
    normed_qual = normalize_by_position_group(list(zip(qual_scores, qual_pos)))
    final_scores: Dict[int, float] = {pid: normed_qual[i] for i, pid in enumerate(qual_pids)}

    # For each position group, compute the mean/SD from qualifying players
    # then project limited players onto that same scale
    import statistics as _st
    for pos_group, pos_set in [('fwd', {'C','L','R'}), ('def', {'D'})]:
        grp_scores = [normed_qual[i] for i, pid in enumerate(qual_pids) if season[pid].position in pos_set]
        if len(grp_scores) < 2:
            continue
        grp_raw = [season[pid].avg_score for pid in qual_pids if season[pid].position in pos_set]
        raw_mean = _st.mean(grp_raw)
        raw_sd   = _st.stdev(grp_raw) or 1.0
        for pid in lim_pids:
            if season[pid].position not in pos_set:
                continue
            z = (season[pid].avg_score - raw_mean) / raw_sd
            final_scores[pid] = 60.0 + z * 12.0

    # ── Sub-grade scores from accumulated stats ───────────────────────────────
    all_pids = list(season.keys())

    def _sub_raw(e: 'SeasonEntry') -> tuple:
        toi_h = e.toi_seconds / 3600.0 or 1e-6
        off  = (e.goals * 3.0 + e.primary_assists * 2.0
                + e.secondary_assists * 1.0
                + e.ixg * 2.0 + (e.xgf - e.ixg) * 1.5) / toi_h
        dfn  = (e.blocked_shots * 1.5 + e.hits * 0.3 + e.takeaways * 1.0) / toi_h
        xg_total = e.xgf + e.xga
        poss = (e.xgf / xg_total * 100) if xg_total > 0 else 50.0
        if e.position == 'D':
            dfn -= (e.xga / toi_h) * 1.5
            dfn += (e.pk_kills / toi_h) * 0.8
            dfn += (poss - 50) * 0.4
        else:
            dfn += (poss - 50) * 0.2
        fo_raw = (e.fo_weighted_sum / e.fo_total) if e.fo_total >= 10 else None
        return off, dfn, poss, fo_raw

    def _normalize_sub(raw_vals, positions):
        """Normalize a sub-grade vector using the same position-split curve."""
        return normalize_by_position_group(list(zip(raw_vals, positions)))

    off_raw   = [_sub_raw(season[p])[0] for p in all_pids]
    def_raw   = [_sub_raw(season[p])[1] for p in all_pids]
    poss_raw  = [_sub_raw(season[p])[2] for p in all_pids]
    pos_list  = [season[p].position      for p in all_pids]

    off_normed  = _normalize_sub(off_raw,  pos_list)
    def_normed  = _normalize_sub(def_raw,  pos_list)
    poss_normed = _normalize_sub(poss_raw, pos_list)

    entry_off_scores:  Dict[int, float] = {p: off_normed[i]  for i, p in enumerate(all_pids)}
    entry_def_scores:  Dict[int, float] = {p: def_normed[i]  for i, p in enumerate(all_pids)}
    entry_poss_scores: Dict[int, float] = {p: poss_normed[i] for i, p in enumerate(all_pids)}

    # FO: only normalize players who took at least 1 faceoff
    fo_pids = [p for p in all_pids if (_sub_raw(season[p])[3] is not None)]
    fo_raw_vals = [_sub_raw(season[p])[3] for p in fo_pids]
    fo_pos_list = [season[p].position     for p in fo_pids]
    if fo_pids:
        fo_normed_vals = _normalize_sub(fo_raw_vals, fo_pos_list)
        entry_fo_scores: Dict[int, float] = {p: fo_normed_vals[i] for i, p in enumerate(fo_pids)}
    else:
        entry_fo_scores = {}

    # ── Print leaderboard per team ────────────────────────────────────────────
    for team_abbrev in TEAMS:
        players = [(pid, e) for pid, e in season.items() if e.team == team_abbrev]
        if not players:
            print(f"No data for {team_abbrev}\n")
            continue

        gp_max    = max(season[pid].gp for pid, _ in players)
        team_name = players[0][1].team

        qualified = sorted([(pid, season[pid], final_scores[pid]) for pid, _ in players if season[pid].gp >= min_gp_for(pid)], key=lambda x: x[2], reverse=True)
        limited   = sorted([(pid, season[pid], final_scores[pid]) for pid, _ in players if season[pid].gp <  min_gp_for(pid)], key=lambda x: x[1].gp, reverse=True)
        ranked    = qualified + limited
        min_gp_team = min_gp_for(players[0][0])

        from rich.console import Console as _Console
        from rich.table import Table as _Table
        from rich import box as _box
        from rich.text import Text as _Text

        console = _Console(width=200)

        def _grade_color(score):
            if score >= 80: return "bold green"
            if score >= 70: return "green"
            if score >= 60: return "yellow"
            if score >= 50: return "dark_orange"
            return "red"

        def _fmt_grade(score, letter):
            return _Text(f"{letter} {score:.1f}", style=_grade_color(score))

        def _fmt_sub(score):
            return _Text(f"{score:.1f}", style=_grade_color(score))

        table = _Table(box=_box.SIMPLE_HEAD, show_header=True,
                       header_style="bold white on grey23",
                       show_edge=False, pad_edge=False,
                       title=f"[bold]{team_name} — 2025 Playoffs  ({gp_max} games)[/bold]",
                       title_style="bold white")

        table.add_column("#",      style="dim",  width=4,  justify="right")
        table.add_column("NAME",                 min_width=20)
        table.add_column("POS",                  width=4,  justify="center")
        table.add_column("GP",                   width=4,  justify="right")
        table.add_column("PHF GRD", header_style="bold cyan on grey23", width=10, justify="center")
        table.add_column("OFF",     header_style="cyan on grey23",       width=7,  justify="center")
        table.add_column("DEF",     header_style="cyan on grey23",       width=7,  justify="center")
        table.add_column("POSS",    header_style="cyan on grey23",       width=7,  justify="center")
        table.add_column("FO",      header_style="cyan on grey23",       width=7,  justify="center")
        table.add_column("TOI/gm",  width=8,  justify="right")
        table.add_column("CF%",     width=7,  justify="right")
        table.add_column("xG%",     width=7,  justify="right")
        table.add_column("G",       width=4,  justify="right")
        table.add_column("A",       width=4,  justify="right")
        table.add_column("PTS",     width=5,  justify="right")
        table.add_column("SOG",     width=5,  justify="right")
        table.add_column("HIT",     width=5,  justify="right")
        table.add_column("BLK",     width=5,  justify="right")
        table.add_column("GVA",     width=5,  justify="right")
        table.add_column("TKA",     width=5,  justify="right")

        printed_divider = False
        rank = 0
        for pid, entry, score in ranked:
            if not printed_divider and entry.gp < min_gp_for(pid):
                table.add_section()
                printed_divider = True
            rank += 1
            letter = score_to_letter(score)
            off_score  = entry_off_scores.get(pid, 60.0)
            def_score  = entry_def_scores.get(pid, 60.0)
            poss_score = entry_poss_scores.get(pid, 60.0)
            fo_score   = entry_fo_scores.get(pid, None)
            fo_total   = entry.es_fo_won + entry.es_fo_lost + entry.pp_fo_won + entry.pp_fo_lost + entry.pk_fo_won + entry.pk_fo_lost
            fo_display = _fmt_sub(fo_score) if fo_total > 0 and fo_score is not None else _Text("—", style="dim")
            table.add_row(
                str(rank),
                entry.name,
                entry.position,
                str(entry.gp),
                _fmt_grade(score, letter),
                _fmt_sub(off_score),
                _fmt_sub(def_score),
                _fmt_sub(poss_score),
                fo_display,
                entry.toi_per_game,
                str(entry.cf_pct),
                str(entry.xg_pct),
                str(entry.goals),
                str(entry.assists),
                str(entry.points),
                str(entry.shots_on_goal),
                str(entry.hits),
                str(entry.blocked_shots),
                str(entry.giveaways),
                str(entry.takeaways),
            )

        console.print(table)
        console.print()


def _fetch_standings(season_str: str) -> dict:
    """Return {abbrev: {w, l, otl, pts}} from the NHL standings API."""
    import ssl
    season_year = int(season_str[:4])
    next_year   = season_year + 1
    today       = _date.today()
    if today.year < next_year or (today.year == next_year and today.month <= 4):
        endpoint = 'now'
    else:
        endpoint = f'{next_year}-04-20'
    url = f'https://api-web.nhle.com/v1/standings/{endpoint}'
    try:
        ctx = ssl.create_default_context()
        try:
            import certifi
            ctx.load_verify_locations(certifi.where())
        except ImportError:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url, headers={'User-Agent': 'PHF/1.0'})
        with urllib.request.urlopen(req, timeout=8, context=ctx) as r:
            payload = json.loads(r.read().decode())
        out = {}
        target_sid = int(season_str)
        for entry in payload.get('standings', []):
            if int(entry.get('seasonId', 0)) != target_sid:
                continue
            abbrev = entry.get('teamAbbrev', {}).get('default', '')
            if abbrev:
                out[abbrev] = {
                    'w':   entry.get('wins', 0),
                    'l':   entry.get('losses', 0),
                    'otl': entry.get('otLosses', 0),
                    'pts': entry.get('points', 0),
                }
        return out
    except Exception as exc:
        print(f'  Warning: standings fetch failed ({exc})', flush=True)
        return {}


# ── Grade descriptions ────────────────────────────────────────────────────────
# Update these whenever the algorithm changes so tooltips stay accurate.
GRADE_DESCRIPTIONS = {
    'overall': 'Play-by-play score across all situations. Every shot, hit, turnover, faceoff, and on-ice event contributes. 60 = league average.',
    'off':     'Goals, assists, and individual expected goals (iXG) per 60 min, plus on-ice shot generation. Measures direct offensive contribution.',
    'dfn':     'Blocked shots and hits per 60 min. For defensemen, also includes shot suppression (xGA against), penalty killing, and possession impact.',
    'poss':    'On-ice expected goals percentage (xGF%). Above 50 means your team controls play when you\'re on the ice.',
    'fo':      'Faceoff score weighted by zone and situation. DZ wins worth more than OZ wins; shorthanded wins valued highest. Requires 10+ faceoffs.',
}


def build_season_grades(season_str: str = SEASON, teams: list = None) -> dict:
    """
    Process all regular season + playoff games and return structured grade data.
    Returns {'players': [...], 'teams': [...], 'team_meta': {...}, 'total_games': int}.
    Usable by both the Flask web app and any other consumer.
    """
    if teams is None:
        teams = TEAMS
    teams_set = set(teams)

    # ── collect game IDs from xlsx files only ────────────────────────────────
    all_game_ids: set[int] = set()
    from manual_loader import LOGS_DIR as _ML_LOGS_DIR
    _year = int(season_str[:4])
    _season_folder = f"{_year}-{str(_year + 1)[-2:]}"   # e.g. "2025-26"
    for _sub in ('Regular Season', 'Playoffs'):
        _sub_dir = _ML_LOGS_DIR / _sub / _season_folder
        if _sub_dir.exists():
            for _f in sorted(_sub_dir.glob('*.xlsx')):
                if _f.name.startswith('~$'):
                    continue
                try:
                    _file_id = int(_f.stem.split()[0])
                    _full_id = int(f"{_year}0{_file_id:05d}")
                    all_game_ids.add(_full_id)
                except (ValueError, IndexError):
                    pass

    game_ids = sorted(all_game_ids)
    total_games = len(game_ids)
    print(f'  {total_games} games to process ({len([g for g in game_ids if game_load(g) is None])} not yet cached)...', flush=True)

    # ── accumulate stats from each game ───────────────────────────────────────
    season_acc: Dict[int, SeasonEntry] = {}
    fo_grades_by_game  = load_fo_grades()
    dz_fo_by_game      = load_dz_fo_counts()

    for gi, game_id in enumerate(game_ids, 1):
        if gi % 25 == 0 or gi == total_games:
            print(f'  Processing games... {gi}/{total_games}', flush=True)
        cached = game_load(game_id)
        if cached is not None:
            player_stats_raw, all_players_raw = cached
            from models import PlayerStats as _PS, PlayerInfo as _PI
            all_players = {int(k): _PI(**v) for k, v in all_players_raw.items()}
            player_stats = {}
            for k, v in player_stats_raw.items():
                s = _PS(player_id=int(k))
                for attr, val in v.items():
                    if attr != 'player_id':
                        setattr(s, attr, val)
                player_stats[int(k)] = s
        else:
            try:
                player_stats, all_players, _, _, _ = process_game(
                    game_id=game_id, season=season_str, verbose=False
                )
                game_save(
                    game_id,
                    {str(k): {f: getattr(v, f) for f in v.__dataclass_fields__}
                     for k, v in player_stats.items()},
                    {str(k): {f: getattr(v, f) for f in v.__dataclass_fields__}
                     for k, v in all_players.items()}
                )
            except Exception:
                time.sleep(2)
                continue
            time.sleep(1.0)

        per_game_scores = compute_per_game_scores(player_stats, all_players)

        for pid, score in per_game_scores.items():
            info  = all_players.get(pid)
            stats = player_stats.get(pid)
            if not info or not stats or info.team not in teams_set or info.position == 'G':
                continue

            if pid not in season_acc:
                season_acc[pid] = SeasonEntry(name=info.name, team=info.team, position=info.position)

            e = season_acc[pid]
            e.gp                += 1
            e.per_game_scores.append(score)
            e.raw_grade_total   += stats.raw_grade
            e.goals             += stats.goals
            e.primary_assists   += stats.primary_assists
            e.secondary_assists += stats.secondary_assists
            e.shots_on_goal     += stats.shots_on_goal
            e.hits              += stats.hits
            e.blocked_shots     += stats.blocked_shots
            e.giveaways         += stats.giveaways
            e.takeaways         += stats.takeaways
            e.toi_seconds       += stats.toi_seconds
            e.cf                += stats.cf
            e.ca                += stats.ca
            e.xgf               += stats.xgf
            e.xga               += stats.xga
            e.ixg               += stats.ixg
            e.pk_xga            += stats.pk_xga
            e.es_fo_won         += stats.es_fo_won
            e.es_fo_lost        += stats.es_fo_lost
            e.pp_fo_won         += stats.pp_fo_won
            e.pp_fo_lost        += stats.pp_fo_lost
            e.pk_fo_won         += stats.pk_fo_won
            e.pk_fo_lost        += stats.pk_fo_lost
            e.pim               += stats.pim
            e.pk_kills          += stats.pk_kills
            e.pk_blocked_shots  += stats.pk_blocked_shots
            fo_entry2 = fo_grades_by_game.get(game_id, {}).get(pid)
            if fo_entry2:
                e.fo_weighted_sum += fo_entry2[0]
                e.fo_total        += fo_entry2[1]

            dz_fo_entry = dz_fo_by_game.get(game_id, {}).get(pid)
            if dz_fo_entry:
                e.dz_fo_won  += dz_fo_entry[0]
                e.dz_fo_lost += dz_fo_entry[1]

            es_blocks = max(0, stats.blocked_shots - stats.pk_blocked_shots)
            e.raw_pk_total             += stats.pk_kills * 0.50 + stats.pk_xga * (-0.40) + stats.pk_blocked_shots * 0.40
            e.raw_tracking_dz_exit_total += es_blocks * 0.30

            ms_grade = get_play_grade(game_id, info.name) or get_microstat_grade(game_id, info.name)
            if ms_grade:
                e.ms_gp += 1

            # Zone-specific penalty contributions (from API, every game)
            pen_off = stats.oz_pen_drawn * 0.45 + stats.oz_pen_taken * (-0.60)
            pen_dfn = stats.dz_pen_drawn * 0.40 + stats.dz_pen_taken * (-0.50)
            e.raw_tracking_off_total     += pen_off
            e.raw_tracking_dz_exit_total += pen_dfn

            trk_record = get_microstat_record(game_id, info.name, info.team, info.position)
            if trk_record:
                trk_off, trk_dz, trk_ed = _compute_tracking_split(trk_record, info.position)
                e.raw_grade_total              += trk_off + trk_dz + trk_ed
                e.raw_tracking_off_total       += trk_off
                e.raw_tracking_dz_exit_total   += trk_dz
                e.raw_tracking_entry_dfn_total += trk_ed
                if not ms_grade:
                    e.ms_gp += 1
                # Use 5v5 TOI from tracking record — it matches the 5v5 numerator stats.
                # Using total TOI (stats.toi_seconds) would deflate rates for PP-heavy players.
                e.ms_toi_seconds += int(trk_record.toi_min * 60)
            elif ms_grade:
                # No tracking record; fall back to total TOI as the best available denominator.
                e.ms_toi_seconds += stats.toi_seconds

    if not season_acc:
        _sy0 = int(season_str[:4])
        return {'season': season_str, 'season_label': f"{_sy0}–{str(_sy0+1)[-2:]}",
                'total_games': len(game_ids), 'players': [], 'teams': [], 'team_meta': {}}

    # ── Stage 2 normalization ─────────────────────────────────────────────────
    team_max_gp: Dict[str, int] = {}
    for pid, e in season_acc.items():
        team_max_gp[e.team] = max(team_max_gp.get(e.team, 0), e.gp)

    def min_gp_for_team(pid: int) -> int:
        return max(1, round(team_max_gp[season_acc[pid].team] * 0.50))

    qual_pids = [p for p in season_acc if season_acc[p].gp >= min_gp_for_team(p)]
    lim_pids  = [p for p in season_acc if season_acc[p].gp <  min_gp_for_team(p)]

    # Bayesian shrinkage: regress per-60 rate stats toward the position-group mean
    # proportional to total TOI. A player needs SEASON_PRIOR_MIN minutes before their
    # rate is fully trusted — prevents a goal in 9 min outranking 20 goals in 90 min.
    SEASON_PRIOR_MIN = 80.0
    # Higher prior for tracking sub-grades (DZ Exit, Entry DFN, PK, OFF) — pulls
    # low-TOI/sheltered players toward the mean more aggressively than overall grade.
    # Revert: change back to 80.0 to match SEASON_PRIOR_MIN.
    SUB_GRADE_PRIOR_MIN = 80.0  # reverted — use TOI floor instead
    def _shrink_rates(values, pids, acc, prior=None):
        if prior is None:
            prior = SEASON_PRIOR_MIN
        fwd_set = {'C', 'L', 'R'}
        fwd_vals = [v for v, p in zip(values, pids) if acc[p].position in fwd_set]
        def_vals = [v for v, p in zip(values, pids) if acc[p].position not in fwd_set]
        fwd_mean = _stats.mean(fwd_vals) if fwd_vals else 0.0
        def_mean = _stats.mean(def_vals) if def_vals else 0.0
        result = []
        for v, p in zip(values, pids):
            toi_min = acc[p].toi_seconds / 60.0
            w = toi_min / (toi_min + prior)
            mean = fwd_mean if acc[p].position in fwd_set else def_mean
            result.append(w * v + (1.0 - w) * mean)
        return result

    qual_raw_scores = [season_acc[p].avg_score for p in qual_pids]
    qual_pos        = [season_acc[p].position   for p in qual_pids]
    qual_scores     = _shrink_rates(qual_raw_scores, qual_pids, season_acc)
    normed_qual = normalize_by_position_group(list(zip(qual_scores, qual_pos)))
    final_scores: Dict[int, float] = {p: normed_qual[i] for i, p in enumerate(qual_pids)}

    for _, pos_set in [('fwd', {'C', 'L', 'R'}), ('def', {'D'})]:
        grp_raw = [season_acc[p].avg_score for p in qual_pids if season_acc[p].position in pos_set]
        if len(grp_raw) < 2:
            continue
        raw_mean = _stats.mean(grp_raw)
        raw_sd   = _stats.stdev(grp_raw) or 1.0
        for p in lim_pids:
            if season_acc[p].position not in pos_set:
                continue
            z = (season_acc[p].avg_score - raw_mean) / raw_sd
            final_scores[p] = 60.0 + z * 12.0

    # ── Sub-grade scores from tracking data ──────────────────────────────────
    all_pids = list(season_acc.keys())

    def _norm_sub(raw_vals, positions):
        return normalize_by_position_group(list(zip(raw_vals, positions)))

    # Floor prevents short-TOI players from getting artificially inflated per-hour rates
    MS_MIN_TOI_PER_GAME = 14.0  # minutes

    def _effective_toi_h(e):
        gp = e.ms_gp or 1
        raw_toi = (e.ms_toi_seconds or e.toi_seconds) / 60.0
        floored = max(raw_toi, gp * MS_MIN_TOI_PER_GAME)
        return floored / 60.0 or 1e-6

    def _tracking_off_rate(e):
        return e.raw_tracking_off_total / _effective_toi_h(e)

    def _tracking_dz_exit_rate(e):
        return e.raw_tracking_dz_exit_total / _effective_toi_h(e)

    def _tracking_entry_dfn_rate(e):
        return e.raw_tracking_entry_dfn_total / _effective_toi_h(e)

    def _fo_raw(e):
        return (e.fo_weighted_sum / e.fo_total) if e.fo_total >= 10 else None

    pos_list = [season_acc[p].position for p in all_pids]

    off_raw_per60     = [_tracking_off_rate(season_acc[p])       for p in all_pids]
    dz_exit_raw_per60 = [_tracking_dz_exit_rate(season_acc[p])   for p in all_pids]
    entry_dfn_per60   = [_tracking_entry_dfn_rate(season_acc[p]) for p in all_pids]

    off_normed       = _norm_sub(_shrink_rates(off_raw_per60,     all_pids, season_acc, SUB_GRADE_PRIOR_MIN), pos_list)
    dz_exit_normed   = _norm_sub(_shrink_rates(dz_exit_raw_per60, all_pids, season_acc, SUB_GRADE_PRIOR_MIN), pos_list)
    entry_dfn_normed = _norm_sub(_shrink_rates(entry_dfn_per60,   all_pids, season_acc, SUB_GRADE_PRIOR_MIN), pos_list)

    # PK component — all positions
    pk_per60 = [season_acc[p].raw_pk_total / (season_acc[p].toi_seconds / 3600.0 or 1e-6)
                for p in all_pids]
    pk_normed_list = _norm_sub(_shrink_rates(pk_per60, all_pids, season_acc, SUB_GRADE_PRIOR_MIN), pos_list)
    pk_scores_map  = {p: pk_normed_list[i] for i, p in enumerate(all_pids)}

    # DZ FO net per 60 — centers with ≥20 DZ faceoffs only
    DZ_FO_MIN = 20
    dz_fo_center_pids = [
        p for p in all_pids
        if season_acc[p].position == 'C'
        and (season_acc[p].dz_fo_won + season_acc[p].dz_fo_lost) >= DZ_FO_MIN
    ]
    dz_fo_normed: Dict[int, float] = {}
    if dz_fo_center_pids:
        dz_fo_rates = [
            (season_acc[p].dz_fo_won - season_acc[p].dz_fo_lost)
            / (season_acc[p].toi_seconds / 3600.0 or 1e-6)
            for p in dz_fo_center_pids
        ]
        dz_fo_normed_vals = _norm_sub(
            dz_fo_rates,
            [season_acc[p].position for p in dz_fo_center_pids]
        )
        dz_fo_normed = {p: dz_fo_normed_vals[i] for i, p in enumerate(dz_fo_center_pids)}

    # Team-relative on-ice possession component (avg of xG% and CF%, strips system bias)
    team_xg_sums: Dict[str, list] = {}
    team_cf_sums: Dict[str, list] = {}
    for pid, e in season_acc.items():
        xg_total = e.xgf + e.xga
        if xg_total > 0:
            team_xg_sums.setdefault(e.team, []).append(e.xgf / xg_total)
        cf_total = e.cf + e.ca
        if cf_total > 0:
            team_cf_sums.setdefault(e.team, []).append(e.cf / cf_total)
    team_avg_xg = {team: _stats.mean(pcts) for team, pcts in team_xg_sums.items() if pcts}
    team_avg_cf = {team: _stats.mean(pcts) for team, pcts in team_cf_sums.items() if pcts}

    poss_vals = []
    for p in all_pids:
        e = season_acc[p]
        xg_total = e.xgf + e.xga
        rel_xg = (e.xgf / xg_total - team_avg_xg.get(e.team, 0.5)) if xg_total > 0 else 0.0
        cf_total = e.cf + e.ca
        rel_cf = (e.cf / cf_total - team_avg_cf.get(e.team, 0.5)) if cf_total > 0 else 0.0
        poss_vals.append((rel_xg + rel_cf) / 2.0)
    poss_normed_list = _norm_sub(poss_vals, pos_list)
    poss_map = {p: poss_normed_list[i] for i, p in enumerate(all_pids)}

    # Blocks per 60
    blk_per60 = [
        season_acc[p].blocked_shots / (season_acc[p].toi_seconds / 3600.0 or 1e-6)
        for p in all_pids
    ]
    blk_normed_list = _norm_sub(_shrink_rates(blk_per60, all_pids, season_acc, SUB_GRADE_PRIOR_MIN), pos_list)
    blk_map = {p: blk_normed_list[i] for i, p in enumerate(all_pids)}

    # Blend DFN:
    #   Centers with ≥20 DZ FOs: 22% DZ Exit + 22% Entry DFN + 11% PK + 20% DZ FO + 20% possession + 5% blocks
    #   Everyone else:            30% DZ Exit + 30% Entry DFN + 15% PK + 20% possession + 5% blocks
    def_blended = []
    for i, pid in enumerate(all_pids):
        dz = dz_exit_normed[i]
        ed = entry_dfn_normed[i]
        pk = pk_scores_map[pid]
        ps = poss_map[pid]
        bk = blk_map[pid]
        if pid in dz_fo_normed:
            def_blended.append(0.22*dz + 0.22*ed + 0.11*pk + 0.20*dz_fo_normed[pid] + 0.20*ps + 0.05*bk)
        else:
            def_blended.append(0.30*dz + 0.30*ed + 0.15*pk + 0.20*ps + 0.05*bk)

    def_normed = _norm_sub(def_blended, pos_list)

    fo_pids = [p for p in all_pids if _fo_raw(season_acc[p]) is not None]
    fo_normed: Dict[int, float] = {}
    if fo_pids:
        fo_normed_vals = _norm_sub(
            [_fo_raw(season_acc[p]) for p in fo_pids],
            [season_acc[p].position for p in fo_pids]
        )
        fo_normed = {p: fo_normed_vals[i] for i, p in enumerate(fo_pids)}

    off_scores = {p: off_normed[i]  for i, p in enumerate(all_pids)}
    def_scores = {p: def_normed[i]  for i, p in enumerate(all_pids)}

    # ── QoC adjustment (PFF-style iterative, 1 round) ─────────────────────────
    # Round-1 overall for each player (pre-QoC)
    def _r1_overall(pid: int) -> float:
        is_d  = season_acc[pid].position == 'D'
        off_w, dfn_w = (0.80, 0.20) if not is_d else (0.20, 0.80)
        return off_w * off_scores[pid] + dfn_w * def_scores[pid]

    r1_overall = {p: _r1_overall(p) for p in all_pids}

    print('  Loading QoC matchup data...', flush=True)
    matchup_totals = load_matchup_totals(game_ids)

    qoc_raw = []
    for p in all_pids:
        opp_map = matchup_totals.get(p, {})
        total_sec = weighted = 0
        for opp_pid, secs in opp_map.items():
            if opp_pid in r1_overall:
                total_sec += secs
                weighted  += r1_overall[opp_pid] * secs
        qoc_raw.append(weighted / total_sec if total_sec > 0 else 60.0)

    qoc_mean  = _stats.mean(qoc_raw)
    qoc_stdev = _stats.stdev(qoc_raw) if len(qoc_raw) > 1 else 1.0
    # Cap at ±3 points so QoC nudges rather than dominates
    qoc_adj = {
        p: max(-3.0, min(3.0, (qoc_raw[i] - qoc_mean) / qoc_stdev * 3.0))
        for i, p in enumerate(all_pids)
    }

    # ── build output rows ─────────────────────────────────────────────────────
    players = []
    for pid in all_pids:
        e        = season_acc[pid]
        fo_score = fo_normed.get(pid)
        fo_total = (e.es_fo_won + e.es_fo_lost + e.pp_fo_won
                    + e.pp_fo_lost + e.pk_fo_won + e.pk_fo_lost)
        off_v = round(off_scores.get(pid, 60.0), 1)
        dfn_v = round(def_scores.get(pid, 60.0), 1)
        is_d  = e.position == 'D'
        # Overall: F = 80% OFF + 20% DFN, D = 20% OFF + 80% DFN
        # QoC adjustment applied to overall only (±3 pts max)
        off_w, dfn_w = (0.80, 0.20) if not is_d else (0.20, 0.80)
        overall_v = round(off_w * off_v + dfn_w * dfn_v + qoc_adj.get(pid, 0.0), 1)
        players.append({
            'player_id':      pid,
            'name':           e.name,
            'team':           e.team,
            'position':       e.position,
            'pos_group':      'fwd' if e.position in ('C', 'L', 'R') else 'def',
            'gp':             e.gp,
            'qualified':      season_acc[pid].gp >= min_gp_for_team(pid),
            'overall':        overall_v,
            'overall_letter': score_to_letter(overall_v),
            'off':            off_v,
            'off_letter':     score_to_letter(off_v),
            'dfn':            dfn_v,
            'dfn_letter':     score_to_letter(dfn_v),
            'fo':             round(fo_score, 1) if (fo_score is not None and fo_total > 0) else None,
            'fo_letter':      score_to_letter(fo_score) if (fo_score is not None and fo_total > 0) else None,
            'toi_per_game':   e.toi_per_game,
            'cf_pct':         str(e.cf_pct),
            'xg_pct':         str(e.xg_pct),
            'goals':          e.goals,
            'assists':        e.assists,
            'points':         e.points,
            'sog':            e.shots_on_goal,
            'hits':           e.hits,
            'blocks':         e.blocked_shots,
            'gva':            e.giveaways,
            'tka':            e.takeaways,
            'pim':            e.pim,
            'fo_pct':         f"{round((e.es_fo_won + e.pp_fo_won + e.pk_fo_won) / fo_total * 100, 1)}%" if fo_total >= 10 else '—',
            'fo_pct_val':     round((e.es_fo_won + e.pp_fo_won + e.pk_fo_won) / fo_total * 100, 1) if fo_total >= 10 else None,
            'ms_gp':          e.ms_gp,
        })

    players.sort(key=lambda x: x['overall'], reverse=True)
    for i, p in enumerate(players, 1):
        p['rank'] = i

    teams_present = [t for t in teams if any(p['team'] == t for p in players)]

    team_meta = {
        team: {
            'gp_max': team_max_gp.get(team, 0),
            'min_gp': max(1, round(team_max_gp.get(team, 1) * 0.50)),
        }
        for team in teams_present
    }

    def _best_grade(p):
        return p.get('overall', 60.0)

    def _avg(pool):
        scores = [_best_grade(p) for p in pool]
        return round(sum(scores) / len(scores), 1) if scores else None

    def _avg_sub(pool, col):
        vals = [p[col] for p in pool if p.get(col) is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    records = _fetch_standings(season_str)

    team_grades = []
    for team in teams_present:
        qual  = [p for p in players if p['team'] == team and p.get('qualified')]
        fwds  = [p for p in qual if p['pos_group'] == 'fwd']
        defs  = [p for p in qual if p['pos_group'] == 'def']
        fo_pl = [p for p in qual if p.get('fo') is not None]
        rec   = records.get(team, {})
        team_grades.append({
            'team':    team,
            'overall': _avg(qual),
            'fwd':     _avg(fwds),
            'dfn':     _avg(defs),
            'off':     _avg_sub(qual, 'off'),
            'def_g':   _avg_sub(qual, 'dfn'),
            'fo_g':    _avg_sub(fo_pl, 'fo'),
            'n_fwd':   len(fwds),
            'n_def':   len(defs),
            'w':       rec.get('w', 0),
            'l':       rec.get('l', 0),
            'otl':     rec.get('otl', 0),
            'pts':     rec.get('pts', 0),
        })
    team_grades.sort(key=lambda t: t['overall'] or 0, reverse=True)
    for i, t in enumerate(team_grades, 1):
        t['rank'] = i

    _sy = int(season_str[:4])
    return {
        'season':       season_str,
        'season_label': f"{_sy}–{str(_sy + 1)[-2:]}",
        'total_games':  len(game_ids),
        'players':      players,
        'teams':        teams_present,
        'team_meta':    team_meta,
        'team_grades':  team_grades,
    }


if __name__ == '__main__':
    main()
