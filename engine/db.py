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
