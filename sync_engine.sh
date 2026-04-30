#!/usr/bin/env bash
# ============================================================
# sync_engine.sh
# Syncs the greyhawk-solo engine to all greyhawk-* siblings.
# Run from any directory — uses script location as source.
# Never touches saves/, config.json, or .db files.
# ============================================================

set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PARENT="$(dirname "$SRC")"

echo
echo "============================================================"
echo " greyhawk-solo Engine Sync"
echo "============================================================"
echo " Source  : $SRC"
echo " Scanning: $PARENT/greyhawk-*"
echo

# ── Collect target folders ────────────────────────────────────
TARGETS=()
for dir in "$PARENT"/greyhawk-*/; do
    name="$(basename "$dir")"
    if [[ "$name" != "greyhawk-solo" && -d "$dir" ]]; then
        TARGETS+=("$dir")
    fi
done

COUNT=${#TARGETS[@]}

if [[ $COUNT -eq 0 ]]; then
    echo " No greyhawk-* character folders found in $PARENT"
    echo
    exit 0
fi

for i in "${!TARGETS[@]}"; do
    echo "  [$((i+1))] $(basename "${TARGETS[$i]}")"
done

echo
read -rp "Sync engine to all these folders? (yes/no): " CONFIRM
if [[ "$CONFIRM" != "yes" ]]; then
    echo "Aborted."
    exit 0
fi

echo
echo "── Starting sync ───────────────────────────────────────────"

PASS=0
FAIL=0

copy_file() {
    local src="$1" dst="$2" label="$3"
    if [[ -f "$src" ]]; then
        if cp "$src" "$dst" 2>/dev/null; then
            echo "   OK  $label"
        else
            echo "   WARN: $label copy failed"
        fi
    fi
}

for i in "${!TARGETS[@]}"; do
    DST="${TARGETS[$i]}"
    DNAME="$(basename "$DST")"
    NUM=$((i+1))

    echo
    echo " [$NUM/$COUNT] $DNAME"

    # engine/ — full folder, exclude .db files
    if rsync -a --exclude="*.db" --exclude="__pycache__/" \
             "$SRC/engine/" "$DST/engine/" 2>/dev/null; then
        echo "   OK  engine/"
    elif cp -r "$SRC/engine/." "$DST/engine/" 2>/dev/null; then
        # fallback if rsync not available
        echo "   OK  engine/ (via cp)"
    else
        echo "   ERROR: engine/ copy failed"
        FAIL=$((FAIL+1))
        continue
    fi

    # server/mcp_server.py
    copy_file "$SRC/server/mcp_server.py"          "$DST/server/mcp_server.py"          "server/mcp_server.py"

    # schema files
    copy_file "$SRC/schema/ddl.sql"                "$DST/schema/ddl.sql"                "schema/ddl.sql"
    copy_file "$SRC/schema/starter.sql"            "$DST/schema/starter.sql"            "schema/starter.sql"

    # clone/sync helper scripts
    copy_file "$SRC/clone_for_new_character.bat"   "$DST/clone_for_new_character.bat"   "clone_for_new_character.bat"
    copy_file "$SRC/clone_for_new_character.sh"    "$DST/clone_for_new_character.sh"    "clone_for_new_character.sh"

    # sync scripts themselves
    copy_file "$SRC/sync_engine.bat"               "$DST/sync_engine.bat"               "sync_engine.bat"
    copy_file "$SRC/sync_engine.sh"                "$DST/sync_engine.sh"                "sync_engine.sh"

    PASS=$((PASS+1))
done

echo
echo "============================================================"
echo " Done.  $PASS folder(s) synced successfully, $FAIL failed."
echo
echo " Restart Claude Desktop to apply changes to all characters."
echo "============================================================"
echo
