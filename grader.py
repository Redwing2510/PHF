import statistics
from models import PlayerStats, PlayerInfo
from typing import List, Tuple


# ─── Play-by-play grade deltas (PFF-style, -2 to +2 per play) ────────────────
# Separate tables for forwards and defensemen.
# Raw grade totals are normalized to mean = 60 across all players at output time.

# Faceoff rows are identical in both tables; FACEOFF_POS_MULTIPLIER handles
# position scaling (0.65× for F, 1.0× for D). pk_defensive_mult is shared (1.5×).

_FO_AND_PK = {
    'pk_defensive_mult':            1.5,
    'fo_es_win_oz':                 0.50,
    'fo_es_win_dz':                 0.80,
    'fo_es_win_nz':                 0.30,
    'fo_es_loss_oz':               -0.50,
    'fo_es_loss_dz':               -0.80,
    'fo_es_loss_nz':               -0.30,
    'fo_pk_win_oz':                 0.60,
    'fo_pk_win_dz':                 1.20,
    'fo_pk_win_nz':                 0.40,
    'fo_pk_loss_oz':               -0.30,
    'fo_pk_loss_dz':               -1.50,
    'fo_pk_loss_nz':               -0.60,
    'fo_pp_win_oz':                 0.70,
    'fo_pp_win_dz':                 0.60,
    'fo_pp_win_nz':                 0.30,
    'fo_pp_loss_oz':               -0.90,
    'fo_pp_loss_dz':               -0.50,
    'fo_pp_loss_nz':               -0.40,
}

GRADE_DELTAS_F = {
    **_FO_AND_PK,

    # Shots — now handled by tracking_grader
    'shot_on_goal_base':            0.00,
    'shot_xg_multiplier':           0.00,
    'missed_shot_base':             0.00,
    'missed_shot_xg_mult':          0.00,
    'blocked_shot_shooter_base':    0.00,
    'blocked_shot_xg_mult':         0.00,
    'blocked_shot_blocker_base':    0.00,
    'blocked_shot_blocker_xg_mult': 0.00,

    # Scoring — now handled by tracking_grader
    'goal_scorer_bonus':            0.00,
    'empty_net_goal_bonus':         0.00,
    'primary_assist':               0.00,
    'secondary_assist':             0.00,
    'en_primary_assist':            0.00,
    'en_secondary_assist':          0.00,

    # Giveaways/takeaways — removed
    'giveaway_oz':                  0.00,
    'giveaway_nz':                  0.00,
    'giveaway_dz':                  0.00,
    'takeaway_oz':                  0.00,
    'takeaway_nz':                  0.00,
    'takeaway_dz':                  0.00,

    # Physical — F only get OZ hit credit; DZ and NZ hits dropped
    'hit_dz':                       0.00,
    'hit':                          0.00,
    'hit_oz':                       0.08,
    'hit_taken':                    0.00,

    # On-ice possession — dropped
    'on_ice_shot_for_base':         0.00,
    'on_ice_shot_for_xg_mult':      0.00,
    'on_ice_shot_against_base':     0.00,
    'on_ice_shot_against_xg_mult':  0.00,

    # Penalties
    'penalty_taken':               -1.00,
    'penalty_drawn':                0.80,
    'pk_kill':                      0.00,
}

GRADE_DELTAS_D = {
    **_FO_AND_PK,

    # Shots — now handled by tracking_grader
    'shot_on_goal_base':            0.00,
    'shot_xg_multiplier':           0.00,
    'missed_shot_base':             0.00,
    'missed_shot_xg_mult':          0.00,
    'blocked_shot_shooter_base':    0.00,
    'blocked_shot_xg_mult':         0.00,
    'blocked_shot_blocker_base':    0.00,
    'blocked_shot_blocker_xg_mult': 0.00,

    # Scoring — now handled by tracking_grader
    'goal_scorer_bonus':            0.00,
    'empty_net_goal_bonus':         0.00,
    'primary_assist':               0.00,
    'secondary_assist':             0.00,
    'en_primary_assist':            0.00,
    'en_secondary_assist':          0.00,

    # Giveaways/takeaways — removed
    'giveaway_oz':                  0.00,
    'giveaway_nz':                  0.00,
    'giveaway_dz':                  0.00,
    'takeaway_oz':                  0.00,
    'takeaway_nz':                  0.00,
    'takeaway_dz':                  0.00,

    # Physical — D only get DZ hit credit; OZ hits dropped
    'hit_dz':                       0.08,
    'hit':                          0.00,
    'hit_oz':                       0.00,
    'hit_taken':                    0.00,

    # On-ice possession — dropped
    'on_ice_shot_for_base':         0.00,
    'on_ice_shot_for_xg_mult':      0.00,
    'on_ice_shot_against_base':     0.00,
    'on_ice_shot_against_xg_mult':  0.00,

    # Penalties
    'penalty_taken':               -0.75,
    'penalty_drawn':                0.50,
    'pk_kill':                      0.00,
}

# Backwards-compatible alias used for position-neutral lookups (faceoffs, pk_defensive_mult).
GRADE_DELTAS = GRADE_DELTAS_F


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
