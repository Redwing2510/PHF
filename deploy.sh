#!/bin/bash
set -e

SERVER="root@167.172.133.38"
REMOTE="/opt/phf"
WITH_LOGS=false

for arg in "$@"; do
  [[ "$arg" == "--with-logs" ]] && WITH_LOGS=true
done

EXCLUDES=(
  --exclude 'venv/'
  --exclude '__pycache__/'
  --exclude '*.pyc'
  --exclude 'cache.db'
  --exclude 'season_cache/'
  --exclude '.git/'
  --exclude 'deploy.sh'
)
if ! $WITH_LOGS; then
  EXCLUDES+=(--exclude 'Manual Game Logs/' --exclude 'NST Playoff Data/')
fi

echo "Syncing files to server..."
rsync -av "${EXCLUDES[@]}" /Users/nicklamanna/Documents/PHF/ "$SERVER:$REMOTE/"

if $WITH_LOGS; then
  echo "Syncing cache.db..."
  rsync -av /Users/nicklamanna/Documents/PHF/cache.db "$SERVER:$REMOTE/cache.db"
  echo "Syncing microstat pkl cache..."
  rsync -av /Users/nicklamanna/Documents/PHF/.ms_grades_cache.pkl "$SERVER:$REMOTE/.ms_grades_cache.pkl"
  echo "Clearing server season cache (logs updated)..."
  ssh "$SERVER" "rm -f /opt/phf/season_cache/*.json"
fi

echo "Restarting Flask app..."
ssh "$SERVER" "systemctl restart phf"

echo "Done. Site is live."
