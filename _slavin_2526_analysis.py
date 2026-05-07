"""
Compute Slavin's pure 2025-26 tracking defensive sub-score
vs all 2025-26 tracked defensemen and vs CAR D specifically.
Only scans Manual Game Logs/Regular Season/2025-26/ — no multi-season contamination.
"""
import sys
import re
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent))
from manual_loader import _load_records_from_file, _norm_name
from tracking_grader import compute_tracking_split

SEASON_DIR = Path(__file__).parent / "Manual Game Logs" / "Regular Season" / "2025-26"
MIN_GAMES = 3   # minimum tracked games to include in normalization pool

# Accumulate per player
player_stats = defaultdict(lambda: {
    'games': 0,
    'toi_min': 0.0,
    'dz_exit_pts': 0.0,
    'entry_dfn_pts': 0.0,
    'team': '',
    'position': '',
})

files = sorted(SEASON_DIR.glob("*.xlsx"))
print(f"Scanning {len(files)} files in 2025-26...", flush=True)

for fpath in files:
    m = re.match(r'^(\d+)', fpath.name)
    if not m:
        continue
    file_game_id = int(m.group(1))
    try:
        records = _load_records_from_file(fpath, file_game_id)
    except Exception as e:
        print(f"  SKIP {fpath.name}: {e}")
        continue

    for r in records:
        if r.position != 'D':
            continue
        if r.toi_min <= 0:
            continue
        key = r.name
        _, dz_exit_pts, entry_dfn_pts = compute_tracking_split(r, 'D')
        ps = player_stats[key]
        ps['games'] += 1
        ps['toi_min'] += r.toi_min
        ps['dz_exit_pts'] += dz_exit_pts
        ps['entry_dfn_pts'] += entry_dfn_pts
        ps['team'] = r.team
        ps['position'] = r.position

# Filter to min games
eligible = {k: v for k, v in player_stats.items() if v['games'] >= MIN_GAMES}
print(f"\n{len(eligible)} D with >= {MIN_GAMES} tracked games in 2025-26\n")

# Compute rates per 60 min
def rate_per60(pts, toi_min):
    if toi_min <= 0:
        return 0.0
    return pts / toi_min * 60.0

# Build pools for z-scoring
dz_rates = {k: rate_per60(v['dz_exit_pts'], v['toi_min']) for k, v in eligible.items()}
ed_rates = {k: rate_per60(v['entry_dfn_pts'], v['toi_min']) for k, v in eligible.items()}

import statistics
dz_vals = list(dz_rates.values())
ed_vals = list(ed_rates.values())

dz_mean, dz_std = statistics.mean(dz_vals), statistics.stdev(dz_vals)
ed_mean, ed_std = statistics.mean(ed_vals), statistics.stdev(ed_vals)

def zscore_to_100(z):
    # clamp to -2..+2, then scale to 0-100
    z = max(-2.0, min(2.0, z))
    return round((z + 2.0) / 4.0 * 100.0, 1)

def z(val, mean, std):
    if std < 1e-9:
        return 0.0
    return (val - mean) / std

# Compute defensive score: 40% DZ exits + 30% entry defense (just the tracking part)
# Note: this excludes PK (25%) and blocks (5%) which need other data sources
# so we renormalize to 40/(40+30) = 57% DZ and 43% ED
def tracking_dfn(name):
    dz_z = z(dz_rates[name], dz_mean, dz_std)
    ed_z = z(ed_rates[name], ed_mean, ed_std)
    dz_100 = zscore_to_100(dz_z)
    ed_100 = zscore_to_100(ed_z)
    # Combined: 40/(40+30) DZ + 30/(40+30) ED = 0.571 DZ + 0.429 ED
    combined = 0.571 * dz_100 + 0.429 * ed_100
    return dz_100, ed_100, combined

# --- ALL D rankings ---
scores = []
for name, data in eligible.items():
    dz_100, ed_100, combined = tracking_dfn(name)
    scores.append((name, data['team'], data['games'], data['toi_min'], dz_100, ed_100, combined))

scores.sort(key=lambda x: x[6], reverse=True)

# Print top 30
print("=" * 75)
print(f"{'Rank':<5} {'Name':<28} {'Team':<5} {'GP':>4} {'TOI':>7} {'DZ_100':>7} {'ED_100':>7} {'Comb':>7}")
print("=" * 75)
for rank, (name, team, gp, toi, dz100, ed100, comb) in enumerate(scores[:30], 1):
    marker = " <-- SLAVIN" if 'slavin' in name else ""
    print(f"{rank:<5} {name:<28} {team:<5} {gp:>4} {toi:>7.1f} {dz100:>7.1f} {ed100:>7.1f} {comb:>7.1f}{marker}")

# Find Slavin rank
slavin_rank = next((i+1 for i, s in enumerate(scores) if 'slavin' in s[0]), None)
if slavin_rank and slavin_rank > 30:
    name, team, gp, toi, dz100, ed100, comb = scores[slavin_rank - 1]
    print(f"\n...Slavin at rank #{slavin_rank}:")
    print(f"     {name:<28} {team:<5} {gp:>4} {toi:>7.1f} {dz100:>7.1f} {ed100:>7.1f} {comb:>7.1f}")

# --- CAR D ---
print("\n" + "=" * 75)
print("CAROLINA HURRICANES DEFENSEMEN (2025-26 tracking)")
print("=" * 75)
print(f"{'Rank':<5} {'Name':<28} {'Team':<5} {'GP':>4} {'TOI':>7} {'DZ_100':>7} {'ED_100':>7} {'Comb':>7}")
print("-" * 75)
car_scores = [(rank, name, team, gp, toi, dz100, ed100, comb)
              for rank, (name, team, gp, toi, dz100, ed100, comb) in enumerate(scores, 1)
              if 'CAR' in team.upper()]
for rank, name, team, gp, toi, dz100, ed100, comb in car_scores:
    marker = " <-- SLAVIN" if 'slavin' in name else ""
    print(f"{rank:<5} {name:<28} {team:<5} {gp:>4} {toi:>7.1f} {dz100:>7.1f} {ed100:>7.1f} {comb:>7.1f}{marker}")

if not car_scores:
    print("No CAR D found with >= 3 tracked games.")

print("\nNote: 'Comb' = pure tracking DZ exits + entry defense only")
print("      Does not include PK kills (25%) or blocks (5%) from the full ATZ dfn formula")
