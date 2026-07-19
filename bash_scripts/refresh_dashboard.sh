#!/bin/bash
# Hourly IPO dashboard refresh — runs locally, pushes updated HTML to GitHub.
# Scheduled via launchd: ~/Library/LaunchAgents/com.kuldeepkr16.ipo-refresh.plist

set -euo pipefail

REPO="/Users/kuldeepkumar/Documents/projects/ipo_analysis"
VENV="$REPO/.venv/bin/python3"
SCRIPT="$REPO/python_files/ipo_tracker.py"
HTML="$REPO/docs/index.html"
LOG="$REPO/bash_scripts/refresh.log"

cd "$REPO"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting refresh..." >> "$LOG"

# Generate dashboard HTML (exits 0 even if data sources are temporarily unavailable)
"$VENV" "$SCRIPT" --output-html "$HTML" --status all >> "$LOG" 2>&1

# Push only if the HTML actually changed
if git diff --quiet HEAD -- docs/index.html; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] No changes to push." >> "$LOG"
else
    git add docs/index.html
    git commit -m "chore: update IPO data [skip ci]"
    git push origin main
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Pushed updated dashboard." >> "$LOG"
fi

# Keep log trimmed to last 500 lines
tail -500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
