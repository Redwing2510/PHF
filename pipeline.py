import json
from collections import defaultdict
from models import PlayerStats, PlayerInfo
from loader import load_all, is_on_ice, time_to_seconds
from grader import GRADE_DELTAS, FACEOFF_POS_MULTIPLIER, normalize_grades, normalize_by_position_group, score_to_letter
from xg import compute_xg
from manual_loader import get_microstat_grade

# Shot event types (goals count as shot attempts in Corsi)
SHOT_EVENTS = {'goal', 'shot-on-goal', 'missed-shot', 'blocked-shot'}


def process_game(game_id: int, season: str = '20252026', from_file: str = None, verbose: bool = True) -> tuple:
    """
    Run the full PBP grading pipeline for a single game.
    Returns (player_stats, all_players, ctx, game_data, play_log).
    Raw grades are populated but NOT normalized — caller handles normalization.
    """
    game_data, ctx, all_players, shifts, boxscore_toi = load_all(
        game_id=game_id,
        season=season,
        from_file=from_file,
        verbose=verbose
    )

    has_shifts = len(shifts) > 0

    # Initialize stats for every player in the roster
    player_stats = {pid: PlayerStats(player_id=pid) for pid in all_players}

    # Per-player event log: pid -> list of (period, time_str, description, delta)
    play_log: dict[int, list] = defaultdict(list)

    def log(pid, period, time_str, description, delta):
        play_log[pid].append((period, time_str, description, delta))

    # Build a fast lookup: player_id -> list of shifts
    player_shift_map = defaultdict(list)
    for shift in shifts:
        player_shift_map[shift.player_id].append(shift)

    def is_on_ice_fast(player_id, period, time_sec):
        for shift in player_shift_map[player_id]:
            if shift.period == period and shift.start_sec <= time_sec <= shift.end_sec:
                return True
        return False

    # Parse all plays
    for play in game_data['plays']:
        event = play['typeDescKey']
        details = play.get('details', {})
        situation = str(play.get('situationCode', '1551'))
        period = play['periodDescriptor']['number']
        time_sec = time_to_seconds(play['timeInPeriod'])

        # Faceoffs
        if event == 'faceoff':
            winning_id = details.get('winningPlayerId')
            losing_id = details.get('losingPlayerId')
            zone = details.get('zoneCode', 'N')

            for pid, is_winner in [(winning_id, True), (losing_id, False)]:
                if pid is None or pid not in player_stats:
                    continue

                info = all_players.get(pid)
                if not info:
                    continue

                code = str(situation)
                away_skaters = int(code[1])
                home_skaters = int(code[2])
                is_home = info.team == ctx.home_team_abbrev
                my_skaters = home_skaters if is_home else away_skaters
                opp_skaters = away_skaters if is_home else home_skaters

                if my_skaters == opp_skaters:
                    sit = 'ES'
                elif my_skaters > opp_skaters:
                    sit = 'PP'
                else:
                    sit = 'PK'

                stats = player_stats[pid]
                if is_winner:
                    if sit == 'ES':
                        stats.es_fo_won += 1
                    elif sit == 'PP':
                        stats.pp_fo_won += 1
                    else:
                        stats.pk_fo_won += 1
                else:
                    if sit == 'ES':
                        stats.es_fo_lost += 1
                    elif sit == 'PP':
                        stats.pp_fo_lost += 1
                    else:
                        stats.pk_fo_lost += 1

                if zone == 'O':
                    stats.oz_faceoffs += 1
                elif zone == 'D':
                    stats.dz_faceoffs += 1
                else:
                    stats.nz_faceoffs += 1

                # Zone is from home team's perspective; flip for away players.
                player_zone = zone if is_home else ('O' if zone == 'D' else ('D' if zone == 'O' else 'N'))
                zone_key = 'oz' if player_zone == 'O' else ('dz' if player_zone == 'D' else 'nz')
                delta_key = f"fo_{sit.lower()}_{'win' if is_winner else 'loss'}_{zone_key}"
                fo_mult = FACEOFF_POS_MULTIPLIER.get(info.position, 1.0)
                d = GRADE_DELTAS[delta_key] * fo_mult
                stats.raw_grade   += d
                stats.raw_faceoff += d
                log(pid, period, play['timeInPeriod'], f"Faceoff {'win' if is_winner else 'loss'} ({sit}, {player_zone}Z)", d)

        # Shot attempts + on-ice xG
        if event in SHOT_EVENTS:
            shooting_team = details.get('eventOwnerTeamId')
            xg_val = compute_xg(
                details.get('xCoord'),
                details.get('yCoord'),
                details.get('shotType'),
                event
            )
            if has_shifts:
                for pid, stats in player_stats.items():
                    if is_on_ice_fast(pid, period, time_sec):
                        info = all_players.get(pid)
                        if not info or info.position == 'G':
                            continue
                        if info.team == ctx.home_team_abbrev and shooting_team == ctx.home_team_id or info.team == ctx.away_team_abbrev and shooting_team != ctx.home_team_id:
                            stats.cf += 1
                            stats.xgf += xg_val
                            d = GRADE_DELTAS['on_ice_shot_for_base'] + xg_val * GRADE_DELTAS['on_ice_shot_for_xg_mult']
                            grade_d = d * (1.5 if info.position == 'D' else 1.0)
                            stats.raw_grade     += grade_d
                            stats.raw_possession += d
                            log(pid, period, play['timeInPeriod'], f'On-ice shot for (xG={xg_val:.3f})', grade_d)
                        else:
                            stats.ca += 1
                            stats.xga += xg_val
                            # Track PK-specific xGA — player is on PK if his side has fewer skaters
                            away_sk = int(situation[1]); home_sk = int(situation[2])
                            is_home_p = info.team == ctx.home_team_abbrev
                            my_sk = home_sk if is_home_p else away_sk
                            opp_sk = away_sk if is_home_p else home_sk
                            if my_sk < opp_sk:
                                stats.pk_xga += xg_val
                            d = GRADE_DELTAS['on_ice_shot_against_base'] + xg_val * GRADE_DELTAS['on_ice_shot_against_xg_mult']
                            grade_d = d * (1.5 if info.position == 'D' else 1.0)
                            stats.raw_grade     += grade_d
                            stats.raw_possession += d
                            if info.position == 'D':
                                stats.raw_defense += d  # suppression counts toward DEF for D-men
                            log(pid, period, play['timeInPeriod'], f'On-ice shot against (xG={xg_val:.3f})', grade_d)

        # Missed shots
        if event == 'missed-shot':
            pid = details.get('shootingPlayerId')
            if pid and pid in player_stats:
                xg_miss = compute_xg(
                    details.get('xCoord'), details.get('yCoord'),
                    details.get('shotType'), 'missed-shot'
                )
                d = GRADE_DELTAS['missed_shot_base'] + xg_miss * GRADE_DELTAS['missed_shot_xg_mult']
                player_stats[pid].raw_grade   += d
                player_stats[pid].raw_offense += d
                log(pid, period, play['timeInPeriod'], f"Missed shot (xG={xg_miss:.3f})", d)

        # Giveaways
        if event == 'giveaway':
            pid = details.get('playerId')
            if pid and pid in player_stats:
                player_stats[pid].giveaways += 1
                raw_zone = details.get('zoneCode', 'N')
                info = all_players.get(pid)
                is_home = info and info.team == ctx.home_team_abbrev
                player_zone = raw_zone if is_home else ('O' if raw_zone == 'D' else ('D' if raw_zone == 'O' else 'N'))
                zone_key = 'oz' if player_zone == 'O' else ('dz' if player_zone == 'D' else 'nz')
                d = GRADE_DELTAS[f'giveaway_{zone_key}']
                player_stats[pid].raw_grade   += d
                log(pid, period, play['timeInPeriod'], f'Giveaway ({player_zone}Z)', d)

        # Takeaways
        if event == 'takeaway':
            pid = details.get('playerId')
            if pid and pid in player_stats:
                player_stats[pid].takeaways += 1
                raw_zone = details.get('zoneCode', 'N')
                info = all_players.get(pid)
                is_home = info and info.team == ctx.home_team_abbrev
                player_zone = raw_zone if is_home else ('O' if raw_zone == 'D' else ('D' if raw_zone == 'O' else 'N'))
                zone_key = 'oz' if player_zone == 'O' else ('dz' if player_zone == 'D' else 'nz')
                my_sk_t  = int(situation[2]) if is_home else int(situation[1])
                opp_sk_t = int(situation[1]) if is_home else int(situation[2])
                pk_mult  = GRADE_DELTAS['pk_defensive_mult'] if my_sk_t < opp_sk_t else 1.0
                d = GRADE_DELTAS[f'takeaway_{zone_key}'] * pk_mult
                player_stats[pid].raw_grade   += d
                label = f'Takeaway ({player_zone}Z{"  PK" if pk_mult > 1 else ""})'
                log(pid, period, play['timeInPeriod'], label, d)

        # Goals
        if event == 'goal':
            scoring_team = details.get('eventOwnerTeamId')
            scorer = details.get('scoringPlayerId')
            assist1 = details.get('assist1PlayerId')
            assist2 = details.get('assist2PlayerId')
            is_empty_net = 'goalieInNetId' not in details
            if scorer and scorer in player_stats:
                player_stats[scorer].goals += 1
                xg_goal = compute_xg(
                    details.get('xCoord'), details.get('yCoord'),
                    details.get('shotType'), 'shot-on-goal'
                )
                player_stats[scorer].ixg += xg_goal
                bonus_key = 'empty_net_goal_bonus' if is_empty_net else 'goal_scorer_bonus'
                d = GRADE_DELTAS['shot_on_goal_base'] + xg_goal * GRADE_DELTAS['shot_xg_multiplier'] + GRADE_DELTAS[bonus_key]
                player_stats[scorer].raw_grade   += d
                player_stats[scorer].raw_offense += d
                label = 'Empty net goal' if is_empty_net else f'Goal (xG={xg_goal:.3f})'
                log(scorer, period, play['timeInPeriod'], label, d)
            if assist1 and assist1 in player_stats:
                player_stats[assist1].primary_assists += 1
                a1_key = 'en_primary_assist' if is_empty_net else 'primary_assist'
                d = GRADE_DELTAS[a1_key]
                player_stats[assist1].raw_grade   += d
                player_stats[assist1].raw_offense += d
                label = 'Primary assist (EN)' if is_empty_net else 'Primary assist'
                log(assist1, period, play['timeInPeriod'], label, d)
            if assist2 and assist2 in player_stats:
                player_stats[assist2].secondary_assists += 1
                a2_key = 'en_secondary_assist' if is_empty_net else 'secondary_assist'
                d = GRADE_DELTAS[a2_key]
                player_stats[assist2].raw_grade   += d
                player_stats[assist2].raw_offense += d
                label = 'Secondary assist (EN)' if is_empty_net else 'Secondary assist'
                log(assist2, period, play['timeInPeriod'], label, d)
            if has_shifts:
                for pid, stats in player_stats.items():
                    if is_on_ice_fast(pid, period, time_sec):
                        info = all_players.get(pid)
                        if not info:
                            continue
                        if (info.team == ctx.home_team_abbrev) == (scoring_team == ctx.home_team_id):
                            stats.gf += 1
                        else:
                            stats.ga += 1

        # Individual shots on goal
        if event == 'shot-on-goal':
            pid = details.get('shootingPlayerId')
            if pid and pid in player_stats:
                player_stats[pid].shots_on_goal += 1
                xg_sog = compute_xg(
                    details.get('xCoord'), details.get('yCoord'),
                    details.get('shotType'), 'shot-on-goal'
                )
                player_stats[pid].ixg += xg_sog
                d = GRADE_DELTAS['shot_on_goal_base'] + xg_sog * GRADE_DELTAS['shot_xg_multiplier']
                player_stats[pid].raw_grade   += d
                player_stats[pid].raw_offense += d
                log(pid, period, play['timeInPeriod'], f'Shot on goal (xG={xg_sog:.3f})', d)

        # Blocked shots
        if event == 'blocked-shot':
            xg_blocked = compute_xg(
                details.get('xCoord'), details.get('yCoord'),
                details.get('shotType'), 'blocked-shot'
            )
            blocker = details.get('blockingPlayerId')
            if blocker and blocker in player_stats:
                player_stats[blocker].blocked_shots += 1
                blocker_info = all_players.get(blocker)
                blocker_pos  = blocker_info.position if blocker_info else 'F'
                is_home_b    = blocker_info and blocker_info.team == ctx.home_team_abbrev
                my_sk_b  = int(situation[2]) if is_home_b else int(situation[1])
                opp_sk_b = int(situation[1]) if is_home_b else int(situation[2])
                pk_mult  = GRADE_DELTAS['pk_defensive_mult'] if my_sk_b < opp_sk_b else 1.0
                blk_xg_mult = GRADE_DELTAS['blocked_shot_blocker_xg_mult'] * (1.5 if blocker_pos == 'D' else 1.0) * pk_mult
                blk_base    = GRADE_DELTAS['blocked_shot_blocker_base']    * (1.5 if blocker_pos == 'D' else 1.0) * pk_mult
                d = blk_base + xg_blocked * blk_xg_mult
                player_stats[blocker].raw_grade   += d
                player_stats[blocker].raw_defense += d
                label = f'Blocked shot (xG={xg_blocked:.3f}{"  PK" if pk_mult > 1 else ""})'
                log(blocker, period, play['timeInPeriod'], label, d)
            shooter = details.get('shootingPlayerId')
            if shooter and shooter in player_stats:
                d = GRADE_DELTAS['blocked_shot_shooter_base'] + xg_blocked * GRADE_DELTAS['blocked_shot_xg_mult']
                player_stats[shooter].raw_grade   += d
                player_stats[shooter].raw_offense += d
                log(shooter, period, play['timeInPeriod'], f'Shot blocked (xG={xg_blocked:.3f})', d)

        # Hits
        if event == 'hit':
            zone = details.get('zoneCode', 'N')
            hit_delta = {'D': GRADE_DELTAS['hit_dz'], 'O': GRADE_DELTAS['hit_oz']}.get(zone, GRADE_DELTAS['hit'])
            pid = details.get('hittingPlayerId')
            if pid and pid in player_stats:
                hitter_info = all_players.get(pid)
                is_home_h   = hitter_info and hitter_info.team == ctx.home_team_abbrev
                my_sk_h  = int(situation[2]) if is_home_h else int(situation[1])
                opp_sk_h = int(situation[1]) if is_home_h else int(situation[2])
                pk_mult  = GRADE_DELTAS['pk_defensive_mult'] if my_sk_h < opp_sk_h else 1.0
                hit_d = hit_delta * pk_mult
                player_stats[pid].hits        += 1
                player_stats[pid].raw_grade   += hit_d
                player_stats[pid].raw_defense += hit_d
                label = f'Hit ({zone}Z{"  PK" if pk_mult > 1 else ""})'
                log(pid, period, play['timeInPeriod'], label, hit_d)
            hittee = details.get('hitteePlayerId')
            if hittee and hittee in player_stats:
                d = GRADE_DELTAS['hit_taken']
                player_stats[hittee].raw_grade   += d
                player_stats[hittee].raw_defense += d
                log(hittee, period, play['timeInPeriod'], f'Hit taken ({zone}Z)', d)

        # Penalties
        if event == 'penalty':
            taker = details.get('committedByPlayerId')
            drawer = details.get('drawnByPlayerId')
            if taker and taker in player_stats:
                player_stats[taker].penalties_taken += 1
                player_stats[taker].pim += details.get('duration', 2)
                d = GRADE_DELTAS['penalty_taken']
                player_stats[taker].raw_grade   += d
                player_stats[taker].raw_defense += d
                log(taker, period, play['timeInPeriod'], 'Penalty taken', d)
            if drawer and drawer in player_stats:
                player_stats[drawer].penalties_drawn += 1
                d = GRADE_DELTAS['penalty_drawn']
                player_stats[drawer].raw_grade   += d
                player_stats[drawer].raw_offense += d
                log(drawer, period, play['timeInPeriod'], 'Penalty drawn', d)

    # Calculate TOI per player — use shift chart if available, else boxscore fallback
    for pid in player_stats:
        if has_shifts:
            player_stats[pid].toi_seconds = sum(
                s.end_sec - s.start_sec for s in player_shift_map[pid]
            )
        else:
            player_stats[pid].toi_seconds = boxscore_toi.get(pid, 0)

    # ── Penalty kill credit (second pass) ────────────────────────────────────
    # For each penalty that expires without a PP goal against the penalized team,
    # reward skaters on ice at the kill moment with a pk_kill grade bonus.
    if has_shifts:
        pk_kill_bonus = GRADE_DELTAS['pk_kill']
        _pk_pens = []   # (period, start_sec, expiry_sec, penalized_team_id)
        _pp_goals = []  # (period, sec) — only uneven-strength goals matter

        for play in game_data['plays']:
            event   = play['typeDescKey']
            period  = play['periodDescriptor']['number']
            t       = time_to_seconds(play['timeInPeriod'])
            details = play.get('details', {})

            if event == 'penalty':
                taker = details.get('committedByPlayerId')
                if taker and taker in all_players:
                    pen_abbrev  = all_players[taker].team
                    pen_team_id = ctx.home_team_id if pen_abbrev == ctx.home_team_abbrev else ctx.away_team_id
                    duration_sec = details.get('duration', 2) * 60
                    _pk_pens.append((period, t, t + duration_sec, pen_team_id))

            if event == 'goal':
                sit = str(play.get('situationCode', '1551'))
                if sit[1] != sit[2]:  # uneven strength = PP goal
                    _pp_goals.append((period, t, details.get('eventOwnerTeamId')))

        for pen_period, pen_start, pen_expiry, pen_team_id in _pk_pens:
            # A kill fails if the opposing team scores a PP goal during this window
            was_scored = any(
                gp == pen_period and pen_start < gs <= pen_expiry and gt != pen_team_id
                for gp, gs, gt in _pp_goals
            )
            if not was_scored:
                check_sec = pen_expiry - 1
                t_str = f"{check_sec // 60}:{check_sec % 60:02d}"
                for pid, stats in player_stats.items():
                    info = all_players.get(pid)
                    if not info or info.position == 'G':
                        continue
                    on_pk_team = (info.team == ctx.home_team_abbrev) == (pen_team_id == ctx.home_team_id)
                    if on_pk_team and is_on_ice_fast(pid, pen_period, check_sec):
                        stats.pk_kills    += 1
                        stats.raw_grade   += pk_kill_bonus
                        stats.raw_defense += pk_kill_bonus
                        log(pid, pen_period, t_str, 'Penalty kill', pk_kill_bonus)

    return player_stats, all_players, ctx, game_data, play_log


def grade_game(player_stats: dict, all_players: dict, game_id: int = 0) -> list:
    """
    Normalize scores and return a sorted list of player grade dicts.
    Shared by display_game() (rich terminal) and the Flask web app.
    Pass game_id to attach microstat grades when an xlsx log exists.
    """
    import statistics as _stats

    graded_players = [
        (all_players[pid], player_stats[pid])
        for pid in player_stats
        if all_players.get(pid) and all_players[pid].position != 'G'
    ]

    PRIOR_MINUTES    = 12.0
    MIN_SCALE_TOI    = 15.0   # per-60 scaling floor: prevents extreme amplification for <15 min players
    positions = [info.position for info, _ in graded_players]

    def scale_and_normalize(raw_list):
        per_60 = [r * (60 / max(s.toi_seconds / 60, MIN_SCALE_TOI)) if s.toi_seconds > 0 else 0.0
                  for r, (_, s) in zip(raw_list, graded_players)]
        mean_p60 = _stats.mean(per_60) if per_60 else 0.0
        scaled = []
        for (_, s), p60 in zip(graded_players, per_60):
            toi_min = s.toi_seconds / 60
            w = toi_min / (toi_min + PRIOR_MINUTES)
            scaled.append(w * p60 + (1 - w) * mean_p60)
        return normalize_by_position_group(list(zip(scaled, positions)))

    def normalize_raw(raw_list):
        # Defensive events are discrete counts — no per-60 scaling.
        # Just Bayesian shrinkage toward the game mean, then normalize.
        mean_raw = _stats.mean(raw_list) if raw_list else 0.0
        scaled = []
        for raw, (_, s) in zip(raw_list, graded_players):
            toi_min = s.toi_seconds / 60
            w = toi_min / (toi_min + PRIOR_MINUTES)
            scaled.append(w * raw + (1 - w) * mean_raw)
        return normalize_by_position_group(list(zip(scaled, positions)))

    overall_norm = scale_and_normalize([s.raw_grade      for _, s in graded_players])
    offense_norm = scale_and_normalize([s.raw_offense    for _, s in graded_players])
    defense_norm = normalize_raw(      [s.raw_defense    for _, s in graded_players])
    possess_norm = scale_and_normalize([s.raw_possession for _, s in graded_players])
    faceoff_norm = scale_and_normalize([s.raw_faceoff    for _, s in graded_players])

    rows = []
    for i, (info, stats) in enumerate(graded_players):
        fo_total = (stats.es_fo_won + stats.es_fo_lost +
                    stats.pk_fo_won + stats.pk_fo_lost +
                    stats.pp_fo_won + stats.pp_fo_lost)
        has_fo_grade = fo_total >= 3
        has_fo_pct   = fo_total >= 10
        rows.append({
            'player_id':      info.player_id,
            'name':           info.name,
            'team':           info.team,
            'position':       info.position,
            'overall':        overall_norm[i],
            'overall_letter': score_to_letter(overall_norm[i]),
            'off':            offense_norm[i],
            'off_letter':     score_to_letter(offense_norm[i]),
            'dfn':            defense_norm[i],
            'dfn_letter':     score_to_letter(defense_norm[i]),
            'poss':           possess_norm[i],
            'poss_letter':    score_to_letter(possess_norm[i]),
            'fo':             faceoff_norm[i] if has_fo_grade else None,
            'fo_letter':      score_to_letter(faceoff_norm[i]) if has_fo_grade else None,
            'toi':            stats.toi_str,
            'cf_pct':         str(stats.cf_pct),
            'xg_pct':         str(stats.xg_pct),
            'goals':          stats.goals,
            'assists':        stats.assists,
            'sog':            stats.shots_on_goal,
            'hits':           stats.hits,
            'blocks':         stats.blocked_shots,
            'gva':            stats.giveaways,
            'tka':            stats.takeaways,
            'pim':            stats.pim,
            'pk_xga':         round(stats.pk_xga, 3),
            'pk_kills':       stats.pk_kills,
            'raw_grade':      stats.raw_grade,
            'fo_pct':         f"{stats.faceoff_pct}%" if has_fo_pct else '—',
            # Microstat grades (None when no xlsx log exists for this game)
            'ms':             None,
        })

    # Attach microstat grades where an xlsx log exists for this game
    if game_id:
        for row in rows:
            ms = get_microstat_grade(game_id, row['name'])
            if ms:
                row['ms'] = {
                    'overall':     ms.overall,
                    'offense':     ms.offense,
                    'entries':     ms.entries,
                    'exits':       ms.exits,
                    'defense':     ms.entry_defense,
                    'forechecking': ms.forechecking,
                    'overall_100':  ms.overall_100,
                    'offense_100':  ms.offense_100,
                    'entries_100':  ms.entries_100,
                    'exits_100':    ms.exits_100,
                    'defense_100':  ms.defense_100,
                    'forechecking_100': ms.forechecking_100,
                    'position':     ms.position,
                    'secondary_assists':   getattr(ms, 'raw_secondary_assists',  0),
                    'pass_entries':        getattr(ms, 'raw_pass_entries',        0),
                    'shots_off_rush':      getattr(ms, 'raw_shots_off_rush',      0),
                    'shots_off_forecheck': getattr(ms, 'raw_shots_off_forecheck', 0),
                    'dz_breakout':         getattr(ms, 'raw_dz_breakout',         0),
                }

    # Inject microstat data for fields the API cannot measure at all:
    #   poss  → replaced by entries+exits average (API has CF%/xG but not
    #            the actual zone-entry/exit mechanics behind them)
    #   dfn   → 70% MS entry defense (denials, CCA) + 30% API defense
    #            (blocks, hits, PK kills) — manual weighted higher, same as season view
    #   overall → recomputed from sub-grades, adding forechecking as a new
    #             component (API has zero signal on forecheck pressure/DZ retrievals)
    # off and fo are left as-is — the API covers goals/xG/faceoffs well.
    if game_id:
        for row in rows:
            ms = row.get('ms')
            if not ms:
                continue
            ms_zone = (ms['entries_100'] + ms['exits_100']) / 2.0
            row['poss'] = ms_zone
            row['dfn']  = 0.70 * ms['defense_100'] + 0.30 * row['dfn']
            is_d = row['position'] == 'D'
            if is_d:
                # Defensemen: off 20% | dfn 50% | poss 30% (fo displaces poss)
                if row['fo'] is not None:
                    row['overall'] = (
                        0.20 * row['off'] +
                        0.50 * row['dfn'] +
                        0.25 * row['poss'] +
                        0.05 * row['fo']
                    )
                else:
                    row['overall'] = (
                        0.20 * row['off'] +
                        0.50 * row['dfn'] +
                        0.30 * row['poss']
                    )
            else:
                # Forwards: off 40% | dfn 25% | poss 35% (fo displaces poss)
                if row['fo'] is not None:
                    row['overall'] = (
                        0.40 * row['off'] +
                        0.25 * row['dfn'] +
                        0.30 * row['poss'] +
                        0.05 * row['fo']
                    )
                else:
                    row['overall'] = (
                        0.40 * row['off'] +
                        0.25 * row['dfn'] +
                        0.35 * row['poss']
                    )
            row['overall_letter'] = score_to_letter(row['overall'])

    rows.sort(key=lambda x: x['overall'], reverse=True)
    for i, row in enumerate(rows, 1):
        row['rank'] = i
    return rows


def display_game(player_stats, all_players, ctx, game_data, play_log, breakdown_player=None):
    """Normalize grades and print the game leaderboard in PFF-style rich table."""
    from rich.console import Console
    from rich.table import Table
    from rich import box
    from rich.text import Text

    console = Console(width=200)

    away = ctx.away_team_name
    home = ctx.home_team_name
    away_score = game_data['awayTeam']['score']
    home_score = game_data['homeTeam']['score']
    console.print(f"\n[bold]{away} @ {home}[/bold]   [dim]{away} {away_score} – {home_score} {home}[/dim]\n")

    rows = grade_game(player_stats, all_players)

    def grade_color(score: float) -> str:
        if score >= 80: return "bold green"
        if score >= 70: return "green"
        if score >= 60: return "yellow"
        if score >= 50: return "dark_orange"
        return "red"

    def fmt_grade(row) -> Text:
        return Text(f"{row['overall_letter']} {row['overall']:.1f}", style=grade_color(row['overall']))

    def fmt_sub(score) -> Text:
        if score is None:
            return Text("—", style="dim")
        return Text(f"{score:.1f}", style=grade_color(score))

    # ── build table ───────────────────────────────────────────────────────────
    table = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold white on grey23",
                  show_edge=False, pad_edge=False, title=None)

    table.add_column("#",       style="dim", width=4, justify="right")
    table.add_column("NAME",    min_width=20)
    table.add_column("TEAM",    width=5)
    table.add_column("POS",     width=4, justify="center")
    table.add_column("PHF GRD", width=10, justify="center", header_style="bold cyan on grey23")
    table.add_column("OFF",     width=7,  justify="center", header_style="cyan on grey23")
    table.add_column("DEF",     width=7,  justify="center", header_style="cyan on grey23")
    table.add_column("POSS",    width=7,  justify="center", header_style="cyan on grey23")
    table.add_column("FO",      width=7,  justify="center", header_style="cyan on grey23")
    table.add_column("TOI",     width=7,  justify="right")
    table.add_column("CF%",     width=7,  justify="right")
    table.add_column("xG%",     width=7,  justify="right")
    table.add_column("G",       width=4,  justify="right")
    table.add_column("A",       width=4,  justify="right")
    table.add_column("SOG",     width=5,  justify="right")
    table.add_column("HIT",     width=5,  justify="right")
    table.add_column("BLK",     width=5,  justify="right")
    table.add_column("GVA",     width=5,  justify="right")
    table.add_column("TKA",     width=5,  justify="right")

    for row in rows:
        table.add_row(
            str(row['rank']),
            row['name'],
            row['team'],
            row['position'],
            fmt_grade(row),
            fmt_sub(row['off']),
            fmt_sub(row['dfn']),
            fmt_sub(row['poss']),
            fmt_sub(row['fo']),
            row['toi'],
            row['cf_pct'],
            row['xg_pct'],
            str(row['goals']),
            str(row['assists']),
            str(row['sog']),
            str(row['hits']),
            str(row['blocks']),
            str(row['gva']),
            str(row['tka']),
        )

    console.print(table)

    # ─── Play-by-play breakdown ───────────────────────────────────────────────
    if breakdown_player:
        target = next((r for r in rows if r['name'] == breakdown_player), None)
        if target:
            pid = target['player_id']
            events = play_log.get(pid, [])
            console.print(f"\n[bold]Play-by-play: {target['name']} ({target['team']}, {target['position']}) — {target['overall_letter']} ({target['overall']:.1f})[/bold]")
            console.print(f"Raw grade: {target['raw_grade']:.2f}")
            bp = Table(box=box.SIMPLE, show_header=True, header_style="dim")
            bp.add_column("P",       width=3)
            bp.add_column("TIME",    width=8)
            bp.add_column("EVENT",   min_width=40)
            bp.add_column("DELTA",   width=7, justify="right")
            bp.add_column("RUNNING", width=9, justify="right")
            running = 0.0
            for p, t, desc, d in events:
                running += d
                sign = "+" if d >= 0 else ""
                color = "green" if d > 0 else ("red" if d < 0 else "dim")
                bp.add_row(str(p), t, desc,
                           Text(f"{sign}{d:.2f}", style=color),
                           Text(f"{running:.2f}", style="dim"))
            console.print(bp)
        else:
            console.print(f"[red]Player '{breakdown_player}' not found.[/red]")


if __name__ == '__main__':
    GAME_ID = 2025030134
    SEASON = '20252026'
    BREAKDOWN_PLAYER = 'Jordan Staal'  # set to None to skip

    player_stats, all_players, ctx, game_data, play_log = process_game(
        game_id=GAME_ID,
        season=SEASON,
        from_file='car_ott.json'
    )
    display_game(player_stats, all_players, ctx, game_data, play_log, breakdown_player=BREAKDOWN_PLAYER)
