"""
PFF-style additive grade points from AllThreeZones tracking data.
Full hierarchy: Passing, Shooting, OZ Activity, DZ Exit, Entry Defense.
All bonuses are additive per event count.
"""
from __future__ import annotations
from manual_loader import MicrostatRecord


def compute_tracking_split(r: MicrostatRecord, position: str = 'F') -> tuple:
    """
    Return (off_pts, dz_exit_pts, entry_dfn_pts) raw grade points.
    off_pts     = Passing + Shooting + OZ Activity
    dz_exit_pts = Defensive Zone Exit
    entry_dfn   = Entry Defense
    """
    off = 0.0
    dz_exit = 0.0
    entry_d = 0.0

    # ── PASSING ──────────────────────────────────────────────────────────────
    off += r.primary_assists   * 0.70
    off += r.secondary_assists * 0.35
    off += r.tertiary_assists  * 0.15
    off += r.chance_assists    * 0.40
    off += r.home_plate_assists  * 0.20
    off += r.low_high_assists    * 0.12
    off += r.behind_net_assists  * 0.30
    off += r.center_lane_assists * 0.18
    off += r.assists_off_rush    * 0.20
    off += r.assists_off_forecheck * 0.15
    off += r.assists_off_cycle   * 0.12
    off += r.onetimer_assists    * 0.20
    off += r.deflect_assists     * 0.15
    off += r.nz_assist   * 0.04
    off += r.dz_assist   * 0.02
    off += r.dz_assists  * 0.10
    off += r.primary_goal_assists   * 1.50
    off += r.secondary_goal_assists * 0.50
    off += r.tertiary_goal_assists  * 0.25

    # ── SHOOTING ─────────────────────────────────────────────────────────────
    off += r.sog     * 0.18
    off += r.chances * 0.30
    off += r.shots_off_rush      * 0.20
    off += r.shots_off_forecheck * 0.15
    off += r.shots_off_cycle     * 0.12
    off += r.shots_off_hd        * 0.16
    off += r.rebounds    * 0.20
    off += r.onetimers   * 0.25
    off += r.deflections * 0.22
    off += r.goals * 2.00

    # ── OFFENSIVE ZONE ACTIVITY ───────────────────────────────────────────────
    off += r.carries          *  0.45
    off += r.carries_w_chances *  0.25
    off += r.failed_entries   * -0.20
    off += r.pass_entries     *  0.38
    off += r.dump_in_chances  *  0.20
    off += r.fc_pressures     *  0.20
    off += r.recoveries       *  0.15

    # ── DEFENSIVE ZONE EXIT ───────────────────────────────────────────────────
    dz_exit += r.carry_exits                  *  0.20
    dz_exit += r.pass_exits                   *  0.22
    dz_exit += r.retrievals_leading_to_exits  *  0.25
    dz_exit += r.clears                       *  0.38
    dz_exit += r.exchanges                    *  0.05
    dz_exit += r.failed_exits                 * -0.50
    dz_exit += r.botched_retrievals           * -0.55
    dz_exit += r.missed_passes                * -0.15

    # ── ENTRY DEFENSE ─────────────────────────────────────────────────────────
    entry_d += r.denials              *  0.80
    entry_d += r.carries_chance_against * -0.45
    entry_d += r.dump_in_chance_against * -0.20
    # DZ retrievals not already credited via retrievals_leading_to_exits
    entry_d += max(0, (r.dz_retrievals or 0) - (r.retrievals_leading_to_exits or 0)) * 0.12

    return off, dz_exit, entry_d


def compute_tracking_grade(r: MicrostatRecord, position: str = 'F') -> float:
    """Return total raw PFF-style grade points (sum of all 5 categories)."""
    off, dz_exit, entry_d = compute_tracking_split(r, position)
    return off + dz_exit + entry_d
