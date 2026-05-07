"""
Break down exactly which tracking events drive Slavin's DZ exit and entry defense scores
vs the pool average and top/bottom percentiles.
"""
import sys, re, statistics
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent))
from manual_loader import _load_records_from_file, _norm_name

SEASON_DIR = Path(__file__).parent / "Manual Game Logs" / "Regular Season" / "2025-26"
MIN_GAMES  = 3

def per60(val, toi): return val / toi * 60.0 if toi > 0 else 0.0
def rate(num, den):  return num / den if den > 0 else 0.0

# Accumulate raw totals per D
stats = defaultdict(lambda: {
    'games': 0, 'toi_min': 0.0,
    'carry_exits': 0, 'pass_exits': 0, 'ret_exits': 0,
    'clears': 0, 'exchanges': 0, 'failed_exits': 0,
    'botched': 0, 'missed_passes': 0,
    'denials': 0, 'cca': 0, 'dica': 0, 'dz_ret': 0,
    'team': '',
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
        s = stats[r.name]
        s['games']       += 1
        s['toi_min']     += r.toi_min
        s['carry_exits'] += r.carry_exits
        s['pass_exits']  += r.pass_exits
        s['ret_exits']   += r.retrievals_leading_to_exits
        s['clears']      += r.clears
        s['exchanges']   += r.exchanges
        s['failed_exits']+= r.failed_exits
        s['botched']     += r.botched_retrievals
        s['missed_passes']+= r.missed_passes
        s['denials']     += r.denials
        s['cca']         += r.carries_chance_against
        s['dica']        += r.dump_in_chance_against
        s['dz_ret']      += r.dz_retrievals
        s['team']         = r.team

pool = {k: v for k, v in stats.items() if v['games'] >= MIN_GAMES}

# Build per-60 vectors for each component
def p60_vec(field):
    return {k: per60(v[field], v['toi_min']) for k, v in pool.items()}

carry_exit_p60 = p60_vec('carry_exits')
pass_exit_p60  = p60_vec('pass_exits')
ret_exit_p60   = p60_vec('ret_exits')
clears_p60     = p60_vec('clears')
exch_p60       = p60_vec('exchanges')
fail_p60       = p60_vec('failed_exits')
botch_p60      = p60_vec('botched')
miss_p60       = p60_vec('missed_passes')
denial_p60     = p60_vec('denials')
cca_p60        = p60_vec('cca')
dica_p60       = p60_vec('dica')
dzret_p60      = {k: per60(max(0, v['dz_ret'] - v['ret_exits']), v['toi_min']) for k, v in pool.items()}

def pool_stats(d):
    vals = list(d.values())
    return statistics.mean(vals), statistics.stdev(vals) if len(vals)>1 else 0.0

def pct_rank(val, vals):
    return sum(1 for v in vals if v <= val) / len(vals) * 100

def show_component(label, val_map, weight, positive=True):
    vals  = list(val_map.values())
    mean, std = pool_stats(val_map)
    slavin_val = val_map.get('jaccob slavin', 0.0)
    pct = pct_rank(slavin_val, vals)
    if not positive: pct = 100 - pct
    contrib = weight * slavin_val
    print(f"  {label:<32} {slavin_val:>6.2f}/60   avg={mean:>5.2f}   pct={pct:>4.0f}th   wt={weight}")

print("=" * 70)
print("SLAVIN — DZ EXIT COMPONENT BREAKDOWN")
print("=" * 70)
show_component("Carry exits",          carry_exit_p60, 0.35)
show_component("Pass exits",           pass_exit_p60,  0.28)
show_component("Retrievals→exits",     ret_exit_p60,   0.25)
show_component("Clears",               clears_p60,     0.10)
show_component("Exchanges",            exch_p60,       0.08)
show_component("Failed exits (neg)",   fail_p60,       0.20, positive=False)
show_component("Botched ret. (neg)",   botch_p60,      0.40, positive=False)
show_component("Missed passes (neg)",  miss_p60,       0.15, positive=False)

# Raw DZ exit score for Slavin
s = pool.get('jaccob slavin', {})
t = s['toi_min']
dz_raw = (
    per60(s['carry_exits'], t) * 0.35 +
    per60(s['pass_exits'],  t) * 0.28 +
    per60(s['ret_exits'],   t) * 0.25 +
    per60(s['clears'],      t) * 0.10 +
    per60(s['exchanges'],   t) * 0.08 -
    per60(s['failed_exits'],t) * 0.20 -
    per60(s['botched'],     t) * 0.40 -
    per60(s['missed_passes'],t)* 0.15
)
print(f"\n  Raw DZ exit pts/60: {dz_raw:.3f}")

print()
print("=" * 70)
print("SLAVIN — ENTRY DEFENSE COMPONENT BREAKDOWN")
print("=" * 70)
show_component("Denials",              denial_p60,  0.45)
show_component("Extra DZ retrievals",  dzret_p60,   0.12)
show_component("Carries→chance ag (neg)", cca_p60,  0.45, positive=False)
show_component("Dump-in chance ag (neg)", dica_p60, 0.20, positive=False)

ed_raw = (
    per60(s['denials'], t) * 0.45 +
    per60(max(0, s['dz_ret'] - s['ret_exits']), t) * 0.12 -
    per60(s['cca'],   t) * 0.45 -
    per60(s['dica'],  t) * 0.20
)
print(f"\n  Raw entry dfn pts/60: {ed_raw:.3f}")

print()
print("=" * 70)
print("SLAVIN RAW TOTALS (10 games, 165 min)")
print("=" * 70)
fields = ['carry_exits','pass_exits','ret_exits','clears','failed_exits',
          'botched','denials','cca','dica','dz_ret']
for f in fields:
    print(f"  {f:<30} {s[f]:>4}   ({per60(s[f],t):.2f}/60)")
