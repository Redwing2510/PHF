from manual_loader import invalidate_cache, get_microstat_grade
invalidate_cache()
from pipeline import grade_game, process_game

player_stats, all_players, ctx, game_data, play_log = process_game(
    game_id=2025030131, season='20252026', verbose=False)
grades_api = grade_game(player_stats, all_players, game_id=0)
grades_ms  = grade_game(player_stats, all_players, game_id=2025030131)
api_dfn    = {r['name']: r['dfn'] for r in grades_api}

print(f"  {'Name':25s}  hits  blk  tka  api_dfn  ms_def100  blended_dfn")
d_rows = [r for r in grades_ms if r['position'] == 'D']
d_rows.sort(key=lambda r: r['dfn'])
for r in d_rows:
    ms = get_microstat_grade(2025030131, r['name'])
    ms_def = f"{ms.defense_100:5.1f}" if ms else "  N/A"
    print(f"  {r['name']:25s}  {r['hits']:4d}  {r['blocks']:3d}  {r['tka']:3d}"
          f"  {api_dfn.get(r['name'], 0):7.1f}  {ms_def:>9}  {r['dfn']:11.1f}")
