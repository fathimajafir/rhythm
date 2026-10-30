#!/bin/sh
# Backup the Rhythm store snapshot. The snapshot is a single atomic JSON file,
# so a plain copy is consistent. Run from cron:
#   15 2 * * * /opt/rhythm/deploy/backup.sh >> /var/log/rhythm-backup.log 2>&1
set -eu

STORE="${RHYTHM_STORE_PATH:-/var/lib/rhythm/store.json}"
BACKUP_DIR="${RHYTHM_BACKUP_DIR:-/var/backups/rhythm}"
KEEP_DAYS="${RHYTHM_BACKUP_KEEP_DAYS:-14}"

[ -f "$STORE" ] || { echo "no store at $STORE — nothing to back up"; exit 0; }

mkdir -p "$BACKUP_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
TARGET="$BACKUP_DIR/store-$STAMP.json"

cp -p "$STORE" "$TARGET"
echo "backed up: $TARGET ($(wc -c < "$TARGET") bytes)"

# Retention
find "$BACKUP_DIR" -name 'store-*.json' -mtime +"$KEEP_DAYS" -print -delete |
    { while read -r f; do [ -n "$f" ] && echo "pruned: $f"; done; } || true

# Optional: push off-box (uncomment and configure)
# rclone copy "$TARGET" remote:rhythm-backups/
