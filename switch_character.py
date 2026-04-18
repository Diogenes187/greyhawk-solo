"""
switch_character.py
--------------------
Scan saves/ for campaign databases and switch the active one.

Reads character name, class, level, and realm name from each database
so you know who is who, then lets you pick one by number. Updates
config.json so the MCP server loads that database on next start.

Usage:
    python switch_character.py

After switching, fully quit and relaunch Claude Desktop to apply the change.
The MCP server process reads config.json once at startup.
"""

import json
import sqlite3
import sys
from pathlib import Path

ROOT   = Path(__file__).parent
SAVES  = ROOT / "saves"
CONFIG = ROOT / "config.json"

# Width constants for display alignment
_W_CHAR    = 22
_W_CLASSES = 30
_W_REALM   = 26


# ─────────────────────────────────────────────────────────────────────────────
# DB introspection helpers
# ─────────────────────────────────────────────────────────────────────────────

def _read_db_info(db_path: Path) -> dict:
    """
    Extract display info from a campaign DB without writing anything.
    Returns a dict with keys: character, classes, realm, ai_turns, error.
    """
    result = {
        "character": "(no character)",
        "classes":   "(no class)",
        "realm":     "(unnamed)",
        "ai_turns":  0,
        "error":     None,
    }

    try:
        uri  = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        cur  = conn.cursor()

        # Realm / campaign name
        try:
            row = cur.execute(
                "SELECT name FROM campaigns WHERE campaign_id = 1"
            ).fetchone()
            if row:
                result["realm"] = row[0]
        except Exception:
            pass

        # PC name (character_id = 1)
        try:
            row = cur.execute(
                "SELECT name FROM characters WHERE character_id = 1"
            ).fetchone()
            if row:
                result["character"] = row[0]
        except Exception:
            pass

        # Class levels for PC
        try:
            rows = cur.execute(
                "SELECT class_name, level FROM class_levels "
                "WHERE character_id = 1 ORDER BY class_name"
            ).fetchall()
            if rows:
                result["classes"] = " / ".join(
                    f"{r['class_name']} {r['level']}" for r in rows
                )
        except Exception:
            pass

        # Turn count — gives a sense of how far along the campaign is
        try:
            row = cur.execute("SELECT COUNT(*) FROM ai_turns").fetchone()
            if row:
                result["ai_turns"] = row[0]
        except Exception:
            pass

        conn.close()

    except sqlite3.OperationalError as e:
        result["error"] = f"Cannot open: {e}"

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Config helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    if CONFIG.exists():
        try:
            return json.loads(CONFIG.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_config(data: dict) -> None:
    CONFIG.write_text(
        json.dumps(data, indent=2) + "\n",
        encoding="utf-8",
    )


def _normalise(path_str: str) -> str:
    """Normalise path separators for comparison."""
    return path_str.replace("\\", "/")


# ─────────────────────────────────────────────────────────────────────────────
# Display helpers
# ─────────────────────────────────────────────────────────────────────────────

def _trunc(s: str, width: int) -> str:
    return s if len(s) <= width else s[: width - 1] + "~"


def _print_header() -> None:
    print()
    print("  greyhawk-solo -- Campaign Switcher")
    print("  " + "-" * 52)
    print()


def _print_entry(index: int, db_path: Path, info: dict, is_active: bool) -> None:
    rel     = _normalise(str(db_path.relative_to(ROOT)))
    active  = " [active]" if is_active else ""
    char    = _trunc(info["character"], _W_CHAR)
    classes = _trunc(info["classes"],   _W_CLASSES)
    realm   = _trunc(info["realm"],     _W_REALM)
    turns   = info["ai_turns"]

    print(f"  {index}.  {char:<{_W_CHAR}}  {classes:<{_W_CLASSES}}")
    print(f"       Realm: {realm}   Turns played: {turns}")
    print(f"       File:  {rel}{active}")
    if info["error"]:
        print(f"       ERROR: {info['error']}")
    print()


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    # Discover campaign databases
    db_files = sorted(SAVES.glob("*.db"))

    if not db_files:
        print()
        print("  No .db files found in saves/.")
        print()
        print("  Create one with:")
        print("    sqlite3 saves/my_campaign.db < schema/starter.sql")
        print("    sqlite3 saves/my_campaign.db < schema/new_character_template.sql")
        print()
        sys.exit(0)

    config  = _load_config()
    current = _normalise(config.get("active_campaign_db", ""))

    _print_header()

    entries: list[tuple[Path, dict]] = []
    for i, db_path in enumerate(db_files, 1):
        info     = _read_db_info(db_path)
        rel      = _normalise(str(db_path.relative_to(ROOT)))
        is_active = (rel == current)
        _print_entry(i, db_path, info, is_active)
        entries.append((db_path, info))

    # Single-database shortcut
    if len(entries) == 1:
        db_path, info = entries[0]
        rel = _normalise(str(db_path.relative_to(ROOT)))
        if rel == current:
            print("  This database is already active. Nothing to do.")
            print()
            return
        raw = input("  Only one database found. Activate it? [Y/n] ").strip().lower()
        if raw not in ("", "y"):
            print("  No change.")
            return
        selected_path, selected_info = entries[0]

    else:
        raw = input(
            f"  Select a campaign [1-{len(entries)}], or press Enter to cancel: "
        ).strip()
        if not raw:
            print("  No change.")
            print()
            return
        try:
            idx = int(raw)
            if not 1 <= idx <= len(entries):
                raise ValueError
        except ValueError:
            print(f"  '{raw}' is not a valid choice. No change.")
            print()
            return
        selected_path, selected_info = entries[idx - 1]

    # Apply
    rel = _normalise(str(selected_path.relative_to(ROOT)))
    config["active_campaign_db"] = rel
    _save_config(config)

    print()
    print("  Active campaign set:")
    print(f"    Character : {selected_info['character']}  ({selected_info['classes']})")
    print(f"    Realm     : {selected_info['realm']}")
    print(f"    Database  : {rel}")
    print()
    print("  Next step: fully quit and relaunch Claude Desktop.")
    print("  The MCP server reads config.json once at startup.")
    print()


if __name__ == "__main__":
    main()
