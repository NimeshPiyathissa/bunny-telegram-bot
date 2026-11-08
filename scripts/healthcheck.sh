#!/usr/bin/env bash
# ==============================================================================
# Bunny Stream Telegram Downloader Bot - Health Check Utility
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

HEALTH_STATUS=0

echo -e "${CYAN}==========================================================${NC}"
echo -e "${CYAN}  Bunny Stream Downloader - System Health Check          ${NC}"
echo -e "${CYAN}==========================================================${NC}"

# 1. Process / Service Status
echo -e "${YELLOW}[1/5] Checking Bot Process & Service Status...${NC}"
SERVICE_RUNNING=0

if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files bunny-bot.service >/dev/null 2>&1; then
    if systemctl is-active --quiet bunny-bot; then
        echo -e "${GREEN}  ✓ systemd service 'bunny-bot' is ACTIVE (running).${NC}"
        SERVICE_RUNNING=1
    else
        echo -e "${RED}  ✗ systemd service 'bunny-bot' is INACTIVE or FAILED.${NC}"
    fi
fi

if [ "${SERVICE_RUNNING}" -eq 0 ]; then
    PID=$(pgrep -f "python.*bot\.py" | head -n 1 || true)
    if [ -n "${PID}" ]; then
        echo -e "${GREEN}  ✓ Bot process is running (PID: ${PID}).${NC}"
        SERVICE_RUNNING=1
    else
        echo -e "${YELLOW}  ! No active bot process found.${NC}"
        # Only mark as degraded if not running in Docker or offline inspection
    fi
fi

# 2. Database Accessibility & Integrity
echo -e "${YELLOW}[2/5] Checking Database Accessibility...${NC}"
DB_PATH="${PROJECT_DIR}/data/bot.db"
if [ -f "${DB_PATH}" ]; then
    if command -v sqlite3 >/dev/null 2>&1; then
        DB_CHECK=$(sqlite3 "${DB_PATH}" "PRAGMA quick_check;" 2>&1 || echo "ERROR")
        if [ "${DB_CHECK}" = "ok" ]; then
            echo -e "${GREEN}  ✓ SQLite database integrity verified: ${DB_PATH} (quick_check: OK).${NC}"
        else
            echo -e "${RED}  ✗ SQLite database integrity check failed: ${DB_CHECK}${NC}"
            HEALTH_STATUS=1
        fi
    else
        if [ -r "${DB_PATH}" ] && [ -w "${DB_PATH}" ]; then
            echo -e "${GREEN}  ✓ SQLite database exists and is readable/writable.${NC}"
        else
            echo -e "${RED}  ✗ SQLite database file permissions issue: cannot read/write ${DB_PATH}${NC}"
            HEALTH_STATUS=1
        fi
    fi
else
    echo -e "${YELLOW}  ! Database file ${DB_PATH} does not exist yet (will be initialized on first run).${NC}"
fi

# 3. Disk Space Threshold Check
echo -e "${YELLOW}[3/5] Checking Disk Capacity...${NC}"
DISK_USAGE=$(df -P "${PROJECT_DIR}" | awk 'NR==2 {print $5}' | tr -d '%')
if [ -n "${DISK_USAGE}" ]; then
    if [ "${DISK_USAGE}" -ge 95 ]; then
        echo -e "${RED}  ✗ CRITICAL: Disk usage is at ${DISK_USAGE}%! Video downloads may fail.${NC}"
        HEALTH_STATUS=1
    elif [ "${DISK_USAGE}" -ge 85 ]; then
        echo -e "${YELLOW}  ! WARNING: Disk usage is high (${DISK_USAGE}%). Consider running cleanup.${NC}"
    else
        echo -e "${GREEN}  ✓ Disk capacity normal: ${DISK_USAGE}% utilized.${NC}"
    fi
fi

# 4. FFmpeg Binary Verification
echo -e "${YELLOW}[4/5] Checking FFmpeg Media Engine...${NC}"
if command -v ffmpeg >/dev/null 2>&1; then
    FFMPEG_VER=$(ffmpeg -version 2>&1 | head -n 1)
    echo -e "${GREEN}  ✓ FFmpeg detected: ${FFMPEG_VER}${NC}"
else
    echo -e "${RED}  ✗ FFmpeg is NOT installed or not in PATH! Video remuxing will fail.${NC}"
    HEALTH_STATUS=1
fi

# 5. Log Error Scan
echo -e "${YELLOW}[5/5] Checking Recent Error Logs...${NC}"
LOG_FILE="${PROJECT_DIR}/logs/bot.log"
if [ -f "${LOG_FILE}" ]; then
    ERROR_COUNT=$(tail -n 100 "${LOG_FILE}" | grep -c "CRITICAL\|Traceback" || true)
    if [ "${ERROR_COUNT}" -gt 0 ]; then
        echo -e "${YELLOW}  ! Found ${ERROR_COUNT} critical/traceback error(s) in last 100 log lines.${NC}"
        echo -e "    Inspect logs with: ${CYAN}tail -n 50 ${LOG_FILE}${NC}"
    else
        echo -e "${GREEN}  ✓ No critical errors or unhandled tracebacks found in recent logs.${NC}"
    fi
else
    echo -e "${GREEN}  ✓ No log errors found.${NC}"
fi

echo ""
if [ "${HEALTH_STATUS}" -eq 0 ]; then
    echo -e "${GREEN}==========================================================${NC}"
    echo -e "${GREEN}  Overall Health Status: HEALTHY                         ${NC}"
    echo -e "${GREEN}==========================================================${NC}"
    exit 0
else
    echo -e "${RED}==========================================================${NC}"
    echo -e "${RED}  Overall Health Status: DEGRADED / UNHEALTHY            ${NC}"
    echo -e "${RED}==========================================================${NC}"
    exit 1
fi
