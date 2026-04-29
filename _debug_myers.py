from season import build_season_grades

data = build_season_grades()
myers = next(p for p in data['players'] if p['name'] == 'Tyler Myers')
print('=== Tyler Myers ===')
print(f"Overall={myers['overall']}  Off={myers['off']}  Dfn={myers['dfn']}  Poss={myers['poss']}")
print(f"GP={myers['gp']}  TOI/gm={myers['toi_per_game']}  CF%={myers['cf_pct']}  xG%={myers['xg_pct']}")
print(f"G={myers['goals']}  A={myers['assists']}  SOG={myers['sog']}  HIT={myers['hits']}  BLK={myers['blocks']}  GVA={myers['gva']}  TKA={myers['tka']}")
print()

d_men = [p for p in data['players'] if p['position'] == 'D' and p['qualified']]
d_men_off  = sorted(d_men, key=lambda x: x['off'],     reverse=True)
d_men_dfn  = sorted(d_men, key=lambda x: x['dfn'],     reverse=True)
d_men_poss = sorted(d_men, key=lambda x: x['poss'],    reverse=True)
d_men_ov   = sorted(d_men, key=lambda x: x['overall'], reverse=True)

print(f"Among qualified D-men ({len(d_men)} total):")
print(f"  Off rank:  {next(i+1 for i,p in enumerate(d_men_off)  if p['name']=='Tyler Myers')} / {len(d_men)}  ({myers['off']})")
print(f"  Dfn rank:  {next(i+1 for i,p in enumerate(d_men_dfn)  if p['name']=='Tyler Myers')} / {len(d_men)}  ({myers['dfn']})")
print(f"  Poss rank: {next(i+1 for i,p in enumerate(d_men_poss) if p['name']=='Tyler Myers')} / {len(d_men)}  ({myers['poss']})")
print(f"  Ovr rank:  {next(i+1 for i,p in enumerate(d_men_ov)   if p['name']=='Tyler Myers')} / {len(d_men)}")
print()
print("Poss bottom 5 qualified D-men (xG%):")
for p in d_men_poss[-5:]:
    print(f"  {p['name']:<22} {p['team']}  xG%={p['xg_pct']}  poss={p['poss']}")
