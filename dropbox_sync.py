"""
dropbox_sync.py

Downloads the shared Dropbox playoff game log folder, copies any new or updated
xlsx files to the local Manual Game Logs directory, and rebuilds playoff grades
if anything changed.

Usage:
    python3 dropbox_sync.py          # 2025-26 season (default)
    python3 dropbox_sync.py 2024     # 2024-25 season
"""
import hashlib
import io
import shutil
import sys
import zipfile
from pathlib import Path

import requests

DROPBOX_URL = (
    'https://www.dropbox.com/scl/fo/6azd6a52j9muyqmy6lkeu'
    '/APMHEXDrhNze8yjdVdNIES8/Playoff%20Game%20Log'
    '?dl=0&rlkey=2ahheo6zj5nd7bc2d4upvlyxc'
)

LOGS_BASE = Path(__file__).parent / 'Manual Game Logs' / 'Playoffs'


def _season_label(mp_year: int) -> str:
    return f'{mp_year}-{mp_year + 1}'


def _file_hash(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def sync(mp_year: int) -> list[str]:
    """Download ZIP, copy new/changed playoff xlsx files. Returns list of changed filenames."""
    season_dir = LOGS_BASE / _season_label(mp_year)
    season_dir.mkdir(parents=True, exist_ok=True)

    print('Downloading Dropbox folder...', flush=True)
    resp = requests.get(DROPBOX_URL, timeout=60)
    resp.raise_for_status()

    changed = []
    with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
        xlsx_files = [
            n for n in z.namelist()
            if n.endswith('.xlsx')
            and not n.startswith('~')          # skip temp files
            and Path(n).stem.split()[0].startswith('3')  # playoff IDs only
            and 'Game Log' not in n            # skip master log
        ]

        print(f'Found {len(xlsx_files)} playoff xlsx files in Dropbox.', flush=True)

        for name in xlsx_files:
            dest = season_dir / name
            data = z.read(name)
            if dest.exists() and hashlib.md5(dest.read_bytes()).hexdigest() == hashlib.md5(data).hexdigest():
                continue  # unchanged
            dest.write_bytes(data)
            status = 'updated' if dest.exists() else 'new'
            print(f'  {status}: {name}', flush=True)
            changed.append(name)

    return changed


if __name__ == '__main__':
    mp_year = int(sys.argv[1]) if len(sys.argv) > 1 else 2025
    changed = sync(mp_year)

    if not changed:
        print('No new or updated files.')
    else:
        print(f'{len(changed)} file(s) changed — rebuilding grades...')
        from build_playoff_grades import build
        build(mp_year)
        print('Done.')
