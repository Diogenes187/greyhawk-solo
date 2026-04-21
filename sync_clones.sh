#!/usr/bin/env bash
# =============================================================================
# sync_clones.sh
# -----------------------------------------------------------------------------
# Mirror code from this greyhawk-solo directory into every sibling
# greyhawk-* folder alongside it.
#
# Usage:
#   ./sync_clones.sh              Sync every sibling (writes files).
#   ./sync_clones.sh dry          Dry run: list what WOULD change.
#   ./sync_clones.sh --dry-run    Same as above.
#   ./sync_clones.sh -n           Same as above.
#
# What gets synced:   everything in the project tree
# What is NEVER touched in the siblings:
#   saves/ (character DBs), config.json, *.db / *.db-shm / *.db-wal,
#   __pycache__, .venv, venv, env, .git, .idea, .vscode, .pytest_cache, *.pyc
#
# Extraneous files in the sibling are left alone (no --delete flag).
# =============================================================================

set -euo pipefail

DRYRUN=0
case "${1:-}" in
    dry|--dry-run|-n) DRYRUN=1 ;;
    '')                ;;
    *)
        echo "Usage: $0 [dry|--dry-run|-n]"
        exit 2
        ;;
esac

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PARENT="$(dirname "$SRC")"
SRCNAME="$(basename "$SRC")"

echo
echo "  greyhawk-solo -- Sync Clones"
echo "  ----------------------------------------"
if [ $DRYRUN -eq 1 ]; then
    echo "  Mode   : DRY RUN (nothing will be written)"
else
    echo "  Mode   : LIVE (files will be overwritten)"
fi
echo "  Source : $SRC"
echo "  Scan   : $PARENT/greyhawk-*  (excluding $SRCNAME)"
echo

if ! command -v rsync >/dev/null 2>&1; then
    echo "ERROR: rsync is required but not found in PATH."
    echo "Install rsync (apt/brew/pacman) and retry."
    exit 1
fi

EXCLUDES=(
    --exclude '/saves/***'
    --exclude '__pycache__/'
    --exclude '.venv/'
    --exclude 'venv/'
    --exclude 'env/'
    --exclude '.git/'
    --exclude '.idea/'
    --exclude '.vscode/'
    --exclude '.pytest_cache/'
    --exclude 'config.json'
    --exclude '*.db'
    --exclude '*.db-shm'
    --exclude '*.db-wal'
    --exclude '*.pyc'
)

count=0
shopt -s nullglob
for dir in "$PARENT"/greyhawk-*; do
    [ -d "$dir" ] || continue
    [ "$(basename "$dir")" = "$SRCNAME" ] && continue
    count=$((count + 1))
    echo "------------------------------------------------------------"
    echo "Sibling: $(basename "$dir")"
    echo "------------------------------------------------------------"

    rsync_flags=(-a)
    if [ $DRYRUN -eq 1 ]; then
        rsync_flags+=(-n -v)
    fi

    rsync "${rsync_flags[@]}" "${EXCLUDES[@]}" "$SRC/" "$dir/"

    if [ $DRYRUN -eq 1 ]; then
        echo "  dry run complete (no changes written)"
    else
        echo "  sync complete"
    fi
    echo
done

if [ $count -eq 0 ]; then
    echo "No sibling greyhawk-* folders found under $PARENT."
    echo "Nothing to sync."
    exit 0
fi

echo "Done. $count sibling(s) processed."
