#!/usr/bin/env bash
# ==============================================================================
# Bunny Stream Telegram Downloader Bot - systemd Service Installer
# ==============================================================================
set -euo pipefail

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

if [ "$EUID" -ne 0 ]; then
  echo -e "${RED}[ERROR] Please run as root (sudo bash scripts/install_service.sh)${NC}"
  exit 1
fi

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="bunny-bot"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
CURRENT_USER="${SUDO_USER:-$USER}"

echo -e "${CYAN}==========================================================${NC}"
echo -e "${CYAN}  Bunny Stream Downloader - systemd Service Installer    ${NC}"
echo -e "${CYAN}==========================================================${NC}"
echo -e "${YELLOW}Configuring service for user: ${CURRENT_USER}${NC}"
echo -e "${YELLOW}Project directory:           ${PROJECT_DIR}${NC}"

# Ensure runtime directories exist with proper ownership
mkdir -p "${PROJECT_DIR}/data" "${PROJECT_DIR}/downloads" "${PROJECT_DIR}/temp" "${PROJECT_DIR}/logs" "${PROJECT_DIR}/backups"
chown -R "${CURRENT_USER}:${CURRENT_USER}" "${PROJECT_DIR}/data" "${PROJECT_DIR}/downloads" "${PROJECT_DIR}/temp" "${PROJECT_DIR}/logs" "${PROJECT_DIR}/backups"
chmod 750 "${PROJECT_DIR}/data" "${PROJECT_DIR}/downloads" "${PROJECT_DIR}/temp" "${PROJECT_DIR}/logs" "${PROJECT_DIR}/backups"

cat > "${SERVICE_FILE}" <<EOF
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
EOF

# Reload daemon and enable service
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"

echo ""
echo -e "${GREEN}Service unit successfully created at: ${SERVICE_FILE}${NC}"
echo -e "${GREEN}Service enabled for automatic startup on system boot.${NC}"
echo ""
echo -e "Service Control Commands:"
echo -e "  Start service:   ${YELLOW}sudo systemctl start ${SERVICE_NAME}${NC}"
echo -e "  Stop service:    ${YELLOW}sudo systemctl stop ${SERVICE_NAME}${NC}"
echo -e "  Restart service: ${YELLOW}sudo systemctl restart ${SERVICE_NAME}${NC}"
echo -e "  Check status:    ${YELLOW}sudo systemctl status ${SERVICE_NAME}${NC}"
echo -e "  View live logs:  ${YELLOW}sudo journalctl -u ${SERVICE_NAME} -f${NC}"
echo ""
