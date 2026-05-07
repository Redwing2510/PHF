"""
mp_loader.py

Loads MoneyPuck per-player per-game CSV data into cache.db.
Only keeps the columns and situations we actually use.
"""
from __future__ import annotations
import sqlite3
from pathlib import Path
import pandas as pd

DB_PATH   = Path(__file__).parent / 'cache.db'
CSV_PATHS = [
    Path(__file__).parent / 'MoneyPuck Game Data' / '2025-26 Player Game Data.csv',
]

# Situations we care about:
#   'all'  — for individual stats, zone shifts, xGA, possession
#   '4on5' — for PK xGA (player is killing the penalty)
#   '5on4' — for PP xGF (player is on the power play)
KEEP_SITUATIONS = {'all', '4on5', '5on4'}

KEEP_COLS = [
    'playerId',
    'gameId',
    'season',
    'playerTeam',
    'situation',
    'icetime',
    # Individual production
    'I_F_goals',
    'I_F_primaryAssists',
    'I_F_secondaryAssists',
    'I_F_shotsOnGoal',
    'I_F_shotAttempts',
    'I_F_hits',
    'I_F_takeaways',
    'I_F_giveaways',
    'I_F_penalityMinutes',
    'gameScore',
    # Individual xG
    'I_F_xGoals',
    'I_F_scoreVenueAdjustedxGoals',
    'I_F_highDangerxGoals',
    'I_F_highDangerShots',
    'I_F_rebounds',
    # Zone shift starts — DZ opportunity normalizer
    'I_F_dZoneShiftStarts',
    'I_F_oZoneShiftStarts',
    'I_F_neutralZoneShiftStarts',
    'I_F_flyShiftStarts',
    # On-ice xGA
    'OnIce_A_xGoals',
    'OnIce_A_scoreVenueAdjustedxGoals',
    'OnIce_A_highDangerxGoals',
    # On-ice xGF
    'OnIce_F_xGoals',
    'OnIce_F_scoreVenueAdjustedxGoals',
    # Possession percentages
    'onIce_xGoalsPercentage',
    'offIce_xGoalsPercentage',
    'onIce_corsiPercentage',
    'offIce_corsiPercentage',
    # Backchecking signal
    'xGoalsAgainstAfterShifts',
    'xGoalsForAfterShifts',
    # DZ-specific giveaways
    'I_F_dZoneGiveaways',
]

COL_RENAME = {
    'playerId':                         'player_id',
    'gameId':                           'game_id',
    'season':                           'season',
    'playerTeam':                       'team',
    'situation':                        'situation',
    'icetime':                          'icetime',
    # Individual production
    'I_F_goals':                        'goals',
    'I_F_primaryAssists':               'primary_assists',
    'I_F_secondaryAssists':             'secondary_assists',
    'I_F_shotsOnGoal':                  'shots_on_goal',
    'I_F_shotAttempts':                 'shot_attempts',
    'I_F_hits':                         'hits',
    'I_F_takeaways':                    'takeaways',
    'I_F_giveaways':                    'giveaways',
    'I_F_penalityMinutes':              'pim',
    'gameScore':                        'game_score',
    # Individual xG
    'I_F_xGoals':                       'ixg',
    'I_F_scoreVenueAdjustedxGoals':     'ixg_adj',
    'I_F_highDangerxGoals':             'ixg_hd',
    'I_F_highDangerShots':              'hd_shots',
    'I_F_rebounds':                     'rebounds',
    # Zone shift starts
    'I_F_dZoneShiftStarts':             'dzone_shift_starts',
    'I_F_oZoneShiftStarts':             'ozone_shift_starts',
    'I_F_neutralZoneShiftStarts':       'nzone_shift_starts',
    'I_F_flyShiftStarts':               'fly_shift_starts',
    # On-ice xGA
    'OnIce_A_xGoals':                   'onice_xga',
    'OnIce_A_scoreVenueAdjustedxGoals': 'onice_xga_adj',
    'OnIce_A_highDangerxGoals':         'onice_xga_hd',
    # On-ice xGF
    'OnIce_F_xGoals':                   'onice_xgf',
    'OnIce_F_scoreVenueAdjustedxGoals': 'onice_xgf_adj',
    # Possession
    'onIce_xGoalsPercentage':           'onice_xg_pct',
    'offIce_xGoalsPercentage':          'office_xg_pct',
    'onIce_corsiPercentage':            'onice_corsi_pct',
    'offIce_corsiPercentage':           'office_corsi_pct',
    # Backchecking
    'xGoalsAgainstAfterShifts':         'xga_after_shifts',
    'xGoalsForAfterShifts':             'xgf_after_shifts',
    # DZ giveaways
    'I_F_dZoneGiveaways':               'dzone_giveaways',
}


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute('DROP TABLE IF EXISTS moneypuck_games')
    conn.execute('''
        CREATE TABLE moneypuck_games (
            player_id               INTEGER NOT NULL,
            game_id                 INTEGER NOT NULL,
            season                  INTEGER NOT NULL,
            team                    TEXT    NOT NULL,
            situation               TEXT    NOT NULL,
            icetime                 REAL,
            -- Individual production
            goals                   REAL,
            primary_assists         REAL,
            secondary_assists       REAL,
            shots_on_goal           REAL,
            shot_attempts           REAL,
            hits                    REAL,
            takeaways               REAL,
            giveaways               REAL,
            pim                     REAL,
            game_score              REAL,
            -- Individual xG
            ixg                     REAL,
            ixg_adj                 REAL,
            ixg_hd                  REAL,
            hd_shots                REAL,
            rebounds                REAL,
            -- Zone shift starts
            dzone_shift_starts      REAL,
            ozone_shift_starts      REAL,
            nzone_shift_starts      REAL,
            fly_shift_starts        REAL,
            -- On-ice xGA
            onice_xga               REAL,
            onice_xga_adj           REAL,
            onice_xga_hd            REAL,
            -- On-ice xGF
            onice_xgf               REAL,
            onice_xgf_adj           REAL,
            -- Possession
            onice_xg_pct            REAL,
            office_xg_pct           REAL,
            onice_corsi_pct         REAL,
            office_corsi_pct        REAL,
            -- Backchecking
            xga_after_shifts        REAL,
            xgf_after_shifts        REAL,
            -- DZ giveaways
            dzone_giveaways         REAL,
            PRIMARY KEY (player_id, game_id, situation)
        )
    ''')
    conn.commit()


def load_moneypuck(force: bool = False) -> None:
    conn = sqlite3.connect(DB_PATH)

    if not force:
        try:
            count = conn.execute('SELECT COUNT(*) FROM moneypuck_games').fetchone()[0]
            if count > 0:
                print(f'  MoneyPuck already loaded ({count:,} rows). Pass force=True to reload.')
                conn.close()
                return
        except Exception:
            pass

    _ensure_table(conn)

    total_rows = 0
    for csv_path in CSV_PATHS:
        if not csv_path.exists():
            print(f'  WARNING: {csv_path} not found, skipping.')
            continue

        print(f'  Loading {csv_path.name}...', flush=True)
        for chunk in pd.read_csv(csv_path, usecols=KEEP_COLS, chunksize=50_000):
            chunk = chunk[chunk['situation'].isin(KEEP_SITUATIONS)]
            if chunk.empty:
                continue

            chunk = chunk.rename(columns=COL_RENAME)
            chunk.to_sql('moneypuck_games', conn, if_exists='append', index=False)
            total_rows += len(chunk)
            print(f'    {total_rows:,} rows loaded...', end='\r', flush=True)

    conn.execute('CREATE INDEX IF NOT EXISTS idx_mp_player_game ON moneypuck_games(player_id, game_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_mp_game ON moneypuck_games(game_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_mp_player ON moneypuck_games(player_id, situation)')
    conn.commit()
    conn.close()
    print(f'\n  Done — {total_rows:,} rows loaded into moneypuck_games.')


def get_player_game(player_id: int, game_id: int, situation: str = 'all') -> dict | None:
    """Look up a single player-game row."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        'SELECT * FROM moneypuck_games WHERE player_id=? AND game_id=? AND situation=?',
        (player_id, game_id, situation)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


if __name__ == '__main__':
    load_moneypuck(force=True)
