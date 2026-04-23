#!/usr/bin/env bash
# ============================================================
# TudouClaw (土豆爪) — One-click Install Script
# Usage:  bash scripts/install.sh [--full]
#
#   Default:  Install core dependencies only
#   --full:   Install all optional dependencies + playwright
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  TudouClaw (土豆爪) Installer${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""

# Check Python version
PYTHON_CMD=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        version=$($cmd -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
        major=$($cmd -c "import sys; print(sys.version_info.major)" 2>/dev/null)
        minor=$($cmd -c "import sys; print(sys.version_info.minor)" 2>/dev/null)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then
            PYTHON_CMD="$cmd"
            echo -e "${GREEN}✓${NC} Found Python $version ($cmd)"
            break
        fi
    fi
done

if [ -z "$PYTHON_CMD" ]; then
    echo -e "${RED}✗ Python >= 3.10 is required but not found.${NC}"
    echo "  Please install Python 3.10+ first."
    exit 1
fi

# Parse args
FULL_INSTALL=false
for arg in "$@"; do
    case "$arg" in
        --full) FULL_INSTALL=true ;;
    esac
done

cd "$PROJECT_DIR"

# Install core dependencies
echo ""
echo -e "${YELLOW}Installing core dependencies...${NC}"
$PYTHON_CMD -m pip install --upgrade pip
$PYTHON_CMD -m pip install requests>=2.31

if [ "$FULL_INSTALL" = true ]; then
    echo ""
    echo -e "${YELLOW}Installing optional dependencies (--full mode)...${NC}"

    # Image processing
    $PYTHON_CMD -m pip install Pillow>=10.0

    # Document processing
    $PYTHON_CMD -m pip install pymupdf>=1.23 openpyxl>=3.1 python-docx>=1.0

    # Web enhancement
    $PYTHON_CMD -m pip install beautifulsoup4>=4.12 lxml>=5.0

    # Stock market data (akshare-stock skill)
    # akshare: Chinese A-share / 港股 / 美股 data
    # matplotlib: chart rendering in skill's plot_* helpers
    $PYTHON_CMD -m pip install akshare>=1.15 matplotlib>=3.7 tabulate>=0.9

    # Skill converter (skill-converter skill)
    # readability-lxml + html2text: clean HTML → markdown extraction
    $PYTHON_CMD -m pip install readability-lxml>=0.8 html2text>=2024.0 lxml_html_clean>=0.4

    # Screenshot (Playwright)
    echo ""
    echo -e "${YELLOW}Installing Playwright + Chromium for screenshots...${NC}"
    $PYTHON_CMD -m pip install playwright>=1.40
    $PYTHON_CMD -m playwright install chromium

    echo ""
    echo -e "${GREEN}✓ Full installation complete!${NC}"
else
    echo ""
    echo -e "${GREEN}✓ Core installation complete!${NC}"
    echo ""
    echo "  To install optional dependencies, run:"
    echo "    bash scripts/install.sh --full"
fi

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "  Start the portal:"
echo -e "    cd $PROJECT_DIR"
echo -e "    $PYTHON_CMD -m app portal --port 9090 --secret mykey123"
echo -e "${GREEN}========================================${NC}"
