#!/bin/bash
# backup.sh — Tag PostgreSQL backup til lokal fil
# Kør som: bash scripts/backup.sh
set -euo pipefail

BACKUP_DIR="$HOME/polymarket-backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
FILENAME="polymarket_${TIMESTAMP}.sql.gz"

mkdir -p "$BACKUP_DIR"

echo "==> Tager backup..."
docker compose exec postgres pg_dump -U bot polymarket | gzip > "$BACKUP_DIR/$FILENAME"

echo "✅ Backup gemt: $BACKUP_DIR/$FILENAME"

# Slet backups ældre end 7 dage
find "$BACKUP_DIR" -name "*.sql.gz" -mtime +7 -delete
echo "==> Gamle backups ryddet op (> 7 dage)"
