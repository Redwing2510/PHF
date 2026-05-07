"""
Trial: bumped weights for clears, failed-exit penalty, and denials.
Shows Slavin's new score vs all D and CAR D.
"""
import sys, re, statistics
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent))
from manual_loader import _load_records_from_file, MicrostatRecord

SEASON_DIR = Path(__file__).parent / "Manual Game Logs" / "Regular Season" / "2025-26"
MIN_GAMES  = 3

def compute_split_trial(r: MicrostatRecord):
    """
    Trial v3 weights:
    DZ Exit:  clears 0.38 (up, shutdown D reward), carry_exits 0.20 (down, offensive skill)
              botched -0.55, failed_exits -0.50, pass_exits 0.22, exchanges 0.05
    Entry:    denials 0.80
    """
    dz_exit = 0.0
    dz_exit += r.carry_exits                 *  0.20   # was 0.35 → down (offensive/transitional skill)
    dz_exit += r.pass_exits                  *  0.22
    dz_exit += r.retrievals_leading_to_exits *  0.25
    dz_exit += r.clears                      *  0.38   # was 0.28 → up (shutdown D reward)
    dz_exit += r.exchanges                   *  0.05
    dz_exit += r.failed_exits                * -0.50
    dz_exit += r.botched_retrievals          * -0.55
    dz_exit += r.missed_passes               * -0.15

    entry_d = 0.0
    entry_d += r.denials                     *  0.80
    entry_d += r.carries_chance_against      * -0.45
    entry_d += r.dump_in_chance_against      * -0.20
    entry_d += max(0, (r.dz_retrievals or 0) - (r.retrievals_leading_to_exits or 0)) * 0.12

    return dz_exit, entry_d

# Accumulate
player_stats = defaultdict(lambda: {
    'games': 0, 'toi_min': 0.0,
    'dz_pts': 0.0, 'ed_pts': 0.0, 'team': '',
})

files = sorted(SEASON_DIR.glob("*.xlsx"))
for fpath in files:
    m = re.match(r'^(\d+)', fpath.name)
    if not m: continue
    try:
        records = _load_records_from_file(fpath, int(m.group(1)))
    except: continue
    for r in records:
        if r.position != 'D' or r.toi_min <= 0: continue
        dz, ed = compute_split_trial(r)
        ps = player_stats[r.name]
        ps['games']   += 1
        ps['toi_min'] += r.toi_min
        ps['dz_pts']  += dz
        ps['ed_pts']  += ed
        ps['team']     = r.team

pool = {k: v for k, v in player_stats.items() if v['games'] >= MIN_GAMES}

def r60(pts, toi): return pts / toi * 60.0 if toi > 0 else 0.0
def zs(val, mean, std): return (val - mean) / std if std > 1e-9 else 0.0
def to100(z): return round((max(-2.0, min(2.0, z)) + 2.0) / 4.0 * 100.0, 1)

dz_rates = {k: r60(v['dz_pts'], v['toi_min']) for k, v in pool.items()}
ed_rates = {k: r60(v['ed_pts'], v['toi_min']) for k, v in pool.items()}

dz_mean, dz_std = statistics.mean(dz_rates.values()), statistics.stdev(dz_rates.values())
ed_mean, ed_std = statistics.mean(ed_rates.values()), statistics.stdev(ed_rates.values())

scores = []
for name, data in pool.items():
    dz100 = to100(zs(dz_rates[name], dz_mean, dz_std))
    ed100 = to100(zs(ed_rates[name], ed_mean, ed_std))
    comb  = round(0.571 * dz100 + 0.429 * ed100, 1)
    scores.append((name, data['team'], data['games'], dz100, ed100, comb))

scores.sort(key=lambda x: x[5], reverse=True)

print("TRIAL v3 — Clears 0.38 | CarryExit 0.20 | Botched -0.55 | Failed -0.50 | Denials 0.80")
print("=" * 72)
print(f"{'Rk':<4} {'Name':<28} {'Tm':<4} {'GP':>3} {'DZ':>6} {'ED':>6} {'Comb':>6}")
print("=" * 72)
for rank, (name, team, gp, dz, ed, comb) in enumerate(scores[:35], 1):
    marker = " <<" if 'slavin' in name else ""
    print(f"{rank:<4} {name:<28} {team:<4} {gp:>3} {dz:>6.1f} {ed:>6.1f} {comb:>6.1f}{marker}")

targets = ['jaccob slavin','jalen chatfield','esa lindell','radko gudas',
           'dante fabbro','ivan provorov','travis hamonic','drew helleson']
for t in targets:
    rank = next((i+1 for i,s in enumerate(scores) if t in s[0]), None)
    if rank and rank > 35:
        s = scores[rank-1]
        print(f"{rank:<4} {s[0]:<28} {s[1]:<4} {s[2]:>3} {s[3]:>6.1f} {s[4]:>6.1f} {s[5]:>6.1f}")

print()
print("--- CAROLINA HURRICANES D ---")
print(f"{'Rk':<4} {'Name':<28} {'Tm':<4} {'GP':>3} {'DZ':>6} {'ED':>6} {'Comb':>6}")
print("-" * 72)
for rank, (name, team, gp, dz, ed, comb) in enumerate(scores, 1):
    if 'CAR' not in team.upper(): continue
    marker = " <<" if 'slavin' in name else ""
    print(f"{rank:<4} {name:<28} {team:<4} {gp:>3} {dz:>6.1f} {ed:>6.1f} {comb:>6.1f}{marker}")
