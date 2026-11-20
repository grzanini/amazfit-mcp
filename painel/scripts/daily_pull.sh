#!/bin/bash
# Coleta diária do relógio (nuvem Zepp) + engine v3. Chamado pelo launchd (com.example.amazfit-daily).
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT" || exit 78
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
echo "=== $(date '+%Y-%m-%d %H:%M:%S') início ==="
uv run --directory "$ROOT" python painel/scripts/daily_pull.py --days 45 2>&1 | grep -v 'INFO\|Processing request'
rc=${PIPESTATUS[0]}
echo "=== fim rc=$rc ==="
exit $rc
