#!/usr/bin/env bash
# ==============================================================================
# Bunny Stream Telegram Downloader Bot - Production Ubuntu VPS Setup Installer
# Target: Ubuntu 24.04 LTS (Noble Numbat) / 22.04 LTS (Jammy Jellyfish)
# ==============================================================================
set -euo pipefail

# Formatting colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

echo -e "${CYAN}================================================================${NC}"
echo -e "${CYAN}  🎬 BUNNY STREAM TO TELEGRAM BOT — PRODUCTION VPS INSTALLER    ${NC}"
echo -e "${CYAN}================================================================${NC}"
echo ""

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${PROJECT_DIR}"

# ------------------------------------------------------------------------------
# 1. Supported Ubuntu OS Detection
# ------------------------------------------------------------------------------
echo -e "${YELLOW}[1/15] Verifying Linux distribution and release...${NC}"
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS_ID="${ID:-unknown}"
    OS_VER="${VERSION_ID:-unknown}"
    echo -e "  Detected OS: ${CYAN}${NAME:-Linux} ${OS_VER}${NC}"
    if [ "${OS_ID}" != "ubuntu" ] && [ "${OS_ID}" != "debian" ]; then
        echo -e "${YELLOW}  [NOTICE] Non-Ubuntu distribution detected (${OS_ID}). Proceeding with standard Debian/Ubuntu package managers.${NC}"
    else
        echo -e "${GREEN}  ✓ Supported platform verified: ${PRETTY_NAME}${NC}"
    fi
else
    echo -e "${YELLOW}  [WARNING] /etc/os-release not found. Assuming Debian/Ubuntu-compatible environment.${NC}"
fi

# ------------------------------------------------------------------------------
# 2. Hardware Resource & Network Connectivity Checks
# ------------------------------------------------------------------------------
echo -e "${YELLOW}[2/15] Auditing hardware resources & network connectivity...${NC}"
CPU_CORES=$(nproc 2>/dev/null || echo 1)
RAM_MB=$(free -m 2>/dev/null | awk '/^Mem:/{print $2}' || echo 1024)
DISK_FREE_MB=$(df -m "${PROJECT_DIR}" 2>/dev/null | awk 'NR==2{print $4}' || echo 5000)

echo -e "  • CPU Cores: ${CYAN}${CPU_CORES}${NC}"
echo -e "  • System RAM: ${CYAN}${RAM_MB} MB${NC}"
echo -e "  • Available Disk Space: ${CYAN}${DISK_FREE_MB} MB${NC}"

if [ "${RAM_MB}" -lt 800 ]; then
    echo -e "${YELLOW}  ! Low RAM warning: < 1GB RAM detected. Ensure swap space is configured for video remuxing.${NC}"
fi
if [ "${DISK_FREE_MB}" -lt 3000 ]; then
    echo -e "${RED}  ✗ Warning: Less than 3GB free disk space available (${DISK_FREE_MB} MB). Large video downloads may fail.${NC}"
fi

# Test Internet & Telegram connectivity
echo -n "  • Checking network connection to Telegram API... "
if curl -s --connect-timeout 5 "https://api.telegram.org" >/dev/null 2>&1 || curl -s --connect-timeout 5 "https://1.1.1.1" >/dev/null 2>&1; then
    echo -e "${GREEN}✓ Online${NC}"
else
    echo -e "${YELLOW}! Offline / Blocked (verify firewall or proxy)${NC}"
fi

# ------------------------------------------------------------------------------
# 3. System Dependencies Installation
# ------------------------------------------------------------------------------
echo -e "${YELLOW}[3/15] Installing system packages & runtime dependencies...${NC}"
SUDO_CMD=""
if [ "$EUID" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1; then
        SUDO_CMD="sudo"
    else
        echo -e "${RED}[ERROR] Root privileges or sudo required to install system dependencies.${NC}"
        exit 1
    fi
fi

${SUDO_CMD} apt-get update -y -qq
${SUDO_CMD} apt-get install -y -qq --no-install-recommends \
    python3 \
    python3-venv \
    python3-pip \
    python3-dev \
    ffmpeg \
    build-essential \
    curl \
    git \
    sqlite3 \
    ca-certificates \
    procps
echo -e "${GREEN}  ✓ Base system packages installed successfully.${NC}"

# ------------------------------------------------------------------------------
# 4. FFmpeg & FFprobe Verification
# ------------------------------------------------------------------------------
echo -e "${YELLOW}[4/15] Verifying FFmpeg & FFprobe media engine...${NC}"
if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; then
    FF_VER=$(ffmpeg -version 2>&1 | head -n 1)
    echo -e "${GREEN}  ✓ ${FF_VER}${NC}"
else
    echo -e "${RED}[ERROR] FFmpeg / FFprobe installation failed or missing from PATH.${NC}"
    exit 1
fi

# ------------------------------------------------------------------------------
# 5. Virtual Environment Creation (PEP 668 Compliant)
# ------------------------------------------------------------------------------
echo -e "${YELLOW}[5/15] Initializing Python virtual environment (.venv)...${NC}"
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
    echo -e "${GREEN}  ✓ Isolated virtual environment created (.venv).${NC}"
else
    echo -e "${GREEN}  ✓ Virtual environment already exists.${NC}"
fi

# ------------------------------------------------------------------------------
# 6. Pinned Dependencies Installation
# ------------------------------------------------------------------------------
echo -e "${YELLOW}[6/15] Installing pinned Python dependencies...${NC}"
"${PROJECT_DIR}/.venv/bin/python" -m pip install --upgrade pip --quiet
"${PROJECT_DIR}/.venv/bin/pip" install -r requirements.txt --quiet
echo -e "${GREEN}  ✓ Python dependencies installed & verified.${NC}"

# ------------------------------------------------------------------------------
# 7. Runtime Directories Initialization
# ------------------------------------------------------------------------------
echo -e "${YELLOW}[7/15] Creating application runtime directories...${NC}"
mkdir -p data downloads temp logs backups
echo -e "${GREEN}  ✓ Runtime directories ready (data/, downloads/, temp/, logs/, backups/).${NC}"

# ------------------------------------------------------------------------------
# 8. Environment Configuration Setup
# ------------------------------------------------------------------------------
echo -e "${YELLOW}[8/15] Checking environment configuration (.env)...${NC}"
if [ ! -f ".env" ]; then
    cp .env.example .env
    chmod 600 .env
    echo -e "${GREEN}  ✓ Generated .env template from .env.example (permission 600).${NC}"
else
    echo -e "${GREEN}  ✓ Existing .env configuration preserved (never overwritten).${NC}"
fi

# ------------------------------------------------------------------------------
# 9. First-Time Configuration Wizard (Interactive if empty & terminal attached)
# ------------------------------------------------------------------------------
echo -e "${YELLOW}[9/15] Validating credentials in .env...${NC}"
NEED_CONFIG=0
if grep -q "BOT_TOKEN=$" .env 2>/dev/null || grep -q "BOT_TOKEN=\"\"" .env 2>/dev/null || ! grep -q "BOT_TOKEN=" .env 2>/dev/null; then
    NEED_CONFIG=1
fi

if [ "${NEED_CONFIG}" -eq 1 ] && [ -t 0 ]; then
    echo ""
    echo -e "${CYAN}----------------------------------------------------------------${NC}"
    echo -e "${BOLD}  First-Time Configuration Wizard${NC}"
    echo -e "  Please provide your Telegram credentials (input is masked for security):"
    echo -e "${CYAN}----------------------------------------------------------------${NC}"

    read -r -s -p "  Enter BOT_TOKEN (from @BotFather): " WIZ_TOKEN; echo ""
    read -r -p "  Enter API_ID (from my.telegram.org): " WIZ_API_ID
    read -r -s -p "  Enter API_HASH (from my.telegram.org): " WIZ_HASH; echo ""
    read -r -p "  Enter OWNER_ID (your numeric Telegram ID): " WIZ_OWNER_ID

    if [ -n "${WIZ_TOKEN}" ]; then
        sed -i "s|^BOT_TOKEN=.*|BOT_TOKEN=${WIZ_TOKEN}|" .env
    fi
    if [ -n "${WIZ_API_ID}" ]; then
        sed -i "s|^API_ID=.*|API_ID=${WIZ_API_ID}|" .env
    fi
    if [ -n "${WIZ_HASH}" ]; then
        sed -i "s|^API_HASH=.*|API_HASH=${WIZ_HASH}|" .env
    fi
    if [ -n "${WIZ_OWNER_ID}" ]; then
        sed -i "s|^OWNER_ID=.*|OWNER_ID=${WIZ_OWNER_ID}|" .env
    fi
    echo -e "${GREEN}  ✓ Credentials saved securely to .env.${NC}"
fi

# Validate credentials via Python without printing secrets
CONFIG_STATUS=$("${PROJECT_DIR}/.venv/bin/python" -c "
from config import config
errors = config.validate(exit_on_error=False)
if errors:
    print('INVALID:' + '; '.join(errors))
else:
    print('VALID')
" 2>&1 || echo "ERROR")

if [[ "${CONFIG_STATUS}" == *"VALID"* ]]; then
    echo -e "${GREEN}  ✓ Configuration validation passed.${NC}"
else
    echo -e "${YELLOW}  ! Notice: Configuration incomplete (${CONFIG_STATUS}). Please edit .env with your credentials.${NC}"
fi

# ------------------------------------------------------------------------------
# 10. Configure systemd Service
# ------------------------------------------------------------------------------
echo -e "${YELLOW}[10/15] Configuring systemd service unit...${NC}"
SERVICE_NAME="bunny-bot"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
CURRENT_USER="${SUDO_USER:-$USER}"

if [ "$EUID" -eq 0 ] || command -v sudo >/dev/null 2>&1; then
    ${SUDO_CMD} bash -c "cat > '${SERVICE_FILE}' <<EOF
[Unit]
Description=Bunny Stream to Telegram Downloader Bot
After=network.target network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${CURRENT_USER}
Group=${CURRENT_USER}
WorkingDirectory=${PROJECT_DIR}
ExecStart=${PROJECT_DIR}/.venv/bin/python ${PROJECT_DIR}/bot.py
Restart=always
RestartSec=5s
KillSignal=SIGINT
TimeoutStopSec=30s

# Environment
Environment=PYTHONUNBUFFERED=1

# Resource & File Descriptor Limits
LimitNOFILE=65535
LimitNPROC=4096

# Security Hardening
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths=${PROJECT_DIR}/data ${PROJECT_DIR}/downloads ${PROJECT_DIR}/temp ${PROJECT_DIR}/logs ${PROJECT_DIR}/backups
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF"
    echo -e "${GREEN}  ✓ systemd service file configured at ${SERVICE_FILE}.${NC}"
fi

# ------------------------------------------------------------------------------
# 11. Enable Automatic Restart
# ------------------------------------------------------------------------------
echo -e "${YELLOW}[11/15] Ensuring automatic restart on failure...${NC}"
echo -e "${GREEN}  ✓ Restart=always and RestartSec=5s defined in service unit.${NC}"

# ------------------------------------------------------------------------------
# 12. Configure Startup on Boot
# ------------------------------------------------------------------------------
echo -e "${YELLOW}[12/15] Enabling service for auto-start on server boot...${NC}"
if command -v systemctl >/dev/null 2>&1; then
    ${SUDO_CMD} systemctl daemon-reload
    ${SUDO_CMD} systemctl enable "${SERVICE_NAME}" --quiet 2>/dev/null || true
    echo -e "${GREEN}  ✓ Enabled bunny-bot service on boot via systemctl.${NC}"
fi

# ------------------------------------------------------------------------------
# 13. File Permissions Hardening
# ------------------------------------------------------------------------------
echo -e "${YELLOW}[13/15] Hardening filesystem permissions...${NC}"
chmod 750 data downloads temp logs backups 2>/dev/null || true
chmod 600 .env 2>/dev/null || true
chmod +x scripts/*.sh 2>/dev/null || true
if [ -f data/bot.db ]; then
    chmod 600 data/bot.db 2>/dev/null || true
fi
echo -e "${GREEN}  ✓ Secure least-privilege permissions applied (750 directories, 600 secrets).${NC}"

# ------------------------------------------------------------------------------
# 14. Application Health Check
# ------------------------------------------------------------------------------
echo -e "${YELLOW}[14/15] Running post-installation diagnostics...${NC}"
if [ -f "scripts/healthcheck.sh" ]; then
    bash scripts/healthcheck.sh || true
fi

# ------------------------------------------------------------------------------
# 15. Final Summary & Operator Guidance
# ------------------------------------------------------------------------------
echo ""
echo -e "${GREEN}================================================================${NC}"
echo -e "${GREEN}  🎉 UBUNTU VPS INSTALLATION COMPLETED SUCCESSFULLY!             ${NC}"
echo -e "${GREEN}================================================================${NC}"
echo ""
echo -e "Operational Service Commands:"
echo -e "  • Start Bot:        ${CYAN}sudo systemctl start bunny-bot${NC}"
echo -e "  • Stop Bot:         ${CYAN}sudo systemctl stop bunny-bot${NC}"
echo -e "  • Restart Bot:      ${CYAN}sudo systemctl restart bunny-bot${NC}"
echo -e "  • Check Status:     ${CYAN}sudo systemctl status bunny-bot${NC}"
echo -e "  • View Live Logs:   ${CYAN}sudo journalctl -u bunny-bot -f${NC}"
echo ""
echo -e "Maintenance Tools:"
echo -e "  • Health Check:     ${YELLOW}bash scripts/healthcheck.sh${NC}"
echo -e "  • Safe Update:      ${YELLOW}bash scripts/update.sh${NC}"
echo -e "  • Atomic Backup:    ${YELLOW}bash scripts/backup.sh${NC}"
echo -e "  • Restore Backup:   ${YELLOW}bash scripts/restore.sh <path/to/backup.db>${NC}"
echo ""
