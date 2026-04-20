"""
engine/db.py
------------
Database access layer for the active campaign database.

The active database is determined by config.json in the project root:
    { "active_campaign_db": "saves/my_campaign.db" }

Use switch_character.py to change the active database. If config.json is
absent or has no entry, falls back to saves/theron.db.

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
    Falls back to saves/theron.db if config.json is missing or malformed.
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


_DB_PATH = _resolve_db_path()

# Campaign and PC IDs — standard for all campaigns created with new_character_template.sql
_CAMPAIGN_ID = 1
_PC_CHARACTER_ID = 1


@contextmanager
def _get_conn(read_only: bool = False):
    """
    Yield an open SQLite connection. Use read_only=True for SELECT-only paths.
    WAL mode is enabled for safe concurrent reads while the game loop writes.
    """
    if read_only:
        uri = f"file:{_DB_PATH}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    else:
        conn = sqlite3.connect(_DB_PATH)
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
    race: str = "",
    character_type: str = "NPC",
    notes: str = "",
    relationship_to_theron: str = "",
    relationship_notes: str = "",
) -> dict:
    """
    Add a new NPC to the characters table. If relationship_to_theron is
    provided, also inserts a row in the relationships table linking this NPC
    to Theron (character_id=1). Returns the new character row.
    """
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO characters (campaign_id, name, character_type, race, notes) "
            "VALUES (?, ?, ?, ?, ?)",
            (_CAMPAIGN_ID, name, character_type, race or None, notes or None),
        )
        new_id = cur.lastrowid

        if relationship_to_theron:
            cur.execute(
                "INSERT INTO relationships "
                "(source_character_id, target_character_id, relationship_type, notes) "
                "VALUES (?, ?, ?, ?)",
                (_PC_CHARACTER_ID, new_id,
                 relationship_to_theron, relationship_notes or None),
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
