from season import build_season_grades

data = build_season_grades()
players = [x for x in data['players'] if 'carrier' in x['name'].lower()]
if not players:
    print("Not found")
else:
    for p in players:
        print(f"{p['name']}  {p['team']}  {p['position']}  GP={p['gp']}  qualified={p['qualified']}  rank={p['rank']}/{len(data['players'])}")
        print(f"  Overall={p['overall']:.1f} ({p['overall_letter']})  Off={p['off']:.1f}  Dfn={p['dfn']:.1f}  Poss={p['poss']:.1f}  FO={p['fo']}")
        print(f"  G={p['goals']}  A={p['assists']}  PTS={p['points']}  SOG={p['sog']}")
        print(f"  HIT={p['hits']}  BLK={p['blocks']}  GVA={p['gva']}  TKA={p['tka']}  PIM={p['pim']}")
        print(f"  TOI/gm={p['toi_per_game']}  toi_h={p.get('toi_h','?'):.2f}  CF%={p['cf_pct']}  xG%={p['xg_pct']}")
        print()
