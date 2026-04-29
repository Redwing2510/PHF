from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Shift:
    player_id: int
    team_id: int
    period: int
    start_sec: int
    end_sec: int


@dataclass
class PlayerInfo:
    player_id: int
    name: str
    position: str
    team: str
    team_name: str = ''


@dataclass
class PlayerStats:
    player_id: int

    # Faceoffs
    es_fo_won: int = 0
    es_fo_lost: int = 0
    pp_fo_won: int = 0
    pp_fo_lost: int = 0
    pk_fo_won: int = 0
    pk_fo_lost: int = 0
    oz_faceoffs: int = 0
    dz_faceoffs: int = 0
    nz_faceoffs: int = 0

    # Shot attempts (Corsi)
    cf: int = 0  # shot attempts for while on ice
    ca: int = 0  # shot attempts against while on ice

    # Puck possession
    giveaways: int = 0
    takeaways: int = 0

    # Individual offensive stats
    goals: int = 0
    primary_assists: int = 0
    secondary_assists: int = 0
    shots_on_goal: int = 0       # individual shots on goal (not on-ice Corsi)
    hits: int = 0
    blocked_shots: int = 0       # individual shot blocks
    penalties_taken: int = 0
    penalties_drawn: int = 0
    pim: int = 0                 # penalty minutes

    # On-ice goal tracking
    gf: int = 0                  # goals scored while on ice
    ga: int = 0                  # goals allowed while on ice

    # Expected goals
    xgf: float = 0.0             # expected goals for while on ice
    xga: float = 0.0             # expected goals against while on ice
    pk_xga: float = 0.0          # expected goals against while on PK (subset of xga)
    ixg: float = 0.0             # individual xG (shooter's own shots on goal)

    # PK tracking
    pk_kills: int = 0            # number of penalty kills player was on ice to complete

    # PFF-style play-by-play grade accumulator
    raw_grade: float = 0.0       # sum of per-play grade deltas; normalized to 0-100 at output

    # Sub-grade accumulators (split by category, normalized separately at output)
    raw_offense: float = 0.0     # goals, assists, shots, ixG
    raw_defense: float = 0.0     # blocks, hits, takeaways, giveaways
    raw_possession: float = 0.0  # on-ice shot for/against deltas
    raw_faceoff: float = 0.0     # all faceoff deltas

    # Time on ice (seconds), computed from shifts
    toi_seconds: int = 0

    # Computed properties
    @property
    def assists(self) -> int:
        return self.primary_assists + self.secondary_assists

    @property
    def points(self) -> int:
        return self.goals + self.primary_assists + self.secondary_assists

    @property
    def on_ice_goal_diff(self) -> int:
        return self.gf - self.ga

    @property
    def toi_str(self) -> str:
        m, s = divmod(self.toi_seconds, 60)
        return f"{m}:{s:02d}"

    @property
    def total_faceoffs(self) -> int:
        return (self.es_fo_won + self.es_fo_lost +
                self.pp_fo_won + self.pp_fo_lost +
                self.pk_fo_won + self.pk_fo_lost)

    @property
    def faceoff_pct(self) -> float:
        total = self.total_faceoffs
        if total == 0:
            return 0.0
        won = self.es_fo_won + self.pp_fo_won + self.pk_fo_won
        return round(won / total * 100, 1)

    @property
    def pk_faceoff_pct(self) -> float:
        total = self.pk_fo_won + self.pk_fo_lost
        if total == 0:
            return 0.0
        return round(self.pk_fo_won / total * 100, 1)

    @property
    def pp_faceoff_pct(self) -> float:
        total = self.pp_fo_won + self.pp_fo_lost
        if total == 0:
            return 0.0
        return round(self.pp_fo_won / total * 100, 1)

    @property
    def cf_pct(self) -> float:
        total = self.cf + self.ca
        if total == 0:
            return 0.0
        return round(self.cf / total * 100, 1)

    @property
    def xg_pct(self) -> float:
        total = self.xgf + self.xga
        if total == 0.0:
            return 0.0
        return round(self.xgf / total * 100, 1)


@dataclass
class GameContext:
    game_id: int
    season: int
    home_team_id: int
    away_team_id: int
    home_team_name: str
    away_team_name: str
    home_team_abbrev: str
    away_team_abbrev: str