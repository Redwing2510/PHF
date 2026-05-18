"""
Refresh NST playoff data for MTL and BUF (force re-download).
Run after game 6 to get updated per-game stats.
"""
import time, shutil
from pathlib import Path
from playwright.sync_api import sync_playwright

FROM_SEASON = '20252026'
THRU_SEASON = '20252026'
STYPE       = '3'
DELAY       = 1.5

DOWNLOADS = [
    ('ind', 'std', 'all'),
    ('ind', 'std', '5on4'),
    ('ind', 'std', '4on5'),
    ('oi',  'oi',  'all'),
]

TEAMS = {
    'MTL': [8475848,8476479,8476875,8476981,8478133,8478851,8480018,8480074,
            8480813,8480865,8481523,8481540,8481593,8481618,8482087,8482737,
            8482775,8482964,8483457,8483515,8484984],
    'BUF': [8474568,8475722,8475842,8477949,8478413,8479359,8479378,8479420,
            8479982,8480064,8480802,8480807,8480839,8480891,8481522,8481524,
            8482097,8482623,8482659,8482671,8482896,8483500,8484145,8484797],
}

BASE = Path(__file__).parent / 'NST Playoff Data' / '20252026'


def nst_url(pid, stdoi, sit):
    return (f'https://www.naturalstattrick.com/playerreport.php'
            f'?fromseason={FROM_SEASON}&thruseason={THRU_SEASON}'
            f'&stype={STYPE}&sit={sit}&stdoi={stdoi}&rate=n&v=g&playerid={pid}')


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx     = browser.new_context(accept_downloads=True)
        page    = ctx.new_page()

        first_pid = TEAMS['MTL'][0]
        print('Opening browser — solve Cloudflare if prompted, then wait...')
        page.goto(nst_url(first_pid, 'std', 'all'))
        try:
            page.wait_for_selector('table', timeout=60000)
            print('Page loaded.\n')
        except Exception:
            print('WARNING: no table detected after 60s — proceeding.')

        for team, pids in TEAMS.items():
            out_dir = BASE / team
            out_dir.mkdir(parents=True, exist_ok=True)
            print(f'\n=== {team} ({len(pids)} players) ===')

            for i, pid in enumerate(pids, 1):
                pid_dir = out_dir / str(pid)
                pid_dir.mkdir(exist_ok=True)
                print(f'  [{i}/{len(pids)}] player {pid}')

                for label, stdoi, sit in DOWNLOADS:
                    dest = pid_dir / f'{stdoi}_{sit}.html'
                    url  = nst_url(pid, stdoi, sit)
                    print(f'    {label}/{sit}...', end=' ', flush=True)
                    try:
                        page.goto(url, wait_until='domcontentloaded', timeout=15000)
                        page.wait_for_load_state('networkidle', timeout=10000)
                        content = page.content()
                        dest.write_text(content, encoding='utf-8')
                        print(f'✓ ({len(content)} bytes)')
                    except Exception as e:
                        print(f'✗ {e}')
                    time.sleep(DELAY)

        browser.close()
    print('\nDone. Run /refresh on Flask to rebuild grades.')


if __name__ == '__main__':
    run()
