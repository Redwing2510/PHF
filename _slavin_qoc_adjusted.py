"""
Slavin QoC-adjusted defensive tracking sub-score.

QoC = weighted average opponent forward ixg_adj/60, using matchup_time
      to weight how much time this D spent against each forward.

Then apply a QoC adjustment to the raw tracking dfn score.
"""
import sys, re, statistics, json
from pathlib import Path
from collections import defaultdict
import sqlite3

sys.path.insert(0, str(Path(__file__).parent))
from manual_loader import _load_records_from_file, _norm_name
from tracking_grader import compute_tracking_split

DB_PATH    = Path(__file__).parent / "cache.db"
SEASON_DIR = Path(__file__).parent / "Manual Game Logs" / "Regular Season" / "2025-26"
MIN_GAMES  = 3

# --------------------------------------------------------------------------
# Step 1: Load player positions + name→ID map from games.all_players
# --------------------------------------------------------------------------
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

positions  = {}   # player_id -> 'F' or 'D'
name_to_pid = {}  # normalized name -> player_id

for row in conn.execute("SELECT all_players FROM games"):
    players = json.loads(row["all_players"])
    for pid_str, info in players.items():
        pid  = info["player_id"]
        pos  = info.get("position", "")
        name = info.get("name", "").strip().lower()
        positions[pid]  = "D" if pos == "D" else "F"
        if name:
            name_to_pid[name] = pid

print(f"Loaded positions for {len(positions):,} players, name map: {len(name_to_pid):,}")

# --------------------------------------------------------------------------
# Step 2: Get 2025-26 game IDs that exist in matchup_time
# --------------------------------------------------------------------------
game_ids_2526 = set()
for row in conn.execute(
    "SELECT DISTINCT game_id FROM matchup_time WHERE game_id BETWEEN 2025020001 AND 2025021312"
):
    game_ids_2526.add(row[0])

print(f"2025-26 games in matchup_time: {len(game_ids_2526)}")

# --------------------------------------------------------------------------
# Step 3: Load per-game ixg_adj and icetime for all players (situation='all')
#         Used to estimate opponent offensive rate
# --------------------------------------------------------------------------
# mp_lookup[(player_id, game_id)] = (ixg_adj, icetime_seconds)
mp_lookup = {}
for row in conn.execute(
    "SELECT player_id, game_id, ixg_adj, icetime FROM moneypuck_games "
    "WHERE situation='all' AND season=2025 AND game_id BETWEEN 2025020001 AND 2025021312"
):
    mp_lookup[(row[0], row[1])] = (row[2] or 0.0, row[3] or 0.0)

print(f"MP per-game rows loaded: {len(mp_lookup):,}")

# --------------------------------------------------------------------------
# Step 4: Load tracking data from 2025-26 xlsx files (same as before)
# --------------------------------------------------------------------------
player_tracking = defaultdict(lambda: {
    'games': 0, 'toi_min': 0.0,
    'dz_exit_pts': 0.0, 'entry_dfn_pts': 0.0,
    'team': '', 'player_id': None,
})

# name_to_pid already built above from games.all_players

files = sorted(SEASON_DIR.glob("*.xlsx"))
print(f"\nScanning {len(files)} tracking files...", flush=True)

for fpath in files:
    m = re.match(r'^(\d+)', fpath.name)
    if not m:
        continue
    file_game_id = int(m.group(1))
    try:
        records = _load_records_from_file(fpath, file_game_id)
    except Exception as e:
        continue

    for r in records:
        if r.position != 'D' or r.toi_min <= 0:
            continue
        key = r.name
        _, dz_exit_pts, entry_dfn_pts = compute_tracking_split(r, 'D')
        ps = player_tracking[key]
        ps['games'] += 1
        ps['toi_min'] += r.toi_min
        ps['dz_exit_pts'] += dz_exit_pts
        ps['entry_dfn_pts'] += entry_dfn_pts
        ps['team'] = r.team
        if ps['player_id'] is None:
            ps['player_id'] = name_to_pid.get(key)

eligible = {k: v for k, v in player_tracking.items() if v['games'] >= MIN_GAMES}
print(f"{len(eligible)} D with >= {MIN_GAMES} tracked 2025-26 games")

# --------------------------------------------------------------------------
# Step 5: Compute QoC for each eligible D
#   QoC = sum(opp_ixg_adj/60 * matchup_sec) / sum(matchup_sec)
#   i.e. the avg ixg_adj/60 of the forwards this D faced, weighted by TOI against
# --------------------------------------------------------------------------
qoc_map = {}  # name -> qoc_ixg_adj_per60

for name, data in eligible.items():
    pid = data['player_id']
    if pid is None:
        continue

    total_weighted_rate = 0.0
    total_matchup_sec   = 0.0

    for row in conn.execute(
        "SELECT opponent_id, game_id, seconds FROM matchup_time "
        "WHERE player_id=? AND game_id BETWEEN 2025020001 AND 2025021312",
        (pid,)
    ):
        opp_id  = row[0]
        game_id = row[1]
        sec     = row[2]

        # Only count forwards as opponents
        if positions.get(opp_id) != 'F':
            continue
        if sec <= 0:
            continue

        opp_key = (opp_id, game_id)
        if opp_key not in mp_lookup:
            continue

        opp_ixg_adj, opp_icetime = mp_lookup[opp_key]
        if opp_icetime <= 0:
            continue

        # Rate: how much xG this forward generates per second of ice time
        opp_rate_per_sec = opp_ixg_adj / opp_icetime  # ixg_adj per second

        total_weighted_rate += opp_rate_per_sec * sec
        total_matchup_sec   += sec

    if total_matchup_sec > 0:
        # Convert to per-60: (total weighted ixg_adj / total matchup seconds) * 3600
        qoc_map[name] = (total_weighted_rate / total_matchup_sec) * 3600.0

print(f"QoC computed for {len(qoc_map)} D")
conn.close()

# --------------------------------------------------------------------------
# Step 6: Normalize raw tracking scores and QoC, then blend
# --------------------------------------------------------------------------
def rate_per60(pts, toi_min):
    return pts / toi_min * 60.0 if toi_min > 0 else 0.0

def zscore_to_100(z):
    z = max(-2.0, min(2.0, z))
    return round((z + 2.0) / 4.0 * 100.0, 1)

def zs(val, mean, std):
    return (val - mean) / std if std > 1e-9 else 0.0

# Build pools — only players who have QoC
pool = {k: v for k, v in eligible.items() if k in qoc_map}
print(f"Players in final pool (tracking + QoC): {len(pool)}")

dz_rates = {k: rate_per60(v['dz_exit_pts'], v['toi_min']) for k, v in pool.items()}
ed_rates = {k: rate_per60(v['entry_dfn_pts'], v['toi_min']) for k, v in pool.items()}
qoc_vals = {k: qoc_map[k] for k in pool}

dz_mean, dz_std = statistics.mean(dz_rates.values()), statistics.stdev(dz_rates.values())
ed_mean, ed_std = statistics.mean(ed_rates.values()), statistics.stdev(ed_rates.values())
qoc_mean, qoc_std = statistics.mean(qoc_vals.values()), statistics.stdev(qoc_vals.values())

def base_dfn(name):
    dz_100 = zscore_to_100(zs(dz_rates[name], dz_mean, dz_std))
    ed_100 = zscore_to_100(zs(ed_rates[name], ed_mean, ed_std))
    return dz_100, ed_100, round(0.571 * dz_100 + 0.429 * ed_100, 1)

def adjusted_dfn(name):
    dz_100, ed_100, base = base_dfn(name)
    qoc_z = zs(qoc_vals[name], qoc_mean, qoc_std)
    # Each +1 SD of QoC adds ~8 points to the combined score (empirically reasonable)
    adj = round(base + qoc_z * 8.0, 1)
    adj = round(max(0.0, min(100.0, adj)), 1)
    return dz_100, ed_100, base, qoc_vals[name], round(qoc_z, 2), adj

# --------------------------------------------------------------------------
# Step 7: Output
# --------------------------------------------------------------------------
scores = []
for name, data in pool.items():
    dz_100, ed_100, base, qoc, qoc_z, adj = adjusted_dfn(name)
    scores.append((name, data['team'], data['games'], data['toi_min'],
                   dz_100, ed_100, base, qoc, qoc_z, adj))

scores.sort(key=lambda x: x[9], reverse=True)

print("\n" + "=" * 95)
print(f"{'Rk':<4} {'Name':<28} {'Tm':<4} {'GP':>3} {'DZ':>6} {'ED':>6} {'Base':>6} {'QoC/60':>7} {'QoC_z':>6} {'Adj':>6}")
print("=" * 95)
for rank, (name, team, gp, toi, dz100, ed100, base, qoc, qoc_z, adj) in enumerate(scores[:35], 1):
    marker = " <<" if 'slavin' in name else ""
    print(f"{rank:<4} {name:<28} {team:<4} {gp:>3} {dz100:>6.1f} {ed100:>6.1f} {base:>6.1f} {qoc:>7.3f} {qoc_z:>+6.2f} {adj:>6.1f}{marker}")

slavin_rank = next((i+1 for i, s in enumerate(scores) if 'slavin' in s[0]), None)
if slavin_rank and slavin_rank > 35:
    s = scores[slavin_rank - 1]
    name, team, gp, toi, dz100, ed100, base, qoc, qoc_z, adj = s
    print(f"\n...Slavin at rank #{slavin_rank}:")
    print(f"{slavin_rank:<4} {name:<28} {team:<4} {gp:>3} {dz100:>6.1f} {ed100:>6.1f} {base:>6.1f} {qoc:>7.3f} {qoc_z:>+6.2f} {adj:>6.1f}")

print("\n--- CAROLINA HURRICANES D ---")
print(f"{'Rk':<4} {'Name':<28} {'Tm':<4} {'GP':>3} {'DZ':>6} {'ED':>6} {'Base':>6} {'QoC/60':>7} {'QoC_z':>6} {'Adj':>6}")
print("-" * 95)
car = [(i+1, *s) for i, s in enumerate(scores) if 'CAR' in s[1].upper()]
for rank, name, team, gp, toi, dz100, ed100, base, qoc, qoc_z, adj in car:
    marker = " <<" if 'slavin' in name else ""
    print(f"{rank:<4} {name:<28} {team:<4} {gp:>3} {dz100:>6.1f} {ed100:>6.1f} {base:>6.1f} {qoc:>7.3f} {qoc_z:>+6.2f} {adj:>6.1f}{marker}")

print()
print("QoC/60 = weighted avg ixg_adj/60 of opposing forwards (higher = tougher opponents)")
print("Adj    = Base + QoC_z × 8.0  (each +1 SD of harder opponents = +8 pts)")
