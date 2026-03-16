#!/bin/bash
# Dump the context store database before shutting down Docker.
# Usage: ./backup-and-down.sh

set -e
cd "$(dirname "$0")"

BACKUP_DIR="data/backups"
mkdir -p "$BACKUP_DIR"

echo "Dumping context store..."
docker compose exec -T postgres pg_dump -U postgres code_storage > "$BACKUP_DIR/context_store_$(date +%Y%m%d_%H%M%S).sql"
echo "Backup saved to $BACKUP_DIR/"

docker compose down
echo "Done."
