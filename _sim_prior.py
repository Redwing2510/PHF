from season import build_season_grades

data = build_season_grades()
ms = [x for x in data['players'] if x['qualified'] and x.get('ms_gp', 0) > 0]
ms.sort(key=lambda x: x['ms_overall'] or 0, reverse=True)

print('--- Current (PRIOR=45 min) ---')
for i, p in enumerate(ms[:10], 1):
    parts = p['toi_per_game'].split(':')
    total_toi = (int(parts[0]) * 60 + int(parts[1])) * p['gp'] / 60
    w = total_toi / (total_toi + 45)
    print(f"  {i:2d}. {p['name']:<25} TOI={total_toi:.0f}min  w={w:.2f}  ms_ov={p['ms_overall']}")

print()
print('--- Approx at PRIOR=90 min ---')
for i, p in enumerate(ms[:10], 1):
    parts = p['toi_per_game'].split(':')
    total_toi = (int(parts[0]) * 60 + int(parts[1])) * p['gp'] / 60
    w_now = total_toi / (total_toi + 45)
    w_new = total_toi / (total_toi + 90)
    ratio = w_new / w_now if w_now else 0
    off_new = 60 + (p['off'] - 60) * ratio
    dfn_new = 60 + (p['dfn'] - 60) * ratio
    ms_poss = p['ms_poss'] or 60
    ms_fc   = p['ms_fc']   or 60
    ms_ov_new = 0.35 * off_new + 0.25 * dfn_new + 0.25 * ms_poss + 0.15 * ms_fc
    print(f"  {i:2d}. {p['name']:<25} TOI={total_toi:.0f}min  off {p['off']:.1f}->{off_new:.1f}  ms_ov {p['ms_overall']}->{ms_ov_new:.1f}")
