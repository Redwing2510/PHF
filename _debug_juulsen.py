from manual_loader import invalidate_cache; invalidate_cache()
from season import build_season_grades

data = build_season_grades()
p = [x for x in data['players'] if 'juulsen' in x['name'].lower()]
if not p:
    print("Not found")
else:
    p = p[0]
    print(f"SEASON: {p['name']}  {p['team']}  {p['position']}  GP={p['gp']}  qualified={p['qualified']}  rank={p['rank']}/{len(data['players'])}")
    print(f"  Overall : {p['overall']:5.1f} ({p['overall_letter']})")
    print(f"  Off={p['off']:5.1f}  Dfn={p['dfn']:5.1f}  Poss={p['poss']:5.1f}  FO={p['fo']}")
    print(f"  G={p['goals']}  A={p['assists']}  PTS={p['points']}  SOG={p['sog']}")
    print(f"  HIT={p['hits']}  BLK={p['blocks']}  GVA={p['gva']}  TKA={p['tka']}  PIM={p['pim']}")
    print(f"  TOI/gm={p['toi_per_game']}  CF%={p['cf_pct']}  xG%={p['xg_pct']}")
    print(f"  MS gp={p['ms_gp']}  ms_overall={p['ms_overall']}  ms_dfn={p['ms_dfn']}  ms_poss={p['ms_poss']}  ms_fc={p['ms_fc']}")

print()
print("PHI players by overall:")
for r in [x for x in data['players'] if x['team']=='PHI']:
    q = 'Q' if r['qualified'] else 'L'
    print(f"  [{q}] {r['name']:25s} {r['position']} GP={r['gp']} {r['overall']:5.1f} ({r['overall_letter']})")
