#!/usr/bin/env bash
# ==============================================================================
# Bunny Stream Telegram Downloader Bot - Safe Database & Config Backup Script
# ==============================================================================
set -euo pipefail

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${PROJECT_DIR}/backups"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
DB_SRC="${PROJECT_DIR}/data/bot.db"
ENV_SRC="${PROJECT_DIR}/.env"
RETENTION_DAYS=30

mkdir -p "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}"

echo -e "${CYAN}==========================================================${NC}"
echo -e "${CYAN}  Bunny Stream Downloader - Automated Backup Utility     ${NC}"
echo -e "${CYAN}==========================================================${NC}"

# 1. Backup SQLite Database
if [ -f "${DB_SRC}" ]; then
    DB_DEST="${BACKUP_DIR}/bot_db_${TIMESTAMP}.db"
    echo -e "${YELLOW}[1/3] Backing up SQLite database...${NC}"

    if command -v sqlite3 >/dev/null 2>&1; then
        # Online atomic backup (safe while bot is running in WAL mode)
        sqlite3 "${DB_SRC}" ".backup '${DB_DEST}'"
        echo -e "${GREEN}  SQLite atomic online backup created: $(basename "${DB_DEST}")${NC}"
    elif [ -x "${PROJECT_DIR}/.venv/bin/python" ]; then
        # Python sqlite3 online backup engine (guarantees WAL checkpoint consistency without sqlite3 CLI)
        "${PROJECT_DIR}/.venv/bin/python" -c "import sqlite3; s=sqlite3.connect('${DB_SRC}'); d=sqlite3.connect('${DB_DEST}'); s.backup(d); d.close(); s.close()"
        echo -e "${GREEN}  SQLite atomic online backup created via Python engine: $(basename "${DB_DEST}")${NC}"
    else
        # Fallback copy including active WAL/SHM if present
        cp -p "${DB_SRC}" "${DB_DEST}"
        if [ -f "${DB_SRC}-wal" ]; then cp -p "${DB_SRC}-wal" "${DB_DEST}-wal"; fi
        if [ -f "${DB_SRC}-shm" ]; then cp -p "${DB_SRC}-shm" "${DB_DEST}-shm"; fi
        echo -e "${YELLOW}  [NOTICE] Performed file copy backup: $(basename "${DB_DEST}")${NC}"
    fi
    chmod 600 "${DB_DEST}"
else
    echo -e "${YELLOW}[1/3] No existing database found at ${DB_SRC} (skipping DB backup).${NC}"
fi

# 2. Backup .env configuration securely
if [ -f "${ENV_SRC}" ]; then
    ENV_DEST="${BACKUP_DIR}/env_${TIMESTAMP}.bak"
    echo -e "${YELLOW}[2/3] Backing up .env configuration (secured 600)...${NC}"
    cp -p "${ENV_SRC}" "${ENV_DEST}"
    chmod 600 "${ENV_DEST}"
    echo -e "${GREEN}  Configuration backup created: $(basename "${ENV_DEST}")${NC}"
else
    echo -e "${YELLOW}[2/3] No .env found to back up.${NC}"
fi

# 3. Retention policy: Remove backups older than RETENTION_DAYS
echo -e "${YELLOW}[3/3] Pruning backups older than ${RETENTION_DAYS} days...${NC}"
PRUNED_COUNT=$(find "${BACKUP_DIR}" -type f \( -name "bot_db_*.db" -o -name "env_*.bak" \) -mtime +${RETENTION_DAYS} 2>/dev/null | wc -l || true)
find "${BACKUP_DIR}" -type f \( -name "bot_db_*.db" -o -name "env_*.bak" \) -mtime +${RETENTION_DAYS} -delete 2>/dev/null || true
echo -e "${GREEN}  Pruned ${PRUNED_COUNT} old backup archive(s).${NC}"

echo ""
echo -e "${GREEN}Backup completed successfully at $(date)!${NC}"
echo -e "Archives stored in: ${CYAN}${BACKUP_DIR}${NC}"
echo ""
