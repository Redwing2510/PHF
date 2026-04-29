from manual_loader import invalidate_cache, _load_all_records, _compute_grades, _load_records_from_file
import pathlib, statistics

invalidate_cache()
all_recs = _load_all_records()
grades_all = _compute_grades(all_recs)

# Distribution of entry_defense_100 for D across all games
d_grades = [(k[1], v.defense_100, v.entry_defense) for k, v in grades_all.items() if v.position == 'D']
vals = [x[1] for x in d_grades]
print(f"D entry_defense_100 across all {len(d_grades)} D player-games:")
print(f"  mean={statistics.mean(vals):.1f}  median={statistics.median(vals):.1f}  stdev={statistics.stdev(vals):.1f}  min={min(vals):.1f}  max={max(vals):.1f}")

# Game 30131 D players raw data
f = pathlib.Path('Manual Game Logs/30131 CAR  vs. OTT.xlsx')
recs_131 = _load_records_from_file(f, 30131)
print()
print("All D in game 30131 (MS entry defense):")
for k, v in sorted(grades_all.items(), key=lambda x: x[1].defense_100, reverse=True):
    if k[0] == 30131 and v.position == 'D':
        rec = next((r for r in recs_131 if r.name == k[1]), None)
        tgts = rec.targets if rec else '?'
        denials = rec.denials if rec else '?'
        cca = rec.carries_chance_against if rec else '?'
        print(f"  {k[1]:25s}  def_100={v.defense_100:5.1f}  z={v.entry_defense:+.2f}  targets={tgts}  denials={denials}  cca={cca}")

# API dfn scores before blending
from pipeline import process_game, grade_game
player_stats, all_players, ctx, game_data, play_log = process_game(game_id=2025030131, season='20252026', verbose=False)
grades_no_ms = grade_game(player_stats, all_players, game_id=0)
print()
print("All D in game 30131 (API dfn only, no MS):")
for r in grades_no_ms:
    if r['position'] == 'D' and r['team'] in ('CAR', 'OTT'):
        print(f"  {r['name']:25s} {r['team']}  dfn_api={r['dfn']:5.1f}  blocks={r['blocks']}  tka={r['tka']}  hits={r['hits']}")
