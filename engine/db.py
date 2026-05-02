"""
engine/db.py
------------
Database access layer for the active campaign database.

The active database is determined by config.json in the project root:
    { "active_campaign_db": "saves/my_campaign.db" }

config.json is re-read on EVERY connection (see _get_conn), so any
change to the active_campaign_db value — whether written by the
switch_campaign MCP tool, switch_character.py, or a manual edit — takes
effect on the very next tool call with no server restart required. If
config.json is absent or malformed, the resolver falls back to
saves/theron.db.

READ functions: load character state, realm state, recent AI turns.
WRITE functions: write a new AI turn, update current scene state.

IMPORTANT: New characters created via character.py use separate JSON saves
and never touch the campaign database. The two systems are fully isolated.
"""

import json
import re
import random
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent


def _resolve_db_path() -> Path:
    """
    Read the active campaign database path from config.json.

    Called fresh on every _get_conn() invocation so that a switch_campaign
    (or any external edit of config.json) takes effect on the very next
    tool call — no process restart required. Falls back to
    saves/theron.db if config.json is missing or malformed.
    """
    config_path = _ROOT / "config.json"
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            rel = cfg.get("active_campaign_db", "")
            if rel:
                return (_ROOT / rel).resolve()
        except Exception:
            pass
    return _ROOT / "saves" / "theron.db"


# Campaign and PC IDs — standard for all campaigns created with new_character_template.sql
_CAMPAIGN_ID = 1
_PC_CHARACTER_ID = 1


@contextmanager
def _get_conn(read_only: bool = False):
    """
    Yield an open SQLite connection targeting the currently-active
    campaign DB. The path is resolved fresh from config.json on every
    call (see _resolve_db_path), so switching the active campaign is a
    hot operation — the next tool call picks up the change automatically.

    Use read_only=True for SELECT-only paths. WAL mode is enabled on
    writable connections for safe concurrent reads while the game loop
    writes.
    """
    db_path = _resolve_db_path()
    if read_only:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    else:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        if not read_only:
            conn.commit()
    finally:
        conn.close()


def _row_to_dict(row) -> dict:
    """Convert a sqlite3.Row to a plain dict."""
    return dict(row) if row else {}


# ══════════════════════════════════════════════════════════════════════════════
# CHARACTER STATE
# ══════════════════════════════════════════════════════════════════════════════

def load_character(character_id: int = _PC_CHARACTER_ID) -> dict:
    """
    Return full character state for the given character_id.
    Joins characters, class_levels, character_status, character_abilities,
    and inventory (equipped items).
    """
    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()

        # Base character row
        cur.execute("""
            SELECT c.character_id, c.name, c.character_type, c.race,
                   c.alignment, c.notes
            FROM characters c
            WHERE c.character_id = ?
        """, (character_id,))
        char = _row_to_dict(cur.fetchone())
        if not char:
            return {}

        # Class levels
        cur.execute("""
            SELECT class_name, level, xp
            FROM class_levels
            WHERE character_id = ?
            ORDER BY class_name
        """, (character_id,))
        char["classes"] = [dict(r) for r in cur.fetchall()]

        # Current status (HP, AC, movement, attacks)
        cur.execute("""
            SELECT hp_current, hp_max, ac, movement, attacks_per_round, status_notes
            FROM character_status
            WHERE character_id = ?
        """, (character_id,))
        status = cur.fetchone()
        char["status"] = _row_to_dict(status)

        # Ability scores
        cur.execute("""
            SELECT strength, intelligence, wisdom, dexterity,
                   constitution, charisma, portrait_path
            FROM character_abilities
            WHERE character_id = ?
        """, (character_id,))
        abilities = cur.fetchone()
        char["abilities"] = _row_to_dict(abilities)

        # Equipped items
        cur.execute("""
            SELECT i.name, i.item_type, i.magic_flag, i.value_gp, i.notes,
                   inv.quantity, inv.equipped_flag, inv.notes AS carry_notes
            FROM inventory inv
            JOIN items i ON i.item_id = inv.item_id
            WHERE inv.character_id = ?
            ORDER BY inv.equipped_flag DESC, i.name
        """, (character_id,))
        char["inventory"] = [dict(r) for r in cur.fetchall()]

    return char


# ══════════════════════════════════════════════════════════════════════════════
# REALM STATE
# ══════════════════════════════════════════════════════════════════════════════

def load_realm() -> dict:
    """
    Return Theron's realm: locations, holdings, troops, treasury, livestock.
    """
    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()

        # Locations
        cur.execute("""
            SELECT l.location_id, l.name, l.location_type, l.status, l.notes,
                   p.name AS parent_name
            FROM locations l
            LEFT JOIN locations p ON p.location_id = l.parent_location_id
            WHERE l.campaign_id = ?
            ORDER BY l.location_id
        """, (_CAMPAIGN_ID,))
        locations = [dict(r) for r in cur.fetchall()]

        # Troops (via the vw_forces_by_location view)
        cur.execute("""
            SELECT t.group_name, t.troop_type, t.count,
                   l.name AS location, c.name AS commander, t.notes
            FROM troops t
            LEFT JOIN locations l ON l.location_id = t.location_id
            LEFT JOIN characters c ON c.character_id = t.commander_character_id
            WHERE t.campaign_id = ?
            ORDER BY l.name, t.troop_type
        """, (_CAMPAIGN_ID,))
        troops = [dict(r) for r in cur.fetchall()]

        # Treasury
        cur.execute("""
            SELECT ta.account_name, l.name AS location,
                   ta.gp, ta.sp, ta.cp, ta.pp,
                   ta.gems_gp_value, ta.notes
            FROM treasury_accounts ta
            LEFT JOIN locations l ON l.location_id = ta.location_id
            WHERE ta.campaign_id = ?
        """, (_CAMPAIGN_ID,))
        treasury = [dict(r) for r in cur.fetchall()]

        # Livestock summary
        cur.execute("""
            SELECT lv.animal_type, SUM(lv.count) AS total,
                   GROUP_CONCAT(lo.name, ', ') AS locations
            FROM livestock lv
            JOIN locations lo ON lo.location_id = lv.location_id
            WHERE lv.campaign_id = ?
            GROUP BY lv.animal_type
            ORDER BY lv.animal_type
        """, (_CAMPAIGN_ID,))
        livestock = [dict(r) for r in cur.fetchall()]

        # Key NPCs (non-PC characters with relationships to PC)
        cur.execute("""
            SELECT c.name, c.race, c.character_type, c.notes,
                   r.relationship_type, r.notes AS rel_notes
            FROM relationships r
            JOIN characters c ON c.character_id = r.target_character_id
            WHERE r.source_character_id = ?
            ORDER BY c.name
        """, (_PC_CHARACTER_ID,))
        npcs = [dict(r) for r in cur.fetchall()]

    return {
        "locations": locations,
        "troops":    troops,
        "treasury":  treasury,
        "livestock": livestock,
        "key_npcs":  npcs,
    }


# ══════════════════════════════════════════════════════════════════════════════
# AI TURN STATE
# ══════════════════════════════════════════════════════════════════════════════

def load_recent_ai_turns(limit: int = 5) -> list[dict]:
    """
    Return the most recent `limit` AI turns, newest first.
    Includes turn_id, player_action, dm_response, model_name, created_at.
    """
    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT turn_id, player_action, dm_response,
                   model_name, created_at, response_id
            FROM ai_turns
            ORDER BY turn_id DESC
            LIMIT ?
        """, (limit,))
        rows = [dict(r) for r in cur.fetchall()]
    # Return chronological order (oldest first within the window)
    return list(reversed(rows))


def load_all_ai_turns() -> list[dict]:
    """Return all AI turns in chronological order."""
    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT turn_id, player_action, dm_response,
                   model_name, created_at, response_id
            FROM ai_turns
            ORDER BY turn_id ASC
        """)
        return [dict(r) for r in cur.fetchall()]


def write_ai_turn(
    player_action: str,
    dm_response: str,
    model_name: str,
    response_id: str | None = None,
    previous_response_id: str | None = None,
    turn_packet_json: str | None = None,
    structured_response_json: str | None = None,
) -> int:
    """
    Insert a new AI turn. Returns the new turn_id.
    This is the ONLY write path into ai_turns from the new engine.
    """
    import datetime
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO ai_turns (
                player_action, dm_response, response_id, previous_response_id,
                model_name, created_at, turn_packet_json, structured_response_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            player_action, dm_response, response_id, previous_response_id,
            model_name, now, turn_packet_json, structured_response_json,
        ))
        return cur.lastrowid


def load_current_scene() -> dict:
    """
    Return the current_scene_state row (singleton id=1).
    Returns empty dict if no scene has been set yet.
    """
    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, current_turn_id, current_player_action,
                   current_dm_response, structured_state_json, updated_at
            FROM current_scene_state
            WHERE id = 1
        """)
        row = cur.fetchone()
        if not row:
            return {}
        result = dict(row)
        # Parse embedded JSON blob if present
        if result.get("structured_state_json"):
            try:
                result["structured_state"] = json.loads(result["structured_state_json"])
            except (json.JSONDecodeError, TypeError):
                result["structured_state"] = None
        return result


def update_current_scene(
    turn_id: int,
    player_action: str,
    dm_response: str,
    structured_state: dict | None = None,
) -> None:
    """
    Upsert the singleton current_scene_state row.
    Call this after every AI turn to keep scene state current.
    """
    import datetime
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    state_json = json.dumps(structured_state) if structured_state else None

    with _get_conn() as conn:
        conn.execute("""
            INSERT INTO current_scene_state
                (id, current_turn_id, current_player_action,
                 current_dm_response, structured_state_json, updated_at)
            VALUES (1, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                current_turn_id       = excluded.current_turn_id,
                current_player_action = excluded.current_player_action,
                current_dm_response   = excluded.current_dm_response,
                structured_state_json = excluded.structured_state_json,
                updated_at            = excluded.updated_at
        """, (turn_id, player_action, dm_response, state_json, now))


# ══════════════════════════════════════════════════════════════════════════════
# COMBINED STATE SNAPSHOT
# ══════════════════════════════════════════════════════════════════════════════

def load_full_state(recent_turns: int = 5) -> dict:
    """
    Return a single dict with character, realm, recent AI turns, and current
    scene. This is the payload used by the /state route and the AI DM prompt.
    """
    return {
        "character":    load_character(),
        "realm":        load_realm(),
        "recent_turns": load_recent_ai_turns(limit=recent_turns),
        "current_scene": load_current_scene(),
    }


# ══════════════════════════════════════════════════════════════════════════════
# WRITE — CHARACTER
# ══════════════════════════════════════════════════════════════════════════════

def update_character_status(
    character_id: int = _PC_CHARACTER_ID,
    hp_current: int | None = None,
    hp_max: int | None = None,
    ac: int | None = None,
    status_notes: str | None = None,
) -> dict:
    """
    Update mutable fields on character_status. Only provided (non-None) fields
    are written — omitting a field leaves it unchanged.
    Returns the full updated status row.
    """
    fields, values = [], []
    if hp_current is not None:
        fields.append("hp_current = ?");  values.append(hp_current)
    if hp_max is not None:
        fields.append("hp_max = ?");      values.append(hp_max)
    if ac is not None:
        fields.append("ac = ?");          values.append(ac)
    if status_notes is not None:
        fields.append("status_notes = ?"); values.append(status_notes)

    if not fields:
        raise ValueError("No fields to update — provide at least one of hp_current, hp_max, ac, status_notes.")

    values.append(character_id)
    with _get_conn() as conn:
        conn.execute(
            f"UPDATE character_status SET {', '.join(fields)} WHERE character_id = ?",
            values,
        )

    # Return updated row
    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT hp_current, hp_max, ac, movement, attacks_per_round, status_notes "
            "FROM character_status WHERE character_id = ?",
            (character_id,),
        )
        return _row_to_dict(cur.fetchone())


# ══════════════════════════════════════════════════════════════════════════════
# WRITE — TREASURY
# ══════════════════════════════════════════════════════════════════════════════

def update_treasury(
    account_name: str,
    gp_delta: int = 0,
    sp_delta: int = 0,
    cp_delta: int = 0,
    pp_delta: int = 0,
    gems_delta: int = 0,
) -> dict:
    """
    Apply signed deltas to a treasury account located by name (case-insensitive
    prefix match). Raises ValueError if the result would go below zero for any
    denomination. Returns the updated account row.
    """
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT treasury_id, account_name, gp, sp, cp, pp, gems_gp_value "
            "FROM treasury_accounts "
            "WHERE LOWER(account_name) LIKE LOWER(?) AND campaign_id = ?",
            (f"{account_name}%", _CAMPAIGN_ID),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Treasury account matching '{account_name}' not found.")

        tid     = row["treasury_id"]
        new_gp  = (row["gp"]  or 0) + gp_delta
        new_sp  = (row["sp"]  or 0) + sp_delta
        new_cp  = (row["cp"]  or 0) + cp_delta
        new_pp  = (row["pp"]  or 0) + pp_delta
        new_gems = (row["gems_gp_value"] or 0) + gems_delta

        for label, val in [("gp", new_gp), ("sp", new_sp), ("cp", new_cp),
                           ("pp", new_pp), ("gems", new_gems)]:
            if val < 0:
                raise ValueError(
                    f"Transaction would leave {label} at {val} — insufficient funds "
                    f"in '{row['account_name']}'."
                )

        conn.execute(
            "UPDATE treasury_accounts SET gp=?, sp=?, cp=?, pp=?, gems_gp_value=? "
            "WHERE treasury_id=?",
            (new_gp, new_sp, new_cp, new_pp, new_gems, tid),
        )

    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT account_name, gp, sp, cp, pp, gems_gp_value, notes "
            "FROM treasury_accounts WHERE treasury_id = ?",
            (tid,),
        )
        return _row_to_dict(cur.fetchone())


# ══════════════════════════════════════════════════════════════════════════════
# WRITE — LOCATIONS
# ══════════════════════════════════════════════════════════════════════════════

def add_location(
    name: str,
    location_type: str,
    status: str,
    notes: str = "",
    parent_location_name: str | None = None,
) -> dict:
    """
    Insert a new location. If parent_location_name is given, looks up its
    location_id for the FK. Returns the new location row.
    """
    parent_id = None
    if parent_location_name:
        with _get_conn(read_only=True) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT location_id FROM locations "
                "WHERE LOWER(name) LIKE LOWER(?) AND campaign_id = ? LIMIT 1",
                (f"{parent_location_name}%", _CAMPAIGN_ID),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Parent location '{parent_location_name}' not found.")
            parent_id = row["location_id"]

    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO locations (campaign_id, name, location_type, parent_location_id, status, notes) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_CAMPAIGN_ID, name, location_type, parent_id, status, notes or None),
        )
        new_id = cur.lastrowid

    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT l.location_id, l.name, l.location_type, l.status, l.notes, "
            "p.name AS parent_name "
            "FROM locations l LEFT JOIN locations p ON p.location_id = l.parent_location_id "
            "WHERE l.location_id = ?",
            (new_id,),
        )
        return _row_to_dict(cur.fetchone())


def update_location_status(
    name: str,
    new_status: str,
    notes: str | None = None,
) -> dict:
    """
    Change the status (and optionally notes) of a location by name.
    Case-insensitive prefix match. Returns the updated row.
    """
    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT location_id FROM locations "
            "WHERE LOWER(name) LIKE LOWER(?) AND campaign_id = ? LIMIT 1",
            (f"{name}%", _CAMPAIGN_ID),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Location '{name}' not found.")
        loc_id = row["location_id"]

    with _get_conn() as conn:
        if notes is not None:
            conn.execute(
                "UPDATE locations SET status = ?, notes = ? WHERE location_id = ?",
                (new_status, notes, loc_id),
            )
        else:
            conn.execute(
                "UPDATE locations SET status = ? WHERE location_id = ?",
                (new_status, loc_id),
            )

    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT l.location_id, l.name, l.location_type, l.status, l.notes, "
            "p.name AS parent_name "
            "FROM locations l LEFT JOIN locations p ON p.location_id = l.parent_location_id "
            "WHERE l.location_id = ?",
            (loc_id,),
        )
        return _row_to_dict(cur.fetchone())


# ══════════════════════════════════════════════════════════════════════════════
# WRITE — TROOPS
# ══════════════════════════════════════════════════════════════════════════════

def update_troop_count(
    group_name: str,
    new_count: int | None = None,
    delta: int | None = None,
) -> dict:
    """
    Set or adjust the count for a troop group by name (case-insensitive prefix).
    Provide either new_count (absolute) or delta (signed adjustment), not both.
    Count cannot go below 0. Returns the updated troop row.
    """
    if new_count is None and delta is None:
        raise ValueError("Provide either new_count or delta.")
    if new_count is not None and delta is not None:
        raise ValueError("Provide new_count OR delta, not both.")

    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT troop_id, group_name, count FROM troops "
            "WHERE LOWER(group_name) LIKE LOWER(?) AND campaign_id = ? LIMIT 1",
            (f"{group_name}%", _CAMPAIGN_ID),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Troop group '{group_name}' not found.")
        troop_id = row["troop_id"]
        current  = row["count"]

    final = new_count if new_count is not None else current + delta
    if final < 0:
        raise ValueError(f"Count would go to {final} — cannot have negative troops.")

    with _get_conn() as conn:
        conn.execute(
            "UPDATE troops SET count = ? WHERE troop_id = ?",
            (final, troop_id),
        )

    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT t.troop_id, t.group_name, t.troop_type, t.count, "
            "l.name AS location, c.name AS commander, t.notes "
            "FROM troops t "
            "LEFT JOIN locations l ON l.location_id = t.location_id "
            "LEFT JOIN characters c ON c.character_id = t.commander_character_id "
            "WHERE t.troop_id = ?",
            (troop_id,),
        )
        return _row_to_dict(cur.fetchone())


def add_troop_group(
    group_name: str,
    troop_type: str,
    count: int,
    location_name: str,
    notes: str = "",
) -> dict:
    """
    Insert a new troop group at a given location. Returns the new troop row.
    """
    if count < 0:
        raise ValueError("Count cannot be negative.")

    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT location_id FROM locations "
            "WHERE LOWER(name) LIKE LOWER(?) AND campaign_id = ? LIMIT 1",
            (f"{location_name}%", _CAMPAIGN_ID),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Location '{location_name}' not found.")
        loc_id = row["location_id"]

    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO troops (campaign_id, location_id, group_name, troop_type, count, notes) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_CAMPAIGN_ID, loc_id, group_name, troop_type, count, notes or None),
        )
        new_id = cur.lastrowid

    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT t.troop_id, t.group_name, t.troop_type, t.count, "
            "l.name AS location, t.notes "
            "FROM troops t LEFT JOIN locations l ON l.location_id = t.location_id "
            "WHERE t.troop_id = ?",
            (new_id,),
        )
        return _row_to_dict(cur.fetchone())


# ══════════════════════════════════════════════════════════════════════════════
# WRITE — ITEMS / INVENTORY
# ══════════════════════════════════════════════════════════════════════════════

def add_item(
    name: str,
    item_type: str = "",
    magic_flag: bool = False,
    value_gp: int | None = None,
    notes: str = "",
    assign_to: str = "character",   # "character" | "location" | "treasury"
    location_name: str | None = None,
    treasury_name: str | None = None,
    equipped: bool = False,
    carry_notes: str = "",
) -> dict:
    """
    Create a new item and assign it to an inventory slot.

    assign_to controls where it goes:
      "character" — assigned to Theron Vale (character_id=1)
      "location"  — assigned to a location by name (location_name required)
      "treasury"  — assigned to a treasury account by name (treasury_name required)

    Returns a dict with item_id, inventory_id, and the assignment details.
    Raises ValueError on bad inputs or missing location/treasury.
    """
    # Resolve assignment FK
    char_id = loc_id = treasury_id = None

    if assign_to == "character":
        char_id = _PC_CHARACTER_ID
    elif assign_to == "location":
        if not location_name:
            raise ValueError("location_name required when assign_to='location'.")
        with _get_conn(read_only=True) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT location_id FROM locations "
                "WHERE LOWER(name) LIKE LOWER(?) AND campaign_id = ? LIMIT 1",
                (f"{location_name}%", _CAMPAIGN_ID),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Location '{location_name}' not found.")
            loc_id = row["location_id"]
    elif assign_to == "treasury":
        if not treasury_name:
            raise ValueError("treasury_name required when assign_to='treasury'.")
        with _get_conn(read_only=True) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT treasury_id FROM treasury_accounts "
                "WHERE LOWER(account_name) LIKE LOWER(?) AND campaign_id = ? LIMIT 1",
                (f"{treasury_name}%", _CAMPAIGN_ID),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Treasury account '{treasury_name}' not found.")
            treasury_id = row["treasury_id"]
    else:
        raise ValueError("assign_to must be 'character', 'location', or 'treasury'.")

    with _get_conn() as conn:
        cur = conn.cursor()
        # Insert item
        cur.execute(
            "INSERT INTO items (campaign_id, name, item_type, magic_flag, value_gp, notes) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_CAMPAIGN_ID, name, item_type or None, 1 if magic_flag else 0,
             value_gp, notes or None),
        )
        item_id = cur.lastrowid

        # Insert inventory row — exactly one FK must be set (enforced by DB CHECK)
        cur.execute(
            "INSERT INTO inventory (character_id, location_id, treasury_id, "
            "item_id, quantity, equipped_flag, notes) VALUES (?, ?, ?, ?, 1, ?, ?)",
            (char_id, loc_id, treasury_id, item_id,
             1 if equipped else 0, carry_notes or None),
        )
        inv_id = cur.lastrowid

    return {
        "item_id":      item_id,
        "inventory_id": inv_id,
        "name":         name,
        "item_type":    item_type,
        "magic_flag":   magic_flag,
        "value_gp":     value_gp,
        "notes":        notes,
        "assigned_to":  assign_to,
        "location_name": location_name,
        "treasury_name": treasury_name,
        "equipped":     equipped,
    }


# ══════════════════════════════════════════════════════════════════════════════
# WRITE — WORLD FACTS
# ══════════════════════════════════════════════════════════════════════════════

def update_world_fact(
    category: str,
    fact_text: str,
    source_note: str = "",
    overwrite_category: bool = False,
) -> dict:
    """
    Upsert a world fact.

    If overwrite_category=True, all existing facts in that category are deleted
    before inserting the new one (use for singleton-per-category facts like
    current weather, active quest, etc.).

    If overwrite_category=False (default), the new fact is appended alongside
    any existing facts in the same category.

    Returns the new fact row.
    """
    with _get_conn() as conn:
        cur = conn.cursor()
        if overwrite_category:
            cur.execute(
                "DELETE FROM world_facts WHERE category = ? AND campaign_id = ?",
                (category, _CAMPAIGN_ID),
            )
        cur.execute(
            "INSERT INTO world_facts (campaign_id, category, fact_text, source_note) "
            "VALUES (?, ?, ?, ?)",
            (_CAMPAIGN_ID, category, fact_text, source_note or None),
        )
        new_id = cur.lastrowid

    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT world_fact_id, category, fact_text, source_note "
            "FROM world_facts WHERE world_fact_id = ?",
            (new_id,),
        )
        return _row_to_dict(cur.fetchone())


# ══════════════════════════════════════════════════════════════════════════════
# WRITE — NPCs
# ══════════════════════════════════════════════════════════════════════════════

def update_npc(
    name: str,
    notes: str | None = None,
    character_type: str | None = None,
    race: str | None = None,
    alignment: str | None = None,
) -> dict:
    """
    Update mutable fields on an NPC's characters row.
    Looks up by name (case-insensitive prefix match).
    Returns the full updated character row.
    """
    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT character_id, name FROM characters "
            "WHERE LOWER(name) LIKE LOWER(?) AND campaign_id = ? LIMIT 1",
            (f"{name}%", _CAMPAIGN_ID),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError(f"NPC '{name}' not found in characters table.")
        char_id = row["character_id"]

    fields, values = [], []
    if notes is not None:
        fields.append("notes = ?");          values.append(notes)
    if character_type is not None:
        fields.append("character_type = ?"); values.append(character_type)
    if race is not None:
        fields.append("race = ?");           values.append(race)
    if alignment is not None:
        fields.append("alignment = ?");      values.append(alignment)

    if not fields:
        raise ValueError("No fields to update — provide at least one of notes, character_type, race, alignment.")

    values.append(char_id)
    with _get_conn() as conn:
        conn.execute(
            f"UPDATE characters SET {', '.join(fields)} WHERE character_id = ?",
            values,
        )

    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT character_id, name, character_type, race, alignment, notes "
            "FROM characters WHERE character_id = ?",
            (char_id,),
        )
        return _row_to_dict(cur.fetchone())


def add_npc(
    name: str,
    race: str | None = None,
    character_type: str = "NPC",
    notes: str | None = None,
    relationship_to_theron: str | None = None,
    relationship_notes: str | None = None,
) -> dict:
    """
    Add a new NPC to the characters table. If relationship_to_theron is
    provided, also inserts a row in the relationships table linking this NPC
    to Theron (character_id=1). Returns the new character row.

    Parameter binding is intentionally aligned with update_npc: each text
    field accepts None (= not provided) or a string (= written verbatim).
    Empty strings are treated as None so the column ends up NULL rather
    than holding an empty string — but a non-empty value is ALWAYS written
    through, never dropped.
    """
    # Normalize "" → None so the empty-string case stores NULL, but a
    # non-empty value passes straight through to the INSERT.
    race                  = (race or "").strip() or None
    notes                 = notes if (notes is not None and notes != "") else None
    relationship_to_theron = (relationship_to_theron or "").strip() or None
    relationship_notes    = (relationship_notes if
                             (relationship_notes is not None
                              and relationship_notes != "") else None)

    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO characters (campaign_id, name, character_type, race, notes) "
            "VALUES (?, ?, ?, ?, ?)",
            (_CAMPAIGN_ID, name, character_type, race, notes),
        )
        new_id = cur.lastrowid

        if relationship_to_theron:
            cur.execute(
                "INSERT INTO relationships "
                "(source_character_id, target_character_id, relationship_type, notes) "
                "VALUES (?, ?, ?, ?)",
                (_PC_CHARACTER_ID, new_id,
                 relationship_to_theron, relationship_notes),
            )

    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT character_id, name, character_type, race, alignment, notes "
            "FROM characters WHERE character_id = ?",
            (new_id,),
        )
        return _row_to_dict(cur.fetchone())


# ══════════════════════════════════════════════════════════════════════════════
# CREATE — NEW CAMPAIGN DATABASE
# ══════════════════════════════════════════════════════════════════════════════

_DDL_PATH = _ROOT / "schema" / "ddl.sql"


def _split_ddl_statements(sql: str) -> list[str]:
    """
    Split a SQL script into individual statements by accumulating lines until
    one ends with ';'.  Handles multi-line CREATE TABLE / CREATE VIEW / etc.
    Skips blank lines, -- comments, and PRAGMA lines.
    """
    stmts:   list[str] = []
    current: list[str] = []
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        if stripped.upper().startswith("PRAGMA"):
            continue
        current.append(line)
        if stripped.endswith(";"):
            full = "\n".join(current).rstrip().rstrip(";")
            if full.strip():
                stmts.append(full)
            current = []
    return stmts


def _bootstrap_new_db(db_path: Path) -> None:
    """
    Create a fresh SQLite campaign database at db_path using schema/ddl.sql.

    All DDL is executed inside a single explicit transaction so the operation
    completes in under a second.  executescript() is intentionally avoided
    because on Windows it commits after every statement, turning a sub-second
    operation into several minutes of disk fsyncs.
    """
    if not _DDL_PATH.exists():
        raise FileNotFoundError(
            f"Cannot find {_DDL_PATH}. "
            "Run from the project root or generate it with: "
            "python -c \"sql=open('schema/starter.sql').read(); "
            "open('schema/ddl.sql','w').write(sql[:sql.find('-- REFERENCE DATA')])\""
        )

    sql   = _DDL_PATH.read_text(encoding="utf-8")
    stmts = _split_ddl_statements(sql)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN")
        for stmt in stmts:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as e:
                if "already exists" not in str(e):
                    raise
        conn.execute("COMMIT")
        conn.execute("PRAGMA journal_mode=WAL")
    finally:
        conn.close()


def create_character_db(
    name:            str,
    race:            str,
    character_class: str,
    str_score:       int,
    int_score:       int,
    wis_score:       int,
    dex_score:       int,
    con_score:       int,
    cha_score:       int,
    alignment:       str = "",
    starting_gold:   int = 0,
) -> dict:
    """
    Create a new campaign database for a freshly confirmed character.

    Accepts the player's confirmed ability scores (pre-racial-modifier),
    applies racial modifiers via CharacterSheet, calculates HP / AC / THAC0 /
    saving throws, writes all character data to saves/<slug>.db, updates
    config.json to make the new campaign active, and returns the full
    character sheet.

    Raises ValueError if the race or class is not found in data files, or if
    saves/<slug>.db already exists.
    """
    import re as _re

    # ── Lazy import to avoid circular dependency ───────────────────────────
    from engine.character import (
        CharacterSheet,
        CON_TABLE as _CON_TABLE,
        DEX_TABLE as _DEX_TABLE,
    )

    # ── Load race / class data ─────────────────────────────────────────────
    _data_dir = _ROOT / "data"

    with open(_data_dir / "races.json",   encoding="utf-8") as f:
        races_data = json.load(f)
    with open(_data_dir / "classes.json", encoding="utf-8") as f:
        classes_data = json.load(f)

    if race not in races_data:
        raise ValueError(
            f"Race '{race}' not found. "
            f"Available: {', '.join(races_data.keys())}"
        )
    if character_class not in classes_data:
        raise ValueError(
            f"Class '{character_class}' not found. "
            f"Available: {', '.join(classes_data.keys())}"
        )

    # ── Validate race allows class ─────────────────────────────────────────
    allowed = races_data[race].get("allowed_classes", [])
    if allowed and character_class not in allowed:
        raise ValueError(
            f"{race}s cannot be {character_class}s. "
            f"Allowed classes: {', '.join(allowed)}"
        )

    # ── Build CharacterSheet with confirmed scores ─────────────────────────
    sheet = CharacterSheet()
    sheet.name  = name
    sheet.level = 1
    sheet.ability_scores = {
        "str": str_score, "int": int_score, "wis": wis_score,
        "dex": dex_score, "con": con_score, "cha": cha_score,
    }

    sheet.apply_race(race,            races_data)
    sheet.apply_class(character_class, classes_data)
    sheet.calculate_derived_stats()
    sheet.alignment = alignment

    final_scores = dict(sheet.ability_scores)   # post-racial-modifier values
    sv           = sheet.saving_throws

    # ── Starting gold ──────────────────────────────────────────────────────
    import random as _random
    if starting_gold > 0:
        gold = starting_gold
    else:
        gold_dice = {
            "Fighter":    (3, 6),
            "Cleric":     (3, 6),
            "Magic-User": (2, 4),
            "Thief":      (2, 6),
        }
        n, sides = gold_dice.get(character_class, (3, 6))
        gold = sum(_random.randint(1, sides) for _ in range(n)) * 10

    # ── Resolve DB path ────────────────────────────────────────────────────
    slug    = _re.sub(r"[^a-z0-9_]", "_", name.lower().strip()).strip("_")
    db_path = _ROOT / "saves" / f"{slug}.db"

    if db_path.exists():
        raise ValueError(
            f"saves/{slug}.db already exists. "
            "Choose a different name or delete the existing file first."
        )

    db_path.parent.mkdir(exist_ok=True)

    # ── Create DB and write character ──────────────────────────────────────
    _bootstrap_new_db(db_path)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        conn.execute(
            "INSERT INTO campaigns (campaign_id, name, setting, notes) "
            "VALUES (1, ?, ?, NULL)",
            (f"{name} Campaign", "World of Greyhawk, 576 CY"),
        )
        conn.execute(
            "INSERT INTO characters "
            "(character_id, campaign_id, name, character_type, race, alignment, notes) "
            "VALUES (1, 1, ?, 'PC', ?, ?, NULL)",
            (name, race, alignment or "Unaligned"),
        )
        conn.execute(
            "INSERT INTO class_levels (character_id, class_name, level, xp) "
            "VALUES (1, ?, 1, 0)",
            (character_class,),
        )

        con_mod = _CON_TABLE.get(final_scores["con"], (0,))[0]
        dex_ac  = _DEX_TABLE.get(final_scores["dex"], (0, 0))[1]

        conn.execute(
            "INSERT INTO character_status "
            "(character_id, hp_current, hp_max, ac, movement, attacks_per_round, status_notes) "
            "VALUES (1, ?, ?, ?, '12\"', '1', ?)",
            (
                sheet.hp["max"],
                sheet.hp["max"],
                sheet.ac,
                f"Level 1 {character_class}, unarmored",
            ),
        )
        conn.execute(
            "INSERT INTO character_abilities "
            "(character_id, strength, intelligence, wisdom, "
            " dexterity, constitution, charisma) "
            "VALUES (1, ?, ?, ?, ?, ?, ?)",
            (
                final_scores["str"], final_scores["int"], final_scores["wis"],
                final_scores["dex"], final_scores["con"], final_scores["cha"],
            ),
        )
        conn.execute(
            "INSERT INTO locations "
            "(campaign_id, name, location_type, parent_location_id, status, notes) "
            "VALUES (1, 'Starting Location', 'Town', NULL, 'Active', "
            "'Rename with update_location_status once you have a home base')",
        )
        conn.execute(
            "INSERT INTO treasury_accounts "
            "(campaign_id, account_name, location_id, gp, sp, cp, pp, "
            " gems_gp_value, notes) "
            "VALUES (1, ?, 1, ?, 0, 0, 0, 0, 'Starting funds')",
            (f"{name} Treasury", gold),
        )
        conn.commit()
    finally:
        conn.close()

    # ── Update config.json to activate the new campaign ───────────────────
    config_path = _ROOT / "config.json"
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8")) \
              if config_path.exists() else {}
        cfg["active_campaign_db"] = f"saves/{slug}.db"
        config_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    except Exception as e:
        pass  # Non-fatal — character is written regardless

    # ── Return full character sheet ────────────────────────────────────────
    xp_table    = classes_data[character_class].get("xp_table", [])
    xp_next     = xp_table[1] if len(xp_table) > 1 else 0
    hit_die     = classes_data[character_class].get("hit_die", "d?")

    racial_mods = races_data[race].get("ability_modifiers", {})

    return {
        "created":    True,
        "db_path":    f"saves/{slug}.db",
        "name":       name,
        "race":       race,
        "class":      character_class,
        "level":      1,
        "alignment":  alignment or "Unaligned",
        "hp":         sheet.hp["max"],
        "hp_rolled":  sheet._hp_rolls[0] if sheet._hp_rolls else sheet.hp["max"],
        "hit_die":    hit_die,
        "con_hp_mod": con_mod,
        "ac":         sheet.ac,
        "dex_ac_mod": dex_ac,
        "thac0":      sheet.thac0,
        "xp":         0,
        "xp_next_level": xp_next,
        "starting_gold": gold,
        "ability_scores": {
            "str": final_scores["str"],
            "int": final_scores["int"],
            "wis": final_scores["wis"],
            "dex": final_scores["dex"],
            "con": final_scores["con"],
            "cha": final_scores["cha"],
        },
        "racial_modifiers_applied": racial_mods,
        "saving_throws": {
            "death":     sv["death"],
            "wands":     sv["wands"],
            "paralysis": sv["paralysis"],
            "breath":    sv["breath"],
            "spells":    sv["spells"],
        },
        "note": (
            "config.json updated — restart Claude Desktop to activate "
            f"saves/{slug}.db, then say: "
            f"'Start a new campaign with {name}. Load my character state.'"
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
# READ — PENDING UPDATES
# ══════════════════════════════════════════════════════════════════════════════

def get_pending_updates(limit: int = 30) -> list[dict]:
    """
    Return recent turns where save_turn was called with scene_notes
    (stored in structured_response_json as {"state_changes": "..."}). These
    represent gameplay events that may need to be committed to the DB via the
    write tools. Returns turns ordered newest-first, up to `limit`.
    """
    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT turn_id, player_action, created_at,
                   structured_response_json
            FROM ai_turns
            WHERE structured_response_json IS NOT NULL
              AND structured_response_json LIKE '%state_changes%'
            ORDER BY turn_id DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = [dict(r) for r in cur.fetchall()]

    results = []
    for row in rows:
        try:
            blob = json.loads(row["structured_response_json"])
        except (json.JSONDecodeError, TypeError):
            blob = {}
        results.append({
            "turn_id":        row["turn_id"],
            "created_at":     row["created_at"],
            "player_action":  row["player_action"][:120],
            "state_changes":  blob.get("state_changes", ""),
            "location":       blob.get("location", ""),
        })

    return results


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 2 — COMBAT STATE
# ══════════════════════════════════════════════════════════════════════════════

# OSRIC/AD&D 1e base XP by monster HD (no HP bonus — simplified for solo play)
_XP_BY_HD: dict[int, int] = {
    0: 5, 1: 10, 2: 20, 3: 35, 4: 75, 5: 175,
    6: 275, 7: 450, 8: 650, 9: 900, 10: 900,
}


def _xp_for_hd(hd: float) -> int:
    """Return base XP for a monster with the given effective HD."""
    key = int(hd)
    if key <= 0:
        return _XP_BY_HD[0]
    if key in _XP_BY_HD:
        return _XP_BY_HD[key]
    return 1100 + (key - 11) * 100   # 11+ HD


def _parse_monster_hd(hd_text: str) -> tuple[int, int, int]:
    """
    Parse a monster HD string → (num_dice, die_sides, bonus).
    "3"   → (3, 8, 0)    "3+1" → (3, 8, 1)    "3-1" → (3, 8, -1)
    "½"   → (1, 4, 0)    "1/2" → (1, 4, 0)
    """
    text = hd_text.strip()
    if text in ("½", "1/2", "0.5"):
        return (1, 4, 0)
    m = re.match(r"^(\d+)\s*([+-]\s*\d+)?$", text)
    if m:
        num   = int(m.group(1))
        bonus = int(m.group(2).replace(" ", "")) if m.group(2) else 0
        return (num, 8, bonus)
    try:
        return (max(1, int(float(text))), 8, 0)
    except ValueError:
        return (1, 8, 0)


def _roll_monster_hp(hd_text: str) -> tuple[int, float]:
    """Roll HP for one monster instance. Returns (hp, effective_hd)."""
    num, sides, bonus = _parse_monster_hd(hd_text)
    rolls  = [random.randint(1, sides) for _ in range(max(1, num))]
    hp     = max(1, sum(rolls) + bonus)
    eff_hd = max(0.5, num + bonus / max(sides, 1))
    return (hp, eff_hd)


def _roll_damage(damage_text: str) -> list[int]:
    """
    Parse and roll AD&D damage notation.
    "1-8"         → [randint(1,8)]
    "1-3/1-3/2-5" → three separate rolls
    "2d6"         → sum of 2d6
    """
    results = []
    for part in damage_text.strip().split("/"):
        part = part.strip()
        m = re.match(r"^(\d+)-(\d+)$", part)
        if m:
            results.append(random.randint(int(m.group(1)), int(m.group(2))))
            continue
        m2 = re.match(r"^(\d+)[dD](\d+)([+-]\d+)?$", part)
        if m2:
            n, s = int(m2.group(1)), int(m2.group(2))
            mod  = int(m2.group(3)) if m2.group(3) else 0
            results.append(max(0, sum(random.randint(1, s) for _ in range(n)) + mod))
            continue
        try:
            results.append(int(part))
        except ValueError:
            results.append(1)
    return results or [1]


# Fighter-best ordering for multi-class attack matrix selection
_CLASS_MATRIX_PRIORITY = [
    ("fighter",    "fighter_matrix"),
    ("ranger",     "fighter_matrix"),
    ("paladin",    "fighter_matrix"),
    ("bard",       "fighter_matrix"),
    ("thief",      "thief_matrix"),
    ("assassin",   "thief_matrix"),
    ("cleric",     "cleric_matrix"),
    ("druid",      "cleric_matrix"),
    ("monk",       "cleric_matrix"),
    ("magic-user", "magic_user_matrix"),
    ("magic_user", "magic_user_matrix"),
    ("illusionist","magic_user_matrix"),
]


def get_active_combat() -> dict | None:
    """Return the active combat state dict, or None if no combat is running."""
    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT fact_text FROM world_facts "
            "WHERE campaign_id = ? AND category = 'active_combat' LIMIT 1",
            (_CAMPAIGN_ID,),
        )
        row = cur.fetchone()
    if not row:
        return None
    try:
        return json.loads(row["fact_text"])
    except (json.JSONDecodeError, TypeError):
        return None


def set_active_combat(state: dict) -> None:
    """Persist the combat state dict to world_facts (replaces previous)."""
    with _get_conn() as conn:
        conn.execute(
            "DELETE FROM world_facts "
            "WHERE campaign_id = ? AND category = 'active_combat'",
            (_CAMPAIGN_ID,),
        )
        conn.execute(
            "INSERT INTO world_facts "
            "(campaign_id, category, fact_text, source_note) "
            "VALUES (?, 'active_combat', ?, 'combat_tracker')",
            (_CAMPAIGN_ID, json.dumps(state)),
        )


def clear_active_combat() -> None:
    """Delete the active combat state from world_facts."""
    with _get_conn() as conn:
        conn.execute(
            "DELETE FROM world_facts "
            "WHERE campaign_id = ? AND category = 'active_combat'",
            (_CAMPAIGN_ID,),
        )


def lookup_monster(name: str) -> dict:
    """
    Look up a monster by exact then prefix match (case-insensitive).
    Returns the monster row as a dict, or {} if not found.
    """
    cols = (
        "monster_id, name, armor_class, hit_dice, damage, "
        "number_of_attacks, special_attacks, special_defenses, "
        "intelligence, alignment, treasure_type, description, notes"
    )
    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT {cols} FROM monsters "
            "WHERE LOWER(name) = LOWER(?) LIMIT 1",
            (name,),
        )
        row = cur.fetchone()
        if not row:
            cur.execute(
                f"SELECT {cols} FROM monsters "
                "WHERE LOWER(name) LIKE LOWER(?) LIMIT 1",
                (f"{name}%",),
            )
            row = cur.fetchone()
    return _row_to_dict(row) if row else {}


def get_attack_target_roll(matrix_code: str, level: int, target_ac: int) -> int:
    """
    Return the minimum d20 roll needed to hit target_ac for an attacker
    using matrix_code at the given level. Falls back to THAC0 formula.
    """
    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT target_roll FROM combat_attack_matrix_entries "
            "WHERE matrix_code = ? AND level_min <= ? "
            "AND (level_max IS NULL OR level_max >= ?) "
            "AND armor_class = ? LIMIT 1",
            (matrix_code, level, level, target_ac),
        )
        row = cur.fetchone()
    if row:
        return row["target_roll"]
    # Fallback: THAC0 formula (fighter-paced)
    thac0 = max(6, 20 - (level - 1))
    return max(1, thac0 - target_ac)


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 2 — SPELL MEMORY
# ══════════════════════════════════════════════════════════════════════════════

def get_spell_memory() -> dict:
    """Return the current spell memory state, or an empty default."""
    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT fact_text FROM world_facts "
            "WHERE campaign_id = ? AND category = 'spell_memory' LIMIT 1",
            (_CAMPAIGN_ID,),
        )
        row = cur.fetchone()
    if not row:
        return {"memorized": [], "last_rest": None}
    try:
        return json.loads(row["fact_text"])
    except (json.JSONDecodeError, TypeError):
        return {"memorized": [], "last_rest": None}


def set_spell_memory(state: dict) -> None:
    """Persist the spell memory state to world_facts (replaces previous)."""
    with _get_conn() as conn:
        conn.execute(
            "DELETE FROM world_facts "
            "WHERE campaign_id = ? AND category = 'spell_memory'",
            (_CAMPAIGN_ID,),
        )
        conn.execute(
            "INSERT INTO world_facts "
            "(campaign_id, category, fact_text, source_note) "
            "VALUES (?, 'spell_memory', ?, 'spell_system')",
            (_CAMPAIGN_ID, json.dumps(state)),
        )


def lookup_spell(spell_name: str, class_name: str | None = None) -> dict:
    """
    Exact then partial name match (case-insensitive).
    class_name filters to a specific class ('magic_user', 'cleric', etc.).
    Returns the spell row dict, or {} if not found.
    """
    cols = (
        "spell_id, name, class_name, spell_level, school, "
        "range_text, duration, area_of_effect, components, casting_time, "
        "saving_throw, summary_text, combat_use_text, utility_use_text, description"
    )
    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        if class_name:
            cur.execute(
                f"SELECT {cols} FROM spells "
                "WHERE LOWER(name) = LOWER(?) "
                "AND LOWER(class_name) = LOWER(?) LIMIT 1",
                (spell_name, class_name),
            )
        else:
            cur.execute(
                f"SELECT {cols} FROM spells "
                "WHERE LOWER(name) = LOWER(?) LIMIT 1",
                (spell_name,),
            )
        row = cur.fetchone()
        if not row:
            like = f"%{spell_name}%"
            if class_name:
                cur.execute(
                    f"SELECT {cols} FROM spells "
                    "WHERE LOWER(name) LIKE LOWER(?) "
                    "AND LOWER(class_name) = LOWER(?) LIMIT 1",
                    (like, class_name),
                )
            else:
                cur.execute(
                    f"SELECT {cols} FROM spells "
                    "WHERE LOWER(name) LIKE LOWER(?) LIMIT 1",
                    (like,),
                )
            row = cur.fetchone()
    return _row_to_dict(row) if row else {}


def get_spells_for_class(
    class_name: str,
    spell_level: int | None = None,
) -> list[dict]:
    """Return all spells for a class, optionally filtered to a spell level."""
    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        if spell_level is not None:
            cur.execute(
                "SELECT spell_id, name, class_name, spell_level, school, "
                "range_text, duration, saving_throw, summary_text "
                "FROM spells "
                "WHERE LOWER(class_name) = LOWER(?) AND spell_level = ? "
                "ORDER BY name",
                (class_name, spell_level),
            )
        else:
            cur.execute(
                "SELECT spell_id, name, class_name, spell_level, school, "
                "range_text, duration, saving_throw, summary_text "
                "FROM spells "
                "WHERE LOWER(class_name) = LOWER(?) "
                "ORDER BY spell_level, name",
                (class_name,),
            )
        return [dict(r) for r in cur.fetchall()]


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 3 — DUNGEON SYSTEM
# Random encounters · Wandering monster checks · Treasure generation
# ══════════════════════════════════════════════════════════════════════════════

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _roll_number_appearing(text: str) -> int:
    """Parse and roll a number-appearing string: '1-4', '6-18', '1', etc."""
    text = text.strip()
    m = re.match(r"^(\d+)-(\d+)$", text)
    if m:
        return random.randint(int(m.group(1)), int(m.group(2)))
    try:
        return max(1, int(float(text)))
    except ValueError:
        return 1


# Base GP values keyed by gem_base_value item_name
_GEM_BASE_GP: dict[str, int] = {
    "Ornamental Stones":       10,
    "Semi-precious Stones":    50,
    "Fancy Stones":           100,
    "Fancy Stones (Precious)": 500,
    "Gem Stones":            1000,
    "Gem Stones (Jewels)":   5000,
}

# GP value ranges for jewelry types (lo, hi)
_JEWELRY_RANGES: dict[str, tuple[int, int]] = {
    "Ivory or wrought silver":         (100,  1000),
    "Wrought silver and gold":         (200,  1200),
    "Wrought gold":                    (300,  1800),
    "Jade, coral or wrought platinum": (500,  3000),
    "Silver with gems":               (1000,  6000),
    "Gold with gems":                 (2000,  8000),
    "Platinum with gems":             (2000, 12000),
}

# Maps magic_item_category_determination item_name → subtable name
_MAGIC_CATEGORY_SUBTABLE: dict[str, str] = {
    "Potions (A.)":               "potions_01_65",
    "Scrolls (B.)":               "scrolls_01_85",
    "Rings (C.)":                 "rings_01_00",
    "Rods, Staves & Wands (D.)":  "rods_staves_wands_01_00",
    "Miscellaneous Magic (E.1.)": "misc_magic_e1_01_00",
    "Miscellaneous Magic (E.2.)": "misc_magic_e2_01_00",
    "Miscellaneous Magic (E.3.)": "misc_magic_e3_01_00",
    "Miscellaneous Magic (E.4.)": "misc_magic_e4_01_00",
    "Miscellaneous Magic (E.5.)": "misc_magic_e5_01_00",
    "Armor & Shields (F.)":       "armor_shield_01_00",
    "Swords (G.)":                "swords_01_95",
    "Miscellaneous Weapons (H.)": "misc_weapons_01_00",
}


def _roll_one_gem() -> dict:
    """Roll one gem on the gem_base_value table. Returns {type, gp_value, roll}."""
    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT roll_low, roll_high, item_name, gp_value_text "
            "FROM adnd_1e_gems_jewelry WHERE subtable_name='gem_base_value' "
            "ORDER BY roll_low"
        )
        table = cur.fetchall()

    roll     = random.randint(1, 100)
    gem_type = "Ornamental Stones"
    gp_text  = "10 g.p. each"
    for row in table:
        if row["roll_low"] <= roll <= row["roll_high"]:
            gem_type = row["item_name"]
            gp_text  = row["gp_value_text"] or ""
            break

    # Parse "1,000 g.p. each" → 1000
    gp_m   = re.search(r"([\d,]+)\s*g\.?p\.", gp_text.replace(",", ""))
    gp_val = int(gp_m.group(1)) if gp_m else _GEM_BASE_GP.get(gem_type, 10)
    return {"type": gem_type, "gp_value": gp_val, "roll": roll}


def _roll_one_jewelry() -> dict:
    """Roll one jewelry piece on the jewelry_base_value table."""
    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT roll_low, roll_high, item_name, gp_value_text "
            "FROM adnd_1e_gems_jewelry WHERE subtable_name='jewelry_base_value' "
            "ORDER BY roll_low"
        )
        table = cur.fetchall()

    roll      = random.randint(1, 100)
    item_name = "Ivory or wrought silver"
    gp_text   = "100-1,000 g.p."
    for row in table:
        if row["roll_low"] <= roll <= row["roll_high"]:
            item_name = row["item_name"]
            gp_text   = row["gp_value_text"] or ""
            break

    # Parse range "100-1,000 g.p." or flat "500 g.p."
    rng_m = re.search(r"([\d,]+)-([\d,]+)", gp_text.replace(",", ""))
    if rng_m:
        gp_val = random.randint(int(rng_m.group(1)), int(rng_m.group(2)))
    else:
        flat_m     = re.search(r"([\d,]+)\s*g\.?p\.", gp_text.replace(",", ""))
        lo, hi     = _JEWELRY_RANGES.get(item_name, (100, 1000))
        gp_val     = int(flat_m.group(1)) if flat_m else random.randint(lo, hi)

    return {"type": item_name, "gp_value": gp_val, "roll": roll}


def _roll_one_magic_item(category_roll: int | None = None) -> dict:
    """
    Roll one magic item.
    1) Roll on magic_item_category_determination (d100).
    2) Roll on the corresponding subtable (d100).
    """
    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()

        if category_roll is None:
            category_roll = random.randint(1, 100)

        cur.execute(
            "SELECT item_name FROM adnd_1e_magic_item_subtables "
            "WHERE subtable_name='magic_item_category_determination' "
            "AND roll_low <= ? AND roll_high >= ? LIMIT 1",
            (category_roll, category_roll),
        )
        cat_row  = cur.fetchone()
        category = cat_row["item_name"] if cat_row else "Potions (A.)"

        subtable  = _MAGIC_CATEGORY_SUBTABLE.get(category, "potions_01_65")
        item_roll = random.randint(1, 100)
        cur.execute(
            "SELECT item_name, xp_value_text, gp_value_text, charges_text "
            "FROM adnd_1e_magic_item_subtables "
            "WHERE subtable_name = ? AND roll_low <= ? AND roll_high >= ? LIMIT 1",
            (subtable, item_roll, item_roll),
        )
        item_row = cur.fetchone()

    item_name = (item_row["item_name"] if item_row else "Potion of Healing")
    gp_text   = ((item_row["gp_value_text"] or "") if item_row else "")
    xp_text   = ((item_row["xp_value_text"] or "") if item_row else "")

    gp_m   = re.search(r"([\d,]+)", gp_text.replace(",", ""))
    gp_val = int(gp_m.group(1)) if gp_m else 0

    return {
        "name":          item_name,
        "category":      category,
        "gp_value":      gp_val,
        "xp_value":      xp_text,
        "category_roll": category_roll,
        "item_roll":     item_roll,
    }


def _parse_maps_or_magic(text: str) -> list[dict]:
    """
    Parse and roll a treasure type's maps_or_magic field.

    Recognized patterns (with % chance check):
      "Any 3: 30%"                → 3 random magic items
      "Any 2 plus 1 potion: 15%" → 2 random + 1 potion
      "Any 3 plus 1 scroll: 25%" → 3 random + 1 scroll
      "Sword, armor, or misc. weapon: 10%" → 1 weapon/armor/sword item
    """
    text = text.strip()
    if not text or text.lower() == "nil":
        return []

    chance_m = re.search(r":?\s*(\d+)%", text)
    if not chance_m:
        return []
    chance = int(chance_m.group(1))
    if random.randint(1, 100) > chance:
        return []

    items: list[dict] = []

    any_m = re.search(r"Any\s+(\d+)", text, re.IGNORECASE)
    if any_m:
        count = int(any_m.group(1))
        for _ in range(count):
            items.append(_roll_one_magic_item())
        # "plus 1 potion" / "plus 1 scroll"
        if re.search(r"\bpotion\b", text, re.IGNORECASE):
            items.append(_roll_one_magic_item(random.randint(1, 20)))
        elif re.search(r"\bscroll\b", text, re.IGNORECASE):
            items.append(_roll_one_magic_item(random.randint(21, 35)))
    elif re.search(r"\b(sword|armor|weapon)\b", text, re.IGNORECASE):
        cat_roll = random.choice([
            random.randint(61, 75),   # Armor & Shields
            random.randint(76, 86),   # Swords
            random.randint(87, 100),  # Misc Weapons
        ])
        items.append(_roll_one_magic_item(cat_roll))
    else:
        items.append(_roll_one_magic_item())

    return items


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_random_dungeon_encounter(dungeon_level: int) -> dict:
    """
    Roll a random dungeon encounter for the given dungeon level.

    1. Roll d20 → look up monster_level_table from dungeon_random_monster_level_matrix.
    2. Roll d100 → look up result_name + number_appearing from
       dungeon_random_monster_table_entries.
    3. Roll number_appearing.
    4. Look up monster stats via lookup_monster().

    Returns a full dict ready for start_combat or narrative use.
    """
    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()

        d20 = random.randint(1, 20)
        cur.execute(
            "SELECT monster_level_table FROM dungeon_random_monster_level_matrix "
            "WHERE dungeon_level_min <= ? "
            "  AND (dungeon_level_max IS NULL OR dungeon_level_max >= ?) "
            "  AND roll_min <= ? AND roll_max >= ? LIMIT 1",
            (dungeon_level, dungeon_level, d20, d20),
        )
        matrix_row = cur.fetchone()

        if not matrix_row:
            # Fall back to the highest defined level band
            cur.execute(
                "SELECT monster_level_table FROM dungeon_random_monster_level_matrix "
                "WHERE dungeon_level_min <= ? "
                "ORDER BY dungeon_level_min DESC LIMIT 1",
                (dungeon_level,),
            )
            matrix_row = cur.fetchone()

        table_code = (matrix_row["monster_level_table"] if matrix_row else "I")

        d100 = random.randint(1, 100)
        cur.execute(
            "SELECT result_name, number_appearing_text, branch_type, notes "
            "FROM dungeon_random_monster_table_entries "
            "WHERE monster_level_table = ? AND roll_min <= ? AND roll_max >= ? LIMIT 1",
            (table_code, d100, d100),
        )
        entry = cur.fetchone()

    if not entry:
        return {
            "monster_name":          "Skeleton",
            "count":                 1,
            "monster_level_table":   table_code,
            "d20_roll":              d20,
            "d100_roll":             d100,
            "branch_type":           "monster",
            "number_appearing_text": "1",
            "notes":                 "Fallback — no table entry matched.",
            "monster_stats":         {},
        }

    result_name        = entry["result_name"]
    branch_type        = entry["branch_type"] or "monster"
    notes              = entry["notes"] or ""
    num_appearing_text = entry["number_appearing_text"] or "1"

    # Roll count (human/subtable branches don't have a simple die range)
    if branch_type in ("human", "subtable") or "see" in num_appearing_text.lower():
        count = 1
    else:
        count = _roll_number_appearing(num_appearing_text)

    monster_stats = lookup_monster(result_name) if branch_type == "monster" else {}

    return {
        "monster_name":          result_name,
        "count":                 count,
        "monster_level_table":   table_code,
        "dungeon_level":         dungeon_level,
        "d20_roll":              d20,
        "d100_roll":             d100,
        "branch_type":           branch_type,
        "number_appearing_text": num_appearing_text,
        "notes":                 notes,
        "monster_stats":         monster_stats,
    }


def roll_treasure_by_type(treasure_type: str) -> dict:
    """
    Roll a complete AD&D 1e treasure haul for the given treasure type letter (A–Z).

    Each component is checked independently:
      - Coins  (cp/sp/ep/gp/pp): chance% roll, then qty × multiplier
      - Gems   : chance% roll, then count gems typed from gem_base_value
      - Jewelry: chance% roll, then count pieces valued from jewelry_base_value
      - Magic  : maps_or_magic text parsed for chance, item count, and category

    Returns itemised results plus total_gp_value (rough GP equivalent of all loot).
    """
    treasure_type = treasure_type.upper().strip()

    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM treasure_types WHERE treasure_type = ? LIMIT 1",
            (treasure_type,),
        )
        row = cur.fetchone()

    if not row:
        return {"error": f"Unknown treasure type '{treasure_type}'. Valid: A–Z (not all types exist)."}

    result: dict = {
        "treasure_type":  treasure_type,
        "coins":          {},
        "gems":           [],
        "jewelry":        [],
        "magic_items":    [],
        "total_gp_value": 0.0,
        "rolls":          {},
    }
    total_gp = 0.0

    gp_rate = {"cp": 0.01, "sp": 0.1, "ep": 0.5, "gp": 1.0, "pp": 5.0}

    # ── Coins ──────────────────────────────────────────────────────────────────
    coin_fields: list[tuple[str, int, str]] = [
        ("copper_1000s",   1000, "cp"),
        ("silver_1000s",   1000, "sp"),
        ("electrum_1000s", 1000, "ep"),
        ("gold_1000s",     1000, "gp"),
        ("platinum_100s",   100, "pp"),
    ]
    for field_name, multiplier, coin_type in coin_fields:
        field_text = (row[field_name] or "").strip()
        if not field_text or field_text.lower() == "nil":
            continue
        m = re.match(r"^(\d+-\d+|\d+):(\d+)%", field_text)
        if not m:
            continue
        chance      = int(m.group(2))
        chance_roll = random.randint(1, 100)
        result["rolls"][f"{coin_type}_chance"] = chance_roll
        if chance_roll > chance:
            continue
        qty         = _roll_number_appearing(m.group(1))
        result["rolls"][f"{coin_type}_qty"] = qty
        total_coins = qty * multiplier
        result["coins"][coin_type] = total_coins
        total_gp   += total_coins * gp_rate[coin_type]

    # ── Gems ───────────────────────────────────────────────────────────────────
    gems_text = (row["gems"] or "").strip()
    if gems_text and gems_text.lower() != "nil":
        m = re.match(r"^(\d+-\d+|\d+):(\d+)%", gems_text)
        if m:
            chance      = int(m.group(2))
            chance_roll = random.randint(1, 100)
            result["rolls"]["gems_chance"] = chance_roll
            if chance_roll <= chance:
                count = _roll_number_appearing(m.group(1))
                result["rolls"]["gems_count"] = count
                gems    = [_roll_one_gem() for _ in range(count)]
                result["gems"] = gems
                total_gp += sum(g["gp_value"] for g in gems)

    # ── Jewelry ────────────────────────────────────────────────────────────────
    jewelry_text = (row["jewelry"] or "").strip()
    if jewelry_text and jewelry_text.lower() != "nil":
        m = re.match(r"^(\d+-\d+|\d+):(\d+)%", jewelry_text)
        if m:
            chance      = int(m.group(2))
            chance_roll = random.randint(1, 100)
            result["rolls"]["jewelry_chance"] = chance_roll
            if chance_roll <= chance:
                count = _roll_number_appearing(m.group(1))
                result["rolls"]["jewelry_count"] = count
                jewelry  = [_roll_one_jewelry() for _ in range(count)]
                result["jewelry"] = jewelry
                total_gp += sum(j["gp_value"] for j in jewelry)

    # ── Maps / Magic ───────────────────────────────────────────────────────────
    maps_text = (row["maps_or_magic"] or "").strip()
    if maps_text and maps_text.lower() != "nil":
        magic_items = _parse_maps_or_magic(maps_text)
        result["magic_items"] = magic_items
        total_gp   += sum(item.get("gp_value", 0) for item in magic_items)

    result["total_gp_value"] = round(total_gp, 2)
    return result


def get_dungeon_turn_count() -> int:
    """Return the current dungeon turn counter stored in world_facts."""
    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT fact_text FROM world_facts "
            "WHERE campaign_id = ? AND category = 'dungeon_turns' LIMIT 1",
            (_CAMPAIGN_ID,),
        )
        row = cur.fetchone()
    try:
        return int(row["fact_text"]) if row else 0
    except (ValueError, TypeError):
        return 0


def increment_dungeon_turn() -> int:
    """Increment and persist the dungeon turn counter. Returns new count."""
    count = get_dungeon_turn_count() + 1
    with _get_conn() as conn:
        conn.execute(
            "DELETE FROM world_facts WHERE campaign_id = ? AND category = 'dungeon_turns'",
            (_CAMPAIGN_ID,),
        )
        conn.execute(
            "INSERT INTO world_facts (campaign_id, category, fact_text, source_note) "
            "VALUES (?, 'dungeon_turns', ?, 'dungeon_system')",
            (_CAMPAIGN_ID, str(count)),
        )
    return count


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 4 — DOMAIN MANAGEMENT
# Domain turns · Income · Upkeep · Construction · Realm Events
# ══════════════════════════════════════════════════════════════════════════════

# ---------------------------------------------------------------------------
# Income rate table  (gp per month, low–high; rolled as randint(lo, hi))
# ---------------------------------------------------------------------------
_HOLDING_INCOME_RATE: dict[str, tuple[int, int]] = {
    # Calibrated so active holdings sum to ~1,190 gp/month (matches world_facts)
    "Keep":                      (120, 220),
    "Farm/Hub":                  (45,   85),
    "Mill/Granary":              (55,  105),
    "Estate/Gardens":            (25,   55),
    "Tower/Keep":                (15,   35),
    "District":                  (70,  130),
    "Inn/Lodge":                 (25,   55),
    "Lodge":                     (15,   35),
    "Workshop":                  (25,   55),
    "Workshop Yard":             (20,   45),
    "Food Works":                (30,   60),
    "Stable/Remount":            (20,   40),
    "Fortified Mill":            (40,   80),
    "Civic Building":            (10,   30),
    "School":                    (0,    20),
    "Archive/Chancery":          (0,    10),
    "Practice Yard/Fairground":  (10,   25),
    "Shrine/Manor":              (5,    20),
    "Settlement":                (80,  180),   # allied settlement
    "City":                      (150, 350),   # Greenreach cities
    "Default":                   (10,   30),
}

# ---------------------------------------------------------------------------
# Troop upkeep  (gp per month per individual)
# ---------------------------------------------------------------------------
_TROOP_UPKEEP_GP: dict[str, int] = {
    "Goblins":       1,
    "Hobgoblins":    2,
    "Orcs":          1,
    "Human Troops":  3,
    "Human Soldiers": 3,
    "Mounted Humans": 6,   # horse upkeep included
    "Halflings":     2,
    "Elves":         5,
    "Dwarves":       4,
    "Gnomes":        4,
    "Ogres":         15,   # they eat a lot
    "Laborers":      1,
    "Constructs":    0,    # no food
    "Default":       3,
}

# ---------------------------------------------------------------------------
# Realm event table  (d20)
# Each entry: (title, description, mechanical_key)
# mechanical_key drives the mcp tool's effect application
# ---------------------------------------------------------------------------
_REALM_EVENTS: list[tuple[int, str, str, str]] = [
    # roll, title, description, mechanical_key
    (1,  "Exceptional Harvest",
         "Bumper crops across the realm's farms and granaries. Peasants are cheerful.",
         "income_bonus_d6x50"),
    (2,  "Good Harvest",
         "A solid growing season. Stores are full and traders are active.",
         "income_bonus_d4x30"),
    (3,  "Trade Windfall",
         "A merchant caravan chose your realm as a hub. Unexpected coin flows in.",
         "income_bonus_d6x30"),
    (4,  "New Settlers Arrive",
         "A band of families seeks safety behind your walls. Labor pool increases.",
         "gain_d4_laborers"),
    (5,  "Religious Festival",
         "A traveling cleric declares a holy day. Donations arrive; morale rises.",
         "income_bonus_d4x25"),
    (6,  "Skilled Craftsmen Offer Service",
         "A group of artisans seeks patronage. Construction projects may accelerate.",
         "construction_speed_up_2_weeks"),
    (7,  "Mercenary Company Passes Through",
         "A mercenary band offers short-term service at 1.5× normal rate.",
         "mercenary_offer"),
    (8,  "Diplomatic Overture",
         "A neighboring power sends an envoy. Relations may shift.",
         "narrative_only"),
    (9,  "Minor Border Dispute",
         "Settlers squabble over boundary stones with a neighboring village.",
         "narrative_only"),
    (10, "Rumour of Treasure",
         "Locals whisper of something valuable in your territory. May be true.",
         "narrative_only"),
    (11, "Harsh Weather",
         "A brutal cold snap or prolonged rain disrupts travel and commerce.",
         "income_penalty_10pct"),
    (12, "Bandit Activity",
         "A bandit gang has been raiding caravans on the realm's roads.",
         "income_loss_d6x20"),
    (13, "Monster Raid",
         "A monster pack strikes an outlying holding before the garrison can respond.",
         "income_loss_d4x50_and_location_damaged"),
    (14, "Plague or Sickness",
         "A fever sweeps through a garrison. Some troops fall ill.",
         "lose_d4_troops_random_unit"),
    (15, "Crop Failure",
         "Blight strikes part of the realm's farmland. Food stores take a hit.",
         "income_loss_d4x40"),
    (16, "Spy Uncovered",
         "An enemy agent is found passing information to a rival. Costly to clean up.",
         "income_loss_d4x25"),
    (17, "Rival Claimant Stirs",
         "A distant noble asserts a claim to part of your territory. No troops yet.",
         "narrative_only"),
    (18, "Unrest in a District",
         "Dissatisfied tenants in one district slow their tithes.",
         "income_loss_d6x15"),
    (19, "Alliance Opportunity",
         "A minor lord sends feelers about a mutual defence pact.",
         "narrative_only"),
    (20, "Great Fortune",
         "The stars align. Roll twice and apply both results (re-roll another 20).",
         "roll_twice"),
]


# ---------------------------------------------------------------------------
# Construction queue  (world_facts category: 'construction_queue')
# Stored as JSON: {str(project_id): {weeks_total, weeks_remaining, cost_per_week}}
# ---------------------------------------------------------------------------

def _get_construction_queue() -> dict[str, dict]:
    """Load construction queue from world_facts. Returns {} if not set."""
    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT fact_text FROM world_facts "
            "WHERE campaign_id = ? AND category = 'construction_queue' LIMIT 1",
            (_CAMPAIGN_ID,),
        )
        row = cur.fetchone()
    if not row:
        return {}
    try:
        return json.loads(row["fact_text"])
    except (json.JSONDecodeError, TypeError):
        return {}


def _set_construction_queue(queue: dict[str, dict]) -> None:
    """Persist construction queue to world_facts."""
    with _get_conn() as conn:
        conn.execute(
            "DELETE FROM world_facts "
            "WHERE campaign_id = ? AND category = 'construction_queue'",
            (_CAMPAIGN_ID,),
        )
        conn.execute(
            "INSERT INTO world_facts (campaign_id, category, fact_text, source_note) "
            "VALUES (?, 'construction_queue', ?, 'domain_system')",
            (_CAMPAIGN_ID, json.dumps(queue)),
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_full_domain_state() -> dict:
    """
    Return a complete snapshot of the domain:
      - All locations with income rate and status
      - All troop groups with count and monthly upkeep
      - All treasury accounts with current balances
      - All active construction projects with weeks_remaining
      - Monthly and seasonal income/upkeep estimates
      - Last domain_turn record
    """
    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()

        # Holdings
        cur.execute(
            "SELECT location_id, name, location_type, status, notes "
            "FROM locations WHERE campaign_id = ? ORDER BY location_id",
            (_CAMPAIGN_ID,),
        )
        locations = [dict(r) for r in cur.fetchall()]

        # Troops
        cur.execute(
            "SELECT t.troop_id, t.group_name, t.troop_type, t.count, t.notes, "
            "l.name AS location_name "
            "FROM troops t LEFT JOIN locations l ON t.location_id = l.location_id "
            "WHERE t.campaign_id = ?",
            (_CAMPAIGN_ID,),
        )
        troops = [dict(r) for r in cur.fetchall()]

        # Treasury
        cur.execute(
            "SELECT ta.treasury_id, ta.account_name, ta.gp, ta.sp, ta.cp, ta.pp, "
            "ta.gems_gp_value, ta.notes, l.name AS location_name "
            "FROM treasury_accounts ta "
            "LEFT JOIN locations l ON ta.location_id = l.location_id "
            "WHERE ta.campaign_id = ?",
            (_CAMPAIGN_ID,),
        )
        treasury = [dict(r) for r in cur.fetchall()]

        # Projects
        cur.execute(
            "SELECT p.project_id, p.name, p.project_type, p.status, p.cost_gp, p.notes, "
            "l.name AS location_name "
            "FROM projects p "
            "LEFT JOIN locations l ON p.location_id = l.location_id "
            "WHERE p.campaign_id = ? ORDER BY p.project_id",
            (_CAMPAIGN_ID,),
        )
        projects = [dict(r) for r in cur.fetchall()]

        # Last domain turn
        cur.execute(
            "SELECT * FROM domain_turns WHERE campaign_id = ? "
            "ORDER BY domain_turn_id DESC LIMIT 1",
            (_CAMPAIGN_ID,),
        )
        last_turn_row = cur.fetchone()
        last_turn = dict(last_turn_row) if last_turn_row else None

    # Annotate locations with income rates
    for loc in locations:
        lo, hi = _HOLDING_INCOME_RATE.get(
            loc["location_type"],
            _HOLDING_INCOME_RATE["Default"],
        )
        loc["monthly_income_range"] = f"{lo}–{hi} gp"
        loc["income_active"] = loc["status"] in (
            "Active", "Established", "Allied – Church of Trithereon",
            "Allied – Lady Ysela", "Active – House Vale-Fingolfin",
            "Stabilized",
        ) or loc["status"].startswith("Active")

    # Annotate troops with upkeep
    monthly_upkeep = 0
    for t in troops:
        rate = _TROOP_UPKEEP_GP.get(t["troop_type"], _TROOP_UPKEEP_GP["Default"])
        t["upkeep_per_month_gp"] = rate * t["count"]
        monthly_upkeep += t["upkeep_per_month_gp"]

    # Estimate monthly income from active holdings
    monthly_income_lo = sum(
        _HOLDING_INCOME_RATE.get(l["location_type"], _HOLDING_INCOME_RATE["Default"])[0]
        for l in locations if l.get("income_active")
    )
    monthly_income_hi = sum(
        _HOLDING_INCOME_RATE.get(l["location_type"], _HOLDING_INCOME_RATE["Default"])[1]
        for l in locations if l.get("income_active")
    )

    # Merge construction queue data into projects
    queue = _get_construction_queue()
    for proj in projects:
        pid = str(proj["project_id"])
        if pid in queue:
            proj["weeks_remaining"] = queue[pid].get("weeks_remaining")
            proj["weeks_total"]     = queue[pid].get("weeks_total")
            proj["cost_per_week"]   = queue[pid].get("cost_per_week", 0)
        else:
            proj["weeks_remaining"] = None
            proj["weeks_total"]     = None
            proj["cost_per_week"]   = 0

    total_gp = sum(acc["gp"] or 0 for acc in treasury)

    return {
        "holdings":              locations,
        "troops":                troops,
        "treasury_accounts":     treasury,
        "treasury_total_gp":     total_gp,
        "projects":              projects,
        "last_domain_turn":      last_turn,
        "monthly_income_range":  f"{monthly_income_lo:,}–{monthly_income_hi:,} gp",
        "monthly_upkeep_gp":     monthly_upkeep,
        "monthly_net_range":     f"{monthly_income_lo - monthly_upkeep:,}–{monthly_income_hi - monthly_upkeep:,} gp",
    }


def db_add_construction_project(
    name: str,
    location_id: int | None,
    project_type: str,
    cost_gp: int,
    weeks_total: int,
    notes: str,
) -> dict:
    """
    Insert a new project row and register it in the construction queue.
    Returns the new project dict including its assigned project_id.
    """
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO projects (campaign_id, name, location_id, project_type, "
            "status, cost_gp, notes) VALUES (?, ?, ?, ?, 'Funded/In Progress', ?, ?)",
            (_CAMPAIGN_ID, name, location_id, project_type, cost_gp, notes),
        )
        project_id = cur.lastrowid

    # Register in construction queue
    cost_per_week = max(1, cost_gp // max(weeks_total, 1))
    queue = _get_construction_queue()
    queue[str(project_id)] = {
        "name":          name,
        "weeks_total":   weeks_total,
        "weeks_remaining": weeks_total,
        "cost_per_week": cost_per_week,
        "cost_gp":       cost_gp,
    }
    _set_construction_queue(queue)

    return {
        "project_id":    project_id,
        "name":          name,
        "project_type":  project_type,
        "location_id":   location_id,
        "cost_gp":       cost_gp,
        "weeks_total":   weeks_total,
        "weeks_remaining": weeks_total,
        "cost_per_week": cost_per_week,
        "status":        "Funded/In Progress",
        "notes":         notes,
    }


def db_collect_income(months: int = 1) -> dict:
    """
    Roll income for all active holdings for the given number of months.
    Records each entry in domain_income_expenses.
    Returns per-holding breakdown and totals.
    """
    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT location_id, name, location_type, status "
            "FROM locations WHERE campaign_id = ? ORDER BY location_id",
            (_CAMPAIGN_ID,),
        )
        locations = [dict(r) for r in cur.fetchall()]

    # Determine which accounts to credit (use treasury_id=1 as default)
    breakdown: list[dict] = []
    total_gp = 0

    income_active_statuses = {
        "Active", "Established", "Stabilized",
    }

    for loc in locations:
        # Only generate income for active/established holdings
        status = loc["status"] or ""
        is_active = (
            status in income_active_statuses
            or status.startswith("Active")
            or status.startswith("Established")
            or status.startswith("Allied")
        )
        if not is_active:
            continue

        lo, hi = _HOLDING_INCOME_RATE.get(
            loc["location_type"],
            _HOLDING_INCOME_RATE["Default"],
        )
        if hi == 0:
            continue

        month_rolls = []
        for _ in range(months):
            month_rolls.append(random.randint(lo, hi))
        holding_total = sum(month_rolls)
        total_gp += holding_total

        breakdown.append({
            "location_id":   loc["location_id"],
            "location_name": loc["name"],
            "location_type": loc["location_type"],
            "monthly_range": f"{lo}–{hi}",
            "rolls":         month_rolls,
            "total_gp":      holding_total,
        })

    # Record in ledger
    _record_ledger_entry(
        entry_type="income",
        amount_gp=total_gp,
        description=(
            f"Domain income — {months} month{'s' if months > 1 else ''} "
            f"across {len(breakdown)} active holdings"
        ),
    )

    return {
        "months":          months,
        "holdings_rolled": len(breakdown),
        "breakdown":       breakdown,
        "total_gp":        total_gp,
    }


def db_pay_upkeep(months: int = 1) -> dict:
    """
    Calculate and deduct troop and holding upkeep for the given number of months.
    Deducts from treasury_id=1 (primary account). Returns breakdown and total.
    """
    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT troop_id, group_name, troop_type, count "
            "FROM troops WHERE campaign_id = ?",
            (_CAMPAIGN_ID,),
        )
        troops = [dict(r) for r in cur.fetchall()]

    breakdown: list[dict] = []
    total_monthly = 0

    for t in troops:
        rate = _TROOP_UPKEEP_GP.get(t["troop_type"], _TROOP_UPKEEP_GP["Default"])
        monthly = rate * t["count"]
        if monthly == 0:
            continue
        total_monthly += monthly
        breakdown.append({
            "group_name":       t["group_name"],
            "troop_type":       t["troop_type"],
            "count":            t["count"],
            "gp_per_month_each": rate,
            "monthly_gp":       monthly,
        })

    # Add holding maintenance (~10% of income as rough AD&D standard)
    # We skip this for now — Claude can narrate ad-hoc maintenance costs

    total_gp = total_monthly * months

    # Deduct from primary treasury
    _deduct_treasury(total_gp)

    # Record in ledger
    _record_ledger_entry(
        entry_type="expense",
        amount_gp=total_gp,
        description=(
            f"Troop upkeep — {months} month{'s' if months > 1 else ''}, "
            f"{sum(t['count'] for t in troops)} troops total"
        ),
    )

    return {
        "months":                months,
        "troop_groups_charged":  len(breakdown),
        "breakdown":             breakdown,
        "total_monthly_upkeep":  total_monthly,
        "total_gp_charged":      total_gp,
    }


def db_roll_realm_event() -> dict:
    """
    Roll 1d20 on the realm events table.
    Returns the event with its mechanical_key for the tool layer to apply.
    """
    roll = random.randint(1, 20)
    for (r, title, desc, key) in _REALM_EVENTS:
        if r == roll:
            return {
                "roll":          roll,
                "title":         title,
                "description":   desc,
                "mechanical_key": key,
            }
    # Fallback (should never hit)
    return {
        "roll":          roll,
        "title":         "Quiet Season",
        "description":   "Nothing notable occurs. The realm rests.",
        "mechanical_key": "narrative_only",
    }


def db_create_domain_turn(turn_label: str, start_date: str = "", end_date: str = "") -> int:
    """Insert a domain_turns row. Returns the new domain_turn_id."""
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO domain_turns (campaign_id, turn_label, start_date, end_date) "
            "VALUES (?, ?, ?, ?)",
            (_CAMPAIGN_ID, turn_label, start_date or None, end_date or None),
        )
        return cur.lastrowid


def db_advance_construction(weeks: int) -> dict:
    """
    Advance all projects in the construction queue by `weeks` weeks.
    Projects that reach 0 weeks_remaining are marked 'Established/Completed'
    in the projects table and removed from the queue.

    Returns: {advanced: [...], completed: [...]}
    """
    queue = _get_construction_queue()
    advanced: list[dict] = []
    completed: list[dict] = []

    for pid_str, entry in list(queue.items()):
        prev_weeks = entry.get("weeks_remaining", 0)
        new_weeks  = max(0, prev_weeks - weeks)
        entry["weeks_remaining"] = new_weeks

        item_info = {
            "project_id":      int(pid_str),
            "name":            entry.get("name", f"Project #{pid_str}"),
            "weeks_before":    prev_weeks,
            "weeks_after":     new_weeks,
            "weeks_advanced":  min(weeks, prev_weeks),
        }

        if new_weeks == 0:
            # Mark complete in projects table
            with _get_conn() as conn:
                conn.execute(
                    "UPDATE projects SET status = 'Established/Completed' "
                    "WHERE project_id = ? AND campaign_id = ?",
                    (int(pid_str), _CAMPAIGN_ID),
                )
            del queue[pid_str]
            completed.append(item_info)
        else:
            queue[pid_str] = entry
            advanced.append(item_info)

    _set_construction_queue(queue)
    return {"advanced": advanced, "completed": completed}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _record_ledger_entry(
    entry_type: str,
    amount_gp:  int,
    description: str,
    domain_turn_id: int | None = None,
    project_id:     int | None = None,
) -> None:
    """Append a row to domain_income_expenses."""
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO domain_income_expenses "
            "(campaign_id, domain_turn_id, treasury_id, entry_type, "
            "amount_gp, description, related_project_id) "
            "VALUES (?, ?, 1, ?, ?, ?, ?)",
            (_CAMPAIGN_ID, domain_turn_id, entry_type, amount_gp,
             description, project_id),
        )


def _deduct_treasury(amount_gp: int) -> None:
    """Subtract gp from treasury_id=1 (primary account). Floor at 0."""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE treasury_accounts SET gp = MAX(0, gp - ?) "
            "WHERE treasury_id = 1 AND campaign_id = ?",
            (amount_gp, _CAMPAIGN_ID),
        )


def _credit_treasury(amount_gp: int) -> None:
    """Add gp to treasury_id=1 (primary account)."""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE treasury_accounts SET gp = gp + ? "
            "WHERE treasury_id = 1 AND campaign_id = ?",
            (amount_gp, _CAMPAIGN_ID),
        )


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 5A — TRAVEL & WEATHER SYSTEM
# Hex-crawl travel · Daily resolution · Weather generation · Getting lost
# ══════════════════════════════════════════════════════════════════════════════

# ---------------------------------------------------------------------------
# Movement rates  (miles per day, by mount type and terrain)
# Foot base: Road 24, Plains 18, Hills/Forest 12, Mountains/Swamp 6  (per spec)
# Horse multipliers per AD&D 1e PHB/DMG
# ---------------------------------------------------------------------------
_BASE_MOVE_MPD: dict[str, dict[str, int]] = {
    "foot": {
        "road": 24, "plains": 18, "hills": 12, "forest": 12,
        "mountains": 6, "swamp": 6, "marsh": 6,
    },
    "light_horse": {
        # Light horse (MV 24"): 2× foot on road/plains; same in rough terrain
        "road": 48, "plains": 36, "hills": 18, "forest": 12,
        "mountains": 6, "swamp": 4, "marsh": 4,
    },
    "heavy_horse": {
        # Heavy horse (MV 15"): 1.5× foot on road/plains; modest gain on hills
        "road": 36, "plains": 27, "hills": 15, "forest": 12,
        "mountains": 6, "swamp": 3, "marsh": 3,
    },
}

# ---------------------------------------------------------------------------
# Encounter chance per terrain  (numerator, denominator for Xd roll)
# ---------------------------------------------------------------------------
_TERRAIN_ENCOUNTER_CHANCE: dict[str, tuple[int, int]] = {
    "road":      (1, 12),   # safer — infrequent
    "plains":    (1, 6),
    "hills":     (1, 6),
    "forest":    (2, 6),    # denser, more dangerous
    "mountains": (1, 6),
    "swamp":     (2, 6),
    "marsh":     (2, 6),
}

# ---------------------------------------------------------------------------
# Getting-lost base chance (% per day)
# ---------------------------------------------------------------------------
_TERRAIN_LOST_CHANCE_PCT: dict[str, int] = {
    "road":      0,    # roads are marked
    "plains":    5,
    "hills":     15,
    "forest":    30,
    "mountains": 25,
    "swamp":     40,
    "marsh":     35,
}

# ---------------------------------------------------------------------------
# Outdoor encounter tables  (terrain → [(roll_min, roll_max, monster_name, na_text)])
# Monsters confirmed present in the DB; all roll ranges span 1–20
# ---------------------------------------------------------------------------
_OUTDOOR_ENCOUNTERS: dict[str, list[tuple[int, int, str, str]]] = {
    "road": [
        (1,  4,  "Bandit (Brigand)", "2d6"),
        (5,  7,  "Orc",              "2d6"),
        (8,  10, "Goblin",           "2d6"),
        (11, 12, "Wolf",             "1d6"),
        (13, 14, "Berserker",        "1d4"),
        (15, 16, "Dog (Wild)",       "2d4"),
        (17, 18, "Ogre",             "1d3"),
        (19, 19, "Hill Giant",       "1d2"),
        (20, 20, "Troll",            "1"),
    ],
    "plains": [
        (1,  3,  "Orc",             "2d6"),
        (4,  5,  "Goblin",          "2d6"),
        (6,  7,  "Bandit (Brigand)","2d4"),
        (8,  9,  "Wolf",            "2d4"),
        (10, 11, "Berserker",       "1d6"),
        (12, 12, "Hill Giant",      "1d3"),
        (13, 13, "Ogre",            "1d4"),
        (14, 14, "Centaur",         "2d4"),
        (15, 15, "Griffon",         "1d3"),
        (16, 16, "Harpy",           "1d4"),
        (17, 17, "Giant Boar",      "1d4"),
        (18, 18, "Boar (Wild)",     "1d6"),
        (19, 19, "Wyvern",          "1d2"),
        (20, 20, "Chimera",         "1"),
    ],
    "forest": [
        (1,  3,  "Orc",             "2d6"),
        (4,  5,  "Goblin",          "2d6"),
        (6,  7,  "Bugbear",         "1d6"),
        (8,  8,  "Troll",           "1d3"),
        (9,  9,  "Wolf",            "2d4"),
        (10, 10, "Wolf, Dire (Worg)","1d4"),
        (11, 11, "Black Bear",      "1d3"),
        (12, 12, "Brown Bear",      "1d2"),
        (13, 13, "Giant Boar",      "1d3"),
        (14, 14, "Bandit (Brigand)","2d4"),
        (15, 15, "Elf",             "2d6"),
        (16, 16, "Dryad",           "1d4"),
        (17, 17, "Treant",          "1d2"),
        (18, 18, "Green Dragon",    "1"),
        (19, 19, "Tiger",           "1d2"),
        (20, 20, "Wyvern",          "1"),
    ],
    "hills": [
        (1,  3,  "Orc",             "2d6"),
        (4,  5,  "Goblin",          "2d6"),
        (6,  7,  "Bugbear",         "1d6"),
        (8,  8,  "Ogre",            "1d4"),
        (9,  9,  "Hill Giant",      "1d3"),
        (10, 10, "Stone Giant",     "1d2"),
        (11, 11, "Troll",           "1d3"),
        (12, 12, "Gnome",           "2d6"),
        (13, 13, "Dwarf",           "2d6"),
        (14, 14, "Mountain Lion",   "1d2"),
        (15, 15, "Giant Eagle",     "1d3"),
        (16, 16, "Griffon",         "1d2"),
        (17, 17, "Wyvern",          "1"),
        (18, 18, "Berserker",       "1d6"),
        (19, 19, "Chimera",         "1"),
        (20, 20, "Black Dragon",    "1"),
    ],
    "mountains": [
        (1,  3,  "Orc",                    "2d4"),
        (4,  5,  "Bugbear",                "1d4"),
        (6,  6,  "Stone Giant",            "1d3"),
        (7,  7,  "Hill Giant",             "1d3"),
        (8,  8,  "Frost Giant",            "1d2"),
        (9,  9,  "Troll",                  "1d3"),
        (10, 10, "Ogre",                   "1d4"),
        (11, 11, "Mountain Lion",          "1d2"),
        (12, 12, "Sabre-Tooth Tiger (Smilodon)", "1"),
        (13, 13, "Wyvern",                 "1d2"),
        (14, 14, "Griffon",                "1d3"),
        (15, 15, "Roc",                    "1"),
        (16, 16, "Giant Eagle",            "1d4"),
        (17, 17, "White Dragon",           "1"),
        (18, 18, "Gargoyle",               "1d6"),
        (19, 19, "Chimera",                "1"),
        (20, 20, "Purple Worm",            "1"),
    ],
    "swamp": [
        (1,  3,  "Lizard Man",             "2d4"),
        (4,  5,  "Troll",                  "1d3"),
        (6,  7,  "Giant Frog",             "2d6"),
        (8,  8,  "Crocodile",              "2d4"),
        (9,  9,  "Giant Crocodile",        "1d2"),
        (10, 10, "Giant Centipede",        "2d6"),
        (11, 11, "Snake, Giant Constrictor","1d2"),
        (12, 12, "Snake, Giant Poisonous", "1d3"),
        (13, 13, "Killer Frog",            "2d4"),
        (14, 14, "Ghoul",                  "1d6"),
        (15, 15, "Will-O-(the)-Wisp",      "1d3"),
        (16, 16, "Black Dragon",           "1"),
        (17, 17, "Giant Crayfish",         "1d3"),
        (18, 18, "Wight",                  "1d6"),
        (19, 19, "Orc",                    "2d4"),
        (20, 20, "Bugbear",                "1d4"),
    ],
    "marsh": [
        (1,  3,  "Lizard Man",             "2d4"),
        (4,  5,  "Giant Frog",             "2d6"),
        (6,  7,  "Crocodile",              "2d4"),
        (8,  9,  "Giant Centipede",        "2d6"),
        (10, 10, "Snake, Giant Poisonous", "1d3"),
        (11, 11, "Killer Frog",            "2d4"),
        (12, 12, "Troll",                  "1d2"),
        (13, 13, "Will-O-(the)-Wisp",      "1d3"),
        (14, 14, "Giant Crayfish",         "1d3"),
        (15, 15, "Giant Crab",             "1d4"),
        (16, 16, "Poisonous Frog",         "2d4"),
        (17, 17, "Ghoul",                  "1d4"),
        (18, 18, "Black Dragon",           "1"),
        (19, 19, "Orc",                    "2d4"),
        (20, 20, "Wight",                  "1d4"),
    ],
}

# ---------------------------------------------------------------------------
# Weather tables  —  Vesve frontier region  (cold temperate, lake-effect)
# Each table: {element: [(cumulative_d100_threshold, value), ...]}
# ---------------------------------------------------------------------------

# Temperature ranges (°F) per season: (low, high)
_SEASON_TEMP_RANGE: dict[str, tuple[int, int]] = {
    "winter": (-10, 25),
    "spring": (35, 60),
    "summer": (65, 88),
    "autumn": (40, 65),
}

# Precipitation table: (cumulative_d100_threshold, condition_key)
_SEASON_PRECIP: dict[str, list[tuple[int, str]]] = {
    "winter": [
        (18, "clear"), (38, "overcast"), (55, "light_snow"),
        (70, "heavy_snow"), (82, "sleet"), (92, "blizzard"),
        (97, "ice_storm"), (100, "freezing_fog"),
    ],
    "spring": [
        (25, "clear"), (45, "partly_cloudy"), (58, "overcast"),
        (72, "light_rain"), (84, "heavy_rain"), (91, "thunderstorm"),
        (95, "light_snow"), (100, "fog"),
    ],
    "summer": [
        (35, "clear"), (55, "partly_cloudy"), (67, "overcast"),
        (77, "light_rain"), (86, "heavy_rain"), (93, "thunderstorm"),
        (97, "fog"), (100, "hail"),
    ],
    "autumn": [
        (22, "clear"), (42, "partly_cloudy"), (56, "overcast"),
        (68, "light_rain"), (80, "heavy_rain"), (88, "fog"),
        (93, "light_snow"), (97, "sleet"), (100, "thunderstorm"),
    ],
}

# Wind table: (cumulative_d100_threshold, condition_key)
_SEASON_WIND: dict[str, list[tuple[int, str]]] = {
    "winter": [(15, "calm"), (40, "light"), (65, "moderate"), (83, "strong"), (93, "gale"), (100, "storm")],
    "spring": [(25, "calm"), (55, "light"), (75, "moderate"), (90, "strong"), (97, "gale"), (100, "storm")],
    "summer": [(35, "calm"), (60, "light"), (78, "moderate"), (91, "strong"), (97, "gale"), (100, "storm")],
    "autumn": [(20, "calm"), (48, "light"), (70, "moderate"), (86, "strong"), (95, "gale"), (100, "storm")],
}

# Movement modifier per precipitation condition
_PRECIP_MOVE_MOD: dict[str, float] = {
    "clear":         1.0,
    "partly_cloudy": 1.0,
    "overcast":      1.0,
    "light_rain":    0.75,
    "heavy_rain":    0.5,
    "thunderstorm":  0.25,  # dangerous
    "fog":           0.75,
    "freezing_fog":  0.5,
    "light_snow":    0.5,
    "heavy_snow":    0.25,
    "sleet":         0.5,
    "blizzard":      0.0,   # halts travel
    "ice_storm":     0.0,
    "hail":          0.5,
}

# Additional wind movement penalty (applied on top of precip)
_WIND_MOVE_MOD: dict[str, float] = {
    "calm":     1.0,
    "light":    1.0,
    "moderate": 1.0,
    "strong":   0.9,
    "gale":     0.5,
    "storm":    0.0,
}

# Visibility (miles) per condition
_PRECIP_VISIBILITY: dict[str, float] = {
    "clear":         10.0,
    "partly_cloudy": 10.0,
    "overcast":       8.0,
    "light_rain":     3.0,
    "heavy_rain":     1.0,
    "thunderstorm":   0.5,
    "fog":            0.5,
    "freezing_fog":   0.25,
    "light_snow":     2.0,
    "heavy_snow":     0.5,
    "sleet":          1.0,
    "blizzard":       0.1,
    "ice_storm":      0.25,
    "hail":           1.0,
}

# Human-readable labels for weather conditions
_PRECIP_LABEL: dict[str, str] = {
    "clear":         "Clear",
    "partly_cloudy": "Partly Cloudy",
    "overcast":      "Overcast",
    "light_rain":    "Light Rain",
    "heavy_rain":    "Heavy Rain",
    "thunderstorm":  "Thunderstorm",
    "fog":           "Fog",
    "freezing_fog":  "Freezing Fog",
    "light_snow":    "Light Snow",
    "heavy_snow":    "Heavy Snow",
    "sleet":         "Sleet",
    "blizzard":      "Blizzard",
    "ice_storm":     "Ice Storm",
    "hail":          "Hail",
}

_WIND_LABEL: dict[str, str] = {
    "calm":     "Calm",
    "light":    "Light Breeze",
    "moderate": "Moderate Wind",
    "strong":   "Strong Wind",
    "gale":     "Gale",
    "storm":    "Storm",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _roll_from_table(table: list[tuple[int, str]]) -> str:
    """Roll d100 and return the value from the first entry whose threshold >= roll."""
    roll = random.randint(1, 100)
    for threshold, value in table:
        if roll <= threshold:
            return value
    return table[-1][1]


def _parse_terrain_path(terrain_path: str, total_miles: int) -> list[dict]:
    """
    Parse terrain path string into segment list.
    Formats:
      "road:20,plains:30,forest:10"  — explicit miles per segment
      "forest:60"                    — single segment with miles
      "forest"                       — single segment, uses total_miles
    Returns [{"terrain": ..., "miles": ..., "miles_remaining": ...}, ...]
    """
    segments: list[dict] = []
    if not terrain_path:
        return [{"terrain": "plains", "miles": total_miles, "miles_remaining": total_miles}]

    parts = [p.strip() for p in terrain_path.split(",") if p.strip()]
    explicit_total = 0
    parsed: list[tuple[str, int | None]] = []

    for part in parts:
        if ":" in part:
            name, _, miles_str = part.partition(":")
            try:
                m = int(miles_str.strip())
            except ValueError:
                m = None
        else:
            name = part
            m = None
        parsed.append((name.strip().lower().replace(" ", "_"), m))
        if m is not None:
            explicit_total += m

    # If no miles were specified, distribute total_miles equally
    if explicit_total == 0:
        per_seg = max(1, total_miles // max(len(parsed), 1))
        for name, _ in parsed:
            miles = per_seg
            segments.append({"terrain": name, "miles": miles, "miles_remaining": miles})
    else:
        for name, m in parsed:
            miles = m if m is not None else 0
            segments.append({"terrain": name, "miles": miles, "miles_remaining": miles})

    return segments


def _build_weather_dict(season: str, date_str: str = "") -> dict:
    """Generate one day's weather for the Vesve frontier region."""
    season = season.lower().strip()
    if season not in _SEASON_TEMP_RANGE:
        season = "summer"

    temp_lo, temp_hi = _SEASON_TEMP_RANGE[season]
    temperature  = random.randint(temp_lo, temp_hi)

    precip_key   = _roll_from_table(_SEASON_PRECIP[season])
    wind_key     = _roll_from_table(_SEASON_WIND[season])

    precip_mod   = _PRECIP_MOVE_MOD.get(precip_key, 1.0)
    wind_mod     = _WIND_MOVE_MOD.get(wind_key, 1.0)
    move_mod     = round(precip_mod * wind_mod, 2)

    visibility   = _PRECIP_VISIBILITY.get(precip_key, 5.0)

    # Extreme cold check (below 10°F)
    survival_required = temperature <= 10 and season == "winter"

    # Build conditions list
    conditions: list[str] = [precip_key]
    if wind_key not in ("calm", "light"):
        conditions.append(wind_key)
    if temperature <= 0:
        conditions.append("extreme_cold")
    elif temperature <= 10:
        conditions.append("bitter_cold")

    # Temp description
    if temperature <= 0:
        temp_desc = "Extreme Cold"
    elif temperature <= 15:
        temp_desc = "Bitter Cold"
    elif temperature <= 32:
        temp_desc = "Freezing"
    elif temperature <= 45:
        temp_desc = "Cold"
    elif temperature <= 60:
        temp_desc = "Cool"
    elif temperature <= 75:
        temp_desc = "Mild"
    elif temperature <= 85:
        temp_desc = "Warm"
    else:
        temp_desc = "Hot"

    return {
        "date":                date_str or "unknown",
        "season":              season,
        "region":              "vesve_frontier",
        "temperature_f":       temperature,
        "temperature_desc":    temp_desc,
        "precipitation":       precip_key,
        "precipitation_label": _PRECIP_LABEL.get(precip_key, precip_key),
        "wind":                wind_key,
        "wind_label":          _WIND_LABEL.get(wind_key, wind_key),
        "visibility_miles":    visibility,
        "movement_modifier":   move_mod,
        "conditions":          conditions,
        "survival_check_required": survival_required,
        "halts_travel":        move_mod == 0.0,
        "conditions_summary": (
            f"{_PRECIP_LABEL.get(precip_key, precip_key)}, "
            f"{_WIND_LABEL.get(wind_key, wind_key)}, "
            f"{temperature}°F ({temp_desc})"
        ),
    }


def _resolve_lost(terrain: str, conditions: list[str]) -> dict:
    """
    Resolve a getting-lost event: direction, hexes off course, time to reorient.
    """
    directions = ["North", "NE", "East", "SE", "South", "SW", "West", "NW"]
    direction   = directions[random.randint(0, 7)]

    # Hexes off course (6-mile hexes): 1d3
    hexes_off   = random.randint(1, 3)
    miles_off   = hexes_off * 6

    # Time to reorient (hours)
    reorient_roll = random.randint(1, 6)
    if reorient_roll <= 2:
        hours_lost  = 2
        extra_days  = 0
    elif reorient_roll <= 4:
        hours_lost  = 4
        extra_days  = 0
    else:
        hours_lost  = 8
        extra_days  = 1   # full day lost

    return {
        "direction_off_course": direction,
        "hexes_off_course":     hexes_off,
        "miles_off_course":     miles_off,
        "hours_to_reorient":    hours_lost,
        "extra_days_lost":      extra_days,
        "description": (
            f"Party drifts {hexes_off} hex(es) {direction} of course. "
            f"Takes {hours_lost} hours to reorient."
        ),
    }


def _roll_outdoor_encounter(terrain: str) -> dict:
    """Roll one outdoor encounter for the given terrain. Returns encounter dict."""
    table  = _OUTDOOR_ENCOUNTERS.get(terrain, _OUTDOOR_ENCOUNTERS["plains"])
    d20    = random.randint(1, 20)

    for (lo, hi, monster_name, na_text) in table:
        if lo <= d20 <= hi:
            count = _roll_number_appearing(na_text) if na_text else 1
            stats = lookup_monster(monster_name)
            return {
                "d20_roll":     d20,
                "monster_name": monster_name,
                "count":        count,
                "terrain":      terrain,
                "monster_stats": stats or {},
                "next_steps": (
                    f"Encounter: {count}x {monster_name} in {terrain}. "
                    "Check for surprise (1-2 on d6), then call start_combat() "
                    "or describe how the party avoids/reacts."
                ),
            }

    # Fallback
    return {
        "d20_roll": d20, "monster_name": "Wolf", "count": 1,
        "terrain": terrain, "monster_stats": lookup_monster("Wolf") or {},
        "next_steps": "Lone wolf spotted.",
    }


# ---------------------------------------------------------------------------
# World-facts persistence helpers
# ---------------------------------------------------------------------------

def _get_world_fact_json(category: str) -> dict | list | None:
    """Read a JSON world_fact by category. Returns parsed object or None."""
    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT fact_text FROM world_facts "
            "WHERE campaign_id = ? AND category = ? LIMIT 1",
            (_CAMPAIGN_ID, category),
        )
        row = cur.fetchone()
    if not row:
        return None
    try:
        return json.loads(row["fact_text"])
    except (json.JSONDecodeError, TypeError):
        return None


def _set_world_fact_json(category: str, data: dict | list, source_note: str = "travel_system") -> None:
    """Upsert a JSON world_fact."""
    with _get_conn() as conn:
        conn.execute(
            "DELETE FROM world_facts WHERE campaign_id = ? AND category = ?",
            (_CAMPAIGN_ID, category),
        )
        conn.execute(
            "INSERT INTO world_facts (campaign_id, category, fact_text, source_note) "
            "VALUES (?, ?, ?, ?)",
            (_CAMPAIGN_ID, category, json.dumps(data), source_note),
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def db_generate_weather(season: str, date_str: str = "") -> dict:
    """
    Generate today's weather and a 3-day forecast for the Vesve frontier region.
    Stores both in world_facts. Returns the current day's weather.
    """
    today    = _build_weather_dict(season, date_str)
    forecast = [_build_weather_dict(season, f"day+{i+1}") for i in range(3)]

    # Simplify forecast entries (strip heavy detail)
    simple_forecast = []
    for f in forecast:
        simple_forecast.append({
            "date":             f["date"],
            "precipitation_label": f["precipitation_label"],
            "wind_label":       f["wind_label"],
            "temperature_f":    f["temperature_f"],
            "temperature_desc": f["temperature_desc"],
            "movement_modifier": f["movement_modifier"],
            "conditions_summary": f["conditions_summary"],
        })

    _set_world_fact_json("current_weather", today,    "weather_system")
    _set_world_fact_json("weather_forecast", simple_forecast, "weather_system")

    today["forecast_3_days"] = simple_forecast
    return today


def db_get_current_weather() -> dict:
    """Return the stored current weather. Generates fresh if none exists."""
    data = _get_world_fact_json("current_weather")
    if data and isinstance(data, dict):
        forecast = _get_world_fact_json("weather_forecast") or []
        data["forecast_3_days"] = forecast
        return data
    # No weather stored yet — return a neutral placeholder
    return {
        "error": "No weather generated yet.",
        "hint":  "Call generate_weather(season='summer') to set today's conditions.",
    }


def db_start_travel(
    origin:         str,
    destination:    str,
    terrain_path:   str,
    mount_type:     str,
    total_miles:    int,
    notes:          str = "",
) -> dict:
    """
    Initialise a new journey and store it in world_facts ('travel_state').
    Returns the full travel plan.
    """
    mount_type = mount_type.lower().replace(" ", "_").replace("-", "_")
    if mount_type not in _BASE_MOVE_MPD:
        mount_type = "foot"

    segments = _parse_terrain_path(terrain_path, total_miles)

    # Recalculate total_miles from segments if they have explicit values
    seg_total = sum(s["miles"] for s in segments)
    if seg_total > 0:
        total_miles = seg_total

    # Estimate days per segment
    total_days_est = 0.0
    days_breakdown: list[dict] = []
    for seg in segments:
        mph = _BASE_MOVE_MPD[mount_type].get(seg["terrain"], 18)
        days = seg["miles"] / max(mph, 1)
        total_days_est += days
        days_breakdown.append({
            "terrain": seg["terrain"],
            "miles":   seg["miles"],
            "miles_per_day": mph,
            "days_est": round(days, 1),
        })

    total_days_int = max(1, int(total_days_est + 0.99))  # ceiling

    # Food/water per person
    food_days_needed  = total_days_int
    water_pints_day   = 2   # per person per day (AD&D standard)

    # Hex count (6-mile hexes)
    hexes = round(total_miles / 6, 1)

    state: dict = {
        "active":               True,
        "origin":               origin,
        "destination":          destination,
        "mount_type":           mount_type,
        "terrain_segments":     [
            {**s, "miles_remaining": s["miles"]} for s in segments
        ],
        "total_miles":          total_miles,
        "miles_traveled":       0,
        "days_elapsed":         0,
        "total_days_estimate":  total_days_int,
        "food_days_needed":     food_days_needed,
        "food_days_consumed":   0,
        "water_pints_per_day_per_person": water_pints_day,
        "encounters_log":       [],
        "weather_delays_days":  0,
        "lost_extra_days":      0,
        "notes":                notes,
    }

    _set_world_fact_json("travel_state", state, "travel_system")

    # Update scene location
    with _get_conn() as conn:
        conn.execute(
            "UPDATE current_scene_state SET updated_at = datetime('now') WHERE id = 1"
        )

    return {
        "journey_started":    True,
        "origin":             origin,
        "destination":        destination,
        "mount_type":         mount_type,
        "total_miles":        total_miles,
        "total_hexes_6mi":    hexes,
        "total_days_estimate": total_days_int,
        "days_breakdown":     days_breakdown,
        "food_per_person":    f"{food_days_needed} days' rations",
        "water_per_person":   f"{water_pints_day * total_days_int} pints ({total_days_int} days × {water_pints_day}/day)",
        "horse_fodder":       f"~{total_days_int * 20} lbs grain per horse" if mount_type != "foot" else None,
        "notes":              notes,
        "next_step":          "Call travel_turn() once per day of travel to resolve each day.",
    }


def db_travel_turn() -> dict:
    """
    Resolve one day of travel. Updates and persists the travel state.

    1. Reads current weather (generates default if absent).
    2. Determines today's terrain from the leading segment.
    3. Calculates actual miles: base × weather_modifier.
    4. Checks for getting lost (terrain-based chance, doubled in bad weather).
    5. Rolls for random encounter.
    6. Consumes resources (1 food-day per person).
    7. Advances terrain segments; detects journey completion.
    Returns a full day-report dict.
    """
    state = _get_world_fact_json("travel_state")
    if not state or not state.get("active"):
        return {"error": "No active journey. Call start_travel() first."}

    segments = state.get("terrain_segments", [])
    if not segments:
        state["active"] = False
        _set_world_fact_json("travel_state", state, "travel_system")
        return {
            "journey_complete": True,
            "day":              state.get("days_elapsed", 0),
            "destination":      state.get("destination", "unknown"),
            "total_days":       state.get("days_elapsed", 0),
            "total_miles":      state.get("miles_traveled", 0),
        }

    current_terrain = segments[0]["terrain"]
    mount_type      = state.get("mount_type", "foot")

    # ── Weather ───────────────────────────────────────────────────────────────
    weather = _get_world_fact_json("current_weather")
    if not weather or not isinstance(weather, dict):
        weather = _build_weather_dict("summer")   # silent default

    weather_mod    = weather.get("movement_modifier", 1.0)
    halted         = weather_mod == 0.0
    base_move      = _BASE_MOVE_MPD.get(mount_type, _BASE_MOVE_MPD["foot"]).get(current_terrain, 18)
    actual_move    = round(base_move * weather_mod, 1) if not halted else 0

    # ── Getting Lost ──────────────────────────────────────────────────────────
    got_lost   = False
    lost_result: dict = {}
    if not halted and current_terrain != "road":
        base_chance = _TERRAIN_LOST_CHANCE_PCT.get(current_terrain, 5)
        # Double chance in heavy precip or very low visibility
        if weather_mod <= 0.5:
            base_chance = min(90, base_chance * 2)
        if base_chance > 0 and random.randint(1, 100) <= base_chance:
            got_lost    = True
            lost_result = _resolve_lost(current_terrain, weather.get("conditions", []))
            state["lost_extra_days"] = state.get("lost_extra_days", 0) + lost_result.get("extra_days_lost", 0)
            if lost_result.get("extra_days_lost", 0):
                state["weather_delays_days"] = state.get("weather_delays_days", 0) + lost_result["extra_days_lost"]

    if halted:
        state["weather_delays_days"] = state.get("weather_delays_days", 0) + 1

    # ── Consume miles from segments ───────────────────────────────────────────
    miles_consumed       = 0.0
    terrains_crossed     = []

    if not halted and not got_lost:
        miles_left = actual_move
        while miles_left > 0 and segments:
            seg = segments[0]
            take = min(miles_left, seg["miles_remaining"])
            seg["miles_remaining"] -= take
            miles_left    -= take
            miles_consumed += take
            if seg["terrain"] not in terrains_crossed:
                terrains_crossed.append(seg["terrain"])
            if seg["miles_remaining"] <= 0:
                segments.pop(0)

    state["miles_traveled"]   = state.get("miles_traveled", 0) + miles_consumed
    state["days_elapsed"]     = state.get("days_elapsed", 0) + 1
    state["terrain_segments"] = segments

    # ── Random Encounter ──────────────────────────────────────────────────────
    encounter: dict = {}
    if not halted and actual_move > 0:
        chance_num, chance_den = _TERRAIN_ENCOUNTER_CHANCE.get(current_terrain, (1, 6))
        if random.randint(1, chance_den) <= chance_num:
            encounter = _roll_outdoor_encounter(current_terrain)
            state.setdefault("encounters_log", []).append({
                "day":     state["days_elapsed"],
                "terrain": current_terrain,
                "monster": encounter.get("monster_name", ""),
                "count":   encounter.get("count", 1),
            })

    # ── Resources ────────────────────────────────────────────────────────────
    state["food_days_consumed"] = state.get("food_days_consumed", 0) + 1
    food_remaining = state["food_days_needed"] - state["food_days_consumed"]

    # ── Journey completion ────────────────────────────────────────────────────
    miles_remaining  = sum(s["miles_remaining"] for s in segments)
    journey_complete = (miles_remaining <= 0 and not halted)

    if journey_complete:
        state["active"]                = False
        state["current_location_desc"] = state.get("destination", "destination")

    _set_world_fact_json("travel_state", state, "travel_system")

    # ── Estimate days remaining ───────────────────────────────────────────────
    days_remaining_est = 0
    if miles_remaining > 0 and base_move > 0:
        days_remaining_est = max(1, int(miles_remaining / base_move + 0.99))

    result: dict = {
        "day":                    state["days_elapsed"],
        "terrain":                current_terrain,
        "terrains_crossed":       terrains_crossed or [current_terrain],
        "weather_summary":        weather.get("conditions_summary", "Unknown"),
        "weather_modifier":       weather_mod,
        "halted_by_weather":      halted,
        "base_miles_per_day":     base_move,
        "actual_miles_today":     miles_consumed,
        "total_miles_traveled":   state["miles_traveled"],
        "miles_remaining":        miles_remaining,
        "days_elapsed":           state["days_elapsed"],
        "days_remaining_estimate": days_remaining_est,
        "food_days_remaining":    food_remaining,
        "got_lost":               got_lost,
        "encounter":              encounter or None,
        "journey_complete":       journey_complete,
        "survival_check_required": weather.get("survival_check_required", False),
    }

    if got_lost:
        result["lost_result"] = lost_result

    if journey_complete:
        result["destination_reached"] = state.get("destination", "destination")
        result["note"] = (
            f"Journey complete! Arrived at {state.get('destination', 'destination')} "
            f"after {state['days_elapsed']} days ({state['miles_traveled']:.0f} miles)."
        )
    elif halted:
        result["note"] = (
            "Severe weather halts travel. The party must shelter for the day. "
            "Call generate_weather() tomorrow for new conditions, then travel_turn() again."
        )

    return result


def db_get_lost(terrain: str, weather_condition: str = "") -> dict:
    """
    Resolve a getting-lost event for the given terrain and weather condition.
    Returns direction, hexes off course, and time to reorient.
    Does NOT require an active journey — can be called standalone.
    """
    terrain   = terrain.lower().strip()
    conditions = [weather_condition] if weather_condition else []
    result    = _resolve_lost(terrain, conditions)

    # Add lost chance context for the narrative
    base_chance = _TERRAIN_LOST_CHANCE_PCT.get(terrain, 10)
    result["terrain"]             = terrain
    result["base_lost_chance_pct"] = base_chance
    result["instructions"] = (
        "Update the party's current position in the travel state by noting "
        "the deviation. Call travel_turn() to resume once reoriented — the "
        "extra_days_lost will be accounted for automatically."
    )
    return result


# ==============================================================================
# PHASE 5B — CAROUSING & DOWNTIME ACTIVITIES
# carouse · research_spell · gather_rumors · religious_observance
# domain_administration · recovery · craft_item
# ==============================================================================

# ------------------------------------------------------------------------------
# CAROUSING TABLE — Jeff Rients style, AD&D 1e flavour
# Roll d20 (modified upward by gold-spend tier). XP = gold spent (always).
# Low results = trouble; high results = colourful but manageable or beneficial.
# ------------------------------------------------------------------------------

_CAROUSING_SPEND_BONUS: list[tuple[int, int]] = [
    # (minimum_gp_spent, d20_roll_modifier)
    (500, +5),
    (200, +4),
    (100, +3),
    ( 50, +2),
    ( 25, +1),
    (  1,  0),
]

_CAROUSING_TABLE: dict[int, dict] = {
    1:  {"consequence": "public_disgrace",
         "description": "You made a spectacular fool of yourself in public. -1 to reaction rolls in this community for 30 days.",
         "mechanical": "reaction_penalty_30d",
         "severity": "moderate"},
    2:  {"consequence": "romantic_entanglement",
         "description": "You wake next to someone with expectations. Roll Charisma or face a complicated social scene — possibly in front of witnesses.",
         "mechanical": "cha_check_required",
         "severity": "minor"},
    3:  {"consequence": "brutal_hangover",
         "description": "Your head is splitting and the room won't stop moving. Incapacitated for 1d6 hours at the start of the next session.",
         "mechanical": "1d6_hours_incapacitated",
         "severity": "minor"},
    4:  {"consequence": "watch_trouble",
         "description": "The watch would like a word about last night. Pay 20 gp fine or spend 1d3 days in a cell.",
         "mechanical": "pay_20gp_or_1d3_days_jail",
         "severity": "moderate"},
    5:  {"consequence": "gambling_losses",
         "description": "The dice were not your friends. Lose an additional 1d6x10 gp from your purse before morning.",
         "mechanical": "lose_1d6x10_gp_extra",
         "severity": "moderate"},
    6:  {"consequence": "local_enemy",
         "description": "You earned the lasting enmity of a local tough. He has friends, a scar, and an excellent memory.",
         "mechanical": "new_enemy_npc",
         "severity": "moderate"},
    7:  {"consequence": "bar_brawl",
         "description": "Bar fight erupted. You took 1d6 damage (armour does not count — this was a tavern). Your name is now in the watch's ledger.",
         "mechanical": "1d6_damage_no_armor_plus_watch_notice",
         "severity": "moderate"},
    8:  {"consequence": "pickpocketed",
         "description": "Someone had nimble fingers and a good eye. Lose an additional 1d6x5 gp from your person.",
         "mechanical": "lose_1d6x5_gp_extra",
         "severity": "minor"},
    9:  {"consequence": "indiscretion",
         "description": "You said something you absolutely should not have — about your plans, your allies, or your enemies. The rumour is already spreading.",
         "mechanical": "rumor_started_about_pc",
         "severity": "moderate"},
    10: {"consequence": "tattoo",
         "description": "You wake with a new tattoo. It is in a visible location. You have absolutely no memory of choosing it, but it is disturbingly well-executed.",
         "mechanical": "cosmetic_only",
         "severity": "minor"},
    11: {"consequence": "dubious_contact",
         "description": "You made a new acquaintance — a fence, a smuggler, a very well-connected rat-catcher. Definitely useful. Probably trouble eventually.",
         "mechanical": "new_contact_npc",
         "severity": "beneficial"},
    12: {"consequence": "gambling_winnings",
         "description": "Fortune smiled on you for once. You won at dice and recovered 1d6x10 gp before sunrise.",
         "mechanical": "recover_1d6x10_gp",
         "severity": "beneficial"},
    13: {"consequence": "local_fame",
         "description": "Your exploits — suitably embellished — are the talk of the common room. Reaction rolls +1 in this community for 30 days.",
         "mechanical": "reaction_bonus_30d",
         "severity": "beneficial"},
    14: {"consequence": "mysterious_patron",
         "description": "A well-dressed stranger bought rounds all night and was extremely interested in your plans. They will contact you. They will want something.",
         "mechanical": "obligation_to_stranger",
         "severity": "mixed"},
    15: {"consequence": "debt",
         "description": "You borrowed against tomorrow. A moneylender is owed 1d4x50 gp within 30 days — at interest. He has friends.",
         "mechanical": "debt_1d4x50_gp_30_days",
         "severity": "moderate"},
    16: {"consequence": "notable_offended",
         "description": "In your cups you publicly insulted a guild officer, temple elder, or minor noble. They remember. They have influence. They are not done with you.",
         "mechanical": "powerful_enemy_created",
         "severity": "serious"},
    17: {"consequence": "political_entanglement",
         "description": "You stumbled into a factional dispute and apparently took a side — without knowing it. Both factions believe you are their enemy.",
         "mechanical": "two_faction_enemies",
         "severity": "serious"},
    18: {"consequence": "cultist_oath",
         "description": "You won an oath-swearing contest at what turned out to be a cult ceremony. They consider you a member. They have expectations.",
         "mechanical": "cult_membership_obligation",
         "severity": "serious"},
    19: {"consequence": "rolled_in_alley",
         "description": "You wake in an alley with a headache and empty pockets. All carried coin is gone. Roll Constitution or lose one item as well.",
         "mechanical": "lose_all_carried_coin_plus_con_check_for_item",
         "severity": "severe"},
    20: {"consequence": "grand_evening",
         "description": "A legendary night by all accounts. The bard is already writing the song. No ill effects whatsoever.",
         "mechanical": "no_consequence",
         "severity": "beneficial"},
}

# ------------------------------------------------------------------------------
# RUMOUR TABLES — by quality tier (1=tavern gossip, 4=reliable intelligence)
# Template placeholders are filled from _RUMOUR_FILL at call time.
# ------------------------------------------------------------------------------

_RUMOUR_TEMPLATES: dict[int, list[str]] = {
    1: [
        "Someone claims to have seen lights moving in the old watchtower three nights running.",
        "Old Marta swears there's a hoard buried under the ruins east of town — her grandfather said so.",
        "The miller's boy went into the forest and came back talking strangely. He won't say what he saw.",
        "Three merchants were robbed on the road to {road_dest}. Nobody saw a thing.",
        "There's a witch living in the hills who can cure any disease — for a price.",
        "Word is the local lord raised taxes again. People are angry but too scared to say it openly.",
        "A foreign soldier was drinking here last tenday. He paid in coins nobody here recognised.",
        "Something big is moving in the river at night. The fishermen won't go out after dark.",
    ],
    2: [
        "A tinker who travels widely says the ruins of {ruin_name} have been disturbed recently — fresh tracks leading in, none coming out.",
        "A traveller from the east claims a necromancer has been buying bodies from the undertakers in {town_name} — paying well.",
        "River traders say there's a new toll on the {river_name} — not levied by any recognised lord.",
        "A retired soldier says there's a cache of arms buried near the old battlefield — enough to equip thirty men.",
        "A hedge wizard passing through detected an unusual concentration of magical aura from the forest to the north.",
        "One of the local guild associates (not that he'd admit his connections) says a rival operation has moved into the territory.",
        "A travelling priest of {deity_name} mentioned that a holy relic stolen from their shrine three years ago has surfaced locally.",
        "Several farmers report missing livestock — not taken by wolves. The tracks are wrong.",
    ],
    3: [
        "A dwarven prospector: there's a mine entrance in the {terrain} hills, sealed from the inside. The stonework is old — pre-migration era.",
        "A retired adventurer, and she knows what she's talking about: the dungeon under {ruin_name} has three levels. She reached the second. She doesn't discuss why she stopped.",
        "A road warden reports a band of {monster_type} using the ruined fort as a base. Roughly {number}. They haven't raided yet but they're scouting the roads.",
        "A merchant with city contacts says a powerful magic item was stolen from a noble house. There is a quiet reward for its recovery — no questions asked.",
        "A message-runner who reads the letters he carries says {faction_name} is planning to move against {target} within the month.",
        "An ex-guild thief, going straight now, says there's a hidden vault beneath the old {building_type} in the next town. He has a partial map.",
    ],
    4: [
        "First-hand account from a survivor: the dungeon at {ruin_name} has a guardian on the third level that cannot be harmed by non-magical weapons. They lost three good people learning that.",
        "A well-paid spy confirms {faction_name} has a mole in the local garrison. Reports go out every tenday via a coded dead drop.",
        "A wizard's apprentice, in exchange for passage out of town, provided her master's notes on the wards and traps protecting his tower. Detailed and current.",
        "A former cultist, seeking redemption, provides the meeting schedule, location, and membership list of a local {cult_name} cell. He wants protection in return.",
        "A cartographer's fresh survey locates three previously unmapped dungeon entrances in the {region_name} region — with rough interior sketches.",
    ],
}

_RUMOUR_FILL: dict[str, list[str]] = {
    "road_dest":    ["the capital", "the coast", "the border fort", "the dwarven holds"],
    "ruin_name":    ["Stonehallow", "the Old Keep", "the Barrow Mounds", "Castle Greystone", "the Sunken Tower"],
    "town_name":    ["Millford", "Rillford", "Eastgate", "the market town"],
    "river_name":   ["the Velverdyva", "the Artonsamay", "the Nyr Dyv tributary"],
    "deity_name":   ["Trithereon", "Pelor", "St. Cuthbert", "Heironeous"],
    "terrain":      ["western", "northern", "eastern", "southern"],
    "monster_type": ["gnolls", "orcs", "bandits", "hobgoblins", "bugbears"],
    "number":       ["a dozen", "twenty or more", "thirty", "a score"],
    "faction_name": ["the Horned Society", "the Scarlet Brotherhood", "the local thieves guild", "a merchant consortium"],
    "target":       ["the town council", "the road garrison", "a rival merchant house", "the temple"],
    "building_type":["granary", "inn", "guildhall", "old temple"],
    "cult_name":    ["Vecna", "Iuz", "Nerull", "Incabulos"],
    "region_name":  ["Vesve", "Velverdyva", "the northern march", "the border lands"],
}


def _fill_rumour(template: str) -> str:
    """Replace {placeholder} tokens in a rumour template with random values."""
    def _replacer(m: "re.Match") -> str:
        key = m.group(1)
        choices = _RUMOUR_FILL.get(key, [key])
        return random.choice(choices)
    return re.sub(r"\{(\w+)\}", _replacer, template)


# ------------------------------------------------------------------------------
# Shared downtime helpers
# ------------------------------------------------------------------------------

def _award_pc_xp(amount: int) -> dict:
    """
    Add XP to the PC's class_level rows (split evenly for multi-class).
    Returns {"xp_awards": [...], "total_xp_awarded": int}.
    """
    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT class_level_id, class_name, level, xp "
            "FROM class_levels WHERE character_id = ?",
            (_PC_CHARACTER_ID,),
        )
        rows = [dict(r) for r in cur.fetchall()]

    if not rows:
        return {"error": "No class_levels found for PC", "xp_awards": [], "total_xp_awarded": 0}

    share = amount // max(1, len(rows))
    updates: list[dict] = []
    with _get_conn() as conn:
        for cls in rows:
            new_xp = cls["xp"] + share
            conn.execute(
                "UPDATE class_levels SET xp = ? WHERE class_level_id = ?",
                (new_xp, cls["class_level_id"]),
            )
            updates.append({
                "class_name": cls["class_name"],
                "level":      cls["level"],
                "xp_before":  cls["xp"],
                "xp_gained":  share,
                "xp_after":   new_xp,
            })
    return {"xp_awards": updates, "total_xp_awarded": amount}


def _log_downtime(activity: str, data: dict) -> None:
    """Append a downtime record to world_facts (category='downtime_log')."""
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO world_facts (campaign_id, category, fact_text, source_note) "
            "VALUES (?, 'downtime_log', ?, ?)",
            (_CAMPAIGN_ID, json.dumps(data), activity),
        )


def _get_pc_ability(stat: str) -> int:
    """Return the PC's score for the named ability (e.g. 'charisma', 'intelligence')."""
    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT {stat} FROM character_abilities WHERE character_id = ?",
            (_PC_CHARACTER_ID,),
        )
        row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 10


def _ability_mod(score: int) -> int:
    """AD&D 1e modifier for ability scores (used for reaction/morale checks)."""
    if score <= 3:   return -3
    if score <= 5:   return -2
    if score <= 7:   return -1
    if score <= 12:  return  0
    if score <= 15:  return  1
    if score <= 17:  return  2
    return 3


def _get_treasury_gp() -> int:
    """Return GP balance of treasury_id=1 (primary account)."""
    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT gp FROM treasury_accounts "
            "WHERE treasury_id = 1 AND campaign_id = ?",
            (_CAMPAIGN_ID,),
        )
        row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _downtime_advance_calendar(days: int, calendar_note: str) -> str:
    """
    Write/replace the 'calendar' world_fact.
    Uses calendar_note verbatim if provided; otherwise appends '+N days' to the
    existing entry.  Returns the final calendar string stored.
    """
    if calendar_note:
        new_time = calendar_note
    else:
        with _get_conn(read_only=True) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT fact_text FROM world_facts "
                "WHERE campaign_id = ? AND category = 'calendar' LIMIT 1",
                (_CAMPAIGN_ID,),
            )
            row = cur.fetchone()
        existing = row["fact_text"] if row else "576 CY (date unknown)"
        new_time = f"{existing} [+{days}d]"

    with _get_conn() as conn:
        conn.execute(
            "DELETE FROM world_facts WHERE campaign_id = ? AND category = 'calendar'",
            (_CAMPAIGN_ID,),
        )
        conn.execute(
            "INSERT INTO world_facts (campaign_id, category, fact_text, source_note) "
            "VALUES (?, 'calendar', ?, 'downtime')",
            (_CAMPAIGN_ID, new_time),
        )
    return new_time


# ------------------------------------------------------------------------------
# PUBLIC DOWNTIME FUNCTIONS
# ------------------------------------------------------------------------------

def db_carouse(gold_spent: int, calendar_note: str = "") -> dict:
    """
    Carousing: spend gold in taverns, earn XP equal to gold spent, roll d20 for
    consequence.  Higher spend tiers add a bonus to the roll (wilder results).
    """
    if gold_spent < 1:
        return {"error": "gold_spent must be at least 1."}

    # Spend-tier roll bonus
    roll_bonus = 0
    for (min_gp, mod) in _CAROUSING_SPEND_BONUS:
        if gold_spent >= min_gp:
            roll_bonus = mod
            break

    # Deduct gold — cap at available balance
    treasury_before = _get_treasury_gp()
    actual_spent    = min(gold_spent, treasury_before)
    if actual_spent <= 0:
        return {"error": "Insufficient funds in primary treasury for carousing."}
    _deduct_treasury(actual_spent)

    # d20 + spend bonus, clamped 1-20
    raw_roll   = random.randint(1, 20)
    final_roll = max(1, min(20, raw_roll + roll_bonus))
    entry      = _CAROUSING_TABLE[final_roll]

    # XP = gold spent (always, win or lose)
    xp_result = _award_pc_xp(actual_spent)

    # Roll extra dice for mechanical effects
    extra_gold_recovered = 0
    extra_gold_lost      = 0
    extra_rolls: dict    = {}

    if entry["consequence"] == "gambling_winnings":
        extra_gold_recovered = random.randint(1, 6) * 10
        _credit_treasury(extra_gold_recovered)
        extra_rolls["winnings_roll"] = extra_gold_recovered

    elif entry["consequence"] in ("gambling_losses",):
        extra_gold_lost = random.randint(1, 6) * 10
        _deduct_treasury(extra_gold_lost)
        extra_rolls["extra_loss_roll"] = extra_gold_lost

    elif entry["consequence"] == "pickpocketed":
        extra_gold_lost = random.randint(1, 6) * 5
        _deduct_treasury(extra_gold_lost)
        extra_rolls["pickpocket_roll"] = extra_gold_lost

    elif entry["consequence"] == "debt":
        debt_amount = random.randint(1, 4) * 50
        extra_rolls["debt_amount_gp"] = debt_amount

    elif entry["consequence"] == "bar_brawl":
        damage = random.randint(1, 6)
        extra_rolls["brawl_damage"] = damage

    elif entry["consequence"] == "brutal_hangover":
        hours_lost = random.randint(1, 6)
        extra_rolls["hours_incapacitated"] = hours_lost

    treasury_after = treasury_before - actual_spent + extra_gold_recovered - extra_gold_lost

    cal = _downtime_advance_calendar(1, calendar_note)

    result = {
        "activity":          "carouse",
        "gold_spent":        actual_spent,
        "treasury_before":   treasury_before,
        "treasury_after":    treasury_after,
        "xp_awarded":        actual_spent,
        "xp_details":        xp_result.get("xp_awards", []),
        "d20_roll":          raw_roll,
        "spend_bonus":       roll_bonus,
        "final_roll":        final_roll,
        "consequence_type":  entry["consequence"],
        "consequence":       entry["description"],
        "mechanical_effect": entry["mechanical"],
        "severity":          entry["severity"],
        "extra_rolls":       extra_rolls,
        "calendar":          cal,
        "dm_note": (
            "Apply mechanical_effect narratively. For damage consequences call "
            "update_character_status. For new_enemy_npc / new_contact_npc call "
            "add_npc. For debt or political consequences record via update_world_fact. "
            "XP is always awarded regardless of consequence."
        ),
    }
    _log_downtime("carouse", result)
    return result


def db_research_spell(
    spell_name:    str,
    spell_level:   int,
    days:          int,
    gold_spent:    int,
    calendar_note: str = "",
) -> dict:
    """
    Magic-User researches a new spell or copies one into their spellbook.
    Success chance: base 45% + INT modifier x5% + extra weeks over minimum x5%.
    Cost guideline: 100 gp x spell_level per week.
    XP on success: 100 x spell_level.
    """
    spell_level = max(1, min(9, spell_level))
    days        = max(1, days)

    int_score = _get_pc_ability("intelligence")
    int_mod   = _ability_mod(int_score)

    # Minimum time: spell_level days; typical: spell_level weeks
    min_days    = spell_level
    weeks_spent = max(1, days // 7)
    extra_weeks = max(0, weeks_spent - spell_level)

    expected_gp = spell_level * 100 * weeks_spent

    # Success chance
    success_pct = 45 + (int_mod * 5) + (extra_weeks * 5)
    success_pct = max(5, min(95, success_pct))

    # Deduct gold
    treasury_before = _get_treasury_gp()
    actual_spent    = min(gold_spent, treasury_before)
    _deduct_treasury(actual_spent)

    roll    = random.randint(1, 100)
    success = roll <= success_pct

    if success:
        xp_award  = spell_level * 100
        xp_result = _award_pc_xp(xp_award)
        note = (
            f"'{spell_name}' (level {spell_level}) successfully researched "
            f"and added to spellbook after {days} days."
        )
    else:
        # On failure: half the time and cost is recoverable; can retry
        xp_award  = 0
        xp_result = {"xp_awards": [], "total_xp_awarded": 0}
        note = (
            f"Research on '{spell_name}' (level {spell_level}) failed after "
            f"{days} days. Half the time invested; may retry with fresh materials."
        )

    cal = _downtime_advance_calendar(days, calendar_note)

    result = {
        "activity":          "research_spell",
        "spell_name":        spell_name,
        "spell_level":       spell_level,
        "days_spent":        days,
        "gold_spent":        actual_spent,
        "expected_cost_gp":  expected_gp,
        "intelligence":      int_score,
        "success_chance_pct": success_pct,
        "roll":              roll,
        "success":           success,
        "xp_awarded":        xp_award,
        "xp_details":        xp_result.get("xp_awards", []),
        "calendar":          cal,
        "note":              note,
        "dm_note": (
            "On success, call update_world_fact(category='spellbook_contents') to "
            "record the new spell. The spell is not yet memorized — use memorize_spells "
            "after the next long rest."
        ),
    }
    _log_downtime("research_spell", result)
    return result


def db_gather_rumors(
    settlement:    str,
    days:          int,
    gold_spent:    int,
    calendar_note: str = "",
) -> dict:
    """
    Spend days in a settlement buying drinks and asking questions.
    Days and gold spent determine quality tier and rumour count.
    Charisma modifier extends maximum quality.
    Results stored in world_facts (category='rumors') for future retrieval.
    """
    days       = max(1, days)
    gold_spent = max(0, gold_spent)

    # Determine quality ceiling and rumour die
    if days >= 8 or gold_spent >= 100:
        max_quality  = 4
        rumour_die   = 8
    elif days >= 4 or gold_spent >= 50:
        max_quality  = 3
        rumour_die   = 6
    elif days >= 2 or gold_spent >= 20:
        max_quality  = 2
        rumour_die   = 4
    else:
        max_quality  = 1
        rumour_die   = 3

    cha_score = _get_pc_ability("charisma")
    cha_mod   = _ability_mod(cha_score)
    effective_max = min(4, max_quality + max(0, cha_mod))

    num_rumors = random.randint(1, rumour_die)

    gathered: list[dict] = []
    for _ in range(num_rumors):
        quality   = random.randint(1, effective_max)
        pool      = _RUMOUR_TEMPLATES.get(quality, _RUMOUR_TEMPLATES[1])
        text      = _fill_rumour(random.choice(pool))
        gathered.append({"quality": quality, "text": text})
    gathered.sort(key=lambda r: r["quality"], reverse=True)

    # Deduct expenses
    treasury_before = _get_treasury_gp()
    actual_spent    = min(gold_spent, treasury_before)
    _deduct_treasury(actual_spent)

    # XP: 10 per day of investigation
    xp_award  = days * 10
    xp_result = _award_pc_xp(xp_award)

    # Persist each rumour to world_facts
    with _get_conn() as conn:
        for r in gathered:
            conn.execute(
                "INSERT INTO world_facts "
                "(campaign_id, category, fact_text, source_note) "
                "VALUES (?, 'rumors', ?, ?)",
                (
                    _CAMPAIGN_ID,
                    r["text"],
                    f"Q{r['quality']} — {settlement}",
                ),
            )

    cal = _downtime_advance_calendar(days, calendar_note)

    result = {
        "activity":       "gather_rumors",
        "settlement":     settlement,
        "days_spent":     days,
        "gold_spent":     actual_spent,
        "charisma":       cha_score,
        "max_quality":    effective_max,
        "rumors_learned": len(gathered),
        "rumors":         gathered,
        "xp_awarded":     xp_award,
        "xp_details":     xp_result.get("xp_awards", []),
        "calendar":       cal,
        "dm_note": (
            "Rumors are stored in world_facts category='rumors'. "
            "Quality 4 = reliable intelligence; quality 1 = colourful tavern gossip "
            "that may or may not be true. Embellish freely. "
            "Call update_world_fact or save_turn to act on any rumour that leads "
            "somewhere significant."
        ),
    }
    _log_downtime("gather_rumors", result)
    return result


def db_religious_observance(
    deity:           str,
    observance_type: str,
    calendar_note:   str = "",
) -> dict:
    """
    Cleric fulfils religious obligations.
    observance_type: 'weekly' | 'holy_day' | 'atonement' | 'major_ritual'

    Tracks cumulative missed observances and active bonuses/penalties in
    world_facts (category='religious_obligations', source_note=deity).
    Missed count >= 3 triggers loss of highest-level spell slot (DM applies).
    """
    # Load existing record for this deity
    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT world_fact_id, fact_text FROM world_facts "
            "WHERE campaign_id = ? AND category = 'religious_obligations' "
            "AND source_note = ? LIMIT 1",
            (_CAMPAIGN_ID, deity),
        )
        row = cur.fetchone()

    if row:
        record  = json.loads(row["fact_text"])
        fact_id = row["world_fact_id"]
    else:
        record  = {
            "deity":          deity,
            "missed_count":   0,
            "penalty_stacks": 0,
            "bonus_active":   None,
            "last_observed":  None,
        }
        fact_id = None

    missed_before  = record.get("missed_count",   0)
    penalty_before = record.get("penalty_stacks", 0)

    OBSERVANCE_PARAMS = {
        "weekly":       {"xp": 50,  "days": 1, "bonus": "prayer_bonus_24h",
                         "desc": "Weekly prayers completed. Divine standing maintained; minor blessing granted."},
        "holy_day":     {"xp": 200, "days": 1, "bonus": "holy_day_bonus_7d",
                         "desc": "Holy day rites observed. +1 to all saving throws for 7 days."},
        "atonement":    {"xp": 100, "days": 2, "bonus": "atonement_cleared",
                         "desc": "Atonement rites complete. All penalties cleared; standing with deity restored."},
        "major_ritual": {"xp": 300, "days": 3, "bonus": "divine_favour_14d",
                         "desc": "Major ritual completed. Deity's favour: +1 morale to all followers for 14 days; +1 to next turn undead attempt."},
    }
    obs  = OBSERVANCE_PARAMS.get(observance_type, OBSERVANCE_PARAMS["weekly"])
    days = obs["days"]

    # Clear atonement fully; otherwise reduce by 1
    if observance_type == "atonement":
        new_missed   = 0
        new_penalties = 0
    else:
        new_missed    = max(0, missed_before - 1)
        new_penalties = max(0, penalty_before - 1)

    record.update({
        "missed_count":   new_missed,
        "penalty_stacks": new_penalties,
        "bonus_active":   obs["bonus"],
        "last_observed":  calendar_note or "recently",
    })

    with _get_conn() as conn:
        if fact_id:
            conn.execute(
                "UPDATE world_facts SET fact_text = ? WHERE world_fact_id = ?",
                (json.dumps(record), fact_id),
            )
        else:
            conn.execute(
                "INSERT INTO world_facts "
                "(campaign_id, category, fact_text, source_note) "
                "VALUES (?, 'religious_obligations', ?, ?)",
                (_CAMPAIGN_ID, json.dumps(record), deity),
            )

    xp_result = _award_pc_xp(obs["xp"])
    cal       = _downtime_advance_calendar(days, calendar_note)

    result = {
        "activity":        "religious_observance",
        "deity":           deity,
        "observance_type": observance_type,
        "days_spent":      days,
        "penalty_before":  penalty_before,
        "penalty_after":   new_penalties,
        "missed_before":   missed_before,
        "missed_after":    new_missed,
        "bonus_granted":   obs["bonus"],
        "xp_awarded":      obs["xp"],
        "xp_details":      xp_result.get("xp_awards", []),
        "description":     obs["desc"],
        "calendar":        cal,
        "dm_note": (
            "Apply bonus narratively: holy_day_bonus_7d = +1 all saves 7 days; "
            "divine_favour_14d = +1 troop/follower morale 14 days and +1 next turn "
            "undead attempt; prayer_bonus_24h = +1 next Wis check. "
            "If missed_count reaches 3+, cleric loses their highest memorized spell "
            "level until atonement is performed."
        ),
    }
    _log_downtime("religious_observance", result)
    return result


def db_domain_administration(
    days:          int,
    focus:         str         = "general",
    calendar_note: str         = "",
) -> dict:
    """
    Hold court and administer the domain for 1-14 days.
    focus: 'general' | 'military' | 'economic' | 'diplomatic' | 'justice'
    Charisma and Intelligence modify the d20 roll.
    Result affects NPC loyalty, troop morale, and treasury efficiency notes.
    """
    days = max(1, min(14, days))

    cha_score = _get_pc_ability("charisma")
    int_score = _get_pc_ability("intelligence")
    cha_mod   = _ability_mod(cha_score)
    int_mod   = _ability_mod(int_score)

    # d20 + ability mods + duration bonus
    raw_roll   = random.randint(1, 20)
    dur_bonus  = (1 if days >= 3 else 0) + (1 if days >= 7 else 0)
    total_roll = max(1, min(20, raw_roll + cha_mod + dur_bonus))

    if total_roll >= 18:
        tier   = "excellent"
        outcome = "Exceptional session — the realm feels your hand on the reins."
        npc_mood    = "improved"
        troop_mood  = "elevated"
        xp_mult     = 3
        bonus_effect = (
            "Treasury efficiency +10% this season. One outstanding petition resolved "
            "decisively in the PC's favour. Key NPC loyalty +1."
        )
    elif total_roll >= 14:
        tier   = "good"
        outcome = "Effective court — business handled, people satisfied."
        npc_mood    = "satisfied"
        troop_mood  = "steady"
        xp_mult     = 2
        bonus_effect = (
            "Key NPCs note the diligence. One piece of actionable intelligence "
            "brought to the PC's attention through normal channels."
        )
    elif total_roll >= 9:
        tier   = "adequate"
        outcome = "Routine session — nothing remarkable."
        npc_mood    = "neutral"
        troop_mood  = "unchanged"
        xp_mult     = 1
        bonus_effect = "Business as usual. No complications."
    elif total_roll >= 5:
        tier   = "poor"
        outcome = "Distracted session — a petition was mishandled."
        npc_mood    = "mildly_dissatisfied"
        troop_mood  = "unchanged"
        xp_mult     = 0
        bonus_effect = "One minor NPC is quietly disgruntled. May surface as a complication later."
    else:
        tier   = "crisis"
        outcome = "Crisis during court — a serious dispute erupted."
        npc_mood    = "alarmed"
        troop_mood  = "unsettled"
        xp_mult     = 0
        bonus_effect = (
            "Serious dispute requires follow-up action next session. "
            "One troop group's morale may drop if not addressed."
        )

    FOCUS_FLAVOUR = {
        "general":    "General court: petitions, disputes, and reports from all quarters.",
        "military":   "Military review: troop readiness, supply, and deployment assessed.",
        "economic":   "Economic session: guild reports, trade routes, and treasury reviewed.",
        "diplomatic": "Diplomatic audience: emissaries, envoys, and petitioners received.",
        "justice":    "Justice session: crimes, punishments, and outstanding disputes adjudicated.",
    }

    xp_award  = days * 20 * max(0, xp_mult)
    xp_result = _award_pc_xp(xp_award) if xp_award > 0 else {"xp_awards": [], "total_xp_awarded": 0}
    cal       = _downtime_advance_calendar(days, calendar_note)

    result = {
        "activity":      "domain_administration",
        "days_spent":    days,
        "focus":         focus,
        "focus_desc":    FOCUS_FLAVOUR.get(focus, FOCUS_FLAVOUR["general"]),
        "charisma":      cha_score,
        "intelligence":  int_score,
        "d20_roll":      raw_roll,
        "modifier":      cha_mod + dur_bonus,
        "final_roll":    total_roll,
        "outcome_tier":  tier,
        "outcome":       outcome,
        "npc_mood":      npc_mood,
        "troop_mood":    troop_mood,
        "bonus_effect":  bonus_effect,
        "xp_awarded":    xp_award,
        "xp_details":    xp_result.get("xp_awards", []),
        "calendar":      cal,
        "dm_note": (
            "For 'excellent'/'good' outcomes, call update_npc on key NPCs to note "
            "improved loyalty. For 'poor'/'crisis', note the disgruntled NPC and "
            "track for future roleplay. A 'crisis' may warrant a save_turn to narrate "
            "the dispute and its resolution."
        ),
    }
    _log_downtime("domain_administration", result)
    return result


def db_recovery(
    injury_description: str,
    days_resting:       int,
    calendar_note:      str = "",
) -> dict:
    """
    Extended rest for serious injuries or magical ailments beyond normal healing.
    Enhanced HP recovery: 2 HP per character level per week (vs 1/level/night).
    7+ days clears minor ailments; 30+ days clears all ailments (status_notes reset).
    """
    days_resting = max(1, min(90, days_resting))

    # Get current HP and status
    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT hp_current, hp_max, status_notes "
            "FROM character_status WHERE character_id = ?",
            (_PC_CHARACTER_ID,),
        )
        status = dict(cur.fetchone())

    # Get total character levels for recovery rate
    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT SUM(level) FROM class_levels WHERE character_id = ?",
            (_PC_CHARACTER_ID,),
        )
        lv_row = cur.fetchone()
    total_levels = int(lv_row[0]) if lv_row and lv_row[0] else 1

    hp_cur = status["hp_current"]
    hp_max = status["hp_max"]

    # 2 HP per level per week of complete bed rest
    hp_per_week  = total_levels * 2
    full_weeks   = days_resting // 7
    partial_days = days_resting % 7
    # Partial week: 1 HP per level per day (normal rate)
    hp_recovered = min(
        hp_max - hp_cur,
        full_weeks * hp_per_week + partial_days * total_levels,
    )
    new_hp = min(hp_max, hp_cur + hp_recovered)

    if days_resting >= 30:
        ailments_cleared = "all"
        new_notes        = None
        recovery_note    = "Full recovery from extended bed rest. All ailments and conditions resolved."
    elif days_resting >= 14:
        ailments_cleared = "moderate"
        new_notes        = status.get("status_notes")
        recovery_note    = "Two weeks of rest cleared moderate ailments. Serious conditions persist."
    elif days_resting >= 7:
        ailments_cleared = "minor"
        new_notes        = status.get("status_notes")
        recovery_note    = "One week of rest cleared minor ailments. Serious conditions persist."
    else:
        ailments_cleared = "none"
        new_notes        = status.get("status_notes")
        recovery_note    = "Short recovery. HP improved; no ailments cleared."

    with _get_conn() as conn:
        conn.execute(
            "UPDATE character_status SET hp_current = ?, status_notes = ? "
            "WHERE character_id = ?",
            (new_hp, new_notes, _PC_CHARACTER_ID),
        )

    # XP: 5 per day (representing time cost, not merit)
    xp_award  = days_resting * 5
    xp_result = _award_pc_xp(xp_award)
    cal       = _downtime_advance_calendar(days_resting, calendar_note)

    result = {
        "activity":           "recovery",
        "injury_description": injury_description,
        "days_resting":       days_resting,
        "hp_before":          hp_cur,
        "hp_after":           new_hp,
        "hp_recovered":       hp_recovered,
        "hp_max":             hp_max,
        "ailments_cleared":   ailments_cleared,
        "recovery_note":      recovery_note,
        "xp_awarded":         xp_award,
        "xp_details":         xp_result.get("xp_awards", []),
        "calendar":           cal,
        "dm_note": (
            "30 days automatically clears status_notes (all conditions). "
            "Magical ailments (curses, lycanthropy, charm, energy drain) require "
            "Remove Curse / Cure Disease / Restoration in addition to bed rest — "
            "recovery alone does not cure magical conditions."
        ),
    }
    _log_downtime("recovery", result)
    return result


def db_craft_item(
    item_name:     str,
    item_type:     str,
    materials_gp:  int,
    days:          int,
    calendar_note: str = "",
) -> dict:
    """
    Craft a mundane or minor magical item.
    item_type: 'mundane' | 'masterwork' | 'scroll' | 'potion' | 'minor_magic'
    On success, item is added to the PC's inventory via the items/inventory tables.
    On failure, half the materials are lost; retry is possible.
    """
    CRAFT_PARAMS: dict[str, dict] = {
        "mundane":     {"base_pct": 90, "min_days": 1,  "xp_base": 50,  "magic": False},
        "masterwork":  {"base_pct": 70, "min_days": 7,  "xp_base": 150, "magic": False},
        "scroll":      {"base_pct": 65, "min_days": 3,  "xp_base": 200, "magic": True},
        "potion":      {"base_pct": 60, "min_days": 7,  "xp_base": 250, "magic": True},
        "minor_magic": {"base_pct": 45, "min_days": 14, "xp_base": 500, "magic": True},
    }
    params = CRAFT_PARAMS.get(item_type, CRAFT_PARAMS["mundane"])

    days         = max(1, days)
    materials_gp = max(0, materials_gp)

    int_score = _get_pc_ability("intelligence")
    int_mod   = _ability_mod(int_score)

    days_over    = max(0, days - params["min_days"])
    extra_bonus  = min(20, (days_over // max(1, params["min_days"])) * 5)
    success_pct  = params["base_pct"] + (int_mod * 3) + extra_bonus
    success_pct  = max(5, min(98, success_pct))

    # Deduct materials
    treasury_before = _get_treasury_gp()
    actual_spent    = min(materials_gp, treasury_before)
    _deduct_treasury(actual_spent)

    roll    = random.randint(1, 100)
    success = roll <= success_pct

    if success:
        # Add item to inventory using existing add_item()
        add_item(
            name       = item_name,
            item_type  = item_type,
            magic_flag = params["magic"],
            value_gp   = actual_spent,
            notes      = f"Crafted — {days} days, {actual_spent} gp materials.",
        )
        xp_award   = params["xp_base"] + (actual_spent // 10)
        xp_result  = _award_pc_xp(xp_award)
        craft_note = (
            f"{item_name} ({item_type}) successfully crafted and added to "
            f"inventory after {days} days."
        )
    else:
        # Half materials lost on failure
        refund = actual_spent // 2
        if refund > 0:
            _credit_treasury(refund)
        xp_award   = 0
        xp_result  = {"xp_awards": [], "total_xp_awarded": 0}
        craft_note = (
            f"Crafting of {item_name} ({item_type}) failed after {days} days. "
            f"{actual_spent - refund} gp in materials lost. May retry."
        )

    cal = _downtime_advance_calendar(days, calendar_note)

    result = {
        "activity":       "craft_item",
        "item_name":      item_name,
        "item_type":      item_type,
        "days_spent":     days,
        "materials_gp":   actual_spent,
        "success_chance": success_pct,
        "roll":           roll,
        "success":        success,
        "item_added":     success,
        "xp_awarded":     xp_award,
        "xp_details":     xp_result.get("xp_awards", []),
        "calendar":       cal,
        "note":           craft_note,
        "dm_note": (
            "Item added to PC inventory on success. For scrolls, also call "
            "update_world_fact(category='spellbook_contents') to note the spell. "
            "For potions and minor_magic items, describe the effect in a follow-up "
            "save_turn. Failed crafting loses half the materials; retry is always allowed."
        ),
    }
    _log_downtime("craft_item", result)
    return result


# ==============================================================================
# PHASE 5C — LOYALTY & AGING SYSTEM
# Henchman loyalty · Morale events · Campaign calendar · Character aging
# ==============================================================================

# ------------------------------------------------------------------------------
# LOYALTY CONSTANTS
# Loyalty scores: 2-12 integer (same range as 2d6 roll)
# 2d6 check: roll 2d6; roll <= loyalty_score → pass
# On a natural 12, additional narrative consequence regardless of loyalty score.
# ------------------------------------------------------------------------------

_LOYALTY_LABELS: dict[int, str] = {
    12: "unshakeable",
    11: "devoted",
    10: "devoted",
    9:  "steadfast",
    8:  "steadfast",
    7:  "reliable",
    6:  "reliable",
    5:  "wavering",
    4:  "wavering",
    3:  "at_risk",
    2:  "at_risk",
}

# CHA score → loyalty bonus applied to initial score (not to checks)
_LOYALTY_CHA_INIT_BONUS: dict = {
    # (min, max): bonus
    (18, 18): +3,
    (16, 17): +2,
    (13, 15): +1,
    (9,  12):  0,
    (6,   8): -1,
    (3,   5): -2,
}

# Relationship type/notes keywords → base score before CHA modifier
_LOYALTY_RELATION_BASE: list[tuple[str, int]] = [
    # Checked in order; first match wins
    ("absolute",     11),
    ("deeply loyal", 10),
    ("fanatical",    11),
    ("devoted",      10),
    ("trusted",      10),
    ("founded",      10),
    ("ally",          9),
    ("allied",        9),
    ("protected",     8),
    ("hired",         8),
    ("staff",         8),
    ("faculty",       8),
    ("ward",          6),
    ("probationary",  5),
]

# 2d6 roll outcome tiers relative to loyalty score
# margin = roll - loyalty_score  (negative = passed with room; positive = failed)
_LOYALTY_CHECK_TIERS: list[tuple[range, str, str]] = [
    (range(-99, -2),  "strong_pass",     "Unwavering. No hesitation."),
    (range(-2,   0),  "pass",            "Holds firm. Carries out the order."),
    (range(0,    1),  "barely_pass",     "Complies, but with visible reluctance or a quiet complaint."),
    (range(1,    2),  "grumbling",       "Grumbles openly. Obeys, but morale note should be recorded."),
    (range(2,    4),  "demands",         "Refuses without concession: raise, acknowledgement, or explanation required."),
    (range(4,  100),  "desertion_risk",  "Serious loyalty crisis. Will desert or act against interests if not addressed immediately."),
]

# Natural-12 rider (applies on top of normal result for loyalty 12+ only — still note-worthy)
_LOYALTY_NAT_12_RIDER = "Even the most devoted will remember this was asked of them."

# Henchman monthly morale event results (2d6 + modifier, clamped 2-12)
_MORALE_EVENT_TABLE: list[tuple[range, str, str, int]] = [
    # (roll_range, label, description, loyalty_delta)
    (range(12, 13), "increased_devotion",
     "Reflects on recent events and feels more committed than ever.",              +1),
    (range(10, 12), "steady",
     "No change. Performs duties with characteristic reliability.",                 0),
    (range(8, 10),  "mild_grumbling",
     "Minor complaint — working conditions, recognition, or a small grievance.",    0),
    (range(6,  8),  "demands",
     "Raises a specific demand: raise, promotion, time off, or public acknowledgement.", 0),
    (range(4,  6),  "troubled",
     "Visibly troubled. Something is wrong — recent events, rumours, or personal concern.", -1),
    (range(2,  4),  "crisis",
     "On the edge. Loyalty check required immediately or desertion becomes likely.", -1),
]

# ------------------------------------------------------------------------------
# AGING CONSTANTS
# Thresholds in years: (Middle Age, Old, Venerable)
# Effects apply once when a threshold is crossed; cumulative thereafter.
# ------------------------------------------------------------------------------

_RACE_AGE_THRESHOLDS: dict[str, tuple[int, int, int]] = {
    "Human":    (40,  60,   90),
    "Elf":      (350, 700, 1000),
    "Half-Elf": (62,  93,  125),
    "Dwarf":    (150, 250, 350),
    "Halfling": (50,  70,   90),
    "Half-Orc": (30,  45,   60),
    "Gnome":    (100, 150, 200),
}

# Maximum natural age range (start_max, end_max) for lifespan reference
_RACE_MAX_AGE: dict[str, tuple[int, int]] = {
    "Human":    (90,  120),
    "Elf":      (1200, 2000),
    "Half-Elf": (160, 200),
    "Dwarf":    (400, 450),
    "Halfling": (100, 120),
    "Half-Orc": (65,  80),
    "Gnome":    (250, 300),
}

# Ability changes when hitting each threshold (applied once, cumulative)
_AGING_EFFECTS: dict[str, dict[str, int]] = {
    "middle_age": {
        "strength":     -1,
        "constitution": -1,
        "wisdom":       +1,
    },
    "old": {
        "strength":     -2,
        "dexterity":    -1,
        "constitution": -1,
        "wisdom":       +1,
    },
    "venerable": {
        "strength":     -1,
        "dexterity":    -1,
        "constitution": -1,
        "wisdom":       +1,
    },
}

# Greyhawk calendar months for year-parsing
_GH_MONTHS: list[str] = [
    "Needfest",    # festival week 0
    "Fireseek",    # 1
    "Readying",    # 2
    "Coldeven",    # 3
    "Growfest",    # festival
    "Planting",    # 4
    "Flocktime",   # 5
    "Wealsun",     # 6
    "Richfest",    # festival
    "Reaping",     # 7
    "Goodmonth",   # 8
    "Harvester",   # 9
    "Brewfest",    # festival
    "Patchwall",   # 10
    "Ready'reat",  # 11
    "Sunsebb",     # 12
]

# Default campaign start year
_DEFAULT_START_YEAR_CY = 576


# ------------------------------------------------------------------------------
# LOYALTY HELPERS
# ------------------------------------------------------------------------------

def _cha_init_bonus(cha_score: int) -> int:
    """Return initial loyalty score bonus based on Charisma."""
    for (lo, hi), bonus in _LOYALTY_CHA_INIT_BONUS.items():
        if lo <= cha_score <= hi:
            return bonus
    return 0


def _relation_base_score(rel_type: str, notes: str) -> int:
    """
    Determine base loyalty score from relationship type and notes text.
    Returns an integer 2-12.
    """
    combined = (rel_type + " " + (notes or "")).lower()
    for (keyword, score) in _LOYALTY_RELATION_BASE:
        if keyword in combined:
            return score
    return 7   # default: neutral hired relationship


def _score_label(score: int) -> str:
    return _LOYALTY_LABELS.get(max(2, min(12, score)), "unknown")


def _get_all_loyalty_records() -> list[dict]:
    """Fetch all loyalty world_facts rows."""
    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT world_fact_id, fact_text FROM world_facts "
            "WHERE campaign_id = ? AND category = 'loyalty'",
            (_CAMPAIGN_ID,),
        )
        rows = cur.fetchall()
    result = []
    for row in rows:
        try:
            d = json.loads(row["fact_text"])
            d["_fact_id"] = row["world_fact_id"]
            result.append(d)
        except (json.JSONDecodeError, TypeError):
            pass
    return result


def _get_loyalty_record(name: str) -> dict | None:
    """Return the loyalty record for a named entity, or None."""
    records = _get_all_loyalty_records()
    name_lower = name.lower().strip()
    for r in records:
        if r.get("name", "").lower() == name_lower:
            return r
    return None


def _save_loyalty_record(data: dict) -> None:
    """Upsert a loyalty record (keyed by name)."""
    fact_id = data.pop("_fact_id", None)
    payload = json.dumps(data)
    with _get_conn() as conn:
        if fact_id:
            conn.execute(
                "UPDATE world_facts SET fact_text = ? WHERE world_fact_id = ?",
                (payload, fact_id),
            )
        else:
            conn.execute(
                "INSERT INTO world_facts (campaign_id, category, fact_text, source_note) "
                "VALUES (?, 'loyalty', ?, ?)",
                (_CAMPAIGN_ID, payload, data.get("name", "unknown")),
            )


def _initialize_loyalty_scores() -> list[dict]:
    """
    Bootstrap loyalty records from the relationships and troops tables.
    Called once when loyalty records do not yet exist.
    Returns the list of initialized records.
    """
    cha_score = _get_pc_ability("charisma")
    cha_bonus = _cha_init_bonus(cha_score)
    initialized = []

    # ── Named NPCs with relationships to the PC ───────────────────────────────
    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT c.character_id, c.name, c.character_type, c.race,
                      r.relationship_type, r.notes
               FROM characters c
               JOIN relationships r ON r.target_character_id = c.character_id
               WHERE r.source_character_id = ? AND c.character_id != ?
                 AND c.character_type != 'PC'""",
            (_PC_CHARACTER_ID, _PC_CHARACTER_ID),
        )
        npc_rows = [dict(r) for r in cur.fetchall()]

    for npc in npc_rows:
        base = _relation_base_score(
            npc.get("relationship_type", ""),
            npc.get("notes", ""),
        )
        score = max(2, min(12, base + cha_bonus))

        # Constructs and undead get fixed loyalty (magical compulsion / undying service)
        race_lower = (npc.get("race") or "").lower()
        if any(k in race_lower for k in ("construct", "undead", "lich", "spirit armor")):
            score = 12  # constructs don't defect

        record = {
            "name":              npc["name"],
            "entity_type":       "npc",
            "entity_id":         npc["character_id"],
            "race":              npc.get("race", "Unknown"),
            "score":             score,
            "base_score":        base,
            "cha_bonus_applied": cha_bonus,
            "status":            _score_label(score),
            "last_check_date":   None,
            "last_event":        "initialized from relationship data",
            "at_risk":           score <= 5,
            "adjustment_history": [],
        }
        _save_loyalty_record(record)
        initialized.append(record)

    # ── Troop groups ──────────────────────────────────────────────────────────
    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT troop_id, group_name, troop_type, count, notes "
            "FROM troops WHERE campaign_id = ?",
            (_CAMPAIGN_ID,),
        )
        troop_rows = [dict(r) for r in cur.fetchall()]

    for troop in troop_rows:
        troop_type_lower = (troop.get("troop_type") or "").lower()
        notes_lower      = (troop.get("notes") or "").lower()

        # Constructs have no morale
        if any(k in troop_type_lower for k in ("construct", "spirit", "animated")):
            score = 12
        else:
            # Troops default to 7 (hired/realm soldiers); adjust by notes
            base = 7
            if "absolute"    in notes_lower: base = 11
            if "trusted"     in notes_lower: base = 10
            if "well paid"   in notes_lower: base += 1
            if "trained"     in notes_lower: base += 1
            if "loyalty"     in notes_lower: base += 1
            score = max(2, min(12, base + cha_bonus))

        record = {
            "name":              troop["group_name"],
            "entity_type":       "troop",
            "entity_id":         troop["troop_id"],
            "troop_type":        troop.get("troop_type", "Unknown"),
            "count":             troop.get("count", 0),
            "score":             score,
            "base_score":        base if "base" in dir() else 7,
            "cha_bonus_applied": cha_bonus,
            "status":            _score_label(score),
            "last_check_date":   None,
            "last_event":        "initialized from troop roster",
            "at_risk":           score <= 5,
            "adjustment_history": [],
        }
        _save_loyalty_record(record)
        initialized.append(record)

    return initialized


# ------------------------------------------------------------------------------
# AGING HELPERS
# ------------------------------------------------------------------------------

def _get_aging_record(character_id: int) -> dict | None:
    """Return aging record from world_facts for given character_id."""
    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT world_fact_id, fact_text FROM world_facts "
            "WHERE campaign_id = ? AND category = 'character_aging' "
            "AND source_note = ? LIMIT 1",
            (_CAMPAIGN_ID, str(character_id)),
        )
        row = cur.fetchone()
    if not row:
        return None
    try:
        d = json.loads(row["fact_text"])
        d["_fact_id"] = row["world_fact_id"]
        return d
    except (json.JSONDecodeError, TypeError):
        return None


def _save_aging_record(data: dict) -> None:
    """Upsert aging record for a character."""
    fact_id = data.pop("_fact_id", None)
    char_id = data.get("character_id", 0)
    payload = json.dumps(data)
    with _get_conn() as conn:
        if fact_id:
            conn.execute(
                "UPDATE world_facts SET fact_text = ? WHERE world_fact_id = ?",
                (payload, fact_id),
            )
        else:
            conn.execute(
                "INSERT INTO world_facts "
                "(campaign_id, category, fact_text, source_note) "
                "VALUES (?, 'character_aging', ?, ?)",
                (_CAMPAIGN_ID, payload, str(char_id)),
            )


def _init_aging_record(character_id: int) -> dict:
    """
    Create a fresh aging record for a character if none exists.
    Assumes campaign is in 576 CY; PC race is read from DB.
    For elves, starting age is typically 100-400+ (use 120 as a young-adult default).
    """
    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT name, race FROM characters WHERE character_id = ?",
            (character_id,),
        )
        row = cur.fetchone()
    name = row["name"] if row else "Unknown"
    race = row["race"] if row else "Human"

    # Sensible starting ages by race (young adult in 576 CY)
    DEFAULT_START_AGES: dict[str, int] = {
        "Human":    25,
        "Elf":      120,
        "Half-Elf": 35,
        "Dwarf":    60,
        "Halfling": 28,
        "Half-Orc": 18,
        "Gnome":    50,
    }
    start_age = DEFAULT_START_AGES.get(race, 25)
    thresholds = _RACE_AGE_THRESHOLDS.get(race, _RACE_AGE_THRESHOLDS["Human"])

    # Determine current aging stage
    if start_age >= thresholds[2]:
        stage = "venerable"
    elif start_age >= thresholds[1]:
        stage = "old"
    elif start_age >= thresholds[0]:
        stage = "middle_age"
    else:
        stage = "young"

    record = {
        "character_id":         character_id,
        "name":                 name,
        "race":                 race,
        "start_age":            start_age,
        "current_age":          start_age,
        "campaign_days_elapsed": 0,
        "aging_stage":          stage,
        "thresholds_passed":    [],
        "ability_changes_applied": {},
        "next_threshold_age":   next(
            (t for t in thresholds if t > start_age), None
        ),
    }
    _save_aging_record(record)
    return record


def _age_label(age: int, thresholds: tuple[int, int, int]) -> str:
    if age >= thresholds[2]: return "venerable"
    if age >= thresholds[1]: return "old"
    if age >= thresholds[0]: return "middle_age"
    return "young"


def _extract_year_from_calendar(cal_text: str) -> int | None:
    """Parse a 4-digit year from a calendar string like 'Fireseek 12, 576 CY'."""
    import re as _re
    m = _re.search(r"\b(\d{3,4})\s*CY\b", cal_text, _re.IGNORECASE)
    return int(m.group(1)) if m else None


# ------------------------------------------------------------------------------
# PUBLIC LOYALTY FUNCTIONS
# ------------------------------------------------------------------------------

def db_get_loyalty_state() -> dict:
    """
    Return loyalty scores for all NPCs and troop groups.
    Auto-initializes from relationship/troop data on first call.
    Flags any entity with score <= 5 as at_risk.
    """
    records = _get_all_loyalty_records()

    # Auto-initialize if no records exist yet
    if not records:
        records = _initialize_loyalty_scores()

    npcs   = [r for r in records if r.get("entity_type") == "npc"]
    troops = [r for r in records if r.get("entity_type") == "troop"]
    at_risk = [r["name"] for r in records if r.get("score", 10) <= 5]

    # Sort each list by score descending
    npcs.sort(  key=lambda r: r.get("score", 0), reverse=True)
    troops.sort(key=lambda r: r.get("score", 0), reverse=True)

    # Strip internal _fact_id from output
    def _clean(r: dict) -> dict:
        return {k: v for k, v in r.items() if not k.startswith("_")}

    return {
        "npcs":          [_clean(r) for r in npcs],
        "troops":        [_clean(r) for r in troops],
        "at_risk":       at_risk,
        "total_tracked": len(records),
        "dm_note": (
            "Loyalty score 2-12. Check: roll 2d6 <= score to remain loyal. "
            "Score 12 = unshakeable; score 7 = reliable (even odds); "
            "score 5 or below = AT RISK. Call loyalty_check for specific events."
        ),
    }


def db_loyalty_check(
    entity_name:   str,
    situation:     str,
    modifier:      int         = 0,
    calendar_note: str         = "",
) -> dict:
    """
    Roll 2d6 loyalty check for a named NPC or troop group.
    modifier: -3 to +3 (negative = worse situation, positive = better).
    Returns result tier, consequence, and whether loyalty score changes.
    """
    record = _get_loyalty_record(entity_name)
    if not record:
        # Try partial match
        all_records = _get_all_loyalty_records()
        name_lower  = entity_name.lower()
        record      = next(
            (r for r in all_records if name_lower in r.get("name", "").lower()),
            None
        )
    if not record:
        return {"error": f"No loyalty record for '{entity_name}'. Call get_loyalty_state first."}

    fact_id = record.pop("_fact_id", None)
    score   = record.get("score", 7)

    # Roll 2d6 with situation modifier
    die1 = random.randint(1, 6)
    die2 = random.randint(1, 6)
    raw  = die1 + die2
    roll = max(2, min(12, raw + modifier))

    margin = roll - score  # negative = passed; positive = failed

    # Determine outcome tier
    tier_label = "desertion_risk"
    tier_desc  = "Critical failure."
    for (rng, label, desc) in _LOYALTY_CHECK_TIERS:
        if margin in rng:
            tier_label = label
            tier_desc  = desc
            break

    # Natural 12 rider (even on pass — memorable moment)
    nat_12_note = _LOYALTY_NAT_12_RIDER if raw == 12 else ""

    # Loyalty score changes from check results
    score_delta = 0
    if tier_label == "desertion_risk":
        score_delta = -1
    elif tier_label == "demands" and modifier < -1:
        score_delta = -1

    new_score = max(2, min(12, score + score_delta))
    passed    = margin <= 0

    # Update record
    record["score"]           = new_score
    record["status"]          = _score_label(new_score)
    record["at_risk"]         = new_score <= 5
    record["last_check_date"] = calendar_note or "recent"
    record["last_event"]      = f"Loyalty check: {situation} | roll={roll} vs {score} → {tier_label}"
    if score_delta != 0:
        record.setdefault("adjustment_history", []).append({
            "date":   calendar_note or "recent",
            "delta":  score_delta,
            "reason": f"loyalty_check consequence: {tier_label}",
        })

    record["_fact_id"] = fact_id
    _save_loyalty_record(record)

    return {
        "entity_name":     entity_name,
        "entity_type":     record.get("entity_type", "npc"),
        "situation":       situation,
        "score_before":    score,
        "score_after":     new_score,
        "status":          _score_label(new_score),
        "dice_rolled":     [die1, die2],
        "raw_roll":        raw,
        "modifier":        modifier,
        "adjusted_roll":   roll,
        "margin":          margin,
        "passed":          passed,
        "outcome_tier":    tier_label,
        "consequence":     tier_desc,
        "nat_12_note":     nat_12_note,
        "score_changed":   score_delta != 0,
        "dm_note": (
            "For 'grumbling' / 'demands', narrate the specific complaint "
            "and call adjust_loyalty when it is addressed. For 'desertion_risk', "
            "an immediate intervention is needed — gifts, explanation, promotion, or "
            "a personal appeal. Loyalty score drops by 1 on serious failures."
        ),
    }


def db_adjust_loyalty(
    entity_name:   str,
    delta:         int,
    reason:        str,
    calendar_note: str = "",
) -> dict:
    """
    Modify a loyalty score by delta (positive or negative).
    Call after gifts, promotions, betrayals, deaths of comrades, pay raises, etc.
    """
    record = _get_loyalty_record(entity_name)
    if not record:
        all_records = _get_all_loyalty_records()
        name_lower  = entity_name.lower()
        record      = next(
            (r for r in all_records if name_lower in r.get("name", "").lower()),
            None
        )
    if not record:
        return {"error": f"No loyalty record for '{entity_name}'. Call get_loyalty_state first."}

    fact_id     = record.pop("_fact_id", None)
    score_before = record.get("score", 7)
    new_score   = max(2, min(12, score_before + delta))

    record["score"]      = new_score
    record["status"]     = _score_label(new_score)
    record["at_risk"]    = new_score <= 5
    record["last_event"] = f"Adjusted {delta:+d}: {reason}"
    record.setdefault("adjustment_history", []).append({
        "date":   calendar_note or "recent",
        "delta":  delta,
        "reason": reason,
    })
    record["_fact_id"] = fact_id
    _save_loyalty_record(record)

    return {
        "entity_name":   entity_name,
        "reason":        reason,
        "score_before":  score_before,
        "delta":         delta,
        "score_after":   new_score,
        "status_before": _score_label(score_before),
        "status_after":  _score_label(new_score),
        "at_risk":       new_score <= 5,
        "dm_note": (
            "Score is capped 2-12. Typical adjustments: "
            "gift/raise +1; major victory +1; betrayal of trust -2; "
            "comrade killed -1; ignored demand -1; public praise +1."
        ),
    }


def db_henchman_morale_event(
    month_label:     str,
    global_modifier: int   = 0,
    calendar_note:   str   = "",
) -> dict:
    """
    Monthly morale roll for all named NPCs (entity_type='npc').
    global_modifier: -3 to +3 applied to every roll.
      Positive: recent victory, wages paid, good leadership.
      Negative: recent defeat, unpaid wages, PC absent.
    Returns a report for each NPC with result and any loyalty score changes.
    """
    records = _get_all_loyalty_records()
    if not records:
        records = _initialize_loyalty_scores()

    npc_records = [r for r in records if r.get("entity_type") == "npc"]

    reports = []
    for record in npc_records:
        fact_id = record.pop("_fact_id", None)
        score   = record.get("score", 7)

        die1  = random.randint(1, 6)
        die2  = random.randint(1, 6)
        raw   = die1 + die2
        roll  = max(2, min(12, raw + global_modifier))

        # Look up result in morale event table
        event_label = "steady"
        event_desc  = "No change."
        loyalty_delta = 0
        for (rng, label, desc, delta) in _MORALE_EVENT_TABLE:
            if roll in rng:
                event_label   = label
                event_desc    = desc
                loyalty_delta = delta
                break

        new_score = max(2, min(12, score + loyalty_delta))
        record["score"]      = new_score
        record["status"]     = _score_label(new_score)
        record["at_risk"]    = new_score <= 5
        record["last_event"] = f"Monthly morale ({month_label}): {event_label}"
        if loyalty_delta != 0:
            record.setdefault("adjustment_history", []).append({
                "date":   calendar_note or month_label,
                "delta":  loyalty_delta,
                "reason": f"monthly_morale_event: {event_label}",
            })

        record["_fact_id"] = fact_id
        _save_loyalty_record(record)

        reports.append({
            "name":          record.get("name"),
            "dice":          [die1, die2],
            "raw_roll":      raw,
            "modifier":      global_modifier,
            "adjusted_roll": roll,
            "event_label":   event_label,
            "description":   event_desc,
            "score_before":  score,
            "score_after":   new_score,
            "status":        _score_label(new_score),
            "at_risk":       new_score <= 5,
        })

    at_risk   = [r["name"] for r in reports if r["at_risk"]]
    demands   = [r["name"] for r in reports if r["event_label"] == "demands"]
    devoted   = [r["name"] for r in reports if r["event_label"] == "increased_devotion"]
    crises    = [r["name"] for r in reports if r["event_label"] == "crisis"]

    return {
        "month":            month_label,
        "global_modifier":  global_modifier,
        "npcs_checked":     len(reports),
        "reports":          reports,
        "summary": {
            "increased_devotion": devoted,
            "demands":            demands,
            "at_risk":            at_risk,
            "crisis":             crises,
        },
        "dm_note": (
            "Address 'demands' within the next session or loyalty drops by 1. "
            "'crisis' requires immediate loyalty_check. 'increased_devotion' "
            "is a good moment for a personal scene with that NPC."
        ),
    }


# ------------------------------------------------------------------------------
# PUBLIC AGING / CALENDAR FUNCTIONS
# ------------------------------------------------------------------------------

def db_advance_time(
    days:          int,
    calendar_note: str = "",
) -> dict:
    """
    Advance the campaign calendar by the given number of days.
    Updates the 'calendar' world_fact. Checks for aging threshold crossings
    and flags overdue religious observances.
    """
    days = max(1, days)

    # Load or initialize PC aging record
    aging = _get_aging_record(_PC_CHARACTER_ID)
    if not aging:
        aging = _init_aging_record(_PC_CHARACTER_ID)

    race       = aging.get("race", "Human")
    thresholds = _RACE_AGE_THRESHOLDS.get(race, _RACE_AGE_THRESHOLDS["Human"])

    old_age         = aging.get("current_age", 25)
    old_days        = aging.get("campaign_days_elapsed", 0)
    new_days        = old_days + days

    # Convert days → fractional years (365.25 days/year)
    years_elapsed   = new_days / 365.25
    new_age         = aging.get("start_age", 25) + years_elapsed
    old_stage       = aging.get("aging_stage", "young")
    new_stage       = _age_label(new_age, thresholds)

    # Detect threshold crossings
    threshold_names = ["middle_age", "old", "venerable"]
    threshold_ages  = list(thresholds)
    newly_crossed   = []
    for i, t_age in enumerate(threshold_ages):
        t_name = threshold_names[i]
        if old_age < t_age <= new_age and t_name not in aging.get("thresholds_passed", []):
            newly_crossed.append(t_name)

    # Update aging record
    aging["current_age"]           = round(new_age, 2)
    aging["campaign_days_elapsed"] = new_days
    aging["aging_stage"]           = new_stage
    aging["thresholds_passed"]     = aging.get("thresholds_passed", []) + newly_crossed
    aging["next_threshold_age"]    = next(
        (t for t in thresholds if t > new_age), None
    )
    _save_aging_record(aging)

    # Update campaign calendar
    cal = _downtime_advance_calendar(days, calendar_note)

    # Check overdue religious observances
    overdue_deities = []
    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT fact_text FROM world_facts "
            "WHERE campaign_id = ? AND category = 'religious_obligations'",
            (_CAMPAIGN_ID,),
        )
        obs_rows = cur.fetchall()
    for obs_row in obs_rows:
        try:
            obs = json.loads(obs_row["fact_text"])
            if obs.get("missed_count", 0) >= 3:
                overdue_deities.append({
                    "deity":        obs.get("deity"),
                    "missed_count": obs.get("missed_count"),
                    "penalty":      "losing highest spell level until atonement",
                })
        except (json.JSONDecodeError, TypeError):
            pass

    return {
        "days_advanced":       days,
        "calendar":            cal,
        "character":           aging.get("name"),
        "race":                race,
        "age_before":          round(old_age, 2),
        "age_after":           round(new_age, 2),
        "aging_stage_before":  old_stage,
        "aging_stage_after":   new_stage,
        "thresholds_crossed":  newly_crossed,
        "next_threshold_age":  aging["next_threshold_age"],
        "aging_check_needed":  len(newly_crossed) > 0,
        "overdue_observances": overdue_deities,
        "dm_note": (
            "If aging_check_needed is True, call aging_check() immediately. "
            "If overdue_observances is non-empty, remind the cleric to perform "
            "religious_observance(). For significant time jumps (seasons), "
            "consider calling henchman_morale_event() for the elapsed months."
        ),
    }


def db_aging_check(
    character_id:    int,
    threshold_stage: str,
) -> dict:
    """
    Apply aging ability score changes when a character crosses an age threshold.
    threshold_stage: 'middle_age' | 'old' | 'venerable'
    Modifies character_abilities in the DB and records the changes.
    """
    effects = _AGING_EFFECTS.get(threshold_stage)
    if not effects:
        return {"error": f"Unknown threshold_stage '{threshold_stage}'. "
                         "Use: middle_age, old, venerable"}

    # Load current ability scores
    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT strength, intelligence, wisdom, dexterity, constitution, charisma "
            "FROM character_abilities WHERE character_id = ?",
            (character_id,),
        )
        row = cur.fetchone()
    if not row:
        return {"error": f"No ability scores found for character_id={character_id}"}

    abilities_before = dict(row)
    abilities_after  = dict(row)

    for stat, delta in effects.items():
        before_val                = abilities_after.get(stat, 10)
        abilities_after[stat]     = max(3, before_val + delta)   # floor at 3

    # Apply changes
    with _get_conn() as conn:
        conn.execute(
            """UPDATE character_abilities
               SET strength=?, dexterity=?, constitution=?, wisdom=?
               WHERE character_id=?""",
            (
                abilities_after["strength"],
                abilities_after["dexterity"],
                abilities_after["constitution"],
                abilities_after["wisdom"],
                character_id,
            ),
        )

    # Record in aging record
    aging = _get_aging_record(character_id)
    if aging:
        applied = aging.get("ability_changes_applied", {})
        applied[threshold_stage] = effects
        aging["ability_changes_applied"] = applied
        _save_aging_record(aging)

    changes = {
        stat: {"before": abilities_before[stat], "after": abilities_after[stat], "delta": delta}
        for stat, delta in effects.items()
    }

    return {
        "character_id":      character_id,
        "threshold_stage":   threshold_stage,
        "ability_changes":   changes,
        "abilities_before":  abilities_before,
        "abilities_after":   abilities_after,
        "dm_note": (
            f"Aging effects for {threshold_stage} applied permanently. "
            "Abilities cannot drop below 3 from aging. Wisdom gains are cumulative "
            "across all thresholds: a character reaching venerable from young gains "
            "+3 Wis total. These changes are reflected in character_abilities."
        ),
    }


def db_get_character_age(character_id: int) -> dict:
    """
    Return current age, race, aging stage, next threshold, and years remaining.
    Auto-initializes the aging record if it doesn't exist.
    """
    aging = _get_aging_record(character_id)
    if not aging:
        aging = _init_aging_record(character_id)

    race       = aging.get("race", "Human")
    thresholds = _RACE_AGE_THRESHOLDS.get(race, _RACE_AGE_THRESHOLDS["Human"])
    max_ages   = _RACE_MAX_AGE.get(race, (90, 120))
    cur_age    = aging.get("current_age", 25)
    next_t     = aging.get("next_threshold_age")

    threshold_info = {
        "middle_age": thresholds[0],
        "old":        thresholds[1],
        "venerable":  thresholds[2],
    }

    return {
        "character_id":         character_id,
        "name":                 aging.get("name"),
        "race":                 race,
        "current_age":          round(cur_age, 1),
        "aging_stage":          aging.get("aging_stage", "young"),
        "thresholds":           threshold_info,
        "thresholds_passed":    aging.get("thresholds_passed", []),
        "next_threshold_age":   next_t,
        "years_to_next_check":  round(next_t - cur_age, 1) if next_t else None,
        "natural_lifespan_max": max_ages[1],
        "campaign_days_elapsed": aging.get("campaign_days_elapsed", 0),
        "ability_changes_applied": aging.get("ability_changes_applied", {}),
        "dm_note": (
            "Aging checks are triggered by advance_time() when a threshold is crossed. "
            "Elves rarely reach middle_age in a campaign context (threshold: 350 years). "
            "Call aging_check(character_id, threshold_stage) when a threshold is crossed."
        ),
    }


# ==============================================================================
# PHASE 5D — SIEGE MECHANICS (FINAL PHASE)
# start_siege · siege_turn · artillery_fire · assault
# get_siege_state · negotiate_surrender
# ==============================================================================

# ------------------------------------------------------------------------------
# SIEGE CONSTANTS
# ------------------------------------------------------------------------------

# Fortification type → (wall_integrity, gate_integrity, base_resist, label)
# base_resist: subtracted from artillery damage (stone construction hardness)
_FORT_TYPES: dict[str, dict] = {
    "palisade":      {"wall": 40,  "gate": 30, "resist": 2, "label": "Wooden palisade"},
    "tower":         {"wall": 60,  "gate": 50, "resist": 4, "label": "Stone tower"},
    "keep":          {"wall": 80,  "gate": 70, "resist": 6, "label": "Stone keep"},
    "castle":        {"wall": 100, "gate": 90, "resist": 8, "label": "Castle"},
    "city_walls":    {"wall": 100, "gate": 80, "resist": 7, "label": "City walls"},
    "fortified_mill":{"wall": 50,  "gate": 40, "resist": 3, "label": "Fortified mill"},
    "fortress":      {"wall": 100, "gate": 90, "resist": 9, "label": "Fortress"},
}

# Artillery type → {thac0, wall_dice, wall_sides, pers_dice, pers_sides, crew}
_ARTILLERY_TYPES: dict[str, dict] = {
    "light_catapult": {
        "thac0": 15, "wall_d": 1, "wall_s": 10,
        "pers_d": 1, "pers_s": 10, "crew": 4,
        "label": "Light catapult",
    },
    "heavy_catapult": {
        "thac0": 12, "wall_d": 2, "wall_s": 10,
        "pers_d": 2, "pers_s": 10, "crew": 6,
        "label": "Heavy catapult",
    },
    "stone_caster": {
        "thac0": 12, "wall_d": 2, "wall_s": 10,
        "pers_d": 2, "pers_s": 10, "crew": 3,
        "label": "Stone-caster (ogre-operated)",
        "crew_names": ["Brak", "Hurn", "Tollug"],
        "crew_bonus_hit": 2,    # ogre Str bonus to hit
        "crew_bonus_dmg_d": 1,  # extra d6 damage from ogre power
        "crew_bonus_dmg_s": 6,
    },
    "ballista": {
        "thac0": 13, "wall_d": 1, "wall_s": 4,
        "pers_d": 2, "pers_s": 6, "crew": 3,
        "label": "Ballista",
    },
    "trebuchet": {
        "thac0": 10, "wall_d": 3, "wall_s": 10,
        "pers_d": 3, "pers_s": 10, "crew": 10,
        "label": "Trebuchet",
    },
}

# Supply consumption: units per side per week (1 unit = supplies for 10 men 1 week)
# Attacker: 1 unit per 10 men; Defender: 1 unit per 8 men (siege rationing harder)
_SUPPLY_PER_10_MEN_ATTACKER = 1
_SUPPLY_PER_10_MEN_DEFENDER = 1   # weeks, not units — stored directly as weeks remaining

# Surrender terms result table
_SURRENDER_TERMS: list[tuple[int, str, str]] = [
    # (min_roll, label, description)
    (20, "unconditional",
     "Unconditional surrender. Garrison lays down arms; all terms dictated by victor."),
    (16, "honourable",
     "Honourable terms. Garrison departs with personal weapons; no looting of civilians."),
    (12, "hard",
     "Hard terms. Officers ransomed; common soldiers disarmed; limited looting permitted."),
    (8,  "refused",
     "Terms refused. Defenders will fight to the last. Parley buys time, nothing more."),
    (4,  "insulted",
     "Offer insulted. The garrison commander is offended; morale improves for defenders."),
    (1,  "betrayal",
     "Betrayal. Defenders use parley to launch a surprise sally. Initiative goes to defenders."),
]


# ------------------------------------------------------------------------------
# SIEGE STATE HELPERS
# ------------------------------------------------------------------------------

def _get_siege_state() -> dict | None:
    """Return the active siege state from world_facts, or None."""
    with _get_conn(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT world_fact_id, fact_text FROM world_facts "
            "WHERE campaign_id = ? AND category = 'siege_state' LIMIT 1",
            (_CAMPAIGN_ID,),
        )
        row = cur.fetchone()
    if not row:
        return None
    try:
        d = json.loads(row["fact_text"])
        d["_fact_id"] = row["world_fact_id"]
        return d
    except (json.JSONDecodeError, TypeError):
        return None


def _set_siege_state(state: dict) -> None:
    """Upsert the siege state world_fact."""
    fact_id = state.pop("_fact_id", None)
    payload = json.dumps(state)
    with _get_conn() as conn:
        if fact_id:
            conn.execute(
                "UPDATE world_facts SET fact_text = ? WHERE world_fact_id = ?",
                (payload, fact_id),
            )
        else:
            conn.execute(
                "INSERT INTO world_facts (campaign_id, category, fact_text, source_note) "
                "VALUES (?, 'siege_state', ?, 'siege_engine')",
                (_CAMPAIGN_ID, payload),
            )


def _clear_siege_state() -> None:
    """Remove the siege state when a siege ends."""
    with _get_conn() as conn:
        conn.execute(
            "DELETE FROM world_facts WHERE campaign_id = ? AND category = 'siege_state'",
            (_CAMPAIGN_ID,),
        )


def _roll_to_hit_wall(thac0: int, bonus: int = 0) -> tuple[int, bool]:
    """Roll d20 + bonus to hit wall AC 8. Returns (roll, hit)."""
    roll = random.randint(1, 20)
    return roll, (roll + bonus) >= (thac0 - 8 + 1)   # needs thac0 - AC


def _pct_casualties(count: int, pct: float) -> int:
    """Convert a percentage casualty rate to a whole-number count."""
    return max(0, round(count * pct / 100))


# ------------------------------------------------------------------------------
# PUBLIC SIEGE FUNCTIONS
# ------------------------------------------------------------------------------

def db_start_siege(
    target_location:    str,
    fortification_type: str,
    role:               str,
    attacker_name:      str,
    attacker_count:     int,
    attacker_supplies:  int,
    defender_name:      str,
    defender_count:     int,
    defender_supplies:  int,
    artillery:          list[dict],
    calendar_note:      str = "",
) -> dict:
    """
    Initiate a new siege. Clears any existing siege state.

    artillery: list of dicts, each with keys:
      {name, type, condition}
      type must be one of: light_catapult, heavy_catapult, stone_caster,
                           ballista, trebuchet
    The stone_caster entry automatically records Brak, Hurn, and Tollug as crew.
    """
    fort_type = fortification_type.lower().strip()
    fort      = _FORT_TYPES.get(fort_type, _FORT_TYPES["keep"])
    role_str  = role.lower().strip()
    if role_str not in ("attacker", "defender"):
        return {"error": "role must be 'attacker' or 'defender'"}

    # Build artillery list with full stats
    artillery_records = []
    for art in (artillery or []):
        art_type = art.get("type", "light_catapult")
        art_data = dict(_ARTILLERY_TYPES.get(art_type, _ARTILLERY_TYPES["light_catapult"]))
        artillery_records.append({
            "name":      art.get("name", art_data["label"]),
            "type":      art_type,
            "label":     art_data["label"],
            "condition": art.get("condition", "operational"),
            "shots_fired_total": 0,
            "hits_total":        0,
            "crew":      art_data.get("crew_names", []),
        })

    attacker_side = {
        "name":           attacker_name,
        "pc_side":        role_str == "attacker",
        "count":          max(1, attacker_count),
        "supply_weeks":   max(0, attacker_supplies),
        "morale":         9,
        "casualties":     0,
        "artillery":      artillery_records if role_str == "attacker" else [],
        "has_artillery":  bool(artillery_records) and role_str == "attacker",
    }

    defender_side = {
        "name":           defender_name,
        "pc_side":        role_str == "defender",
        "count":          max(1, defender_count),
        "supply_weeks":   max(0, defender_supplies),
        "morale":         9,
        "casualties":     0,
        "artillery":      artillery_records if role_str == "defender" else [],
        "has_artillery":  bool(artillery_records) and role_str == "defender",
    }

    state = {
        "active":            True,
        "role":              role_str,
        "target_location":   target_location,
        "fortification_type": fort_type,
        "fortification_label": fort["label"],
        "start_date":        calendar_note or "campaign date",
        "weeks_elapsed":     0,
        "wall_integrity":    fort["wall"],
        "gate_integrity":    fort["gate"],
        "base_resist":       fort["resist"],
        "breach_points":     [],
        "last_assault_repelled": False,
        "attacker":          attacker_side,
        "defender":          defender_side,
        "events_log":        [],
    }

    _set_siege_state(state)

    return {
        "siege_started":        True,
        "target_location":      target_location,
        "fortification":        fort["label"],
        "role":                 role_str,
        "attacker":             attacker_name,
        "attacker_count":       attacker_count,
        "attacker_supplies":    attacker_supplies,
        "defender":             defender_name,
        "defender_count":       defender_count,
        "defender_supplies":    defender_supplies,
        "wall_integrity":       fort["wall"],
        "gate_integrity":       fort["gate"],
        "artillery_registered": len(artillery_records),
        "stone_caster_crew":    ["Brak", "Hurn", "Tollug"],
        "dm_note": (
            "Siege initiated. Call siege_turn() weekly to advance the siege. "
            "Call artillery_fire() each day of bombardment before siege_turn(). "
            "Call assault() when ready to attempt a direct attack. "
            "Breach opens when wall_integrity drops below 30."
        ),
    }


def db_siege_turn(
    mining:        bool  = False,
    calendar_note: str   = "",
) -> dict:
    """
    Resolve one week of siege operations.
    Rolls for: supply attrition, disease, sally attempt, relief attempt,
    morale degradation, and optional mining progress.
    mining: True if attacker has sappers digging under the walls.
    """
    state = _get_siege_state()
    if not state:
        return {"error": "No active siege. Call start_siege() first."}

    fact_id = state.pop("_fact_id", None)
    events: list[str] = []
    week = state["weeks_elapsed"] + 1

    # ── Supply attrition ──────────────────────────────────────────────────────
    for side_key in ("attacker", "defender"):
        side = state[side_key]
        # Each side uses 1 supply week per 100 effective combatants
        consumption = max(1, side["count"] // 100)
        old_supply  = side["supply_weeks"]
        side["supply_weeks"] = max(0, old_supply - consumption)
        if side["supply_weeks"] == 0 and old_supply > 0:
            events.append(f"{side['name']} has exhausted their supplies!")
        elif side["supply_weeks"] <= 1 and old_supply > 1:
            events.append(f"{side['name']} is down to final week of supplies.")

    # ── Morale from supply state ──────────────────────────────────────────────
    for side_key in ("attacker", "defender"):
        side = state[side_key]
        if side["supply_weeks"] == 0:
            side["morale"] = max(2, side["morale"] - 1)
            events.append(f"{side['name']} morale drops (no supplies). Morale now {side['morale']}.")
        casualty_pct = (side["casualties"] / max(1, side["count"] + side["casualties"])) * 100
        if casualty_pct > 30 and random.randint(1, 6) <= 2:
            side["morale"] = max(2, side["morale"] - 1)
            events.append(
                f"{side['name']} morale shaken by heavy losses ({casualty_pct:.0f}% casualties). "
                f"Morale now {side['morale']}."
            )

    # ── Disease check ─────────────────────────────────────────────────────────
    disease_results = {}
    for side_key in ("attacker", "defender"):
        side   = state[side_key]
        d_roll = random.randint(1, 6)
        if d_roll == 1:
            sick   = _pct_casualties(side["count"], 5)
            side["count"]      = max(0, side["count"]      - sick)
            side["casualties"] = side.get("casualties", 0) + sick
            disease_results[side_key] = {"outbreak": True, "sick": sick}
            events.append(
                f"Disease breaks out in {side['name']}! {sick} men fall ill. "
                f"Effective strength now {side['count']}."
            )
        else:
            disease_results[side_key] = {"outbreak": False}

    # ── Sally attempt (defender only) ─────────────────────────────────────────
    sally_result = {"attempted": False}
    defender     = state["defender"]
    def_morale   = defender["morale"]
    sally_roll   = random.randint(1, 6)
    if sally_roll <= 2 and def_morale >= 7:
        # Sally attempted
        attacker = state["attacker"]
        sally_strength = _pct_casualties(defender["count"], 20)  # 20% of garrison sallies
        att_loss = _pct_casualties(attacker["count"], 3)
        def_loss = _pct_casualties(sally_strength,    15)
        attacker["count"]      = max(0, attacker["count"]      - att_loss)
        attacker["casualties"] = attacker.get("casualties", 0) + att_loss
        defender["count"]      = max(0, defender["count"]      - def_loss)
        defender["casualties"] = defender.get("casualties", 0) + def_loss
        # Check if siege engine damaged
        engine_damaged = random.random() < 0.25 and attacker.get("has_artillery")
        sally_result   = {
            "attempted":      True,
            "sally_strength": sally_strength,
            "attacker_loss":  att_loss,
            "defender_loss":  def_loss,
            "engine_damaged": engine_damaged,
        }
        if engine_damaged:
            # Mark first operational engine as damaged
            for art in attacker.get("artillery", []):
                if art["condition"] == "operational":
                    art["condition"] = "damaged"
                    break
        events.append(
            f"Garrison sallies! {sally_strength} defenders attack the siege lines. "
            f"Attacker loses {att_loss} men; defender loses {def_loss}."
            + (" A siege engine is damaged in the raid!" if engine_damaged else "")
        )
    else:
        events.append("No sally this week.")

    # ── Relief attempt ────────────────────────────────────────────────────────
    relief_roll   = random.randint(1, 6)
    relief_result = {
        "spotted":     relief_roll == 1,
        "description": (
            "A relief force has been spotted in the distance! The siege may be threatened."
            if relief_roll == 1 else
            "No relief force observed this week."
        ),
    }
    if relief_roll == 1:
        events.append("Relief force spotted! Attacker may need to detach forces.")

    # ── Mining / countermining ────────────────────────────────────────────────
    mining_result = {"attempted": False}
    if mining:
        mine_roll = random.randint(1, 6)
        if mine_roll <= 1:
            mining_result = {"attempted": True, "outcome": "countermined",
                             "description": "Defenders countermined the tunnel — it collapses harmlessly."}
            events.append("Mining tunnel countermined by defenders.")
        elif mine_roll <= 4:
            wall_dmg = random.randint(5, 15)
            wall_dmg = max(0, wall_dmg - state["base_resist"])
            state["wall_integrity"] = max(0, state["wall_integrity"] - wall_dmg)
            mining_result = {"attempted": True, "outcome": "progress",
                             "wall_damage": wall_dmg,
                             "wall_integrity": state["wall_integrity"],
                             "description": f"Mining progresses. Wall integrity reduced by {wall_dmg} to {state['wall_integrity']}%."}
            events.append(f"Mining tunnel advances — wall cracked for {wall_dmg} points of damage.")
        else:
            wall_dmg = random.randint(15, 30)
            wall_dmg = max(0, wall_dmg - state["base_resist"] // 2)
            state["wall_integrity"] = max(0, state["wall_integrity"] - wall_dmg)
            if state["wall_integrity"] < 30 and "mine_collapse" not in state["breach_points"]:
                state["breach_points"].append("mine_collapse")
            mining_result = {"attempted": True, "outcome": "major_collapse",
                             "wall_damage": wall_dmg,
                             "wall_integrity": state["wall_integrity"],
                             "description": f"Mine collapses under the wall! {wall_dmg} damage — wall at {state['wall_integrity']}%."}
            events.append(f"MINE COLLAPSE! Wall breached for {wall_dmg} damage.")

    # ── Check for breach ──────────────────────────────────────────────────────
    if state["wall_integrity"] < 30 and "wall" not in state["breach_points"]:
        state["breach_points"].append("wall")
        events.append("BREACH! Wall integrity below 30% — assault is now possible at the breach.")
    if state["gate_integrity"] < 20 and "gate" not in state["breach_points"]:
        state["breach_points"].append("gate")
        events.append("GATE BREACH! Gate has been destroyed — the way is open.")

    # ── Update state ──────────────────────────────────────────────────────────
    state["weeks_elapsed"] = week
    state["events_log"]    = (state.get("events_log", []) + events)[-20:]  # keep last 20

    state["_fact_id"] = fact_id
    _set_siege_state(state)

    return {
        "week":             week,
        "events":           events,
        "supply": {
            "attacker_weeks_left": state["attacker"]["supply_weeks"],
            "defender_weeks_left": state["defender"]["supply_weeks"],
        },
        "disease":          disease_results,
        "sally":            sally_result,
        "relief":           relief_result,
        "mining":           mining_result,
        "wall_integrity":   state["wall_integrity"],
        "gate_integrity":   state["gate_integrity"],
        "breach_points":    state["breach_points"],
        "attacker_morale":  state["attacker"]["morale"],
        "defender_morale":  state["defender"]["morale"],
        "attacker_count":   state["attacker"]["count"],
        "defender_count":   state["defender"]["count"],
        "calendar":         calendar_note or f"Week {week} of siege",
        "dm_note": (
            "Call artillery_fire() between turns for bombardment damage. "
            "When breach_points is non-empty or supplies are exhausted, "
            "consider calling assault() or negotiate_surrender()."
        ),
    }


def db_artillery_fire(
    engine_name:    str,
    target:         str  = "walls",
    volleys:        int  = 1,
    calendar_note:  str  = "",
) -> dict:
    """
    Resolve one day of artillery bombardment.
    engine_name: name of the artillery piece (e.g. 'Stone-Caster').
    target: 'walls', 'gate', or 'defenders'.
    volleys: number of shots this day (typically 1-3 for catapults).
    """
    state = _get_siege_state()
    if not state:
        return {"error": "No active siege. Call start_siege() first."}

    fact_id = state.pop("_fact_id", None)

    # Find the engine
    all_artillery = (
        state["attacker"].get("artillery", []) +
        state["defender"].get("artillery", [])
    )
    engine = next(
        (a for a in all_artillery
         if a["name"].lower() == engine_name.lower()),
        None,
    )
    if engine is None:
        # Try partial match
        engine = next(
            (a for a in all_artillery
             if engine_name.lower() in a["name"].lower()),
            None,
        )
    if engine is None:
        state["_fact_id"] = fact_id
        _set_siege_state(state)
        return {"error": f"Artillery engine '{engine_name}' not found. "
                         f"Registered engines: {[a['name'] for a in all_artillery]}"}

    if engine.get("condition") == "destroyed":
        state["_fact_id"] = fact_id
        _set_siege_state(state)
        return {"error": f"'{engine_name}' is destroyed and cannot fire."}

    art_data  = _ARTILLERY_TYPES.get(engine["type"], _ARTILLERY_TYPES["heavy_catapult"])
    thac0     = art_data["thac0"]
    hit_bonus = art_data.get("crew_bonus_hit", 0)

    # Damaged engine penalty
    if engine.get("condition") == "damaged":
        thac0 += 2
        hit_bonus = max(0, hit_bonus - 1)

    volleys = max(1, min(5, volleys))
    shot_results = []
    total_wall_damage = 0
    total_gate_damage = 0
    total_pers_damage = 0

    for v in range(volleys):
        roll, hit = _roll_to_hit_wall(thac0, hit_bonus)

        if not hit:
            shot_results.append({"volley": v + 1, "roll": roll, "hit": False})
            continue

        # Calculate raw damage
        if target in ("walls", "wall"):
            raw_dmg = sum(random.randint(1, art_data["wall_s"])
                          for _ in range(art_data["wall_d"]))
            if "crew_bonus_dmg_d" in art_data:
                raw_dmg += sum(random.randint(1, art_data["crew_bonus_dmg_s"])
                               for _ in range(art_data["crew_bonus_dmg_d"]))
            actual_dmg = max(0, raw_dmg - state["base_resist"])
            state["wall_integrity"] = max(0, state["wall_integrity"] - actual_dmg)
            total_wall_damage += actual_dmg
            shot_results.append({
                "volley": v + 1, "roll": roll, "hit": True,
                "target": "walls", "raw_damage": raw_dmg,
                "resist": state["base_resist"], "net_damage": actual_dmg,
                "wall_integrity": state["wall_integrity"],
            })

        elif target in ("gate",):
            raw_dmg = sum(random.randint(1, art_data["wall_s"])
                          for _ in range(art_data["wall_d"]))
            if "crew_bonus_dmg_d" in art_data:
                raw_dmg += sum(random.randint(1, art_data["crew_bonus_dmg_s"])
                               for _ in range(art_data["crew_bonus_dmg_d"]))
            # Gates have lower resist than walls
            gate_resist = max(1, state["base_resist"] // 2)
            actual_dmg  = max(0, raw_dmg - gate_resist)
            state["gate_integrity"] = max(0, state["gate_integrity"] - actual_dmg)
            total_gate_damage += actual_dmg
            shot_results.append({
                "volley": v + 1, "roll": roll, "hit": True,
                "target": "gate", "raw_damage": raw_dmg,
                "resist": gate_resist, "net_damage": actual_dmg,
                "gate_integrity": state["gate_integrity"],
            })

        else:  # defenders
            raw_dmg = sum(random.randint(1, art_data["pers_s"])
                          for _ in range(art_data["pers_d"]))
            # Personnel damage: 1 casualty per 5 points in the impact zone
            casualties = max(0, raw_dmg // 5)
            # Hits the defender side
            target_side = state["defender"] if state["role"] == "attacker" else state["attacker"]
            target_side["count"]      = max(0, target_side["count"]      - casualties)
            target_side["casualties"] = target_side.get("casualties", 0) + casualties
            total_pers_damage += casualties
            shot_results.append({
                "volley": v + 1, "roll": roll, "hit": True,
                "target": "defenders", "raw_damage": raw_dmg,
                "casualties": casualties,
            })

    # Update engine stats
    engine["shots_fired_total"] = engine.get("shots_fired_total", 0) + volleys
    engine["hits_total"]        = engine.get("hits_total",        0) + sum(
        1 for s in shot_results if s.get("hit")
    )

    # Check for new breaches
    events_added = []
    if state["wall_integrity"] < 30 and "wall" not in state["breach_points"]:
        state["breach_points"].append("wall")
        events_added.append("BREACH! Wall integrity below 30% — assault is now possible.")
    if state["gate_integrity"] < 20 and "gate" not in state["breach_points"]:
        state["breach_points"].append("gate")
        events_added.append("GATE BREACH! Gates destroyed.")

    state["_fact_id"] = fact_id
    _set_siege_state(state)

    stone_caster_note = ""
    if engine["type"] == "stone_caster":
        stone_caster_note = (
            "Brak, Hurn, and Tollug crew this weapon. Their ogre strength grants "
            "+2 to hit and +1d6 bonus damage per hit."
        )

    return {
        "engine_name":       engine["name"],
        "engine_type":       engine.get("label", engine["type"]),
        "engine_condition":  engine.get("condition", "operational"),
        "target":            target,
        "volleys_fired":     volleys,
        "shots":             shot_results,
        "hits":              sum(1 for s in shot_results if s.get("hit")),
        "total_wall_damage": total_wall_damage,
        "total_gate_damage": total_gate_damage,
        "total_pers_casualties": total_pers_damage,
        "wall_integrity":    state["wall_integrity"],
        "gate_integrity":    state["gate_integrity"],
        "breach_points":     state["breach_points"],
        "new_events":        events_added,
        "stone_caster_note": stone_caster_note,
        "dm_note": (
            "Wall integrity < 30 opens a breach; assault becomes possible. "
            "Gate integrity < 20 destroys the gate; assault through the gate is possible. "
            "Call assault() to storm the breach or gate."
        ),
    }


def db_assault(
    breach_point:   str  = "walls",
    waves:          int  = 1,
    scaling_ladders: bool = False,
    battering_ram:  bool  = False,
    calendar_note:  str   = "",
) -> dict:
    """
    Resolve a direct assault on walls or gates.
    breach_point: 'walls', 'gate', or 'breach'.
    waves: number of assault waves (1-3). Each wave costs casualties.
    scaling_ladders: attackers use ladders (higher attacker casualties, no breach needed).
    battering_ram: targeting the gate specifically.
    """
    state = _get_siege_state()
    if not state:
        return {"error": "No active siege. Call start_siege() first."}

    fact_id  = state.pop("_fact_id", None)
    attacker = state["attacker"]
    defender = state["defender"]

    # Validate assault preconditions
    has_breach = bool(state["breach_points"])
    if not has_breach and not scaling_ladders and not battering_ram:
        state["_fact_id"] = fact_id
        _set_siege_state(state)
        return {
            "error": (
                "Cannot assault — no breach point, scaling ladders, or battering ram. "
                f"Wall integrity: {state['wall_integrity']}%; "
                f"Gate integrity: {state['gate_integrity']}%. "
                "Use artillery_fire() to create a breach, or specify scaling_ladders=True."
            )
        }

    waves = max(1, min(3, waves))
    wave_reports = []
    assault_success = False

    for w in range(waves):
        # Base attacker casualty rate: 10% per wave
        att_base_pct = 10.0
        def_base_pct = 8.0

        # Modifiers
        if scaling_ladders:
            att_base_pct += 5.0   # exposed on ladders
        if battering_ram:
            att_base_pct += 3.0   # concentrated defenders at gate
            def_base_pct += 2.0   # ram crew take hits
        if "breach" in state["breach_points"] or "wall" in state["breach_points"]:
            att_base_pct -= 3.0   # attacker advantages at breach
            def_base_pct += 4.0   # defenders compressed at breach point
        if "gate" in state["breach_points"] or "mine_collapse" in state["breach_points"]:
            att_base_pct -= 2.0
            def_base_pct += 3.0
        if attacker["morale"] <= 5:
            att_base_pct += 5.0
        if defender["morale"] <= 5:
            def_base_pct += 5.0

        # Numerical superiority: attacker > 3× defender
        if attacker["count"] >= defender["count"] * 3:
            def_base_pct += 5.0
        if defender["count"] >= attacker["count"] * 2:
            att_base_pct += 3.0

        att_pct = max(2.0, att_base_pct)
        def_pct = max(1.0, def_base_pct)

        att_lost = _pct_casualties(attacker["count"], att_pct)
        def_lost = _pct_casualties(defender["count"], def_pct)

        attacker["count"]      = max(0, attacker["count"]      - att_lost)
        attacker["casualties"] = attacker.get("casualties", 0) + att_lost
        defender["count"]      = max(0, defender["count"]      - def_lost)
        defender["casualties"] = defender.get("casualties", 0) + def_lost

        # Morale checks after each wave
        att_morale_roll = random.randint(2, 12)
        def_morale_roll = random.randint(2, 12)
        att_routes = att_morale_roll > attacker["morale"] and w == waves - 1
        def_routes = def_morale_roll > defender["morale"]

        wave_reports.append({
            "wave":                w + 1,
            "attacker_casualties": att_lost,
            "defender_casualties": def_lost,
            "attacker_remaining":  attacker["count"],
            "defender_remaining":  defender["count"],
            "attacker_morale_roll": att_morale_roll,
            "defender_morale_roll": def_morale_roll,
            "defender_routes":     def_routes,
            "attacker_routes":     att_routes,
        })

        if def_routes:
            assault_success = True
            break
        if att_routes:
            # Assault repelled
            state["last_assault_repelled"] = True
            defender["morale"] = min(12, defender["morale"] + 1)  # morale boost for repelling
            break

    else:
        # All waves complete — check if defender is below 25%
        def_strength = defender["count"] / max(1, defender["count"] + defender["casualties"])
        if def_strength < 0.25:
            assault_success = True

    state["last_assault_repelled"] = not assault_success

    state["_fact_id"] = fact_id
    _set_siege_state(state)

    total_att_casualties = sum(w["attacker_casualties"] for w in wave_reports)
    total_def_casualties = sum(w["defender_casualties"] for w in wave_reports)

    return {
        "assault_type":         "breach" if has_breach else ("ladders" if scaling_ladders else "ram"),
        "breach_point":         breach_point,
        "waves_attempted":      len(wave_reports),
        "assault_succeeded":    assault_success,
        "assault_repelled":     state["last_assault_repelled"],
        "wave_reports":         wave_reports,
        "total_attacker_casualties": total_att_casualties,
        "total_defender_casualties": total_def_casualties,
        "attacker_remaining":   attacker["count"],
        "defender_remaining":   defender["count"],
        "attacker_morale":      attacker["morale"],
        "defender_morale":      defender["morale"],
        "wall_integrity":       state["wall_integrity"],
        "gate_integrity":       state["gate_integrity"],
        "dm_note": (
            "If assault_succeeded, call negotiate_surrender() for formal terms, "
            "then record outcomes via update_world_fact and update_troop_count. "
            "If assault_repelled, defender morale gets +1 bonus. "
            "Attacker may try again next turn after rest and resupply."
        ),
    }


def db_get_siege_state() -> dict:
    """Return full current siege status. Returns error if no active siege."""
    state = _get_siege_state()
    if not state:
        return {"error": "No active siege in progress. Call start_siege() to begin one."}

    attacker = state["attacker"]
    defender = state["defender"]

    def _strength_pct(side: dict) -> float:
        total = side["count"] + side.get("casualties", 0)
        return round(side["count"] / max(1, total) * 100, 1)

    return {
        "active":              state.get("active", True),
        "target_location":     state.get("target_location"),
        "fortification":       state.get("fortification_label"),
        "role":                state.get("role"),
        "weeks_elapsed":       state.get("weeks_elapsed", 0),
        "start_date":          state.get("start_date"),
        "wall_integrity":      state.get("wall_integrity"),
        "gate_integrity":      state.get("gate_integrity"),
        "breach_points":       state.get("breach_points", []),
        "last_assault_repelled": state.get("last_assault_repelled", False),
        "attacker": {
            "name":          attacker.get("name"),
            "count":         attacker.get("count"),
            "casualties":    attacker.get("casualties", 0),
            "strength_pct":  _strength_pct(attacker),
            "supply_weeks":  attacker.get("supply_weeks"),
            "morale":        attacker.get("morale"),
            "morale_label":  _score_label(attacker.get("morale", 7)),
            "has_artillery": attacker.get("has_artillery", False),
            "artillery":     attacker.get("artillery", []),
        },
        "defender": {
            "name":          defender.get("name"),
            "count":         defender.get("count"),
            "casualties":    defender.get("casualties", 0),
            "strength_pct":  _strength_pct(defender),
            "supply_weeks":  defender.get("supply_weeks"),
            "morale":        defender.get("morale"),
            "morale_label":  _score_label(defender.get("morale", 7)),
            "has_artillery": defender.get("has_artillery", False),
        },
        "recent_events":       state.get("events_log", [])[-5:],
        "dm_note": (
            "wall_integrity < 30 = breach point open. "
            "gate_integrity < 20 = gate destroyed. "
            "supply_weeks = 0 means the side is starving — morale drops weekly. "
            "Call assault() when ready to storm."
        ),
    }


def db_negotiate_surrender(
    terms_offered:    str,
    calendar_note:    str = "",
) -> dict:
    """
    Attempt to negotiate surrender based on current siege state.
    terms_offered: brief description of the terms being proposed.
    Roll d20 + modifiers based on relative strength, supplies, wall integrity,
    PC reputation, and Charisma.
    """
    state = _get_siege_state()
    if not state:
        return {"error": "No active siege. Call start_siege() first."}

    fact_id  = state.pop("_fact_id", None)
    attacker = state["attacker"]
    defender = state["defender"]

    # Build modifier
    mod = 0
    modifier_notes = []

    # Numerical superiority
    if attacker["count"] >= defender["count"] * 3:
        mod += 3
        modifier_notes.append("+3 (attacker outnumbers 3:1)")
    elif attacker["count"] >= defender["count"] * 2:
        mod += 2
        modifier_notes.append("+2 (attacker outnumbers 2:1)")

    # Wall condition
    if state["wall_integrity"] < 20:
        mod += 3
        modifier_notes.append("+3 (wall nearly destroyed)")
    elif state["wall_integrity"] < 50:
        mod += 2
        modifier_notes.append("+2 (walls badly damaged)")
    elif state["wall_integrity"] < 75:
        mod += 1
        modifier_notes.append("+1 (walls damaged)")

    # Gate condition
    if state["gate_integrity"] < 30:
        mod += 2
        modifier_notes.append("+2 (gate destroyed or nearly so)")

    # Defender supply
    if defender["supply_weeks"] == 0:
        mod += 4
        modifier_notes.append("+4 (defender supplies exhausted)")
    elif defender["supply_weeks"] <= 1:
        mod += 2
        modifier_notes.append("+2 (defender critically low on supplies)")

    # Defender morale
    if defender["morale"] <= 4:
        mod += 3
        modifier_notes.append("+3 (defender morale collapsing)")
    elif defender["morale"] <= 6:
        mod += 1
        modifier_notes.append("+1 (defender morale poor)")
    elif defender["morale"] >= 10:
        mod -= 2
        modifier_notes.append("-2 (defender morale strong)")

    # Last assault repelled
    if state.get("last_assault_repelled"):
        mod -= 2
        modifier_notes.append("-2 (last assault repelled)")

    # PC Charisma
    cha_score = _get_pc_ability("charisma")
    cha_mod   = _ability_mod(cha_score)
    if cha_mod != 0:
        mod += cha_mod
        modifier_notes.append(f"{cha_mod:+d} (Charisma {cha_score})")

    raw_roll   = random.randint(1, 20)
    final_roll = max(1, raw_roll + mod)

    # Look up result
    result_label = "refused"
    result_desc  = "Terms refused."
    for (min_r, label, desc) in _SURRENDER_TERMS:
        if final_roll >= min_r:
            result_label = label
            result_desc  = desc
            break

    # Apply consequence to defender morale
    if result_label in ("insulted", "betrayal"):
        defender["morale"] = min(12, defender["morale"] + 1)
    elif result_label in ("unconditional", "honourable"):
        state["active"] = False

    state["_fact_id"] = fact_id
    _set_siege_state(state)

    return {
        "terms_offered":   terms_offered,
        "d20_roll":        raw_roll,
        "modifier":        mod,
        "modifier_notes":  modifier_notes,
        "final_roll":      final_roll,
        "result":          result_label,
        "description":     result_desc,
        "siege_ends":      result_label in ("unconditional", "honourable"),
        "attacker_count":  attacker["count"],
        "defender_count":  defender["count"],
        "wall_integrity":  state["wall_integrity"],
        "defender_morale": defender["morale"],
        "dm_note": (
            "If siege_ends is True, call update_world_fact and update_troop_count "
            "to record the outcome. Record NPC fate via update_npc. "
            "If 'betrayal', resolve as an immediate assault with defenders getting "
            "first initiative. Narrate the outcome dramatically."
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
# TURN VERIFICATION  (Phase 6 — self-auditing turns)
# ══════════════════════════════════════════════════════════════════════════════
#
# Verification is driven by structured markers stored in
# ai_turns.structured_response_json under the "markers" key. save_turn writes
# the markers list; db_verify_turn parses each marker and cross-checks it
# against current DB state.
#
# Marker formats (canonical, prefix-keyed strings):
#     cast:[spell name]
#     item_added:[item name]      / item_used:[item name]
#     hp:[old]>[new]
#     spent:[amount]gp            / gained:[amount]gp
#     npc_added:[name]
#     location_changed:[name]
#     troop_change:[group]:[old]>[new]
#
# A turn with no markers returns verdict "no_claims" — silence is explicitly
# distinguishable from "verified clean".
# ══════════════════════════════════════════════════════════════════════════════

_VRY_KNOWN_PREFIXES = {
    "cast", "item_added", "item_used", "hp",
    "spent", "gained", "npc_added", "location_changed", "troop_change",
}


def _vry_parse_marker(marker: str) -> dict:
    """
    Parse one marker string into a typed dict. Returns
    {"type": "<prefix>", ...args, "raw": <original>} on success, or
    {"type": "malformed"|"unknown", "raw": ..., "reason": ...} on failure.
    """
    if not marker or not isinstance(marker, str):
        return {"type": "malformed", "raw": str(marker), "reason": "empty marker"}

    s = marker.strip()
    if ":" not in s:
        return {"type": "malformed", "raw": marker,
                "reason": "missing ':' separator"}

    prefix, _, rest = s.partition(":")
    prefix = prefix.strip().lower()
    rest   = rest.strip()

    if prefix not in _VRY_KNOWN_PREFIXES:
        return {"type": "unknown", "raw": marker,
                "reason": f"unknown prefix '{prefix}'"}

    if prefix == "cast":
        if not rest:
            return {"type": "malformed", "raw": marker,
                    "reason": "expected cast:[spell name]"}
        return {"type": "cast", "spell": rest, "raw": marker}

    if prefix in ("item_added", "item_used"):
        if not rest:
            return {"type": "malformed", "raw": marker,
                    "reason": f"expected {prefix}:[item name]"}
        return {"type": prefix, "name": rest, "raw": marker}

    if prefix == "hp":
        if ">" not in rest:
            return {"type": "malformed", "raw": marker,
                    "reason": "expected hp:[old]>[new]"}
        old_s, _, new_s = rest.partition(">")
        try:
            return {
                "type": "hp",
                "old":  int(old_s.strip()),
                "new":  int(new_s.strip()),
                "raw":  marker,
            }
        except ValueError:
            return {"type": "malformed", "raw": marker,
                    "reason": "hp:[old]>[new] requires integers"}

    if prefix in ("spent", "gained"):
        amt = rest.lower().replace(",", "").strip()
        if amt.endswith("gp"):
            amt = amt[:-2].strip()
        try:
            return {"type": prefix, "amount": int(amt), "raw": marker}
        except ValueError:
            return {"type": "malformed", "raw": marker,
                    "reason": f"expected {prefix}:[amount]gp with integer amount"}

    if prefix == "npc_added":
        if not rest:
            return {"type": "malformed", "raw": marker,
                    "reason": "expected npc_added:[name]"}
        return {"type": "npc_added", "name": rest, "raw": marker}

    if prefix == "location_changed":
        if not rest:
            return {"type": "malformed", "raw": marker,
                    "reason": "expected location_changed:[name]"}
        return {"type": "location_changed", "name": rest, "raw": marker}

    if prefix == "troop_change":
        # rest format: "[group]:[old]>[new]"
        group, _, deltas = rest.rpartition(":")
        group  = group.strip()
        deltas = deltas.strip()
        if not group or ">" not in deltas:
            return {"type": "malformed", "raw": marker,
                    "reason": "expected troop_change:[group]:[old]>[new]"}
        old_s, _, new_s = deltas.partition(">")
        try:
            return {
                "type":  "troop_change",
                "group": group,
                "old":   int(old_s.strip()),
                "new":   int(new_s.strip()),
                "raw":   marker,
            }
        except ValueError:
            return {"type": "malformed", "raw": marker,
                    "reason": "troop_change requires integer [old]>[new]"}

    return {"type": "malformed", "raw": marker, "reason": "unhandled prefix"}


def db_verify_turn(turn_id: int | None = None) -> dict:
    """
    Cross-check structured markers from a saved turn against the database.

    Reads the markers list from ai_turns.structured_response_json (set by
    save_turn). A turn with no markers returns verdict "no_claims" — that is
    distinct from "clean", which means markers were present and all matched
    DB state.

    Verdict values:
        "no_claims"       — no markers; verification did not run
        "clean"           — every marker matched DB state
        "needs_attention" — at least one unverified or malformed marker
        "conflict"        — at least one marker contradicts DB state

    For unverified/conflict entries, suggested_call gives the exact tool
    invocation to resolve the gap.
    """
    with _get_conn(read_only=True) as conn:
        if turn_id is None:
            row = conn.execute(
                "SELECT turn_id, structured_response_json "
                "FROM ai_turns ORDER BY turn_id DESC LIMIT 1"
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT turn_id, structured_response_json "
                "FROM ai_turns WHERE turn_id = ?", (turn_id,)
            ).fetchone()

    if not row:
        return {"error": "No turn found."}

    found_turn_id = row["turn_id"]

    # ── Pull markers list out of structured_response_json ─────────────────────
    # Capture both what was stored AND the shape it was stored in, so the AI
    # can see at a glance whether the markers round-tripped correctly.
    markers: list[str] = []
    raw_in_db: Any = None
    raw_type:  str  = "missing"
    if row["structured_response_json"]:
        try:
            sc = json.loads(row["structured_response_json"]) or {}
            raw_in_db = sc.get("markers")
            raw_type  = type(raw_in_db).__name__ if raw_in_db is not None else "absent"
            if isinstance(raw_in_db, list):
                markers = [str(m) for m in raw_in_db
                           if isinstance(m, str) and m.strip()]
        except (json.JSONDecodeError, TypeError) as exc:
            raw_type = f"json_error: {exc.__class__.__name__}"

    if not markers:
        return {
            "turn_id":    found_turn_id,
            "verdict":    "no_claims",
            "confirmed":  [],
            "unverified": [],
            "conflicts":  [],
            "debug": {
                "markers_in_db_raw":  raw_in_db,
                "markers_in_db_type": raw_type,
                "markers_parsed":     [],
            },
            "warning": (
                "No markers in this turn — state cannot be verified. "
                "Pass markers=[...] to save_turn whenever game state changes. "
                "Formats: cast:[spell], item_added:[name], item_used:[name], "
                "hp:[old]>[new], spent:[N]gp, gained:[N]gp, npc_added:[name], "
                "location_changed:[name], troop_change:[group]:[old]>[new]."
            ),
        }

    parsed = [_vry_parse_marker(m) for m in markers]
    debug_parsed = [
        {
            "raw":  p["raw"],
            "type": p["type"],
            **({"reason": p["reason"]} if "reason" in p else {}),
        }
        for p in parsed
    ]

    confirmed:  list[dict] = []
    unverified: list[dict] = []
    conflicts:  list[dict] = []
    malformed:  list[dict] = []

    with _get_conn(read_only=True) as conn:
        for marker in parsed:
            mtype = marker["type"]
            raw   = marker["raw"]

            if mtype in ("malformed", "unknown"):
                malformed.append({
                    "marker": raw,
                    "reason": marker.get("reason", "could not parse"),
                })
                continue

            # ── HP ─────────────────────────────────────────────────────────────
            if mtype == "hp":
                cs = conn.execute(
                    "SELECT hp_current FROM character_status WHERE character_id = ?",
                    (_PC_CHARACTER_ID,),
                ).fetchone()
                actual = cs["hp_current"] if cs else None
                claim_label = f"HP {marker['old']} → {marker['new']}"
                if actual == marker["new"]:
                    confirmed.append({
                        "type":   "hp",
                        "claim":  claim_label,
                        "actual": f"character_status.hp_current = {actual}  ✓",
                    })
                else:
                    conflicts.append({
                        "type":           "hp",
                        "claim":          claim_label,
                        "actual_in_db":   actual,
                        "suggested_call": f"update_character_status(hp_current={marker['new']})",
                    })
                continue

            # ── Spent / gained gold ───────────────────────────────────────────
            if mtype == "spent":
                unverified.append({
                    "type":           "spent",
                    "claim":          f"Spent {marker['amount']} gp",
                    "reason":         "Gold delta — confirm update_treasury was called.",
                    "suggested_call": (
                        f'update_treasury(account_name="<account>", '
                        f"gp_delta={-marker['amount']})"
                    ),
                })
                continue
            if mtype == "gained":
                unverified.append({
                    "type":           "gained",
                    "claim":          f"Gained {marker['amount']} gp",
                    "reason":         "Gold delta — confirm update_treasury was called.",
                    "suggested_call": (
                        f'update_treasury(account_name="<account>", '
                        f"gp_delta={marker['amount']})"
                    ),
                })
                continue

            # ── Items ─────────────────────────────────────────────────────────
            if mtype == "item_added":
                name = marker["name"].strip()
                inv = conn.execute(
                    """SELECT i.name FROM inventory inv
                       JOIN items i ON inv.item_id = i.item_id
                       WHERE inv.character_id = ?
                         AND LOWER(i.name) LIKE ?
                       LIMIT 1""",
                    (_PC_CHARACTER_ID, f"%{name.lower()[:18]}%"),
                ).fetchone()
                if inv:
                    confirmed.append({
                        "type":   "item_added",
                        "claim":  f"Acquired: {name}",
                        "actual": f"In inventory: {inv['name']}  ✓",
                    })
                else:
                    unverified.append({
                        "type":           "item_added",
                        "claim":          f"Acquired: {name}",
                        "reason":         "Item not found in inventory.",
                        "suggested_call": f'add_item(name="{name}", owner_type="character", owner_id=1)',
                    })
                continue

            if mtype == "item_used":
                name = marker["name"].strip()
                inv = conn.execute(
                    """SELECT inv.inventory_id, i.name FROM inventory inv
                       JOIN items i ON inv.item_id = i.item_id
                       WHERE inv.character_id = ?
                         AND LOWER(i.name) LIKE ?
                       LIMIT 1""",
                    (_PC_CHARACTER_ID, f"%{name.lower()[:18]}%"),
                ).fetchone()
                if inv:
                    unverified.append({
                        "type":           "item_used",
                        "claim":          f"Used/lost: {name}",
                        "reason":         f"'{inv['name']}' still in inventory.",
                        "suggested_call": (
                            f'remove_inventory_item(inventory_id={inv["inventory_id"]}, '
                            f'reason="{name} consumed/lost")'
                        ),
                    })
                else:
                    confirmed.append({
                        "type":   "item_used",
                        "claim":  f"Used/lost: {name}",
                        "actual": "Item not in inventory  ✓",
                    })
                continue

            # ── Spells cast ───────────────────────────────────────────────────
            if mtype == "cast":
                spell_name = marker["spell"].strip()
                wf = conn.execute(
                    "SELECT fact_text FROM world_facts "
                    "WHERE category = 'spell_memory' LIMIT 1"
                ).fetchone()
                spell_mem: dict = {}
                if wf:
                    try:
                        spell_mem = json.loads(wf["fact_text"]) or {}
                    except (json.JSONDecodeError, TypeError):
                        spell_mem = {}
                found_expended = False
                for _lvl, lvl_data in spell_mem.items():
                    for slot in (lvl_data.get("slots") or []):
                        if (slot.get("expended")
                                and spell_name.lower() in (slot.get("spell") or "").lower()):
                            found_expended = True
                            break
                    if found_expended:
                        break
                if found_expended:
                    confirmed.append({
                        "type":   "cast",
                        "claim":  f"Cast {spell_name}",
                        "actual": "Slot marked expended in spell memory  ✓",
                    })
                else:
                    unverified.append({
                        "type":           "cast",
                        "claim":          f"Cast {spell_name}",
                        "reason":         "No expended slot found for this spell.",
                        "suggested_call": f'cast_spell(spell_name="{spell_name}")',
                    })
                continue

            # ── NPCs ──────────────────────────────────────────────────────────
            if mtype == "npc_added":
                name = marker["name"].strip()
                npc_row = conn.execute(
                    "SELECT name FROM characters "
                    "WHERE LOWER(name) LIKE ? AND character_id != ? LIMIT 1",
                    (f"%{name.lower()[:18]}%", _PC_CHARACTER_ID),
                ).fetchone()
                if npc_row:
                    confirmed.append({
                        "type":   "npc_added",
                        "claim":  f"NPC added: {name}",
                        "actual": f"In characters table: {npc_row['name']}  ✓",
                    })
                else:
                    unverified.append({
                        "type":           "npc_added",
                        "claim":          f"NPC added: {name}",
                        "reason":         "NPC not found in characters table.",
                        "suggested_call": f'add_npc(name="{name}")',
                    })
                continue

            # ── Location ──────────────────────────────────────────────────────
            if mtype == "location_changed":
                name = marker["name"].strip()
                scene = conn.execute(
                    "SELECT structured_state_json FROM current_scene_state WHERE id = 1"
                ).fetchone()
                scene_loc = ""
                if scene and scene["structured_state_json"]:
                    try:
                        scene_loc = (json.loads(scene["structured_state_json"]) or {}).get(
                            "location"
                        ) or ""
                    except (json.JSONDecodeError, TypeError):
                        scene_loc = ""
                if scene_loc and name.lower() in scene_loc.lower():
                    confirmed.append({
                        "type":   "location_changed",
                        "claim":  f"Location: {name}",
                        "actual": f"current_scene_state.location = '{scene_loc}'  ✓",
                    })
                else:
                    unverified.append({
                        "type":           "location_changed",
                        "claim":          f"Location: {name}",
                        "reason":         f"current_scene_state.location is '{scene_loc}'.",
                        "suggested_call": f'save_turn(..., scene_location="{name}")',
                    })
                continue

            # ── Troop count ───────────────────────────────────────────────────
            if mtype == "troop_change":
                group = marker["group"].strip()
                trow = conn.execute(
                    "SELECT group_name, count FROM troops "
                    "WHERE LOWER(group_name) LIKE LOWER(?) AND campaign_id = ? LIMIT 1",
                    (f"{group.lower()}%", _CAMPAIGN_ID),
                ).fetchone()
                claim_label = f"{group}: {marker['old']} → {marker['new']}"
                if not trow:
                    unverified.append({
                        "type":           "troop_change",
                        "claim":          claim_label,
                        "reason":         f"Troop group '{group}' not found.",
                        "suggested_call": (
                            f'add_troop_group(group_name="{group}", '
                            f"count={marker['new']})"
                        ),
                    })
                    continue
                actual = trow["count"]
                if actual == marker["new"]:
                    confirmed.append({
                        "type":   "troop_change",
                        "claim":  claim_label,
                        "actual": f"troops.count = {actual}  ✓",
                    })
                else:
                    conflicts.append({
                        "type":           "troop_change",
                        "claim":          claim_label,
                        "actual_in_db":   actual,
                        "suggested_call": (
                            f'update_troop_count(group_name="{group}", '
                            f"new_count={marker['new']})"
                        ),
                    })
                continue

    # ── Verdict ────────────────────────────────────────────────────────────────
    if conflicts:
        verdict = "conflict"
    elif unverified or malformed:
        verdict = "needs_attention"
    else:
        verdict = "clean"

    result: dict = {
        "turn_id":    found_turn_id,
        "verdict":    verdict,
        "confirmed":  confirmed,
        "unverified": unverified,
        "conflicts":  conflicts,
        "marker_count": len(markers),
        "debug": {
            "markers_in_db_raw":  raw_in_db,
            "markers_in_db_type": raw_type,
            "markers_parsed":     debug_parsed,
        },
    }
    if malformed:
        result["malformed"] = malformed
    return result


def db_update_turn_verification(turn_id: int, verification_json: str) -> None:
    """
    Persist a verification result into ai_turns.validation_errors_json.
    Called automatically by save_turn after every successful write.
    """
    with _get_conn() as conn:
        conn.execute(
            "UPDATE ai_turns SET validation_errors_json = ? WHERE turn_id = ?",
            (verification_json, turn_id),
        )


# ══════════════════════════════════════════════════════════════════════════════
# AREA PRE-POPULATION  (Phase 7)
# ══════════════════════════════════════════════════════════════════════════════
#
# The world exists before the player encounters it. populate_area pre-rolls all
# encounters and treasure for a location at populate time and stores them in
# area_instances. Subsequent visits return the same monsters with the same HP
# and the same treasure, so retreating and returning is consistent.
#
# Schema lives in schema/ddl.sql; _ensure_area_instances_table runs a
# CREATE TABLE IF NOT EXISTS at the top of every public function so existing
# campaign DBs auto-migrate on first use without any manual step.
# ══════════════════════════════════════════════════════════════════════════════

_AREA_INSTANCES_DDL = """
CREATE TABLE IF NOT EXISTS area_instances (
    area_instance_id    INTEGER PRIMARY KEY,
    campaign_id         INTEGER NOT NULL,
    location_id         INTEGER,
    location_name       TEXT NOT NULL,
    room_label          TEXT,
    dungeon_level       INTEGER NOT NULL DEFAULT 1,
    monster_type        TEXT,
    monster_count       INTEGER NOT NULL DEFAULT 0,
    individual_hp_json  TEXT,
    monster_status_json TEXT,
    treasure_json       TEXT,
    treasure_status     TEXT NOT NULL DEFAULT 'intact',
    encounter_status    TEXT NOT NULL DEFAULT 'pending',
    tier                TEXT NOT NULL DEFAULT 'standard',
    created_date        TEXT NOT NULL,
    notes               TEXT
);
CREATE INDEX IF NOT EXISTS idx_area_instances_location_name
    ON area_instances(location_name);
"""


def _ensure_area_instances_table(conn) -> None:
    """
    Idempotent runtime migration. Only runs on writable connections.
    Handles both 'table missing' and 'table exists but missing tier column'
    (the latter for campaign DBs created before Phase 7 tiering).
    """
    conn.executescript(_AREA_INSTANCES_DDL)
    # Defensive ALTER: add the `tier` column if an older population pass
    # created the table without it.
    cols = [r["name"] for r in conn.execute(
        "PRAGMA table_info(area_instances)").fetchall()]
    if "tier" not in cols:
        conn.execute(
            "ALTER TABLE area_instances "
            "ADD COLUMN tier TEXT NOT NULL DEFAULT 'standard'"
        )


def _area_instances_table_exists(conn) -> bool:
    """Check if the area_instances table exists. Safe on read-only connections."""
    row = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='area_instances' LIMIT 1"
    ).fetchone()
    return row is not None


def _resolve_location_id(conn, location_name: str) -> int | None:
    """Best-effort lookup of a location_id by name. Returns None if no match."""
    row = conn.execute(
        "SELECT location_id FROM locations "
        "WHERE LOWER(name) = LOWER(?) AND campaign_id = ? LIMIT 1",
        (location_name, _CAMPAIGN_ID),
    ).fetchone()
    return row["location_id"] if row else None


def _roll_individual_hp(hd_text: str, count: int) -> tuple[list[int], float]:
    """Roll HP for `count` monsters of the given HD. Returns (hp_list, eff_hd)."""
    hp_list: list[int] = []
    eff_hd = 1.0
    for _ in range(max(1, int(count))):
        hp, eff_hd = _roll_monster_hp(hd_text)
        hp_list.append(int(hp))
    return hp_list, eff_hd


# ── populate_area ─────────────────────────────────────────────────────────────

def db_populate_area(
    location_name: str,
    dungeon_level: int = 1,
    num_rooms: int | None = None,
    room_specs: list[dict] | None = None,
    notes: str | None = None,
) -> dict:
    """
    Pre-roll every encounter and treasure haul for a location.

    Two modes:
      - Auto: pass `num_rooms` (or leave default 4–6) and the function rolls
        random encounters via get_random_dungeon_encounter for each room.
      - Explicit: pass `room_specs` — a list of dicts like
            {"room_label": "Guard post", "monster_name": "Goblin", "count": 4,
             "treasure_type": "L"}
        Either field may be omitted; missing fields are auto-rolled.

    Idempotent on location_name: if any rows already exist for this location
    and no force flag is set, returns the existing summary unchanged.
    """
    import datetime
    now = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    with _get_conn() as conn:
        _ensure_area_instances_table(conn)

        existing = conn.execute(
            "SELECT COUNT(*) AS n FROM area_instances WHERE location_name = ?",
            (location_name,),
        ).fetchone()
        if existing and existing["n"] > 0:
            return db_get_area_encounters(
                location_name=location_name,
                auto_populate=False,
            )

        location_id = _resolve_location_id(conn, location_name)

        # ── Build the room plan ───────────────────────────────────────────────
        rooms: list[dict] = []
        if room_specs:
            for spec in room_specs:
                rooms.append({
                    "room_label":    spec.get("room_label") or f"Room {len(rooms)+1}",
                    "monster_name":  spec.get("monster_name"),
                    "count":         spec.get("count"),
                    "treasure_type": spec.get("treasure_type"),
                    "tier":          spec.get("tier"),
                })
        else:
            n = num_rooms if (num_rooms is not None and num_rooms > 0) else random.randint(4, 6)
            for i in range(n):
                rooms.append({
                    "room_label":    f"Room {i+1}",
                    "monster_name":  None,
                    "count":         None,
                    "treasure_type": None,
                })

        # ── Roll each room ────────────────────────────────────────────────────
        created_rows: list[dict] = []

        for room in rooms:
            mname = room["monster_name"]
            count = room["count"]
            ttype = room["treasure_type"]
            room_tier = room.get("tier")

            if not mname or not count:
                rolled = get_random_dungeon_encounter(dungeon_level)
                mname = mname or rolled.get("monster_name") or "Skeleton"
                count = count or max(1, rolled.get("count", 1))
                if not ttype:
                    mstats = rolled.get("monster_stats") or {}
                    ttype = (mstats.get("treasure_type") or "").strip() or None

            mstats = lookup_monster(mname) if mname else {}
            hd_text = (mstats.get("hit_dice") or "1") if mstats else "1"
            if not ttype and mstats:
                ttype = (mstats.get("treasure_type") or "").strip() or None

            # Auto-classify tier if the room spec didn't lock one in.
            if not room_tier:
                room_tier = _classify_npc_tier(
                    monster_name=mname, monster_stats=mstats,
                )
            room_tier = _normalize_tier(room_tier)

            hp_list, _eff_hd = _roll_individual_hp(hd_text, int(count))
            statuses = ["alive"] * len(hp_list)

            # Treasure — only roll for valid letters; otherwise empty.
            treasure: dict = {}
            ttype_letter = (ttype or "").strip().upper()
            if (len(ttype_letter) == 1
                    and ttype_letter.isalpha()
                    and ttype_letter != "NIL"):
                try:
                    treasure = roll_treasure_by_type(ttype_letter)
                except Exception as exc:
                    treasure = {"error": str(exc), "treasure_type": ttype_letter}

            cur = conn.execute(
                """
                INSERT INTO area_instances (
                    campaign_id, location_id, location_name, room_label,
                    dungeon_level, monster_type, monster_count,
                    individual_hp_json, monster_status_json,
                    treasure_json, treasure_status, encounter_status,
                    tier, created_date, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'intact', 'pending', ?, ?, ?)
                """,
                (
                    _CAMPAIGN_ID, location_id, location_name, room["room_label"],
                    int(dungeon_level), mname, len(hp_list),
                    json.dumps(hp_list), json.dumps(statuses),
                    json.dumps(treasure) if treasure else None,
                    room_tier, now, notes,
                ),
            )
            created_rows.append({
                "area_instance_id": cur.lastrowid,
                "room_label":       room["room_label"],
                "monster_type":     mname,
                "monster_count":    len(hp_list),
                "individual_hp":    hp_list,
                "tier":             room_tier,
                "treasure_summary": _summarize_treasure(treasure),
            })

    return {
        "populated":     True,
        "location_name": location_name,
        "dungeon_level": dungeon_level,
        "rooms":         created_rows,
        "room_count":    len(created_rows),
    }


def _summarize_treasure(t: dict) -> dict:
    """Compact treasure summary for return values (full detail is in DB)."""
    if not t:
        return {}
    return {
        "treasure_type":   t.get("treasure_type"),
        "total_gp_value":  t.get("total_gp_value", 0),
        "coin_keys":       sorted([k for k, v in (t.get("coins") or {}).items() if v]),
        "gem_count":       len(t.get("gems") or []),
        "jewelry_count":   len(t.get("jewelry") or []),
        "magic_item_count": len(t.get("magic_items") or []),
    }


# ── get_area_encounters ───────────────────────────────────────────────────────

def db_get_area_encounters(
    location_name: str,
    auto_populate: bool = True,
    dungeon_level: int = 1,
) -> dict:
    """
    Return every pre-rolled room for a location, plus the treasure each
    room contains. If nothing exists and auto_populate is true, populate
    first with the given dungeon_level.
    """
    with _get_conn() as conn:
        _ensure_area_instances_table(conn)
        rows = conn.execute(
            "SELECT * FROM area_instances "
            "WHERE location_name = ? "
            "ORDER BY area_instance_id ASC",
            (location_name,),
        ).fetchall()
    # The first connection (writable) ensures the table exists, so subsequent
    # reads in this function can safely query it.

    if not rows:
        if auto_populate:
            return db_populate_area(
                location_name=location_name,
                dungeon_level=dungeon_level,
            )
        return {
            "populated":     False,
            "location_name": location_name,
            "rooms":         [],
            "room_count":    0,
        }

    rooms: list[dict] = []
    for r in rows:
        try:
            hp_list = json.loads(r["individual_hp_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            hp_list = []
        try:
            status_list = json.loads(r["monster_status_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            status_list = []
        try:
            treasure = json.loads(r["treasure_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            treasure = {}

        # `tier` is a Phase-7.1 column; older rows may not have it. Default
        # to 'standard' when absent.
        tier_val = "standard"
        try:
            tier_val = r["tier"] or "standard"
        except (IndexError, KeyError):
            tier_val = "standard"

        rooms.append({
            "area_instance_id": r["area_instance_id"],
            "room_label":       r["room_label"],
            "monster_type":     r["monster_type"],
            "monster_count":    r["monster_count"],
            "individual_hp":    hp_list,
            "monster_status":   status_list,
            "encounter_status": r["encounter_status"],
            "tier":             tier_val,
            "treasure":         treasure,
            "treasure_status":  r["treasure_status"],
            "dungeon_level":    r["dungeon_level"],
        })

    return {
        "populated":     True,
        "location_name": location_name,
        "rooms":         rooms,
        "room_count":    len(rooms),
    }


# ── get / update monster instance ─────────────────────────────────────────────

def db_get_monster_instance(area_instance_id: int, monster_index: int = 0) -> dict:
    """
    Return one monster's stats from a pre-rolled instance.
    monster_index is 0-based.
    """
    with _get_conn(read_only=True) as conn:
        if not _area_instances_table_exists(conn):
            return {"error": f"area_instance_id={area_instance_id} not found"}
        row = conn.execute(
            "SELECT * FROM area_instances WHERE area_instance_id = ?",
            (area_instance_id,),
        ).fetchone()

    if not row:
        return {"error": f"area_instance_id={area_instance_id} not found"}

    try:
        hp_list = json.loads(row["individual_hp_json"] or "[]")
    except (json.JSONDecodeError, TypeError):
        hp_list = []
    try:
        statuses = json.loads(row["monster_status_json"] or "[]")
    except (json.JSONDecodeError, TypeError):
        statuses = []
    try:
        treasure = json.loads(row["treasure_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        treasure = {}

    if not (0 <= monster_index < len(hp_list)):
        return {"error": f"monster_index {monster_index} out of range "
                         f"(0..{len(hp_list)-1})"}

    mstats = lookup_monster(row["monster_type"]) or {}

    tier_val = "standard"
    try:
        tier_val = row["tier"] or "standard"
    except (IndexError, KeyError):
        tier_val = "standard"

    return {
        "area_instance_id": area_instance_id,
        "room_label":       row["room_label"],
        "monster_type":     row["monster_type"],
        "monster_index":    monster_index,
        "hp_current":       hp_list[monster_index],
        "status":           statuses[monster_index] if monster_index < len(statuses) else "alive",
        "tier":             tier_val,
        "stats":            mstats,
        "shared_treasure":  treasure,
        "treasure_status":  row["treasure_status"],
    }


def db_update_monster_instance(
    area_instance_id: int,
    monster_index: int | None = None,
    hp_current: int | None = None,
    status: str | None = None,
    treasure_status: str | None = None,
    encounter_status: str | None = None,
) -> dict:
    """
    Update a single monster's HP/status, or the room's
    treasure_status / encounter_status. Pass monster_index to target
    a specific monster; leave None to update only room-level fields.
    """
    with _get_conn() as conn:
        _ensure_area_instances_table(conn)
        row = conn.execute(
            "SELECT * FROM area_instances WHERE area_instance_id = ?",
            (area_instance_id,),
        ).fetchone()
        if not row:
            return {"error": f"area_instance_id={area_instance_id} not found"}

        try:
            hp_list = json.loads(row["individual_hp_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            hp_list = []
        try:
            statuses = json.loads(row["monster_status_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            statuses = []

        changed: list[str] = []

        if monster_index is not None:
            if not (0 <= monster_index < len(hp_list)):
                return {"error": f"monster_index {monster_index} out of range"}
            if hp_current is not None:
                hp_list[monster_index] = max(0, int(hp_current))
                changed.append(f"hp[{monster_index}]={hp_list[monster_index]}")
                # Auto-mark dead when HP hits 0 unless caller overrode status.
                if hp_list[monster_index] <= 0 and status is None:
                    while len(statuses) <= monster_index:
                        statuses.append("alive")
                    statuses[monster_index] = "dead"
                    changed.append(f"status[{monster_index}]=dead")
            if status is not None:
                while len(statuses) <= monster_index:
                    statuses.append("alive")
                statuses[monster_index] = status
                changed.append(f"status[{monster_index}]={status}")

        new_treasure_status = treasure_status or row["treasure_status"]
        new_encounter_status = encounter_status or row["encounter_status"]

        # Auto-progress encounter_status if every monster is dead.
        if statuses and all(s in ("dead", "fled") for s in statuses):
            if new_encounter_status == "pending":
                new_encounter_status = "cleared"
                changed.append("encounter_status=cleared (all monsters dead/fled)")

        conn.execute(
            "UPDATE area_instances "
            "SET individual_hp_json = ?, monster_status_json = ?, "
            "    treasure_status = ?, encounter_status = ? "
            "WHERE area_instance_id = ?",
            (json.dumps(hp_list), json.dumps(statuses),
             new_treasure_status, new_encounter_status, area_instance_id),
        )

    return {
        "updated":          True,
        "area_instance_id": area_instance_id,
        "individual_hp":    hp_list,
        "monster_status":   statuses,
        "treasure_status":  new_treasure_status,
        "encounter_status": new_encounter_status,
        "changed":          changed,
    }


def db_find_pre_rolled_for_combat(
    location_name: str,
    monster_type: str | None = None,
) -> dict | None:
    """
    Look up the next pending pre-rolled encounter at this location.
    Used by start_combat to consult pre-rolled HP before rolling fresh.

    Returns the matching area_instances row as a dict, or None.
    """
    if not location_name:
        return None
    with _get_conn(read_only=True) as conn:
        if not _area_instances_table_exists(conn):
            return None
        if monster_type:
            row = conn.execute(
                "SELECT * FROM area_instances "
                "WHERE LOWER(location_name) = LOWER(?) "
                "  AND LOWER(monster_type) = LOWER(?) "
                "  AND encounter_status = 'pending' "
                "ORDER BY area_instance_id ASC LIMIT 1",
                (location_name, monster_type),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM area_instances "
                "WHERE LOWER(location_name) = LOWER(?) "
                "  AND encounter_status = 'pending' "
                "ORDER BY area_instance_id ASC LIMIT 1",
                (location_name,),
            ).fetchone()
    return _row_to_dict(row) if row else None


# ── populate_npc ──────────────────────────────────────────────────────────────
#
# Minimal AD&D 1e NPC stat roller. Aim is consistency, not full simulation —
# we want every named NPC to have a deterministic, persisted stat block so
# repeat encounters behave the same way.

_NPC_HIT_DIE = {
    "fighter":     8,   # use d10 ideally, but d8 keeps NPCs leaner
    "paladin":     10,
    "ranger":      8,
    "cleric":      8,
    "druid":       8,
    "magic-user":  4,
    "magicuser":   4,
    "mage":        4,
    "thief":       6,
    "assassin":    6,
    "monk":        4,
    "bard":        6,
}

_NPC_DEFAULT_AC = {
    "fighter": 4, "paladin": 3, "ranger": 5,
    "cleric":  4, "druid":   6,
    "magic-user": 10, "magicuser": 10, "mage": 10,
    "thief":   7, "assassin": 7, "monk": 9, "bard": 8,
}


def _con_hp_bonus(con: int, fighter_class: bool) -> int:
    if con <= 6:    return -2 if con <= 3 else -1
    if con <= 14:   return 0
    if con == 15:   return 1
    if con == 16:   return 2
    # 17+ — fighters get more
    if con == 17:   return 3 if fighter_class else 2
    return 4 if fighter_class else 2  # 18


def _dex_ac_bonus(dex: int) -> int:
    if dex <= 6:    return -2 if dex <= 3 else -1
    if dex >= 18:   return 4
    if dex == 17:   return 3
    if dex == 16:   return 2
    if dex == 15:   return 1
    return 0


def _npc_thac0(class_name: str, level: int) -> int:
    """Approximate AD&D 1e THAC0 by class and level."""
    cl = (class_name or "").strip().lower().replace("-", "").replace(" ", "")
    if cl in ("fighter", "paladin", "ranger"):
        # 20 at L1, -1/level, capped at 6 (L15).
        return max(6, 20 - max(0, level - 1))
    if cl in ("cleric", "druid"):
        # 20 at L1, -1 every 3 levels
        return max(6, 20 - ((level - 1) // 3) * 1)
    if cl in ("thief", "assassin", "monk", "bard"):
        return max(6, 20 - ((level - 1) // 4) * 1)
    if cl in ("magicuser", "mage"):
        return max(6, 20 - ((level - 1) // 5) * 1)
    return max(6, 20 - max(0, level - 1) // 3)


_CLASS_ABILITY_PRIORITY: dict[str, list[str]] = {
    # Order: highest score → first ability, etc. Prime requisite first,
    # then survivability/utility per AD&D 1e class concerns.
    "fighter":     ["strength", "constitution", "dexterity", "wisdom", "charisma", "intelligence"],
    "paladin":     ["strength", "constitution", "wisdom", "charisma", "dexterity", "intelligence"],
    "ranger":      ["strength", "constitution", "dexterity", "wisdom", "intelligence", "charisma"],
    "cleric":      ["wisdom", "constitution", "strength", "charisma", "dexterity", "intelligence"],
    "druid":       ["wisdom", "constitution", "charisma", "strength", "dexterity", "intelligence"],
    "magicuser":   ["intelligence", "constitution", "dexterity", "wisdom", "charisma", "strength"],
    "mage":        ["intelligence", "constitution", "dexterity", "wisdom", "charisma", "strength"],
    "illusionist": ["intelligence", "constitution", "dexterity", "wisdom", "charisma", "strength"],
    "thief":       ["dexterity", "constitution", "strength", "charisma", "intelligence", "wisdom"],
    "assassin":    ["dexterity", "constitution", "strength", "intelligence", "wisdom", "charisma"],
    "monk":        ["strength", "wisdom", "dexterity", "constitution", "charisma", "intelligence"],
    "bard":        ["charisma", "dexterity", "intelligence", "constitution", "strength", "wisdom"],
}

_ARCHETYPE_PRIORITY: dict[str, list[str]] = {
    # For monsters / generic NPCs without a real class. Pick by description.
    "combat":  ["strength", "constitution", "dexterity", "wisdom", "charisma", "intelligence"],
    "arcane":  ["intelligence", "constitution", "dexterity", "wisdom", "charisma", "strength"],
    "divine":  ["wisdom", "constitution", "strength", "charisma", "dexterity", "intelligence"],
    "stealth": ["dexterity", "constitution", "strength", "intelligence", "charisma", "wisdom"],
}


def _normalize_class_token(s: str) -> str:
    return (s or "").strip().lower().replace("-", "").replace(" ", "")


def _ability_priority(class_name: str) -> list[str]:
    """
    Return the ability list in priority order for assignment (highest score
    → first ability, etc.). Handles single class, multi-class (split with /
    or |), known archetypes ('combat'/'arcane'/'divine'/'stealth'), and
    unknown class names (falls back to combat priority).

    For multi-class, primes from each class are stacked in declared order,
    then CON for survivability, then the rest.
    """
    raw = (class_name or "").strip()
    if not raw:
        return list(_ARCHETYPE_PRIORITY["combat"])

    # Archetype shortcut for monsters / generic NPCs
    arch = _ARCHETYPE_PRIORITY.get(_normalize_class_token(raw))
    if arch:
        return list(arch)

    parts = [p for p in re.split(r"[/|]", raw) if p.strip()]

    primes: list[str] = []
    for p in parts:
        cl = _normalize_class_token(p)
        cls_prio = _CLASS_ABILITY_PRIORITY.get(cl)
        if cls_prio and cls_prio[0] not in primes:
            primes.append(cls_prio[0])

    # Special case: monk — explicitly STR primary, WIS secondary.
    if len(parts) == 1 and _normalize_class_token(parts[0]) == "monk":
        return list(_CLASS_ABILITY_PRIORITY["monk"])

    if not primes:
        # Unrecognized class name — default to combat priority.
        return list(_ARCHETYPE_PRIORITY["combat"])

    result: list[str] = list(primes)
    if "constitution" not in result:
        result.append("constitution")

    fillers = ["dexterity", "wisdom", "charisma", "intelligence",
               "strength", "constitution"]
    for ab in fillers:
        if ab not in result:
            result.append(ab)
    return result[:6]


def _roll_3d6_straight() -> int:
    """Roll 3d6. Returns int 3..18. Used for minion-tier NPCs."""
    return sum(random.randint(1, 6) for _ in range(3))


def _roll_4d6_drop_lowest() -> int:
    """Roll 4d6 and drop the lowest die. Returns int 3..18."""
    rolls = [random.randint(1, 6) for _ in range(4)]
    rolls.sort()
    return sum(rolls[1:])  # drop rolls[0] (the lowest)


def _roll_5d6_keep_best_3() -> int:
    """Roll 5d6 and keep the best 3. Returns int 3..18 with a strong upper bias."""
    rolls = sorted([random.randint(1, 6) for _ in range(5)], reverse=True)
    return sum(rolls[:3])  # keep top 3


_TIER_ROLL_METHOD = {
    "minion":   "3d6_straight",
    "standard": "4d6_drop_lowest",
    "boss":     "5d6_keep_best_3",
}


def _normalize_tier(tier: str | None) -> str:
    t = (tier or "standard").strip().lower()
    return t if t in _TIER_ROLL_METHOD else "standard"


def _roll_one_score(tier: str) -> int:
    if tier == "minion":   return _roll_3d6_straight()
    if tier == "boss":     return _roll_5d6_keep_best_3()
    return _roll_4d6_drop_lowest()


def _roll_and_assign_abilities(class_name: str, tier: str = "standard") -> dict:
    """
    Roll six ability scores using the tier's roll method and assign them.

    Tier semantics:
      - 'minion':   3d6 straight in fixed STR/INT/WIS/DEX/CON/CHA order with
                    NO smart assignment. Cannon-fodder NPCs feel cannon-fodder.
      - 'standard': 4d6 drop lowest, sorted descending, assigned by class
                    priority (highest → prime requisite).
      - 'boss':     5d6 keep best 3, sorted descending, assigned by class
                    priority. Mean ≈ 15.7 — boss NPCs feel meaningfully harder.

    Returns a dict keyed by ability name (lowercase).
    """
    tier = _normalize_tier(tier)

    if tier == "minion":
        return {
            "strength":     _roll_3d6_straight(),
            "intelligence": _roll_3d6_straight(),
            "wisdom":       _roll_3d6_straight(),
            "dexterity":    _roll_3d6_straight(),
            "constitution": _roll_3d6_straight(),
            "charisma":     _roll_3d6_straight(),
        }

    scores = sorted([_roll_one_score(tier) for _ in range(6)], reverse=True)
    priority = _ability_priority(class_name)
    return {ability: int(scores[i]) for i, ability in enumerate(priority)}


# ── Tier auto-detection ──────────────────────────────────────────────────────

# Title / leadership keywords that always elevate an encounter to boss tier.
_BOSS_KEYWORDS = (
    "leader", "chief", "captain", "lord", "lady",
    "king", "queen", "prince", "princess",
    "boss", "champion", "warlord", "general",
    "high priest", "high priestess",
    "archmage", "warden", "master", "matriarch", "patriarch",
    "elder", "ancient", "dread",
)


def _parse_hd_value(hd_text: str) -> float:
    """
    Pull a numeric HD value out of a monsters.hit_dice string. Returns 0.0
    when nothing useful can be extracted.
    """
    if not hd_text:
        return 0.0
    s = str(hd_text).strip().lower()
    if s in ("½", "1/2"):
        return 0.5
    # Take the leading number before the first +/-/space
    m = re.match(r"\s*(\d+(?:\.\d+)?)", s)
    if not m:
        return 0.0
    try:
        return float(m.group(1))
    except ValueError:
        return 0.0


def _classify_npc_tier(
    monster_name:  str | None = None,
    monster_stats: dict | None = None,
    level:         int = 0,
    is_named_npc:  bool = False,
) -> str:
    """
    Auto-tier an encounter by inspecting its traits. Returns one of
    'minion' | 'standard' | 'boss'. Conservative — defaults to 'standard'.

    Boss triggers (any one):
      - monster_name contains a leadership / title keyword
      - monster_stats.hit_dice >= 8
      - level >= 7  (NPC class level)

    `is_named_npc` is accepted for future use but does NOT by itself
    elevate to boss — most named NPCs are merchants, henchmen, or one-line
    flavor characters who shouldn't roll boss-tier stats. The AI should
    pass npc_tier='boss' explicitly when a proper-named character is a
    deliberate antagonist below level 7.
    """
    name_l = (monster_name or "").lower()
    if any(kw in name_l for kw in _BOSS_KEYWORDS):
        return "boss"

    hd = _parse_hd_value((monster_stats or {}).get("hit_dice", ""))
    if hd >= 8:
        return "boss"

    if level >= 7:
        return "boss"

    return "standard"


def _roll_npc_stats(
    class_name: str,
    level: int = 1,
    npc_tier: str = "standard",
) -> dict:
    """
    Roll AD&D 1e NPC stats: 6 abilities, HP, AC, THAC0, starting equipment,
    carried gold, and a small chance of a personal magic item. Pure rolling
    — no DB writes.

    Tier controls roll method:
      'minion'   — 3d6 straight in order. No smart assignment. Mean ≈ 10.5.
                   Use for cannon fodder, generic guards, random encounters.
      'standard' — 4d6 drop lowest with smart class-aware assignment. Mean
                   ≈ 12.24. Use for most named NPCs.
      'boss'     — 5d6 keep best 3 with smart class-aware assignment. Mean
                   ≈ 15.7. Use for named villains, faction leaders, dungeon
                   bosses, anyone with a title in the story.

    For 'standard' and 'boss', ability assignment puts the highest rolled
    score on the class's prime requisite (STR for Fighter, INT for
    Magic-User, WIS for Cleric, DEX for Thief, etc.). Second-highest goes
    to CON for survivability across most classes (Monk exception → WIS).
    Multi-class names like 'Fighter/Magic-User' split the top scores
    between both prime requisites in declared order.
    """
    tier = _normalize_tier(npc_tier)
    cl = _normalize_class_token(class_name) or "fighter"
    hd  = _NPC_HIT_DIE.get(cl, 8)
    base_ac = _NPC_DEFAULT_AC.get(cl, 9)

    abilities = _roll_and_assign_abilities(class_name, tier=tier)

    fighter_class = cl in ("fighter", "paladin", "ranger")
    con_bonus = _con_hp_bonus(abilities["constitution"], fighter_class)
    hp_rolls = [random.randint(1, hd) for _ in range(max(1, level))]
    hp_max   = max(1, sum(hp_rolls) + con_bonus * level)

    dex_ac = _dex_ac_bonus(abilities["dexterity"])
    ac     = base_ac - dex_ac

    thac0 = _npc_thac0(class_name, level)

    equipment = _npc_equipment(cl, level)
    carried_gp = random.randint(level, level * 50) + random.randint(0, 50)
    magic_items: list = []
    # ~5% per level chance for one personal magic item, capped at L20.
    if random.randint(1, 100) <= min(40, 5 * level):
        try:
            haul = roll_treasure_by_type("M")
            magic_items = haul.get("magic_items") or []
        except Exception:
            magic_items = []

    return {
        "class":             class_name,
        "level":             level,
        "tier":              tier,
        "hit_die":           f"d{hd}",
        "abilities":         abilities,
        "ability_priority":  _ability_priority(class_name) if tier != "minion" else None,
        "roll_method":       _TIER_ROLL_METHOD[tier],
        "hp_max":            hp_max,
        "hp_rolls":          hp_rolls,
        "con_hp_bonus":      con_bonus,
        "ac":                ac,
        "dex_ac_bonus":      dex_ac,
        "thac0":             thac0,
        "equipment":         equipment,
        "carried_gp":        carried_gp,
        "magic_items":       magic_items,
    }


def _npc_equipment(cl: str, level: int) -> list[str]:
    """Class-appropriate starting equipment, lightly scaled by level."""
    eq: list[str] = []
    if cl in ("fighter", "paladin", "ranger"):
        eq += ["long sword", "chain mail", "shield"]
        if level >= 3: eq.append("composite long bow + 20 arrows")
    elif cl in ("cleric", "druid"):
        eq += ["mace", "chain mail", "shield", "holy symbol"]
    elif cl in ("magicuser", "mage"):
        eq += ["dagger", "staff", "spell book"]
    elif cl in ("thief", "assassin"):
        eq += ["short sword", "dagger", "leather armor", "thieves' tools"]
    elif cl == "monk":
        eq += ["bo stick", "robes"]
    elif cl == "bard":
        eq += ["short sword", "leather armor", "lute"]
    else:
        eq += ["club", "leather armor"]
    eq += ["backpack", "rations (1 week)", "waterskin", "torch x2"]
    return eq


def db_populate_npc(
    npc_name: str,
    level: int = 1,
    class_name: str = "Fighter",
    race: str | None = None,
    force_reroll: bool = False,
    npc_tier: str | None = None,
) -> dict:
    """
    Pre-roll full stats for a named NPC and persist them to the existing
    characters/character_abilities/character_status/class_levels tables.

    npc_tier controls stat-rolling strength:
      'minion'   — 3d6 straight, no smart assignment (cannon fodder)
      'standard' — 4d6 drop lowest with class-aware assignment (default)
      'boss'     — 5d6 keep best 3 with class-aware assignment (named
                   villains, faction leaders, dungeon bosses)

    When npc_tier is None (default), the tier is auto-detected from level:
    level >= 7 ⇒ 'boss', otherwise 'standard'. Pass an explicit value to
    override.

    Idempotent: if the NPC already has a class_levels row AND an abilities
    row, returns the existing stats unchanged. Pass force_reroll=True to
    overwrite.

    The NPC must already exist in the characters table (created via
    add_npc). This function does NOT create a new character row.
    """
    # Auto-detect tier when caller didn't specify.
    if npc_tier is None:
        npc_tier = _classify_npc_tier(
            monster_name=npc_name, level=level, is_named_npc=True,
        )
    tier = _normalize_tier(npc_tier)

    with _get_conn(read_only=True) as conn:
        npc_row = conn.execute(
            "SELECT character_id, name, character_type, race "
            "FROM characters WHERE LOWER(name) LIKE LOWER(?) "
            "  AND campaign_id = ? LIMIT 1",
            (f"{npc_name}%", _CAMPAIGN_ID),
        ).fetchone()

    if not npc_row:
        return {"error": f"NPC '{npc_name}' not found in characters table. "
                         "Call add_npc first."}

    char_id  = npc_row["character_id"]
    npc_race = race or npc_row["race"] or "Human"

    with _get_conn(read_only=True) as conn:
        has_class = conn.execute(
            "SELECT 1 FROM class_levels WHERE character_id = ? LIMIT 1",
            (char_id,),
        ).fetchone()
        has_abilities = conn.execute(
            "SELECT 1 FROM character_abilities WHERE character_id = ? LIMIT 1",
            (char_id,),
        ).fetchone()

    if has_class and has_abilities and not force_reroll:
        return _read_npc_full_stats(char_id, already_populated=True)

    stats = _roll_npc_stats(class_name, level, npc_tier=tier)
    a = stats["abilities"]

    with _get_conn() as conn:
        # class_levels
        existing_cl = conn.execute(
            "SELECT class_level_id FROM class_levels WHERE character_id = ? LIMIT 1",
            (char_id,),
        ).fetchone()
        if existing_cl:
            conn.execute(
                "UPDATE class_levels SET class_name = ?, level = ?, xp = ? "
                "WHERE class_level_id = ?",
                (class_name, level, 0, existing_cl["class_level_id"]),
            )
        else:
            conn.execute(
                "INSERT INTO class_levels (character_id, class_name, level, xp) "
                "VALUES (?, ?, ?, 0)",
                (char_id, class_name, level),
            )

        # character_abilities (PK is character_id, so upsert)
        conn.execute(
            "INSERT INTO character_abilities "
            "(character_id, strength, intelligence, wisdom, dexterity, "
            " constitution, charisma) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(character_id) DO UPDATE SET "
            "  strength = excluded.strength, "
            "  intelligence = excluded.intelligence, "
            "  wisdom = excluded.wisdom, "
            "  dexterity = excluded.dexterity, "
            "  constitution = excluded.constitution, "
            "  charisma = excluded.charisma",
            (char_id, a["strength"], a["intelligence"], a["wisdom"],
             a["dexterity"], a["constitution"], a["charisma"]),
        )

        # character_status — upsert by character_id
        existing_status = conn.execute(
            "SELECT status_id FROM character_status WHERE character_id = ? LIMIT 1",
            (char_id,),
        ).fetchone()
        if existing_status:
            conn.execute(
                "UPDATE character_status "
                "SET hp_current = ?, hp_max = ?, ac = ? "
                "WHERE character_id = ?",
                (stats["hp_max"], stats["hp_max"], stats["ac"], char_id),
            )
        else:
            conn.execute(
                "INSERT INTO character_status "
                "(character_id, hp_current, hp_max, ac) VALUES (?, ?, ?, ?)",
                (char_id, stats["hp_max"], stats["hp_max"], stats["ac"]),
            )

        # Stash equipment + carried gold + thac0 + magic items + roll
        # provenance in a category='npc_stats' world_facts row keyed to
        # this character.
        wf_payload = {
            "character_id":      char_id,
            "name":              npc_row["name"],
            "tier":              stats.get("tier", "standard"),
            "thac0":             stats["thac0"],
            "hit_die":           stats["hit_die"],
            "equipment":         stats["equipment"],
            "carried_gp":        stats["carried_gp"],
            "magic_items":       stats["magic_items"],
            "roll_method":       stats.get("roll_method"),
            "ability_priority":  stats.get("ability_priority"),
        }
        # Replace any prior npc_stats for this char.
        conn.execute(
            "DELETE FROM world_facts "
            "WHERE category = 'npc_stats' AND fact_text LIKE ?",
            (f'%"character_id": {char_id},%',),
        )
        conn.execute(
            "INSERT INTO world_facts (campaign_id, category, fact_text, source_note) "
            "VALUES (?, 'npc_stats', ?, ?)",
            (_CAMPAIGN_ID, json.dumps(wf_payload),
             f"populate_npc(name={npc_row['name']!r}, level={level}, class={class_name!r})"),
        )

    return _read_npc_full_stats(char_id, already_populated=False)


def _read_npc_full_stats(character_id: int, already_populated: bool) -> dict:
    """Read and assemble the full NPC stat block from all related tables."""
    with _get_conn(read_only=True) as conn:
        char = conn.execute(
            "SELECT character_id, name, character_type, race, alignment, notes "
            "FROM characters WHERE character_id = ?",
            (character_id,),
        ).fetchone()
        cls = conn.execute(
            "SELECT class_name, level, xp FROM class_levels "
            "WHERE character_id = ? LIMIT 1",
            (character_id,),
        ).fetchone()
        ab = conn.execute(
            "SELECT strength, intelligence, wisdom, dexterity, constitution, charisma "
            "FROM character_abilities WHERE character_id = ?",
            (character_id,),
        ).fetchone()
        st = conn.execute(
            "SELECT hp_current, hp_max, ac FROM character_status "
            "WHERE character_id = ? LIMIT 1",
            (character_id,),
        ).fetchone()
        wf = conn.execute(
            "SELECT fact_text FROM world_facts "
            "WHERE category = 'npc_stats' AND fact_text LIKE ? LIMIT 1",
            (f'%"character_id": {character_id},%',),
        ).fetchone()

    extra: dict = {}
    if wf and wf["fact_text"]:
        try:
            extra = json.loads(wf["fact_text"])
        except (json.JSONDecodeError, TypeError):
            pass

    return {
        "populated":         True,
        "already_populated": already_populated,
        "character_id":      character_id,
        "name":              char["name"] if char else None,
        "race":              char["race"] if char else None,
        "class":             cls["class_name"] if cls else None,
        "level":             cls["level"] if cls else None,
        "tier":              extra.get("tier", "standard"),
        "abilities":         _row_to_dict(ab) if ab else {},
        "hp_current":        st["hp_current"] if st else None,
        "hp_max":            st["hp_max"] if st else None,
        "ac":                st["ac"] if st else None,
        "thac0":             extra.get("thac0"),
        "hit_die":           extra.get("hit_die"),
        "equipment":         extra.get("equipment", []),
        "carried_gp":        extra.get("carried_gp"),
        "magic_items":       extra.get("magic_items", []),
        "roll_method":       extra.get("roll_method", "4d6_drop_lowest"),
        "ability_priority":  extra.get("ability_priority", []),
    }


# ══════════════════════════════════════════════════════════════════════════════
# CHARACTER ROSTER, XP, AND CLASS-LEVEL MANAGEMENT (Phase 8)
# ══════════════════════════════════════════════════════════════════════════════
#
# Live-gameplay improvements:
#   - Resolve characters by ID *or* name across every party-management tool
#   - grant_xp: party-wide XP awards with auto level-up detection + audit log
#   - add_class_level: explicit insertion (multi-class, henchmen, dual-class)
#   - list_characters: one-shot roster so the AI never has to probe IDs
# ══════════════════════════════════════════════════════════════════════════════

# Cache the classes.json XP tables on first read.
_XP_TABLE_CACHE: dict[str, list[int]] | None = None


def _load_xp_tables() -> dict[str, list[int]]:
    """Lazy-load every class's xp_table from data/classes.json."""
    global _XP_TABLE_CACHE
    if _XP_TABLE_CACHE is not None:
        return _XP_TABLE_CACHE
    path = _ROOT / "data" / "classes.json"
    out: dict[str, list[int]] = {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for cls_name, cls_data in (data or {}).items():
            tbl = cls_data.get("xp_table") or []
            if isinstance(tbl, list) and tbl:
                out[cls_name.lower()] = [int(x) for x in tbl]
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        # Missing or malformed classes.json — return whatever we managed to
        # parse (possibly empty). Callers degrade gracefully.
        pass
    _XP_TABLE_CACHE = out
    return out


def _xp_threshold_for_next_level(class_name: str, current_level: int) -> int | None:
    """
    Return the XP needed to advance from `current_level` to `current_level+1`
    for the given class, or None if the table is unavailable / capped.

    classes.json xp_table is 0-indexed by *level minus one*: index 0 = level 1
    starting XP (always 0); index 1 = XP needed for level 2; etc.  So the
    threshold to reach level (L+1) lives at xp_table[L].
    """
    tbl = _load_xp_tables().get((class_name or "").strip().lower())
    if not tbl:
        return None
    if current_level < 1:
        current_level = 1
    if current_level >= len(tbl):
        return None  # past the end of the table — no further auto-level
    return int(tbl[current_level])


def _resolve_character(target: int | str | None) -> int | None:
    """
    Resolve a character target (int id, numeric string, or name prefix) to
    a character_id within the active campaign. Returns None if not found.
    """
    if target is None:
        return None
    # Direct int.
    if isinstance(target, int):
        with _get_conn(read_only=True) as conn:
            row = conn.execute(
                "SELECT character_id FROM characters "
                "WHERE character_id = ? AND campaign_id = ? LIMIT 1",
                (target, _CAMPAIGN_ID),
            ).fetchone()
        return row["character_id"] if row else None

    # Strings: try numeric first, then name prefix (case-insensitive).
    s = str(target).strip()
    if not s:
        return None
    if s.isdigit():
        return _resolve_character(int(s))
    with _get_conn(read_only=True) as conn:
        row = conn.execute(
            "SELECT character_id FROM characters "
            "WHERE LOWER(name) LIKE LOWER(?) AND campaign_id = ? "
            "ORDER BY character_id LIMIT 1",
            (f"{s}%", _CAMPAIGN_ID),
        ).fetchone()
    return row["character_id"] if row else None


# ── grant_xp ─────────────────────────────────────────────────────────────────

def db_grant_xp(
    character_targets: list,
    amount: int,
    event_description: str,
) -> dict:
    """
    Add XP to every targeted character's class_levels row(s) and check each
    for a level-up threshold. The same XP amount is added to every class
    row a character has (multi-class characters split XP by AD&D rules
    elsewhere; this is a flat grant for narrative simplicity — the AI can
    rebalance via update_class_level if needed).

    Targets accept ints (character_id) or strings (numeric or name prefix).
    Returns a per-character result with new XP totals, current level,
    next-level threshold, and a `levelup_available` flag.

    Logs a single category='xp_log' world_facts row recording the grant
    with timestamp, amount, event_description, and the resolved
    character_ids.
    """
    import datetime
    if not isinstance(character_targets, list):
        character_targets = [character_targets]
    if amount is None:
        return {"error": "amount is required"}
    try:
        amount_int = int(amount)
    except (TypeError, ValueError):
        return {"error": f"amount must be an integer; got {amount!r}"}

    results: list[dict] = []
    resolved_ids: list[int] = []
    unresolved: list = []

    with _get_conn() as conn:
        for tgt in character_targets:
            cid = _resolve_character(tgt)
            if cid is None:
                unresolved.append(tgt)
                results.append({
                    "target":   tgt,
                    "error":    f"character not found: {tgt!r}",
                })
                continue
            resolved_ids.append(cid)

            char_row = conn.execute(
                "SELECT name FROM characters WHERE character_id = ?",
                (cid,),
            ).fetchone()
            char_name = char_row["name"] if char_row else f"#{cid}"

            class_rows = conn.execute(
                "SELECT class_level_id, class_name, level, xp "
                "FROM class_levels WHERE character_id = ? ORDER BY class_level_id",
                (cid,),
            ).fetchall()

            if not class_rows:
                results.append({
                    "target":         tgt,
                    "character_id":   cid,
                    "name":           char_name,
                    "error":          (f"{char_name} has no class_levels row. "
                                       "Use add_class_level to seed one before "
                                       "granting XP."),
                })
                continue

            per_class: list[dict] = []
            any_levelup = False
            for cr in class_rows:
                new_xp = max(0, int(cr["xp"] or 0) + amount_int)
                conn.execute(
                    "UPDATE class_levels SET xp = ? WHERE class_level_id = ?",
                    (new_xp, cr["class_level_id"]),
                )
                threshold = _xp_threshold_for_next_level(
                    cr["class_name"], int(cr["level"]),
                )
                levelup = bool(threshold is not None and new_xp >= threshold)
                if levelup:
                    any_levelup = True
                per_class.append({
                    "class_name":            cr["class_name"],
                    "current_level":         int(cr["level"]),
                    "old_xp":                int(cr["xp"] or 0),
                    "new_xp":                new_xp,
                    "next_level_threshold":  threshold,
                    "xp_to_next_level":      (None if threshold is None
                                              else max(0, threshold - new_xp)),
                    "levelup_available":     levelup,
                })

            results.append({
                "target":             tgt,
                "character_id":       cid,
                "name":               char_name,
                "amount_added":       amount_int,
                "classes":            per_class,
                "levelup_available":  any_levelup,
            })

        # ── Audit log ─────────────────────────────────────────────────────
        if resolved_ids:
            now = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            log_payload = {
                "ts":                now,
                "amount":            amount_int,
                "event":             event_description or "",
                "character_ids":     resolved_ids,
                "unresolved":        [str(t) for t in unresolved],
            }
            conn.execute(
                "INSERT INTO world_facts "
                "(campaign_id, category, fact_text, source_note) "
                "VALUES (?, 'xp_log', ?, ?)",
                (_CAMPAIGN_ID, json.dumps(log_payload),
                 f"grant_xp: {amount_int} XP to {len(resolved_ids)} char(s) — "
                 f"{event_description or 'no description'}"),
            )

    return {
        "granted":    bool(resolved_ids),
        "amount":     amount_int,
        "event":      event_description or "",
        "results":    results,
        "unresolved": [str(t) for t in unresolved] if unresolved else [],
    }


# ── add_class_level ──────────────────────────────────────────────────────────

def db_add_class_level(
    character_target: int | str,
    class_name: str,
    level: int = 1,
    xp: int = 0,
) -> dict:
    """
    Insert a fresh class_levels row for a character. Refuses to insert a
    duplicate (same character_id + class_name) — caller must use
    update_class_level / direct_db_edit to modify an existing row.

    Returns the new class_level_id and the inserted values, or an error
    when the character can't be resolved or the row already exists.
    """
    cid = _resolve_character(character_target)
    if cid is None:
        return {"error": f"character not found: {character_target!r}"}
    class_name = (class_name or "").strip()
    if not class_name:
        return {"error": "class_name is required"}
    try:
        level = int(level)
        xp    = int(xp)
    except (TypeError, ValueError):
        return {"error": "level and xp must be integers"}
    if level < 1:
        return {"error": f"level must be >= 1; got {level}"}
    if xp < 0:
        return {"error": f"xp must be >= 0; got {xp}"}

    with _get_conn() as conn:
        existing = conn.execute(
            "SELECT class_level_id, level, xp FROM class_levels "
            "WHERE character_id = ? AND LOWER(class_name) = LOWER(?) LIMIT 1",
            (cid, class_name),
        ).fetchone()
        if existing:
            return {
                "error": (f"class_levels row already exists for character_id={cid}, "
                          f"class_name={class_name!r}. Use update_class_level "
                          "(or direct_db_edit) to modify an existing row."),
                "existing": {
                    "class_level_id": existing["class_level_id"],
                    "level":          existing["level"],
                    "xp":             existing["xp"],
                },
            }

        cur = conn.execute(
            "INSERT INTO class_levels (character_id, class_name, level, xp) "
            "VALUES (?, ?, ?, ?)",
            (cid, class_name, level, xp),
        )
        new_id = cur.lastrowid

        char_row = conn.execute(
            "SELECT name FROM characters WHERE character_id = ?", (cid,),
        ).fetchone()

    return {
        "added":          True,
        "class_level_id": new_id,
        "character_id":   cid,
        "name":           char_row["name"] if char_row else None,
        "class_name":     class_name,
        "level":          level,
        "xp":             xp,
        "next_level_threshold": _xp_threshold_for_next_level(class_name, level),
    }


# ── list_characters ──────────────────────────────────────────────────────────

def db_list_characters() -> dict:
    """
    Return every character in the active campaign with a one-line summary:
    id, name, type, race, alignment, class levels, notes preview.
    """
    with _get_conn(read_only=True) as conn:
        rows = conn.execute(
            "SELECT character_id, name, character_type, race, alignment, notes "
            "FROM characters WHERE campaign_id = ? "
            "ORDER BY character_id",
            (_CAMPAIGN_ID,),
        ).fetchall()

        out: list[dict] = []
        for r in rows:
            cls_rows = conn.execute(
                "SELECT class_name, level, xp FROM class_levels "
                "WHERE character_id = ? ORDER BY class_level_id",
                (r["character_id"],),
            ).fetchall()
            classes = [
                {"class_name": cr["class_name"],
                 "level":      cr["level"],
                 "xp":         cr["xp"]}
                for cr in cls_rows
            ]
            classes_summary = (
                "/".join(f"{c['class_name']} {c['level']}" for c in classes)
                if classes else "(no class)"
            )
            notes_preview = (r["notes"] or "")
            if len(notes_preview) > 120:
                notes_preview = notes_preview[:117].rstrip() + "..."

            out.append({
                "character_id":     r["character_id"],
                "name":             r["name"],
                "character_type":   r["character_type"],
                "race":             r["race"],
                "alignment":        r["alignment"],
                "classes":          classes,
                "classes_summary":  classes_summary,
                "notes_preview":    notes_preview,
            })

    return {"count": len(out), "characters": out}


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 9 — AERIAL ENCOUNTERS & REACTION ROLLS
# Sky-scanning encounter checks · 2d6 social reaction table
# ══════════════════════════════════════════════════════════════════════════════

# ---------------------------------------------------------------------------
# Aerial encounter tables (AD&D 1e DMG-style)
# Keyed by elevation tier → terrain → list of (lo, hi, creature, num_appearing)
# Each terrain table is a d20 roll (1–20). Unlisted terrains fall back to
# the elevation's "default" table.
# ---------------------------------------------------------------------------
_AERIAL_ENCOUNTERS: dict[str, dict[str, list[tuple[int, int, str, str]]]] = {
    # Low altitude — under 1000 ft. Predators, scavengers, lower-tier flyers.
    "low": {
        "forest": [
            (1,  4,  "Giant Eagle",        "1d3"),
            (5,  7,  "Giant Hawk",         "1d4"),
            (8,  9,  "Stirge",             "3d6"),
            (10, 11, "Harpy",              "1d4"),
            (12, 13, "Hippogriff",         "1d4"),
            (14, 15, "Giant Owl",          "1d3"),
            (16, 16, "Wyvern",             "1d2"),
            (17, 17, "Griffon",            "1d2"),
            (18, 18, "Peryton",            "1d2"),
            (19, 19, "Pegasus",            "1d2"),
            (20, 20, "Green Dragon",       "1"),
        ],
        "mountains": [
            (1,  3,  "Giant Eagle",        "1d4"),
            (4,  6,  "Hippogriff",         "1d4"),
            (7,  8,  "Griffon",            "1d3"),
            (9,  10, "Wyvern",             "1d2"),
            (11, 12, "Gargoyle",           "1d6"),
            (13, 14, "Giant Hawk",         "1d4"),
            (15, 15, "Peryton",            "1d2"),
            (16, 16, "Harpy",              "1d4"),
            (17, 17, "Aarakocra",          "2d6"),
            (18, 18, "Manticore",          "1d2"),
            (19, 19, "White Dragon",       "1"),
            (20, 20, "Red Dragon",         "1"),
        ],
        "plains": [
            (1,  4,  "Giant Eagle",        "1d3"),
            (5,  7,  "Giant Hawk",         "1d4"),
            (8,  10, "Hippogriff",         "1d4"),
            (11, 12, "Pegasus",            "1d4"),
            (13, 14, "Griffon",            "1d3"),
            (15, 16, "Harpy",              "1d4"),
            (17, 17, "Sphinx (Androsphinx)", "1"),
            (18, 18, "Wyvern",             "1d2"),
            (19, 19, "Chimera",            "1"),
            (20, 20, "Blue Dragon",        "1"),
        ],
        "coast": [
            (1,  4,  "Giant Eagle",        "1d3"),
            (5,  8,  "Giant Hawk",         "1d4"),
            (9,  10, "Stirge",             "2d6"),
            (11, 12, "Harpy",              "1d4"),
            (13, 14, "Pegasus",            "1d3"),
            (15, 16, "Hippogriff",         "1d4"),
            (17, 17, "Griffon",            "1d2"),
            (18, 18, "Wyvern",             "1"),
            (19, 19, "Sea Hag",            "1d2"),
            (20, 20, "Bronze Dragon",      "1"),
        ],
        "swamp": [
            (1,  4,  "Stirge",             "3d6"),
            (5,  7,  "Giant Mosquito",     "2d4"),
            (8,  9,  "Harpy",              "1d4"),
            (10, 11, "Giant Bat",          "2d4"),
            (12, 13, "Vampire Bat",        "1d6"),
            (14, 14, "Wyvern",             "1"),
            (15, 15, "Will-O-(the)-Wisp",  "1d3"),
            (16, 16, "Imp",                "1"),
            (17, 17, "Quasit",             "1"),
            (18, 18, "Mist Dragon",        "1"),
            (19, 19, "Black Dragon",       "1"),
            (20, 20, "Demon (Type I)",     "1"),
        ],
        "hills": [
            (1,  3,  "Giant Eagle",        "1d4"),
            (4,  6,  "Giant Hawk",         "1d4"),
            (7,  9,  "Hippogriff",         "1d4"),
            (10, 11, "Griffon",            "1d3"),
            (12, 13, "Harpy",              "1d4"),
            (14, 15, "Gargoyle",           "1d4"),
            (16, 16, "Wyvern",             "1d2"),
            (17, 17, "Peryton",            "1d2"),
            (18, 18, "Manticore",          "1d2"),
            (19, 19, "Chimera",            "1"),
            (20, 20, "Green Dragon",       "1"),
        ],
        "desert": [
            (1,  4,  "Giant Hawk",         "1d4"),
            (5,  7,  "Giant Eagle",        "1d3"),
            (8,  10, "Hippogriff",         "1d4"),
            (11, 12, "Vulture",            "2d6"),
            (13, 14, "Griffon",            "1d2"),
            (15, 16, "Harpy",              "1d4"),
            (17, 17, "Wyvern",             "1d2"),
            (18, 18, "Sphinx (Criosphinx)", "1"),
            (19, 19, "Manticore",          "1d2"),
            (20, 20, "Blue Dragon",        "1"),
        ],
        "default": [
            (1,  4,  "Giant Eagle",        "1d3"),
            (5,  7,  "Giant Hawk",         "1d4"),
            (8,  10, "Hippogriff",         "1d4"),
            (11, 12, "Stirge",             "2d6"),
            (13, 14, "Harpy",              "1d4"),
            (15, 16, "Griffon",            "1d2"),
            (17, 17, "Gargoyle",           "1d4"),
            (18, 18, "Wyvern",             "1d2"),
            (19, 19, "Manticore",          "1"),
            (20, 20, "Green Dragon",       "1"),
        ],
    },

    # Medium altitude — 1000–5000 ft. Dragons, manticores, pegasi, chimera.
    "medium": {
        "forest": [
            (1,  4,  "Giant Hawk",         "1d4"),
            (5,  7,  "Pegasus",            "1d4"),
            (8,  9,  "Griffon",            "1d3"),
            (10, 11, "Hippogriff",         "1d4"),
            (12, 13, "Wyvern",             "1d2"),
            (14, 15, "Manticore",          "1d2"),
            (16, 16, "Peryton",            "1d3"),
            (17, 17, "Chimera",            "1"),
            (18, 18, "Green Dragon",       "1"),
            (19, 19, "Roc (small)",        "1"),
            (20, 20, "Couatl",             "1"),
        ],
        "mountains": [
            (1,  3,  "Giant Hawk",         "1d4"),
            (4,  6,  "Griffon",            "1d3"),
            (7,  8,  "Wyvern",             "1d2"),
            (9,  10, "Manticore",          "1d2"),
            (11, 12, "Chimera",            "1"),
            (13, 14, "Roc",                "1"),
            (15, 15, "Pegasus",            "1d3"),
            (16, 16, "Red Dragon",         "1"),
            (17, 17, "Silver Dragon",      "1"),
            (18, 18, "White Dragon",       "1"),
            (19, 19, "Cloud Giant (mounted)", "1"),
            (20, 20, "Storm Giant (mounted)", "1"),
        ],
        "plains": [
            (1,  4,  "Giant Hawk",         "1d4"),
            (5,  7,  "Pegasus",            "1d4"),
            (8,  10, "Griffon",            "1d3"),
            (11, 12, "Wyvern",             "1d2"),
            (13, 14, "Manticore",          "1d2"),
            (15, 16, "Chimera",            "1"),
            (17, 17, "Roc (small)",        "1"),
            (18, 18, "Blue Dragon",        "1"),
            (19, 19, "Brass Dragon",       "1"),
            (20, 20, "Couatl",             "1"),
        ],
        "coast": [
            (1,  4,  "Giant Hawk",         "1d4"),
            (5,  7,  "Pegasus",            "1d3"),
            (8,  9,  "Griffon",            "1d2"),
            (10, 11, "Wyvern",             "1d2"),
            (12, 13, "Manticore",          "1d2"),
            (14, 15, "Roc",                "1"),
            (16, 16, "Chimera",            "1"),
            (17, 17, "Bronze Dragon",      "1"),
            (18, 18, "Storm Giant",        "1"),
            (19, 19, "Cloud Giant",        "1"),
            (20, 20, "Couatl",             "1"),
        ],
        "swamp": [
            (1,  3,  "Giant Hawk",         "1d4"),
            (4,  6,  "Wyvern",             "1d2"),
            (7,  8,  "Manticore",          "1d2"),
            (9,  10, "Will-O-(the)-Wisp",  "1d4"),
            (11, 12, "Imp",                "1"),
            (13, 14, "Quasit",             "1"),
            (15, 16, "Mist Dragon",        "1"),
            (17, 17, "Black Dragon",       "1"),
            (18, 18, "Demon (Type II)",    "1"),
            (19, 19, "Devil (Erinyes)",    "1d2"),
            (20, 20, "Roc",                "1"),
        ],
        "hills": [
            (1,  3,  "Giant Hawk",         "1d4"),
            (4,  6,  "Pegasus",            "1d3"),
            (7,  9,  "Griffon",            "1d3"),
            (10, 11, "Wyvern",             "1d2"),
            (12, 13, "Manticore",          "1d2"),
            (14, 15, "Chimera",            "1"),
            (16, 16, "Peryton",            "1d3"),
            (17, 17, "Roc (small)",        "1"),
            (18, 18, "Green Dragon",       "1"),
            (19, 19, "Brass Dragon",       "1"),
            (20, 20, "Cloud Giant",        "1"),
        ],
        "desert": [
            (1,  3,  "Giant Hawk",         "1d4"),
            (4,  6,  "Wyvern",             "1d2"),
            (7,  9,  "Manticore",          "1d2"),
            (10, 11, "Griffon",            "1d2"),
            (12, 13, "Chimera",            "1"),
            (14, 15, "Roc",                "1"),
            (16, 16, "Sphinx (Androsphinx)", "1"),
            (17, 17, "Blue Dragon",        "1"),
            (18, 18, "Brass Dragon",       "1"),
            (19, 19, "Djinni",             "1"),
            (20, 20, "Efreeti",            "1"),
        ],
        "default": [
            (1,  4,  "Giant Hawk",         "1d4"),
            (5,  7,  "Pegasus",            "1d3"),
            (8,  10, "Griffon",            "1d2"),
            (11, 12, "Wyvern",             "1d2"),
            (13, 14, "Manticore",          "1d2"),
            (15, 16, "Chimera",            "1"),
            (17, 17, "Roc (small)",        "1"),
            (18, 18, "Peryton",            "1d3"),
            (19, 19, "Couatl",             "1"),
            (20, 20, "Red Dragon",         "1"),
        ],
    },

    # High altitude — above 5000 ft. Rocs, true elementals, dragons, planar.
    "high": {
        "forest": [
            (1,  4,  "Roc",                "1"),
            (5,  7,  "Air Elemental",      "1"),
            (8,  10, "Invisible Stalker",  "1"),
            (11, 12, "Cloud Giant",        "1"),
            (13, 14, "Storm Giant",        "1"),
            (15, 16, "Couatl",             "1"),
            (17, 17, "Djinni",             "1"),
            (18, 18, "Silver Dragon",      "1"),
            (19, 19, "Gold Dragon",        "1"),
            (20, 20, "Aerial Servant",     "1"),
        ],
        "mountains": [
            (1,  3,  "Roc",                "1d2"),
            (4,  6,  "Air Elemental",      "1"),
            (7,  9,  "Storm Giant",        "1"),
            (10, 11, "Cloud Giant",        "1d2"),
            (12, 13, "Silver Dragon",      "1"),
            (14, 15, "Gold Dragon",        "1"),
            (16, 16, "Red Dragon",         "1"),
            (17, 17, "Djinni",             "1"),
            (18, 18, "Invisible Stalker",  "1"),
            (19, 19, "Couatl",             "1"),
            (20, 20, "Aerial Servant",     "1"),
        ],
        "plains": [
            (1,  4,  "Roc",                "1"),
            (5,  7,  "Air Elemental",      "1"),
            (8,  10, "Invisible Stalker",  "1"),
            (11, 12, "Cloud Giant",        "1"),
            (13, 14, "Couatl",             "1"),
            (15, 16, "Storm Giant",        "1"),
            (17, 17, "Djinni",             "1"),
            (18, 18, "Brass Dragon",       "1"),
            (19, 19, "Gold Dragon",        "1"),
            (20, 20, "Aerial Servant",     "1"),
        ],
        "coast": [
            (1,  3,  "Roc",                "1"),
            (4,  6,  "Air Elemental",      "1"),
            (7,  9,  "Storm Giant",        "1d2"),
            (10, 11, "Cloud Giant",        "1"),
            (12, 13, "Bronze Dragon",      "1"),
            (14, 15, "Silver Dragon",      "1"),
            (16, 16, "Couatl",             "1"),
            (17, 17, "Djinni",             "1"),
            (18, 18, "Invisible Stalker",  "1"),
            (19, 19, "Gold Dragon",        "1"),
            (20, 20, "Aerial Servant",     "1"),
        ],
        "swamp": [
            (1,  4,  "Air Elemental",      "1"),
            (5,  7,  "Invisible Stalker",  "1"),
            (8,  9,  "Roc",                "1"),
            (10, 11, "Mist Dragon",        "1"),
            (12, 13, "Demon (Type IV)",    "1"),
            (14, 15, "Devil (Horned)",     "1"),
            (16, 16, "Couatl",             "1"),
            (17, 17, "Storm Giant",        "1"),
            (18, 18, "Cloud Giant",        "1"),
            (19, 19, "Djinni",             "1"),
            (20, 20, "Solar",              "1"),
        ],
        "hills": [
            (1,  3,  "Roc",                "1"),
            (4,  6,  "Air Elemental",      "1"),
            (7,  9,  "Cloud Giant",        "1"),
            (10, 11, "Storm Giant",        "1"),
            (12, 13, "Invisible Stalker",  "1"),
            (14, 15, "Couatl",             "1"),
            (16, 16, "Silver Dragon",      "1"),
            (17, 17, "Gold Dragon",        "1"),
            (18, 18, "Brass Dragon",       "1"),
            (19, 19, "Djinni",             "1"),
            (20, 20, "Aerial Servant",     "1"),
        ],
        "desert": [
            (1,  3,  "Roc",                "1d2"),
            (4,  6,  "Air Elemental",      "1"),
            (7,  9,  "Djinni",             "1"),
            (10, 12, "Efreeti",            "1"),
            (13, 14, "Cloud Giant",        "1"),
            (15, 16, "Storm Giant",        "1"),
            (17, 17, "Couatl",             "1"),
            (18, 18, "Brass Dragon",       "1"),
            (19, 19, "Gold Dragon",        "1"),
            (20, 20, "Aerial Servant",     "1"),
        ],
        "default": [
            (1,  4,  "Roc",                "1"),
            (5,  7,  "Air Elemental",      "1"),
            (8,  10, "Invisible Stalker",  "1"),
            (11, 12, "Cloud Giant",        "1"),
            (13, 14, "Storm Giant",        "1"),
            (15, 16, "Couatl",             "1"),
            (17, 17, "Djinni",             "1"),
            (18, 18, "Silver Dragon",      "1"),
            (19, 19, "Gold Dragon",        "1"),
            (20, 20, "Aerial Servant",     "1"),
        ],
    },
}

_AERIAL_ALTITUDE_LABEL: dict[str, str] = {
    "low":    "Under 1000 ft",
    "medium": "1000-5000 ft",
    "high":   "Above 5000 ft",
}

_AERIAL_APPROACH_DIRECTIONS: list[str] = [
    "from the north",
    "from the northeast",
    "from the east",
    "from the southeast",
    "from the south",
    "from the southwest",
    "from the west",
    "from the northwest",
    "directly above (descending)",
    "from below (rising on a thermal)",
]

# Purely instinctual fauna — reaction roll is meaningless because the
# creature is acting on hunger or territorial drive rather than disposition.
_AERIAL_INSTINCT_ONLY: set[str] = {
    "Stirge",
    "Giant Mosquito",
    "Giant Bat",
    "Vampire Bat",
    "Vulture",
    "Giant Hawk",
    "Giant Owl",
    "Giant Eagle",
}


def _roll_dice_expr(text: str) -> int:
    """
    Roll a simple dice expression: 'NdM', 'NdM+K', 'NdM-K', 'X-Y', or a
    plain integer. Returns the rolled total (minimum 1).
    """
    if not text:
        return 1
    s = str(text).strip().lower()
    m = re.match(r"^(\d+)d(\d+)([+-]\d+)?$", s)
    if m:
        n     = int(m.group(1))
        sides = int(m.group(2))
        mod   = int(m.group(3) or 0)
        total = sum(random.randint(1, sides) for _ in range(n)) + mod
        return max(1, total)
    m = re.match(r"^(\d+)-(\d+)$", s)
    if m:
        return random.randint(int(m.group(1)), int(m.group(2)))
    try:
        return max(1, int(float(s)))
    except ValueError:
        return 1


def db_check_aerial_encounter(
    elevation:   str = "low",
    terrain:     str = "plains",
    chance_in_6: int = 1,
) -> dict:
    """
    Check for an aerial encounter at the given elevation tier and terrain.

    Rolls 1d6 against chance_in_6. On miss returns a 'clear' result with
    encounter=False. On hit, rolls a creature on the AD&D 1e aerial table
    for the given elevation x terrain, with count, altitude descriptor,
    approach direction, and a reaction_roll_eligible flag for whether
    parley/reaction is meaningful for that creature.
    """
    elev = (elevation or "low").lower().strip()
    if elev not in _AERIAL_ENCOUNTERS:
        elev = "low"
    terr = (terrain or "plains").lower().strip().replace(" ", "_")
    chance = max(1, min(int(chance_in_6 or 1), 6))

    d6 = random.randint(1, 6)
    triggered = d6 <= chance

    result: dict = {
        "encounter":    triggered,
        "d6_roll":      d6,
        "chance_in_6":  chance,
        "elevation":    elev,
        "altitude":     _AERIAL_ALTITUDE_LABEL.get(elev, ""),
        "terrain":      terr,
    }

    if not triggered:
        result.update({
            "creature":               None,
            "count":                  0,
            "approach":               "",
            "reaction_roll_eligible": False,
            "note": (
                f"Sky is clear (rolled {d6}, needed <= {chance})."
            ),
        })
        return result

    by_terrain = _AERIAL_ENCOUNTERS[elev]
    table = by_terrain.get(terr) or by_terrain["default"]

    d20 = random.randint(1, 20)
    creature, num_appearing_text = table[-1][2], table[-1][3]
    for (lo, hi, name, na) in table:
        if lo <= d20 <= hi:
            creature, num_appearing_text = name, na
            break

    count    = _roll_dice_expr(num_appearing_text)
    approach = random.choice(_AERIAL_APPROACH_DIRECTIONS)
    reaction_eligible = creature not in _AERIAL_INSTINCT_ONLY

    result.update({
        "creature":               creature,
        "count":                  count,
        "number_appearing_text":  num_appearing_text,
        "d20_roll":               d20,
        "approach":               approach,
        "reaction_roll_eligible": reaction_eligible,
        "note": (
            f"Aerial contact at {_AERIAL_ALTITUDE_LABEL.get(elev, '')}: "
            f"{count}x {creature} approaching {approach} over {terr} "
            f"(d20={d20})."
        ),
    })
    return result


# ---------------------------------------------------------------------------
# Reaction roll (AD&D 1e DMG, 2d6 + Cha + situation)
# ---------------------------------------------------------------------------
_REACTION_TIERS: list[tuple[int, str, str, str]] = [
    # (upper_bound, tier_key, label, interpretation_template)
    (2,  "immediate_attack",
         "Immediate Attack",
         "{name} attacks at once - no parlay possible. Roll initiative."),
    (5,  "hostile",
         "Hostile",
         "{name} is hostile and likely to attack unless given strong reason "
         "otherwise (gifts, threats, retreat)."),
    (8,  "uncertain",
         "Uncertain / Wary",
         "{name} is wary and undecided - watching, weapons ready. Further "
         "interaction may swing the encounter either way."),
    (11, "indifferent",
         "Indifferent / Neutral",
         "{name} is neutral - willing to converse but not predisposed to "
         "help. Trade or persuasion may shift them."),
    (999, "friendly",
         "Friendly",
         "{name} is friendly and willing to talk, trade, or even cooperate "
         "within reason."),
]


def db_roll_reaction(
    creature_name:      str,
    charisma_modifier:  int = 0,
    situation_modifier: int = 0,
) -> dict:
    """
    Roll 2d6 + Cha mod + situation mod on the AD&D 1e reaction table.

    situation_modifier is clamped to [-3, +3]. Logs the full roll to
    world_facts (category 'reaction_log') so the DM can audit social
    encounter history.
    """
    name    = (creature_name or "the creature").strip() or "the creature"
    cha_mod = int(charisma_modifier or 0)
    sit_mod = max(-3, min(3, int(situation_modifier or 0)))

    d_a  = random.randint(1, 6)
    d_b  = random.randint(1, 6)
    base  = d_a + d_b
    final = base + cha_mod + sit_mod

    tier_key, label, interpretation = "friendly", "Friendly", ""
    for upper, key, lbl, tmpl in _REACTION_TIERS:
        if final <= upper:
            tier_key, label = key, lbl
            interpretation = tmpl.format(name=name)
            break

    breakdown = (
        f"2d6=({d_a}+{d_b})={base}, Cha {cha_mod:+d}, "
        f"situation {sit_mod:+d} -> {final}"
    )

    log_entry = {
        "creature":           name,
        "d6_a":               d_a,
        "d6_b":               d_b,
        "base_2d6":           base,
        "charisma_modifier":  cha_mod,
        "situation_modifier": sit_mod,
        "final_result":       final,
        "tier":               tier_key,
        "label":              label,
    }

    persisted = True
    try:
        with _get_conn() as conn:
            conn.execute(
                "INSERT INTO world_facts "
                "(campaign_id, category, fact_text, source_note) "
                "VALUES (?, 'reaction_log', ?, 'reaction_system')",
                (_CAMPAIGN_ID, json.dumps(log_entry)),
            )
    except Exception:
        persisted = False

    return {
        "creature":           name,
        "d6_rolls":           [d_a, d_b],
        "base_2d6":           base,
        "charisma_modifier":  cha_mod,
        "situation_modifier": sit_mod,
        "final_result":       final,
        "tier":               tier_key,
        "disposition":        label,
        "interpretation":     interpretation,
        "breakdown":          breakdown,
        "logged":             persisted,
    }
