"""
nst_downloader.py

Downloads per-player game log CSVs from Natural Stat Trick for a given
team's playoff skaters. Opens a visible browser so you can solve the
Cloudflare challenge once manually, then automates the rest.

Usage:
    python3 nst_downloader.py

Downloads go to: NST Playoff Data/<TEAM>/
"""
import time
import shutil
from pathlib import Path
from playwright.sync_api import sync_playwright

# ── Config ────────────────────────────────────────────────────────────────────
TEAM       = 'COL'
FROM_SEASON = '20252026'
THRU_SEASON = '20252026'
STYPE       = '3'        # 3 = playoffs
OUT_DIR     = Path(__file__).parent / 'NST Playoff Data' / TEAM

PLAYER_IDS = {
    8477476: 'A. Lehkonen',
    8470613: 'B. Burns',
    8476967: 'B. Kulak',
    8475754: 'B. Nelson',
    8480069: 'C. Makar',
    8478038: 'D. Toews',
    8476455: 'G. Landeskog',
    8482072: 'J. Ahcan',
    8480835: 'J. Drury',
    8481641: 'J. Kiviranta',
    8476312: 'J. Manson',
    8481186: "L. O'Connor",
    8480039: 'M. Necas',
    8483565: 'N. Blankenburg',
    8475172: 'N. Kadri',
    8477492: 'N. MacKinnon',
    8478462: 'N. Roy',
    8480448: 'P. Kelly',
    8479525: 'R. Colton',
    8484258: 'S. Malinski',
    8477501: 'V. Nichushkin',
}

SITUATIONS = {
    'all':  'all',
    '5on4': '5on4',
    '4on5': '4on5',
}

REPORT_TYPES = {
    'ind': 'std',   # individual stats
    'oi':  'oi',    # on-ice stats
}

# Only download these combos (we don't need on-ice for PP/PK)
DOWNLOADS = [
    ('ind', 'all'),
    ('ind', '5on4'),
    ('ind', '4on5'),
    ('oi',  'all'),
]

DELAY = 1.5   # seconds between downloads


def nst_url(player_id: int, stdoi: str, sit: str) -> str:
    return (
        f'https://www.naturalstattrick.com/playerreport.php'
        f'?fromseason={FROM_SEASON}&thruseason={THRU_SEASON}'
        f'&stype={STYPE}&sit={sit}&stdoi={stdoi}&rate=n&v=g'
        f'&playerid={player_id}'
    )


def already_done(player_id: int, report: str, sit: str) -> bool:
    target = OUT_DIR / f'{player_id}_{report}_{sit}.csv'
    return target.exists() and target.stat().st_size > 100


def run():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    total = len(PLAYER_IDS) * len(DOWNLOADS)
    done  = sum(1 for pid in PLAYER_IDS for rep, sit in DOWNLOADS
                if already_done(pid, rep, sit))
    print(f'Starting — {done}/{total} already downloaded.')

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx     = browser.new_context(accept_downloads=True)
        page    = ctx.new_page()

        # ── Warm up: wait for Cloudflare to clear automatically ──────────────
        first_pid = next(iter(PLAYER_IDS))
        first_url = nst_url(first_pid, 'std', 'all')
        print(f'\nOpening browser — waiting up to 60s for Cloudflare to clear...')
        page.goto(first_url)
        # Wait until a table appears on the page (means Cloudflare is past)
        try:
            page.wait_for_selector('table', timeout=60000)
            print('Page loaded successfully.')
        except Exception:
            print('WARNING: Could not detect table after 60s — proceeding anyway.')

        # ── Loop through all players ──────────────────────────────────────────
        for i, (pid, name) in enumerate(PLAYER_IDS.items(), 1):
            print(f'\n[{i}/{len(PLAYER_IDS)}] {name} ({pid})')

            for report, sit in DOWNLOADS:
                if already_done(pid, report, sit):
                    print(f'  {report}/{sit} — already done, skipping')
                    continue

                stdoi = REPORT_TYPES[report]
                url   = nst_url(pid, stdoi, sit)
                print(f'  {report}/{sit} — fetching...', end=' ', flush=True)

                try:
                    page.goto(url, wait_until='domcontentloaded', timeout=15000)
                    page.wait_for_load_state('networkidle', timeout=10000)

                    # Find and click the CSV download link/button
                    with page.expect_download(timeout=10000) as dl_info:
                        # NST download links typically contain 'download' or are <a> tags with CSV href
                        dl_link = page.locator('a[href*=".csv"], a:has-text("Download"), button:has-text("Download CSV")').first
                        dl_link.click()

                    download = dl_info.value
                    dest = OUT_DIR / f'{pid}_{report}_{sit}.csv'
                    download.save_as(dest)
                    print(f'✓ saved ({dest.stat().st_size} bytes)')

                except Exception as e:
                    print(f'✗ failed: {e}')

                time.sleep(DELAY)

        browser.close()

    total_done = sum(1 for pid in PLAYER_IDS for rep, sit in DOWNLOADS
                     if already_done(pid, report, sit))
    print(f'\nDone — {total_done}/{total} files downloaded to {OUT_DIR}')


if __name__ == '__main__':
    run()
