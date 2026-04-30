import statistics
from models import PlayerStats, PlayerInfo
from typing import List, Tuple


# ─── Play-by-play grade deltas (PFF-style, -2 to +2 per play) ────────────────
# Applied per discrete event in pipeline.py.
# Raw grade totals are normalized to mean = 60 across all players at output time.

GRADE_DELTAS = {
    # Shots — base delta + xG weight so shot quality drives the score
    # A 0.30 xG shot-on-goal = 0.5 + 0.30*2.0 = +1.1  (well above average)
    # A 0.05 xG perimeter shot = 0.5 + 0.05*2.0 = +0.6  (modest credit)
    'shot_on_goal_base':            0.5,
    'shot_xg_multiplier':           2.0,
    'missed_shot_base':             0.1,
    'missed_shot_xg_mult':          1.5,
    'blocked_shot_shooter_base':    0.05,   # shooter loses most credit
    'blocked_shot_xg_mult':         1.0,

    # Shot blocking (the defender)
    'blocked_shot_blocker_base':    0.5,
    'blocked_shot_blocker_xg_mult': 1.5,   # blocking a high-danger chance = more credit

    # Scoring events — PFF treats TDs as +3 (breaks the normal -2/+2 ceiling).
    # A goal is hockey's equivalent: bonus stacks on top of the shot delta.
    # SOG(0.20 xG) + bonus = 0.5 + 0.40 + 2.5 = +3.4 for a typical goal scorer.
    'goal_scorer_bonus':            2.5,   # stacks on top of the shot delta
    'empty_net_goal_bonus':         0.8,   # EN goals: still good but no goalie to beat
    'primary_assist':               1.875,
    'secondary_assist':             0.625,
    'en_primary_assist':            0.6,   # EN primary assist
    'en_secondary_assist':          0.3,   # EN secondary assist

    # Puck battles — zone-aware (zone from the player's own perspective)
    'giveaway_oz':                 -0.50,  # minor: far from your net
    'giveaway_nz':                 -1.00,  # neutral zone turnover
    'giveaway_dz':                 -2.00,  # catastrophic: direct scoring chance
    'takeaway_oz':                  0.75,  # pressuring in their zone
    'takeaway_nz':                  1.00,  # neutral zone battle won
    'takeaway_dz':                  1.50,  # saved a clear danger

    # Physical play — zone-aware
    'hit_dz':                       0.45,  # DZ hit: protecting your net
    'hit':                          0.30,  # NZ hit
    'hit_oz':                       0.15,  # OZ hit: forecheck, less critical
    'hit_taken':                   -0.15,  # being physically dominated

    # On-ice possession — every shot attempt while on ice contributes
    # so CF%/xG% feed directly into raw_grade for all skaters, not just shooters.
    # A high-danger shot for you on ice (xG=0.25): +0.10 + 0.30*0.25 = +0.175
    # A perimeter shot for you on ice (xG=0.04):  +0.10 + 0.30*0.04 = +0.112
    'on_ice_shot_for_base':         0.10,
    'on_ice_shot_for_xg_mult':      0.30,
    'on_ice_shot_against_base':    -0.10,
    'on_ice_shot_against_xg_mult': -0.30,

    # Penalties
    'penalty_taken':               -1.5,
    'penalty_drawn':                0.8,
    'pk_kill':                      0.8,   # bonus for being on ice when a PK is successfully killed
    'pk_defensive_mult':            1.5,   # blocks, hits, takeaways are worth 1.5x while shorthanded

    # Faceoffs — situation × zone matrix
    # ES: standard zone weights
    'fo_es_win_oz':                 0.50,
    'fo_es_win_dz':                 0.80,
    'fo_es_win_nz':                 0.30,
    'fo_es_loss_oz':               -0.50,
    'fo_es_loss_dz':               -0.80,
    'fo_es_loss_nz':               -0.30,
    # PK: DZ amplified — losing it while shorthanded is critical
    'fo_pk_win_oz':                 0.60,   # sustained pressure while killing
    'fo_pk_win_dz':                 1.20,   # cleared danger while shorthanded
    'fo_pk_win_nz':                 0.40,
    'fo_pk_loss_oz':               -0.30,   # at least it's far from your net
    'fo_pk_loss_dz':               -1.50,   # shorthanded + puck in your zone = worst case
    'fo_pk_loss_nz':               -0.60,
    # PP: OZ amplified — losing it squanders the power play
    'fo_pp_win_oz':                 0.70,   # sets up the man advantage properly
    'fo_pp_win_dz':                 0.60,   # clearing while on PP
    'fo_pp_win_nz':                 0.30,
    'fo_pp_loss_oz':               -0.90,   # squandering the PP setup
    'fo_pp_loss_dz':               -0.50,   # expected they'll try to clear it
    'fo_pp_loss_nz':               -0.40,
}


# ─── Position faceoff multiplier ────────────────────────────────────────────
# Forwards: faceoffs are important but secondary to possession.
# Defensemen: faceoffs are rare, keep full weight when they do occur.
FACEOFF_POS_MULTIPLIER = {
    'C': 0.65,
    'L': 0.65,
    'R': 0.65,
    'D': 1.00,
}

# ─── Normalization ────────────────────────────────────────────────────────────
# PFF standard: average player in a game = 60, normalized via z-score.

NORM_MEAN   = 60.0   # target mean for every game
NORM_SD_PTS = 12.0   # one standard deviation = 12 grade points
                     # +1 SD → ~72 (B-)  |  +2 SD → ~84 (A-)
                     # -1 SD → ~48 (F)   |  -2 SD → ~36 (F)


def normalize_grades(raw_grades: List[float]) -> List[float]:
    """
    Normalize a list of raw play-by-play grade totals to a 0-100 scale
    where the mean is 60 (PFF standard) and one SD = 12 points.
    """
    if len(raw_grades) < 2:
        return [NORM_MEAN] * len(raw_grades)
    mean = statistics.mean(raw_grades)
    std  = statistics.stdev(raw_grades)
    if std == 0:
        return [NORM_MEAN] * len(raw_grades)
    return [
        round(max(0.0, min(100.0, NORM_MEAN + (r - mean) / std * NORM_SD_PTS)), 1)
        for r in raw_grades
    ]


def normalize_by_position_group(grades_with_pos: List[Tuple[float, str]]) -> List[float]:
    """
    Normalize forwards (C/L/R) and defensemen (D) separately.
    Each group targets mean=60, 1 SD=12 points — so a forward is graded
    relative to other forwards in the same game, not mixed with D-men.
    """
    FORWARD_POS = {'C', 'L', 'R'}
    fwd_idx = [i for i, (_, pos) in enumerate(grades_with_pos) if pos in FORWARD_POS]
    def_idx = [i for i, (_, pos) in enumerate(grades_with_pos) if pos == 'D']

    result = [NORM_MEAN] * len(grades_with_pos)
    for indices in (fwd_idx, def_idx):
        if not indices:
            continue
        group = [grades_with_pos[i][0] for i in indices]
        normed = normalize_grades(group)
        for i, val in zip(indices, normed):
            result[i] = val
    return result


# ─── Legacy weighted scoring (kept for reference, no longer used) ─────────────
WEIGHTS = {
    'center': {
        'xg_pct':        0.18,
        'cf_pct':        0.05,
        'es_faceoff':    0.12,
        'pk_faceoff':    0.12,
        'pp_faceoff':    0.04,
        'turnovers':     0.07,
        'points':        0.17,
        'ixg':           0.05,
        'shots':         0.03,
        'hits':          0.02,
        'blocked_shots': 0.02,
        'on_ice_goals':  0.09,
        'penalties':     0.04,
    },
    'winger': {
        'xg_pct':        0.18,
        'cf_pct':        0.05,
        'es_faceoff':    0.02,
        'pk_faceoff':    0.02,
        'pp_faceoff':    0.02,
        'turnovers':     0.09,
        'points':        0.22,
        'ixg':           0.08,
        'shots':         0.07,
        'hits':          0.06,
        'blocked_shots': 0.02,
        'on_ice_goals':  0.12,
        'penalties':     0.05,
    },
    'defenseman': {
        'xg_pct':        0.20,
        'cf_pct':        0.05,
        'es_faceoff':    0.01,
        'pk_faceoff':    0.02,
        'pp_faceoff':    0.01,
        'turnovers':     0.09,
        'points':        0.08,
        'ixg':           0.03,
        'shots':         0.03,
        'hits':          0.09,
        'blocked_shots': 0.13,
        'on_ice_goals':  0.22,
        'penalties':     0.04,
    },
    'goalie': {
        'xg_pct': 0.0, 'cf_pct': 0.0, 'es_faceoff': 0.0, 'pk_faceoff': 0.0, 'pp_faceoff': 0.0,
        'turnovers': 0.0, 'points': 0.0, 'ixg': 0.0, 'shots': 0.0, 'hits': 0.0,
        'blocked_shots': 0.0, 'on_ice_goals': 0.0, 'penalties': 0.0,
    }
}


def get_position_group(position: str) -> str:
    if position == 'G':
        return 'goalie'
    elif position == 'D':
        return 'defenseman'
    elif position in ('L', 'R'):
        return 'winger'
    else:
        return 'center'


def score_to_letter(score: float) -> str:
    if score >= 90:   return 'A+'
    elif score >= 85: return 'A'
    elif score >= 80: return 'A-'
    elif score >= 77: return 'B+'
    elif score >= 73: return 'B'
    elif score >= 70: return 'B-'
    elif score >= 67: return 'C+'
    elif score >= 63: return 'C'
    elif score >= 60: return 'C-'
    elif score >= 57: return 'D+'
    elif score >= 53: return 'D'
    elif score >= 50: return 'D-'
    else:             return 'F'
