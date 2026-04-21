#!/usr/bin/env bash
# =============================================================================
# clone_for_new_character.sh
# -----------------------------------------------------------------------------
# Clone this greyhawk-solo project into a sibling folder for a new character.
#
#   1. Prompts for a character name
#   2. Creates ../greyhawk-<slug>
#   3. Copies every project file EXCEPT: saves/ contents, config.json,
#      *.db / *.db-shm / *.db-wal, caches and build artifacts, .git
#   4. Creates an empty saves/ folder in the clone
#   5. Ensures schema/starter.sql and schema/ddl.sql are present
#   6. Prints the exact Claude Desktop MCP config snippet to paste in
# =============================================================================

set -euo pipefail

echo
echo "  greyhawk-solo -- New Character Clone"
echo "  ----------------------------------------"
echo

read -r -p "Enter character name (e.g. Mei Lin, Elric): " CHARNAME
# Trim surrounding whitespace
CHARNAME="$(printf '%s' "$CHARNAME" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
if [ -z "$CHARNAME" ]; then
    echo "ERROR: Character name cannot be empty."
    exit 1
fi

# Normalise: lowercase, collapse whitespace to _, strip anything not [a-z0-9_-]
SLUG="$(printf '%s' "$CHARNAME" \
    | tr '[:upper:]' '[:lower:]' \
    | tr -s '[:space:]' '_' \
    | sed 's/[^a-z0-9_-]//g')"
if [ -z "$SLUG" ]; then
    echo "ERROR: Name normalised to empty slug. Try a plainer name."
    exit 1
fi

# Source = directory containing this script; destination = sibling folder.
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PARENT="$(dirname "$SRC")"
DEST="$PARENT/greyhawk-$SLUG"

if [ -e "$DEST" ]; then
    echo
    echo "ERROR: Destination already exists:"
    echo "  $DEST"
    echo "Pick a different character name or remove that folder first."
    exit 1
fi

echo
echo "Slug        : greyhawk-$SLUG"
echo "Source      : $SRC"
echo "Destination : $DEST"
echo
echo "Copying project files (excluding saves, DBs, config.json, caches)..."

if command -v rsync >/dev/null 2>&1; then
    rsync -a \
        --exclude '/saves/***' \
        --exclude '__pycache__/' \
        --exclude '.venv/' \
        --exclude 'venv/' \
        --exclude 'env/' \
        --exclude '.git/' \
        --exclude '.idea/' \
        --exclude '.vscode/' \
        --exclude '.pytest_cache/' \
        --exclude 'config.json' \
        --exclude '*.db' \
        --exclude '*.db-shm' \
        --exclude '*.db-wal' \
        --exclude '*.pyc' \
        "$SRC/" "$DEST/"
else
    # Fallback: full copy, then prune.
    cp -R "$SRC" "$DEST"
    rm -rf "$DEST/saves" \
           "$DEST/__pycache__" \
           "$DEST/.venv" "$DEST/venv" "$DEST/env" \
           "$DEST/.git" "$DEST/.idea" "$DEST/.vscode" "$DEST/.pytest_cache"
    rm -f "$DEST/config.json"
    find "$DEST" -type f \
        \( -name '*.db' -o -name '*.db-shm' -o -name '*.db-wal' -o -name '*.pyc' \) \
        -delete
    find "$DEST" -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
fi

# Empty saves/ folder, with .gitkeep if the source had one.
mkdir -p "$DEST/saves"
[ -f "$SRC/saves/.gitkeep" ] && cp "$SRC/saves/.gitkeep" "$DEST/saves/.gitkeep"

# Safety net: re-copy schema files even if excludes ever grow to cover them.
mkdir -p "$DEST/schema"
[ -f "$SRC/schema/starter.sql" ] && cp -f "$SRC/schema/starter.sql" "$DEST/schema/starter.sql"
[ -f "$SRC/schema/ddl.sql"     ] && cp -f "$SRC/schema/ddl.sql"     "$DEST/schema/ddl.sql"

# Detect the Claude Desktop config location for the instructions.
if [ "$(uname)" = "Darwin" ]; then
    CFG_PATH="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
    QUIT_HINT="menu bar icon -> Quit Claude"
else
    CFG_PATH="$HOME/.config/Claude/claude_desktop_config.json"
    QUIT_HINT="close the app fully (pkill -f Claude if needed)"
fi

cat <<EOF

============================================================
 Clone complete.
============================================================

Next steps:

1. Create the character in the new folder:

     cd "$DEST"
     python create_character.py

2. Add the following entry to your Claude Desktop config:

     $CFG_PATH

   Merge it into the existing "mcpServers" block - do NOT
   overwrite other servers.

   ------------------------------------------------------------
   {
     "mcpServers": {
       "greyhawk-$SLUG": {
         "command": "python",
         "args": ["$DEST/server/mcp_server.py"]
       }
     }
   }
   ------------------------------------------------------------

3. Fully quit Claude Desktop ($QUIT_HINT) and relaunch.

EOF
