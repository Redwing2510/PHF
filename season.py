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
from manual_loader import get_microstat_grade, _norm_name as _ml_norm_name
from play_grader import get_play_grade, get_season_def_aggregates
from fo_grade_loader import load_fo_grades

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

    # MS tracking accumulation (only for games that have xlsx tracking data)
    ms_gp:            int   = 0
    ms_offense_sum:   float = 0.0
    ms_entries_sum:   float = 0.0
    ms_exits_sum:     float = 0.0
    ms_defense_sum:   float = 0.0
    ms_poss_sum:      float = 0.0
    ms_fc_sum:        float = 0.0
    ms_hits:          int   = 0
    ms_blocked_shots: int   = 0
    ms_giveaways:     int   = 0
    ms_takeaways:     int   = 0
    ms_toi_seconds:   int   = 0

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
        dfn  = (e.blocked_shots * 1.5 + e.hits * 0.3) / toi_h
        xg_total = e.xgf + e.xga
        poss = (e.xgf / xg_total * 100) if xg_total > 0 else 50.0
        if e.position == 'D':
            dfn -= (e.xga / toi_h) * 1.5       # suppression penalty
            dfn += (e.pk_kills / toi_h) * 0.8  # reward for killing penalties
            dfn += (poss - 50) * 0.4            # xG% above/below 50 folds into DEF for D-men
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


def build_season_grades(season_str: str = SEASON, teams: list = None) -> dict:
    """
    Process all regular season + playoff games and return structured grade data.
    Returns {'players': [...], 'teams': [...], 'team_meta': {...}, 'total_games': int}.
    Usable by both the Flask web app and any other consumer.
    """
    if teams is None:
        teams = TEAMS
    teams_set = set(teams)

    # ── collect game IDs ──────────────────────────────────────────────────────
    all_game_ids: set[int] = set()

    # Playoff games from NHL API schedule — only for the current/active season
    if season_str == '20252026':
        for team in PLAYOFF_TEAMS:
            games = fetch_schedule(team, season_str)
            for g in games:
                if g.get('gameType') == 3 and g.get('gameState') in ('OFF', 'FINAL'):
                    all_game_ids.add(g['id'])

    # Regular season games from xlsx files in Manual Game Logs/Regular Season/{YYYY-YY}/
    from manual_loader import LOGS_DIR as _ML_LOGS_DIR
    _year = int(season_str[:4])
    _season_folder = f"{_year}-{str(_year + 1)[-2:]}"   # e.g. "2025-26"
    _rs_dir = _ML_LOGS_DIR / 'Regular Season' / _season_folder
    if _rs_dir.exists():
        for _f in sorted(_rs_dir.glob('*.xlsx')):
            try:
                _file_id = int(_f.stem.split()[0])   # e.g. "20001 CHI vs. FLA" → 20001
                _full_id = int(f"{_year}0{_file_id:05d}")
                all_game_ids.add(_full_id)
            except (ValueError, IndexError):
                pass

    game_ids = sorted(all_game_ids)
    total_games = len(game_ids)
    print(f'  {total_games} games to process ({len([g for g in game_ids if game_load(g) is None])} not yet cached)...', flush=True)

    # ── accumulate stats from each game ───────────────────────────────────────
    season_acc: Dict[int, SeasonEntry] = {}
    fo_grades_by_game = load_fo_grades()

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
            fo_entry2 = fo_grades_by_game.get(game_id, {}).get(pid)
            if fo_entry2:
                e.fo_weighted_sum += fo_entry2[0]
                e.fo_total        += fo_entry2[1]

            ms_grade = get_play_grade(game_id, info.name) or get_microstat_grade(game_id, info.name)
            if ms_grade:
                e.ms_gp             += 1
                e.ms_offense_sum    += ms_grade.offense_100
                e.ms_entries_sum    += ms_grade.entries_100
                e.ms_exits_sum      += ms_grade.exits_100
                e.ms_defense_sum    += ms_grade.defense_100
                e.ms_poss_sum       += getattr(ms_grade, 'poss_100', 50.0)
                e.ms_fc_sum         += ms_grade.forechecking_100
                e.ms_hits           += stats.hits
                e.ms_blocked_shots  += stats.blocked_shots
                e.ms_giveaways      += stats.giveaways
                e.ms_takeaways      += stats.takeaways
                e.ms_toi_seconds    += stats.toi_seconds

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
    SEASON_PRIOR_MIN = 45.0
    def _shrink_rates(values, pids, acc):
        fwd_set = {'C', 'L', 'R'}
        fwd_vals = [v for v, p in zip(values, pids) if acc[p].position in fwd_set]
        def_vals = [v for v, p in zip(values, pids) if acc[p].position not in fwd_set]
        fwd_mean = _stats.mean(fwd_vals) if fwd_vals else 0.0
        def_mean = _stats.mean(def_vals) if def_vals else 0.0
        result = []
        for v, p in zip(values, pids):
            toi_min = acc[p].toi_seconds / 60.0
            w = toi_min / (toi_min + SEASON_PRIOR_MIN)
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

    # ── sub-grades ────────────────────────────────────────────────────────────
    all_pids = list(season_acc.keys())

    def _sub_raw(e):
        toi_h    = e.toi_seconds / 3600.0 or 1e-6
        off      = (e.goals * 3.0 + e.primary_assists * 2.0
                    + e.secondary_assists * 1.0
                    + e.ixg * 2.0 + (e.xgf - e.ixg) * 1.5) / toi_h
        dfn      = (e.blocked_shots * 1.5 + e.hits * 0.3) / toi_h
        xg_total = e.xgf + e.xga
        poss     = (e.xgf / xg_total * 100) if xg_total > 0 else 50.0
        if e.position == 'D':
            dfn -= (e.xga / toi_h) * 1.5       # suppression penalty
            dfn += (e.pk_kills / toi_h) * 0.8  # reward for killing penalties
            dfn += (poss - 50) * 0.4            # xG% above/below 50 folds into DEF for D-men
        fo_raw = (e.fo_weighted_sum / e.fo_total) if e.fo_total >= 10 else None
        return off, dfn, poss, fo_raw

    def _norm_sub(raw_vals, positions):
        return normalize_by_position_group(list(zip(raw_vals, positions)))

    pos_list    = [season_acc[p].position for p in all_pids]
    off_raw_per60  = [_sub_raw(season_acc[p])[0] for p in all_pids]
    def_raw_per60  = [_sub_raw(season_acc[p])[1] for p in all_pids]
    off_normed  = _norm_sub(_shrink_rates(off_raw_per60, all_pids, season_acc), pos_list)
    def_normed  = _norm_sub(_shrink_rates(def_raw_per60, all_pids, season_acc), pos_list)
    poss_normed = _norm_sub([_sub_raw(season_acc[p])[2] for p in all_pids], pos_list)

    fo_pids = [p for p in all_pids if _sub_raw(season_acc[p])[3] is not None]
    fo_normed: Dict[int, float] = {}
    if fo_pids:
        fo_normed_vals = _norm_sub(
            [_sub_raw(season_acc[p])[3] for p in fo_pids],
            [season_acc[p].position for p in fo_pids]
        )
        fo_normed = {p: fo_normed_vals[i] for i, p in enumerate(fo_pids)}

    # ── MS-blended sub-grades ────────────────────────────────────────────────
    # For players with ms_gp > 0, replace dfn/poss with MS data and add fc.
    # MS sub-scores are already on 0-100 z-score scale from manual_loader.
    def _ms_api_dfn_raw(e):
        toi_h = e.ms_toi_seconds / 3600.0 or 1e-6
        return (e.ms_blocked_shots * 1.5 + e.ms_hits * 0.3 + e.ms_takeaways * 1.0) / toi_h

    all_pids_idx  = {p: i for i, p in enumerate(all_pids)}
    ms_pids       = [p for p in all_pids if season_acc[p].ms_gp > 0]
    ms_grades_computed: Dict[int, dict] = {}
    if ms_pids:
        ms_api_dfn_normed = _norm_sub(
            [_ms_api_dfn_raw(season_acc[p]) for p in ms_pids],
            [season_acc[p].position         for p in ms_pids],
        )

        # ── Season-level defense: aggregate raw def_s/toi across all tracked games ──
        # More stable than averaging per-game defense_100 (single-event games skew the mean).
        _sd_agg = get_season_def_aggregates(int(season_str[:4]))
        def _sd_rate(pid):
            agg = _sd_agg.get(_ml_norm_name(season_acc[pid].name))
            if agg is None or agg[1] < 2:
                return None
            def_s, _def_n, toi_min, _ = agg
            return def_s / (toi_min / 60.0) if toi_min > 0 else None

        _sd_rates    = [_sd_rate(p) for p in ms_pids]
        _sd_valid    = [r for r in _sd_rates if r is not None]
        if len(_sd_valid) >= 3:
            _sd_mean = sum(_sd_valid) / len(_sd_valid)
            _sd_var  = sum((r - _sd_mean) ** 2 for r in _sd_valid) / (len(_sd_valid) - 1)
            _sd_std  = _sd_var ** 0.5 if _sd_var > 1e-18 else None
        else:
            _sd_mean = _sd_std = None

        def _sd_to100(rate):
            if rate is None or _sd_mean is None or _sd_std is None:
                return 50.0
            z = max(-2.0, min(2.0, (rate - _sd_mean) / _sd_std))
            return round((z + 2.0) / 4.0 * 100.0, 1)

        _sd_normed = [_sd_to100(r) for r in _sd_rates]

        # ── Bayesian shrinkage for MS sub-grades ────────────────────────────
        # Shrink each player's average toward the position-group mean weighted
        # by their tracked TOI — more games = more trust in observed rate.
        _MS_PRIOR_MIN = 120.0  # ~6-8 tracked games before full trust
        _fwd_set = {'C', 'L', 'R'}

        def _ms_shrink(values):
            fwd_v = [v for v, p in zip(values, ms_pids) if season_acc[p].position in _fwd_set]
            def_v = [v for v, p in zip(values, ms_pids) if season_acc[p].position not in _fwd_set]
            fwd_m = sum(fwd_v) / len(fwd_v) if fwd_v else 50.0
            def_m = sum(def_v) / len(def_v) if def_v else 50.0
            out = []
            for v, p in zip(values, ms_pids):
                toi_min = season_acc[p].ms_toi_seconds / 60.0
                w = toi_min / (toi_min + _MS_PRIOR_MIN)
                mean = fwd_m if season_acc[p].position in _fwd_set else def_m
                out.append(w * v + (1.0 - w) * mean)
            return out

        _ms_off_shrunk  = _ms_shrink([season_acc[p].ms_offense_sum / season_acc[p].ms_gp for p in ms_pids])
        _ms_poss_shrunk = _ms_shrink([season_acc[p].ms_poss_sum    / season_acc[p].ms_gp for p in ms_pids])
        _ms_fc_shrunk   = _ms_shrink([season_acc[p].ms_fc_sum      / season_acc[p].ms_gp for p in ms_pids])
        _ms_def_shrunk  = _ms_shrink(_sd_normed)

        for idx, p in enumerate(ms_pids):
            e        = season_acc[p]
            ms_poss  = _ms_poss_shrunk[idx]
            ms_def_z = _ms_def_shrunk[idx]
            ms_fc    = _ms_fc_shrunk[idx]
            ms_dfn   = ms_def_z
            ms_off_z = _ms_off_shrunk[idx]
            api_off  = off_normed[all_pids_idx[p]]
            off      = 0.5 * ms_off_z + 0.5 * api_off
            fo       = fo_normed.get(p)
            is_d     = e.position == 'D'
            if is_d:
                if fo is not None:
                    ms_ov = 0.20 * off + 0.50 * ms_dfn + 0.25 * ms_poss + 0.05 * fo
                else:
                    ms_ov = 0.20 * off + 0.50 * ms_dfn + 0.30 * ms_poss
            else:
                if fo is not None:
                    ms_ov = 0.40 * off + 0.25 * ms_dfn + 0.30 * ms_poss + 0.05 * fo
                else:
                    ms_ov = 0.40 * off + 0.25 * ms_dfn + 0.35 * ms_poss
            ms_grades_computed[p] = {
                'ms_gp':   e.ms_gp,
                'ms_dfn':  round(ms_dfn, 1),
                'ms_poss': round(ms_poss, 1),
                'ms_fc':   round(ms_fc, 1),
                '_ms_ov_raw': ms_ov,
            }

        # Re-normalize ms_overall by position so the distribution uses the full
        # 0-100 range (averaging components compresses variance toward 50).
        ms_ov_raw   = [ms_grades_computed[p]['_ms_ov_raw'] for p in ms_pids]
        ms_ov_pos   = [season_acc[p].position              for p in ms_pids]
        ms_ov_normed = normalize_by_position_group(list(zip(ms_ov_raw, ms_ov_pos)))
        for i, p in enumerate(ms_pids):
            normed = ms_ov_normed[i]
            ms_grades_computed[p]['ms_overall']        = round(normed, 1)
            ms_grades_computed[p]['ms_overall_letter'] = score_to_letter(normed)
            del ms_grades_computed[p]['_ms_ov_raw']

        # Re-normalize ms_dfn so it uses the full 0-100 range (same reason as ms_overall).
        ms_dfn_raw = [ms_grades_computed[p]['ms_dfn'] for p in ms_pids]
        ms_dfn_normed = normalize_by_position_group(list(zip(ms_dfn_raw, ms_ov_pos)))
        for i, p in enumerate(ms_pids):
            ms_grades_computed[p]['ms_dfn'] = round(ms_dfn_normed[i], 1)

    off_scores  = {p: off_normed[i]  for i, p in enumerate(all_pids)}
    def_scores  = {p: def_normed[i]  for i, p in enumerate(all_pids)}
    poss_scores = {p: poss_normed[i] for i, p in enumerate(all_pids)}

    # ── build output rows ─────────────────────────────────────────────────────
    players = []
    for pid in all_pids:
        e        = season_acc[pid]
        score    = final_scores.get(pid, 60.0)
        fo_score = fo_normed.get(pid)
        fo_total = (e.es_fo_won + e.es_fo_lost + e.pp_fo_won
                    + e.pp_fo_lost + e.pk_fo_won + e.pk_fo_lost)
        players.append({
            'player_id':      pid,
            'name':           e.name,
            'team':           e.team,
            'position':       e.position,
            'pos_group':      'fwd' if e.position in ('C', 'L', 'R') else 'def',
            'gp':             e.gp,
            'qualified':      season_acc[pid].gp >= min_gp_for_team(pid),
            'overall':        round(score, 1),
            'overall_letter': score_to_letter(score),
            'off':            round(off_scores.get(pid, 60.0), 1),
            'off_letter':     score_to_letter(off_scores.get(pid, 60.0)),
            'dfn':            round(ms_grades_computed[pid]['ms_dfn'] if pid in ms_grades_computed else def_scores.get(pid, 60.0), 1),
            'dfn_letter':     score_to_letter(ms_grades_computed[pid]['ms_dfn'] if pid in ms_grades_computed else def_scores.get(pid, 60.0)),
            'poss':           round(poss_scores.get(pid, 60.0), 1),
            'poss_letter':    score_to_letter(poss_scores.get(pid, 60.0)),
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
            **ms_grades_computed.get(pid, {
                'ms_gp': 0, 'ms_dfn': None, 'ms_poss': None,
                'ms_fc': None, 'ms_overall': None, 'ms_overall_letter': None,
            }),
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
        return p.get('ms_overall') if p.get('ms_overall') is not None else p.get('overall', 60.0)

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
            'poss':    _avg_sub(qual, 'poss'),
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
