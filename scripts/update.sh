#!/usr/bin/env bash
# ==============================================================================
# Bunny Stream Telegram Downloader Bot - Safe Application Update Script
# ==============================================================================
set -euo pipefail

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${PROJECT_DIR}"

echo -e "${CYAN}==========================================================${NC}"
echo -e "${CYAN}  Bunny Stream Downloader - Safe Application Update      ${NC}"
echo -e "${CYAN}==========================================================${NC}"

# Step 1: Automated Safety Backup
echo -e "${YELLOW}[1/6] Performing pre-update safety backup...${NC}"
if [ -f "scripts/backup.sh" ]; then
    bash scripts/backup.sh
else
    echo -e "${YELLOW}  scripts/backup.sh not found, skipping backup.${NC}"
fi

# Step 2: Fetch and Pull Git Updates
echo -e "${YELLOW}[2/6] Checking for upstream repository updates...${NC}"
if [ -d ".git" ]; then
    CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "main")
    echo -e "  Current branch: ${CYAN}${CURRENT_BRANCH}${NC}"

    # Check for uncommitted changes in tracked files
    if ! git diff-index --quiet HEAD -- 2>/dev/null; then
        echo -e "${RED}[WARNING] You have uncommitted local modifications.${NC}"
        echo -e "${RED}Please commit or stash your changes before updating.${NC}"
        exit 1
    fi

    git pull --ff-only origin "${CURRENT_BRANCH}" || {
        echo -e "${RED}[ERROR] Fast-forward update failed. Please inspect git status.${NC}"
        exit 1
    }
    echo -e "${GREEN}  Git pull completed successfully.${NC}"
else
    echo -e "${YELLOW}  [NOTICE] Not a Git repository checkout; skipping git pull.${NC}"
fi

# Step 3: Update Python Dependencies
echo -e "${YELLOW}[3/6] Updating Python dependencies in .venv...${NC}"
if [ -d ".venv" ]; then
    .venv/bin/python -m pip install --upgrade pip --quiet
    .venv/bin/pip install -r requirements.txt --quiet
    echo -e "${GREEN}  Python packages up to date.${NC}"
else
    echo -e "${RED}[ERROR] Virtual environment (.venv) not found. Run scripts/setup_ubuntu.sh first.${NC}"
    exit 1
fi

# Step 4: Verify Database Schema
echo -e "${YELLOW}[4/6] Verifying database schema...${NC}"
.venv/bin/python -c "
import asyncio
from config import config
from app.database.models import init_db
asyncio.run(init_db(config.DATABASE_URL))
"
echo -e "${GREEN}  Database schema initialized and verified.${NC}"

# Step 5: Run Automated Test Suite
echo -e "${YELLOW}[5/6] Running automated test suite before restarting...${NC}"
if .venv/bin/pytest -q --tb=short; then
    echo -e "${GREEN}  All automated tests passed successfully!${NC}"
else
    echo -e "${RED}[CRITICAL ERROR] Test suite failed! Aborting service restart to avoid downtime.${NC}"
    echo -e "${RED}Please inspect test errors and restore from backup if needed.${NC}"
    exit 1
fi

# Step 6: Restart Service if Running
echo -e "${YELLOW}[6/6] Checking systemd service status...${NC}"
if command -v systemctl >/dev/null 2>&1; then
    if systemctl is-active --quiet bunny-bot 2>/dev/null; then
        echo -e "${YELLOW}  Restarting bunny-bot systemd service...${NC}"
        sudo systemctl restart bunny-bot
        echo -e "${GREEN}  Service restarted.${NC}"
    else
        echo -e "${YELLOW}  bunny-bot service is not currently active.${NC}"
    fi
fi

# Run Post-update Health Check
if [ -f "scripts/healthcheck.sh" ]; then
    echo ""
    echo -e "${CYAN}Running post-update health check:${NC}"
    bash scripts/healthcheck.sh || true
fi

echo ""
echo -e "${GREEN}==========================================================${NC}"
echo -e "${GREEN}  Application update completed successfully!             ${NC}"
echo -e "${GREEN}==========================================================${NC}"
echo ""
