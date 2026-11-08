#!/usr/bin/env bash
# ==============================================================================
# Bunny Stream Telegram Downloader Bot - Safe Database Restore Script
# ==============================================================================
set -euo pipefail

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB_DEST="${PROJECT_DIR}/data/bot.db"

echo -e "${CYAN}==========================================================${NC}"
echo -e "${CYAN}  Bunny Stream Downloader - Safe Database Restore        ${NC}"
echo -e "${CYAN}==========================================================${NC}"

if [ $# -lt 1 ]; then
    echo -e "${RED}[ERROR] Missing backup file argument.${NC}"
    echo -e "Usage:"
    echo -e "  bash scripts/restore.sh <path-to-backup.db>"
    echo ""
    echo -e "Available backups in ${PROJECT_DIR}/backups:"
    ls -lht "${PROJECT_DIR}/backups"/bot_db_*.db 2>/dev/null || echo "  (No backups found)"
    exit 1
fi

BACKUP_FILE="$1"

if [ ! -f "${BACKUP_FILE}" ]; then
    echo -e "${RED}[ERROR] Backup file not found: ${BACKUP_FILE}${NC}"
    exit 1
fi

if [ ! -s "${BACKUP_FILE}" ]; then
    echo -e "${RED}[ERROR] Backup file is empty (0 bytes): ${BACKUP_FILE}${NC}"
    exit 1
fi

# 1. Pre-restore Integrity Check
echo -e "${YELLOW}[1/4] Verifying backup archive integrity...${NC}"
if command -v sqlite3 >/dev/null 2>&1; then
    INTEGRITY_OUTPUT=$(sqlite3 "${BACKUP_FILE}" "PRAGMA integrity_check;" 2>&1)
    if [ "${INTEGRITY_OUTPUT}" != "ok" ]; then
        echo -e "${RED}[CRITICAL ERROR] Backup archive failed integrity check: ${INTEGRITY_OUTPUT}${NC}"
        echo -e "${RED}Restore aborted to prevent database corruption.${NC}"
        exit 1
    fi
    echo -e "${GREEN}  Backup archive integrity verified: OK.${NC}"
else
    echo -e "${YELLOW}  [NOTICE] sqlite3 tool not found; skipping binary PRAGMA check.${NC}"
fi

# 2. Check if service is active and stop temporarily
WAS_RUNNING=0
if command -v systemctl >/dev/null 2>&1; then
    if systemctl is-active --quiet bunny-bot 2>/dev/null; then
        echo -e "${YELLOW}[2/4] Stopping bunny-bot service to prevent lock contention...${NC}"
        sudo systemctl stop bunny-bot
        WAS_RUNNING=1
        echo -e "${GREEN}  Service stopped.${NC}"
    else
        echo -e "${GREEN}[2/4] bunny-bot service is not currently active.${NC}"
    fi
else
    echo -e "${GREEN}[2/4] Not running under systemd.${NC}"
fi

# 3. Create pre-restore safety snapshot of current database
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
if [ -f "${DB_DEST}" ]; then
    SNAPSHOT="${DB_DEST}.pre_restore_${TIMESTAMP}"
    echo -e "${YELLOW}[3/4] Creating safety snapshot of existing active database...${NC}"
    cp -p "${DB_DEST}" "${SNAPSHOT}"
    if [ -f "${DB_DEST}-wal" ]; then cp -p "${DB_DEST}-wal" "${SNAPSHOT}-wal"; fi
    if [ -f "${DB_DEST}-shm" ]; then cp -p "${DB_DEST}-shm" "${SNAPSHOT}-shm"; fi
    echo -e "${GREEN}  Active database snapshot saved to: $(basename "${SNAPSHOT}")${NC}"
else
    echo -e "${YELLOW}[3/4] No existing active database found; creating fresh restore.${NC}"
fi

# 4. Perform atomic restore
echo -e "${YELLOW}[4/4] Restoring database from ${BACKUP_FILE}...${NC}"
mkdir -p "$(dirname "${DB_DEST}")"
# Remove any existing WAL or shared-memory journals to prevent replaying stale logs onto restored DB
rm -f "${DB_DEST}-wal" "${DB_DEST}-shm" "${DB_DEST}-journal"
cp -p "${BACKUP_FILE}" "${DB_DEST}"
chmod 600 "${DB_DEST}"

# Re-verify restored file
if command -v sqlite3 >/dev/null 2>&1; then
    RESTORE_CHECK=$(sqlite3 "${DB_DEST}" "PRAGMA integrity_check;" 2>&1)
    if [ "${RESTORE_CHECK}" != "ok" ]; then
        echo -e "${RED}[ERROR] Restored database check failed: ${RESTORE_CHECK}${NC}"
        exit 1
    fi
    echo -e "${GREEN}  Restored database verification: OK.${NC}"
fi

# Restart service if it was running
if [ "${WAS_RUNNING}" -eq 1 ]; then
    echo -e "${YELLOW}Restarting bunny-bot service...${NC}"
    sudo systemctl start bunny-bot
    echo -e "${GREEN}  bunny-bot service restarted successfully.${NC}"
fi

echo ""
echo -e "${GREEN}==========================================================${NC}"
echo -e "${GREEN}  Database restore completed successfully!               ${NC}"
echo -e "${GREEN}==========================================================${NC}"
echo ""
