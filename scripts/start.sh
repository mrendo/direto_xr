#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# ── Check venv ────────────────────────────────────────────────────────────────
if [[ ! -f "venv/bin/activate" ]]; then
    echo " [ERROR] Virtual environment not found. Run ./scripts/install.sh first."
    exit 1
fi

# shellcheck disable=SC1091
source venv/bin/activate

echo ""
echo " ============================================="
echo "  Direto XR Controller"
echo " ============================================="
echo ""
echo "  Server starting at http://localhost:8000"
echo "  Press Ctrl+C to stop."
echo ""

# ── Open browser after a short delay ─────────────────────────────────────────
(
    sleep 2
    if command -v xdg-open &>/dev/null; then
        xdg-open "http://localhost:8000" &>/dev/null &
    elif command -v open &>/dev/null; then
        open "http://localhost:8000"
    fi
) &

# ── Run server ────────────────────────────────────────────────────────────────
python direto_server.py
