"""
server/mcp_server.py
--------------------
Local MCP server for the Greyhawk Solo RPG.

The game is played entirely inside Claude.ai chat. Claude acts as DM and calls
these tools to read/write persistent game state stored in saves/theron.db.

Tools exposed:
  SESSION
  session_start         -- One-call briefing: character + scene + history + pending updates

  READ
  get_character_state   -- Theron's full stats, HP, AC, inventory, abilities
  get_realm_state       -- All locations, troops, treasury, active projects
  roll_dice             -- Parse and roll any dice expression (e.g. "3d6+2")
  get_current_scene     -- Current location, active NPCs, recent events
  get_recent_history    -- Last N turns from ai_turns table
  get_pending_updates   -- Turns with state_changes notes not yet DB-committed

  WRITE
  save_turn             -- Write player action + DM narrative to the database
  update_character_status  -- Change HP, AC, status notes on Theron
  update_treasury          -- Add/subtract coins or gems from a treasury account
  add_location             -- Insert a new location into the realm
  update_location_status   -- Change status/notes on an existing location
  update_troop_count       -- Set or adjust count on a troop group
  add_troop_group          -- Insert a new troop group
  add_item                 -- Create an item and assign it to inventory
  update_world_fact        -- Upsert a campaign fact in world_facts
  update_npc               -- Change notes, status, race, alignment on an NPC
  add_npc                  -- Add a new NPC and optional relationship to Theron

Architecture:
  - FastMCP (mcp 1.27.0) runs over stdio; Claude Desktop connects as a client.
  - All DB access goes through engine/db.py — this file contains zero SQL.
  - saves/theron.db is read for all get_* tools; only save_turn writes to it.

Run standalone for testing:
    python server/mcp_server.py
"""

import json
import random
import re
import sys
from pathlib import Path
from typing import Annotated

# Allow imports from project root (works whether run from root or server/)
_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from mcp.server.fastmcp import FastMCP

from engine.db import (
    # Read
    load_character,
    load_realm,
    load_recent_ai_turns,
    load_current_scene,
    get_pending_updates      as db_get_pending_updates,
    # Write — turns
    write_ai_turn,
    update_current_scene,
    # Write — game state
    update_character_status  as db_update_character_status,
    update_treasury          as db_update_treasury,
    add_location             as db_add_location,
    update_location_status   as db_update_location_status,
    update_troop_count       as db_update_troop_count,
    add_troop_group          as db_add_troop_group,
    add_item                 as db_add_item,
    update_world_fact        as db_update_world_fact,
    update_npc               as db_update_npc,
    add_npc                  as db_add_npc,
    # Create — new campaign
    create_character_db      as db_create_character_db,
)

# ── Server instance ────────────────────────────────────────────────────────────

mcp = FastMCP(
    name="greyhawk-solo",
    instructions=(
        "You are the Dungeon Master for Theron Vale's ongoing AD&D 1e campaign "
        "set in the World of Greyhawk (576 CY). "
        "Use these tools to read persistent game state before narrating, "
        "always call roll_dice for any mechanical outcome rather than inventing "
        "results, and call save_turn after each meaningful player action. "
        "The database is the source of truth — never contradict it."
    ),
)


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: get_character_state
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_character_state() -> dict:
    """
    Return Theron Vale's complete character state from the database.

    Includes: name, race, class levels (Fighter 7 / Magic-User 7), current and
    max HP, AC, movement, attacks per round, all six ability scores, full
    equipped and carried inventory with magic item flags, and status notes.

    Call this at session start or any time you need to verify Theron's current
    stats before describing combat outcomes, skill checks, or spell options.
    """
    char = load_character()
    if not char:
        return {"error": "Character not found in database."}

    # Flatten for readability at the tool boundary
    status = char.pop("status", {}) or {}
    abilities = char.pop("abilities", {}) or {}

    char["hp_current"]       = status.get("hp_current")
    char["hp_max"]           = status.get("hp_max")
    char["ac"]               = status.get("ac")
    char["movement"]         = status.get("movement")
    char["attacks_per_round"]= status.get("attacks_per_round")
    char["status_notes"]     = status.get("status_notes")

    char["str"] = abilities.get("strength")
    char["int"] = abilities.get("intelligence")
    char["wis"] = abilities.get("wisdom")
    char["dex"] = abilities.get("dexterity")
    char["con"] = abilities.get("constitution")
    char["cha"] = abilities.get("charisma")
    char["portrait_path"] = abilities.get("portrait_path")

    return char


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: get_realm_state
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_realm_state() -> dict:
    """
    Return the full state of Theron's realm (Quasquetan and all holdings).

    Includes:
      locations   -- All 24 named locations with type, status, and parent
      troops      -- All 13 troop groups with counts, commanders, and billets
      treasury    -- All treasury accounts with coin and gem totals
      livestock   -- Animal counts by type across all farms
      key_npcs    -- Named NPCs with relationship type and notes

    Call this when the player asks about domain affairs, troop dispositions,
    treasury balance, ongoing construction projects, or NPC whereabouts.
    """
    realm = load_realm()
    if not realm:
        return {"error": "Realm data not found in database."}

    # Summarise treasury into a quick total for convenience
    gp_total = sum(a.get("gp", 0) or 0 for a in realm.get("treasury", []))
    gems_total = sum(a.get("gems_gp_value", 0) or 0 for a in realm.get("treasury", []))
    realm["treasury_summary"] = {
        "liquid_gp": gp_total,
        "gems_gp_value": gems_total,
        "total_gp_equivalent": gp_total + gems_total,
    }

    troop_total = sum(t.get("count", 0) or 0 for t in realm.get("troops", []))
    realm["troop_summary"] = {"total_troops": troop_total}

    return realm


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: roll_dice
# ══════════════════════════════════════════════════════════════════════════════

_DICE_RE = re.compile(
    r"^\s*(?:(\d+)\s*[dD])?\s*(\d+)\s*([+-]\s*\d+)?\s*$"
)

def _parse_dice(expression: str) -> tuple[int, int, int]:
    """
    Parse a dice expression into (num_dice, die_sides, modifier).
    Accepts: "d20", "1d20", "3d6", "3d6+2", "2d8-1", "20" (flat number).
    Returns (num_dice, sides, modifier). Raises ValueError on bad input.
    """
    expr = expression.strip()
    # Handle bare integer (no dice)
    if re.match(r"^\d+$", expr):
        return (0, 0, int(expr))

    m = re.match(
        r"^(\d+)?[dD](\d+)\s*([+-]\s*\d+)?$",
        expr.replace(" ", ""),
    )
    if not m:
        raise ValueError(f"Cannot parse dice expression: '{expression}'")

    num   = int(m.group(1)) if m.group(1) else 1
    sides = int(m.group(2))
    mod_str = (m.group(3) or "0").replace(" ", "")
    mod   = int(mod_str)

    if num < 1:
        raise ValueError("Number of dice must be at least 1.")
    if sides < 2:
        raise ValueError("Die must have at least 2 sides.")
    if num > 100:
        raise ValueError("Maximum 100 dice per roll.")

    return (num, sides, mod)


@mcp.tool()
def roll_dice(
    expression: Annotated[
        str,
        "Dice expression to evaluate. Examples: '1d20', 'd6', '3d6+2', "
        "'2d8-1', '7d6', '4d6'. Use standard NdS+M notation.",
    ],
    label: Annotated[
        str,
        "Optional label for this roll, e.g. 'attack vs goblin', 'fireball damage'.",
    ] = "",
) -> dict:
    """
    Roll any standard dice expression and return the full breakdown.

    ALWAYS use this tool for mechanical outcomes — attack rolls, damage,
    saving throws, random encounters, ability checks, morale. Never invent
    dice results. The engine is the source of truth for all randomness.

    Returns:
      expression   -- the expression as given
      label        -- the label as given
      num_dice     -- number of dice rolled
      die_sides    -- sides on each die
      modifier     -- flat modifier applied
      individual_rolls -- list of each die result
      subtotal     -- sum of dice before modifier
      total        -- final result (subtotal + modifier)
      natural_20   -- true if 1d20 and result was 20 (for attack rolls)
      natural_1    -- true if 1d20 and result was 1 (fumble)
    """
    try:
        num, sides, mod = _parse_dice(expression)
    except ValueError as e:
        return {"error": str(e), "expression": expression}

    # Flat number (no dice)
    if num == 0:
        return {
            "expression":       expression,
            "label":            label,
            "num_dice":         0,
            "die_sides":        0,
            "modifier":         mod,
            "individual_rolls": [],
            "subtotal":         0,
            "total":            mod,
            "natural_20":       False,
            "natural_1":        False,
        }

    rolls = [random.randint(1, sides) for _ in range(num)]
    subtotal = sum(rolls)
    total = subtotal + mod

    return {
        "expression":       expression,
        "label":            label,
        "num_dice":         num,
        "die_sides":        sides,
        "modifier":         mod,
        "individual_rolls": rolls,
        "subtotal":         subtotal,
        "total":            total,
        "natural_20":       (num == 1 and sides == 20 and rolls[0] == 20),
        "natural_1":        (num == 1 and sides == 20 and rolls[0] == 1),
    }


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: get_current_scene
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_current_scene() -> dict:
    """
    Return the current scene state: where Theron is, what's happening,
    and the immediate narrative context.

    If a scene has been explicitly set by save_turn, that record is returned.
    Otherwise falls back to: last AI turn summary, Theron's home base
    (Quasquetan), and the most recent 3 turn previews for continuity.

    Call this at the start of every session and after any teleport, travel,
    or major scene transition before writing new narration.
    """
    scene = load_current_scene()

    # Enrich with recent history for context even when a scene record exists
    recent = load_recent_ai_turns(limit=3)
    last_action   = recent[-1]["player_action"]  if recent else ""
    last_response = recent[-1]["dm_response"]    if recent else ""

    if scene:
        scene["last_player_action"]  = last_action
        scene["last_dm_response_preview"] = last_response[:300] if last_response else ""
        return scene

    # No scene record yet — build a default from DB context
    return {
        "scene_set":            False,
        "location":             "Quasquetan (main keep)",
        "region":               "Vesve/Furyondy frontier, World of Greyhawk",
        "calendar_note":        "576 CY — ongoing campaign",
        "note":                 (
            "No explicit scene state has been written yet. "
            "Theron's last recorded session ended mid-investigation. "
            "Resume from the most recent AI turn."
        ),
        "last_player_action":          last_action,
        "last_dm_response_preview":    last_response[:300] if last_response else "",
        "recent_turn_count":           len(recent),
    }


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: save_turn
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def save_turn(
    player_action: Annotated[
        str,
        "The player's action exactly as they typed it.",
    ],
    dm_narrative: Annotated[
        str,
        "Your full DM narrative response for this turn.",
    ],
    scene_location: Annotated[
        str,
        "Current location after this turn, e.g. 'Quasquetan — north wall'.",
    ] = "",
    scene_notes: Annotated[
        str,
        "Any important state changes this turn: HP changes, items gained/lost, "
        "NPCs encountered, combat outcomes. Free text, will be stored as scene notes.",
    ] = "",
    model_name: Annotated[
        str,
        "Model that generated this turn. Default 'claude'.",
    ] = "claude",
) -> dict:
    """
    Persist a completed game turn to the database.

    Call this AFTER delivering your narrative response to the player —
    once per meaningful turn. Do NOT call for meta questions, OOC chat,
    or rule clarifications that aren't part of the fiction.

    Writes to ai_turns (append) and updates current_scene_state (upsert).
    Returns the new turn_id and confirmation.
    """
    structured = {}
    if scene_location:
        structured["location"] = scene_location
    if scene_notes:
        structured["state_changes"] = scene_notes

    turn_id = write_ai_turn(
        player_action=player_action,
        dm_response=dm_narrative,
        model_name=model_name,
        structured_response_json=json.dumps(structured) if structured else None,
    )

    update_current_scene(
        turn_id=turn_id,
        player_action=player_action,
        dm_response=dm_narrative,
        structured_state=structured or None,
    )

    return {
        "saved":        True,
        "turn_id":      turn_id,
        "location":     scene_location or "(unchanged)",
        "notes_stored": bool(scene_notes),
        "world_fact_reminder": (
            "Check this turn for anything that should be written to the database "
            "immediately — do not let it live only in chat history. Call "
            "update_world_fact for: named NPCs encountered or mentioned, items "
            "acquired or lost, decisions made, quests opened or closed, rulings "
            "established, alliances or hostilities formed. Call add_npc for any "
            "new named character. Call add_item for any new item Theron now carries."
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: get_recent_history
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_recent_history(
    n: Annotated[
        int,
        "Number of recent turns to return. Default 5, max 20.",
    ] = 5,
) -> list[dict]:
    """
    Return the last N turns from the ai_turns table in chronological order.

    Each turn includes: turn_id, player_action, dm_response, model_name,
    created_at. Use this to re-establish context when resuming a session,
    resolve continuity questions, or review what was narrated recently.

    Keep N small (5-10) for normal session resumption. Use larger values
    only when the player explicitly asks about earlier events.
    """
    n = max(1, min(n, 20))  # clamp: 1..20
    turns = load_recent_ai_turns(limit=n)
    return turns


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: update_character_status
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def update_character_status(
    hp_current: Annotated[int | None, "New current HP value. Omit to leave unchanged."] = None,
    hp_max:     Annotated[int | None, "New maximum HP value. Omit to leave unchanged."] = None,
    ac:         Annotated[int | None, "New AC value. Omit to leave unchanged."] = None,
    status_notes: Annotated[str | None,
        "Replace the status notes field (gear worn, conditions, etc.). Omit to leave unchanged."
    ] = None,
) -> dict:
    """
    Update Theron's mutable combat status in the database.

    Call this whenever HP changes (combat damage, healing, rest), AC changes
    (armor removed, magical effects), or status conditions change. Only fields
    you provide are written — all others remain untouched.

    Returns the full updated status row as confirmation.
    """
    try:
        result = db_update_character_status(
            hp_current=hp_current, hp_max=hp_max,
            ac=ac, status_notes=status_notes,
        )
        return {"updated": True, **result}
    except ValueError as e:
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: update_treasury
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def update_treasury(
    account_name: Annotated[
        str,
        "Name of the treasury account to update. "
        "Partial match OK: 'Quasquetan' matches 'Quasquetan Treasury'. "
        "Accounts: 'Quasquetan Treasury', 'Valor Tree Cache', 'Main Granary Reserve'.",
    ],
    gp_delta: Annotated[int, "Gold pieces to add (positive) or subtract (negative)."] = 0,
    sp_delta: Annotated[int, "Silver pieces to add or subtract."] = 0,
    cp_delta: Annotated[int, "Copper pieces to add or subtract."] = 0,
    pp_delta: Annotated[int, "Platinum pieces to add or subtract."] = 0,
    gems_delta: Annotated[int, "Gem value in GP to add or subtract."] = 0,
) -> dict:
    """
    Add or subtract coins/gems from a treasury account.

    Use negative deltas to spend money (e.g. gp_delta=-800 for an 800 gp
    construction cost). The tool validates that no denomination goes below zero
    and returns an error instead of allowing overdraft.

    Returns the account's full updated balances as confirmation.
    """
    try:
        result = db_update_treasury(
            account_name, gp_delta=gp_delta, sp_delta=sp_delta,
            cp_delta=cp_delta, pp_delta=pp_delta, gems_delta=gems_delta,
        )
        return {"updated": True, **result}
    except ValueError as e:
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: add_location
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def add_location(
    name: Annotated[str, "Name of the new location."],
    location_type: Annotated[
        str,
        "Type such as 'Keep', 'Tower', 'Farm', 'Mill', 'Village', 'Dungeon', etc.",
    ],
    status: Annotated[
        str,
        "Current status: 'Active', 'Under Construction/Financed', 'Ruined', "
        "'Friendly/Independent', 'Contested', etc.",
    ],
    notes: Annotated[str, "Description and relevant details about this location."] = "",
    parent_location_name: Annotated[
        str,
        "Name of a parent location if this is a sub-location (e.g. a building inside Quasquetan). "
        "Leave blank for top-level locations.",
    ] = "",
) -> dict:
    """
    Add a new location to Theron's realm.

    Use this when a new place is discovered, built, or claimed: a newly
    constructed outpost, a dungeon the party has entered, a village that
    comes under Theron's protection, etc.

    Returns the new location_id and full row as confirmation.
    """
    try:
        result = db_add_location(
            name=name, location_type=location_type, status=status,
            notes=notes, parent_location_name=parent_location_name or None,
        )
        return {"created": True, **result}
    except ValueError as e:
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: update_location_status
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def update_location_status(
    name: Annotated[
        str,
        "Name (or prefix) of the location to update. "
        "E.g. 'Upland Keep', 'Black Mill', 'Crossing Keep'.",
    ],
    new_status: Annotated[
        str,
        "New status value, e.g. 'Active', 'Completed', 'Destroyed', 'Contested', "
        "'Under Siege', 'Abandoned'.",
    ],
    notes: Annotated[
        str,
        "Updated description/notes. Omit (leave blank) to keep existing notes unchanged.",
    ] = "",
) -> dict:
    """
    Change the status of an existing location.

    Call this when construction completes ('Under Construction' -> 'Active'),
    a location is captured or destroyed, or its operational state changes.

    Returns the updated location row as confirmation.
    """
    try:
        result = db_update_location_status(
            name=name, new_status=new_status,
            notes=notes if notes else None,
        )
        return {"updated": True, **result}
    except ValueError as e:
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: update_troop_count
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def update_troop_count(
    group_name: Annotated[
        str,
        "Name (or prefix) of the troop group to update. "
        "E.g. 'Quasquetan Goblins', 'Iron Marsh Hobgoblins', 'Realm Ogres'.",
    ],
    new_count: Annotated[
        int | None,
        "Set the count to this exact number. Use this for hard corrections. "
        "Provide either new_count OR delta, not both.",
    ] = None,
    delta: Annotated[
        int | None,
        "Adjust count by this signed amount (e.g. -3 for 3 casualties, +5 for recruits). "
        "Provide either new_count OR delta, not both.",
    ] = None,
) -> dict:
    """
    Set or adjust the headcount for a troop group.

    Use delta for incremental changes (casualties, desertions, new recruits).
    Use new_count to correct to a known value from hard-copy records.
    Count cannot go below zero.

    Returns the updated troop row as confirmation.
    """
    try:
        result = db_update_troop_count(
            group_name=group_name, new_count=new_count, delta=delta,
        )
        return {"updated": True, **result}
    except ValueError as e:
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: add_troop_group
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def add_troop_group(
    group_name: Annotated[str, "Name for this troop group."],
    troop_type: Annotated[str, "Type, e.g. 'Human Soldiers', 'Goblins', 'Mercenaries', 'Cavalry'."],
    count:      Annotated[int, "Initial headcount."],
    location_name: Annotated[str, "Location where this group is based."],
    notes:      Annotated[str, "Equipment, special abilities, or context notes."] = "",
) -> dict:
    """
    Add a new troop group to the realm.

    Use this when Theron hires mercenaries, recruits new soldiers, gains allied
    forces, or when a new type of unit needs to be tracked separately.

    Returns the new troop_id and full row as confirmation.
    """
    try:
        result = db_add_troop_group(
            group_name=group_name, troop_type=troop_type,
            count=count, location_name=location_name, notes=notes,
        )
        return {"created": True, **result}
    except ValueError as e:
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: add_item
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def add_item(
    name: Annotated[str, "Item name."],
    item_type: Annotated[str, "Category: 'Weapon', 'Armor', 'Ring', 'Potion', 'Scroll', 'Siege Engine', etc."] = "",
    magic_flag: Annotated[bool, "True if this item is magical."] = False,
    value_gp: Annotated[int | None, "Estimated value in gold pieces, or null if unknown."] = None,
    notes: Annotated[str, "Description, enchantment details, provenance, etc."] = "",
    assign_to: Annotated[
        str,
        "Where to put the item: 'character' (Theron's personal inventory), "
        "'location' (stored at a realm location), or 'treasury' (in a treasury account).",
    ] = "character",
    location_name: Annotated[
        str,
        "Required when assign_to='location'. Partial name match.",
    ] = "",
    treasury_name: Annotated[
        str,
        "Required when assign_to='treasury'. Partial name match.",
    ] = "",
    equipped: Annotated[bool, "True if Theron is wearing/wielding this item right now."] = False,
    carry_notes: Annotated[str, "How it's carried: 'Worn', 'In pack', 'Sheathed', etc."] = "",
) -> dict:
    """
    Create a new item and place it in an inventory.

    Use this when Theron acquires new gear, loots a defeated enemy, commissions
    equipment, or when a realm asset (siege engine, stored supplies) needs to
    be tracked.

    Returns item_id, inventory_id, and assignment details as confirmation.
    """
    try:
        result = db_add_item(
            name=name, item_type=item_type, magic_flag=magic_flag,
            value_gp=value_gp, notes=notes, assign_to=assign_to,
            location_name=location_name or None,
            treasury_name=treasury_name or None,
            equipped=equipped, carry_notes=carry_notes,
        )
        return {"created": True, **result}
    except ValueError as e:
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: update_world_fact
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def update_world_fact(
    category: Annotated[
        str,
        "Category namespace for this fact. Use existing categories where possible: "
        "'canon_authority', 'tone', 'realm', 'holding', 'npc', 'forces', 'treasury', "
        "'infrastructure', 'livestock', 'town', 'public_private', 'runtime_dm_behavior'. "
        "Or create a new category for new fact types (e.g. 'weather', 'active_quest', 'treaty').",
    ],
    fact_text: Annotated[str, "The fact to record. Be specific and complete."],
    source_note: Annotated[
        str,
        "Where this fact comes from: 'Player confirmed', 'Session 2026-04-16', "
        "'Hard-copy override', etc.",
    ] = "",
    overwrite_category: Annotated[
        bool,
        "If True, delete all existing facts in this category before inserting. "
        "Use for singleton facts like current weather, active treaty terms, or "
        "the current status of a single ongoing quest. "
        "If False (default), append alongside existing facts in the category.",
    ] = False,
) -> dict:
    """
    Record or update a campaign world fact.

    Use this for anything that doesn't fit the structured tables: current
    weather conditions, active quest objectives, diplomatic agreements,
    rulings on ambiguous rules, DM behavioral notes, player-confirmed
    corrections to canon. These facts are readable by get_realm_state
    and inform future narration.

    Returns the new fact_id and full row as confirmation.
    """
    try:
        result = db_update_world_fact(
            category=category, fact_text=fact_text,
            source_note=source_note, overwrite_category=overwrite_category,
        )
        return {"saved": True, **result}
    except Exception as e:
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: get_pending_updates
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_pending_updates(
    limit: Annotated[
        int,
        "Maximum number of turns to check. Default 30.",
    ] = 30,
) -> list[dict]:
    """
    Return recent turns where save_turn was called with scene_notes describing
    state changes that may not yet be committed to the database.

    Use this at the start of a session or after a long sequence of turns to
    audit what changed and whether the write tools (update_treasury,
    update_troop_count, update_character_status, etc.) need to be called to
    bring the DB in sync with what was narrated.

    Each result includes: turn_id, created_at, player_action summary,
    state_changes text, and location at time of turn.
    """
    limit = max(1, min(limit, 100))
    return db_get_pending_updates(limit=limit)


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: update_npc
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def update_npc(
    name: Annotated[
        str,
        "Name (or prefix) of the NPC to update. E.g. 'Ruk', 'Pell', 'Red Rider', 'Elowen'. "
        "Partial match OK.",
    ],
    notes: Annotated[
        str | None,
        "Replace the NPC's notes field. Use this to record status changes: "
        "wounded, imprisoned, freed, promoted, turned hostile, deceased, etc. "
        "Omit to leave unchanged.",
    ] = None,
    character_type: Annotated[
        str | None,
        "Update the character type. Common values: 'NPC', 'Deceased', 'Prisoner', "
        "'Ally', 'Hostile', 'Construct'. Omit to leave unchanged.",
    ] = None,
    race: Annotated[
        str | None,
        "Update race if it was unknown or needs correction. Omit to leave unchanged.",
    ] = None,
    alignment: Annotated[
        str | None,
        "Update alignment (e.g. 'Neutral Good', 'Chaotic Evil'). Omit to leave unchanged.",
    ] = None,
) -> dict:
    """
    Update an NPC's record in the database.

    Call this when an NPC's status changes during play: Ruk takes casualties,
    Red Rider's prisoner status changes, a new NPC reveals their alignment,
    an ally turns hostile, or someone dies. The notes field is the primary
    place to record narrative state.

    Returns the full updated character row as confirmation.
    """
    try:
        result = db_update_npc(
            name=name, notes=notes, character_type=character_type,
            race=race, alignment=alignment,
        )
        return {"updated": True, **result}
    except ValueError as e:
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: add_npc
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def add_npc(
    name: Annotated[str, "Full name of the new NPC."],
    race: Annotated[str, "Race (Human, Elf, Goblin, etc.). Leave blank if unknown."] = "",
    character_type: Annotated[
        str,
        "Type: 'NPC', 'Prisoner', 'Ally', 'Hostile', 'Construct', 'Unknown'.",
    ] = "NPC",
    notes: Annotated[
        str,
        "Description, backstory, role in the campaign, known abilities or items.",
    ] = "",
    relationship_to_theron: Annotated[
        str,
        "If this NPC has a relationship with Theron, describe it here: "
        "'Hired Soldier', 'Enemy', 'Quest Giver', 'Merchant', 'Prisoner', etc. "
        "Leave blank if no relationship entry is needed.",
    ] = "",
    relationship_notes: Annotated[
        str,
        "Additional context for the relationship (circumstances of meeting, etc.).",
    ] = "",
) -> dict:
    """
    Add a newly encountered or newly relevant NPC to the database.

    Call this when Theron meets someone worth tracking: a merchant he may
    return to, an enemy commander whose name was learned, a quest giver,
    a new hireling, or a prisoner taken during play.

    Returns the new character_id and full row. If relationship_to_theron
    is provided, a relationship record is also created.
    """
    try:
        result = db_add_npc(
            name=name, race=race, character_type=character_type,
            notes=notes, relationship_to_theron=relationship_to_theron,
            relationship_notes=relationship_notes,
        )
        return {"created": True, **result}
    except Exception as e:
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: create_character
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def create_character(
    name: Annotated[
        str,
        "The character's full name, e.g. 'Aldric Vane'.",
    ],
    race: Annotated[
        str,
        "Race exactly as spelled: Human, Elf, Half-Elf, Dwarf, Halfling, Half-Orc.",
    ],
    character_class: Annotated[
        str,
        "Class exactly as spelled: Fighter, Cleric, Magic-User, Thief.",
    ],
    str_score: Annotated[int, "Confirmed Strength score (3-18)."],
    int_score: Annotated[int, "Confirmed Intelligence score (3-18)."],
    wis_score: Annotated[int, "Confirmed Wisdom score (3-18)."],
    dex_score: Annotated[int, "Confirmed Dexterity score (3-18)."],
    con_score: Annotated[int, "Confirmed Constitution score (3-18)."],
    cha_score: Annotated[int, "Confirmed Charisma score (3-18)."],
    alignment: Annotated[
        str,
        "Character alignment, e.g. 'Lawful Good', 'True Neutral', 'Chaotic Evil'. "
        "Leave blank if not yet decided.",
    ] = "",
    starting_gold: Annotated[
        int,
        "Starting gold in GP if already rolled in chat (e.g. from roll_dice). "
        "Pass 0 to auto-roll based on class (Fighter/Cleric 3d6x10, MU 2d4x10, Thief 2d6x10).",
    ] = 0,
) -> dict:
    """
    Finalise a confirmed character and create their campaign database.

    Call this ONLY after the player has confirmed their name, race, class,
    and ability scores in chat. The rolling and deliberation happens through
    conversation and roll_dice -- this tool is the commit step.

    This is an AD&D 1e / OSRIC campaign. Ability scores should be rolled
    in chat using one of the two supported methods before calling this tool:
      - 5d6 keep best 3  (recommended): roll roll_dice("5d6") six times,
        keep the three highest dice from each roll.
      - 4d6 drop lowest  (classic alternative): roll roll_dice("4d6") six
        times, drop the lowest die from each roll.

    What happens:
      1. Racial ability modifiers are applied to the provided scores.
      2. HP is rolled (HD + CON modifier, minimum 1).
      3. AC, THAC0, and all five saving throws are calculated from class tables.
      4. A fresh saves/<name>.db is created with the full campaign schema.
      5. The character is written to that database.
      6. config.json is updated so this becomes the active campaign.

    Returns the complete character sheet — narrate it to the player as
    confirmation, then tell them to restart Claude Desktop to activate the
    new database.

    Raises an error (does not write anything) if:
      - The race or class name is unrecognised.
      - The racial class restriction is violated.
      - A save file with that name already exists.
    """
    try:
        result = db_create_character_db(
            name=name,
            race=race,
            character_class=character_class,
            str_score=str_score,
            int_score=int_score,
            wis_score=wis_score,
            dex_score=dex_score,
            con_score=con_score,
            cha_score=cha_score,
            alignment=alignment,
            starting_gold=starting_gold,
        )
        return result
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Unexpected error: {e}"}


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: session_start
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def session_start() -> dict:
    """
    Call this FIRST at the start of every session, before any narration.

    Returns a single consolidated briefing containing everything needed to
    resume the campaign accurately:

      character       -- Full PC stats, HP, AC, ability scores, inventory
      scene           -- Current location and last turn context
      recent_history  -- Last 10 turns in chronological order
      pending_updates -- Turns whose state_changes have not yet been committed
                         to the database (HP adjustments, treasure, etc.)
      briefing_notes  -- Session startup checklist for the DM

    After receiving this briefing you should:
      1. Resolve any pending_updates before starting — call the appropriate
         write tools (update_character_status, update_treasury, etc.) for
         each uncommitted change listed there.
      2. Orient the player: tell them where they are, what just happened, and
         what immediate situation they face.
      3. Never invent a state that contradicts what this briefing contains.
    """
    # ── Character (same flattening as get_character_state) ────────────────────
    char = load_character() or {}
    status    = char.pop("status", {}) or {}
    abilities = char.pop("abilities", {}) or {}
    char.update({
        "hp_current":        status.get("hp_current"),
        "hp_max":            status.get("hp_max"),
        "ac":                status.get("ac"),
        "movement":          status.get("movement"),
        "attacks_per_round": status.get("attacks_per_round"),
        "status_notes":      status.get("status_notes"),
        "str": abilities.get("strength"),
        "int": abilities.get("intelligence"),
        "wis": abilities.get("wisdom"),
        "dex": abilities.get("dexterity"),
        "con": abilities.get("constitution"),
        "cha": abilities.get("charisma"),
    })

    # ── Scene ─────────────────────────────────────────────────────────────────
    scene   = load_current_scene() or {}
    history = load_recent_ai_turns(limit=10)

    if history:
        scene["last_player_action"]       = history[-1]["player_action"]
        scene["last_dm_response_preview"] = (history[-1]["dm_response"] or "")[:300]

    # ── Pending updates ───────────────────────────────────────────────────────
    pending = db_get_pending_updates(limit=30)

    return {
        "character":       char if char else {"error": "Character not found in database."},
        "scene":           scene,
        "recent_history":  history,
        "pending_updates": pending,
        "briefing_notes": (
            "SESSION STARTUP CHECKLIST — complete before narrating: "
            "(1) If pending_updates is non-empty, commit each unresolved state "
            "change now using the appropriate write tools. "
            "(2) Orient the player from the scene and recent_history context. "
            "(3) After each turn, call save_turn then act on its "
            "world_fact_reminder — write every named NPC, item, decision, and "
            "quest development to the database immediately. Nothing important "
            "should exist only in chat history."
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    mcp.run()
