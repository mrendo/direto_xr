#!/usr/bin/env bash
set -euo pipefail

# ── Colours ───────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e " ${GREEN}[OK]${NC}    $*"; }
warn() { echo -e " ${YELLOW}[WARN]${NC}  $*"; }
err()  { echo -e " ${RED}[ERROR]${NC} $*"; }

# ── Move to project root (the directory containing this script's parent) ──────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

echo ""
echo " ============================================="
echo "  Direto XR Controller — Installer"
echo " ============================================="
echo ""

# ── Check Python 3.10+ ────────────────────────────────────────────────────────
PYTHON=""
for candidate in python3.12 python3.11 python3.10 python3 python; do
    if command -v "$candidate" &>/dev/null; then
        ver=$("$candidate" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
        major=${ver%%.*}; minor=${ver##*.}
        if [[ $major -ge 3 && $minor -ge 10 ]]; then
            PYTHON="$candidate"; break
        fi
    fi
done

if [[ -z "$PYTHON" ]]; then
    err "Python 3.10+ not found."
    echo ""
    echo "  macOS:  brew install python@3.12"
    echo "  Ubuntu: sudo apt install python3.12 python3.12-venv"
    echo ""
    exit 1
fi
ok "Found $($PYTHON --version)"

# ── Create venv ───────────────────────────────────────────────────────────────
if [[ ! -d "venv" ]]; then
    echo " [SETUP] Creating virtual environment..."
    "$PYTHON" -m venv venv
    ok "Virtual environment created."
else
    ok "Virtual environment already exists."
fi

# ── Activate + install ────────────────────────────────────────────────────────
# shellcheck disable=SC1091
source venv/bin/activate

echo " [SETUP] Installing dependencies..."
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet
ok "Dependencies installed."

# ── macOS BLE permissions hint ────────────────────────────────────────────────
if [[ "$(uname)" == "Darwin" ]]; then
    echo ""
    warn "macOS: If the app can't find your trainer, grant Bluetooth"
    warn "       permission to Terminal (System Settings → Privacy → Bluetooth)."
fi

# ── Linux BLE permissions hint ────────────────────────────────────────────────
if [[ "$(uname)" == "Linux" ]]; then
    if ! groups | grep -q bluetooth; then
        echo ""
        warn "Linux: Your user may need Bluetooth permissions. Run:"
        warn "       sudo usermod -aG bluetooth \$USER  (then log out and back in)"
    fi
fi

echo ""
echo " ============================================="
echo "  Setup complete!"
echo " ============================================="
echo ""
echo "  To start the controller, run:  ./scripts/start.sh"
echo ""
