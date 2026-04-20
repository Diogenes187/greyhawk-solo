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

  COMBAT
  start_combat         -- Initialize encounter, roll initiative, build turn order
  get_combat_state     -- Current HP, initiative order, round number
  attack               -- Resolve one attack: roll to-hit, damage, HP update, morale
  end_combat           -- Close encounter, award XP, clear combat state

  SPELLS
  get_spell_slots      -- Memorized spells and remaining slots for today
  memorize_spells      -- Set today's memorized spell list
  cast_spell           -- Expend a memorized slot, return spell description
  rest                 -- Long rest: restore spell slots, recover HP, advance calendar

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
    # Phase 2 — combat
    get_active_combat, set_active_combat, clear_active_combat,
    lookup_monster, get_attack_target_roll,
    _xp_for_hd, _roll_monster_hp, _roll_damage,
    _CLASS_MATRIX_PRIORITY,
    # Phase 2 — spells
    get_spell_memory, set_spell_memory, lookup_spell, get_spells_for_class,
    # Phase 3 — dungeon
    get_random_dungeon_encounter, roll_treasure_by_type,
    get_dungeon_turn_count, increment_dungeon_turn,
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
# PHASE 2A — COMBAT TRACKER
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def start_combat(
    encounter_name: Annotated[
        str,
        "Brief name for this encounter, e.g. 'Goblin ambush at the ford'.",
    ],
    enemies: Annotated[
        str,
        "JSON list of enemy groups. Each entry needs 'name' (monster name) and "
        "'count' (number of that monster). Optional: 'hp_override' (int, sets HP "
        "instead of rolling), 'ac_override' (int). "
        "Example: '[{\"name\": \"Goblin\", \"count\": 3}, "
        "{\"name\": \"Hobgoblin\", \"count\": 1}]'",
    ],
    location: Annotated[
        str,
        "Current location for the encounter log.",
    ] = "",
) -> dict:
    """
    Initialise a new combat encounter.

    Looks up each enemy type in the monsters table, rolls HP for every
    individual, rolls initiative for all sides (d10 per combatant, DEX
    modifier applied to PC), builds the full initiative order, and stores
    the complete combat state in world_facts so every subsequent tool call
    can read and update it.

    Returns the full initiative order with opening HP and AC for every
    combatant. Narrate the scene, then begin calling attack() in initiative
    order each round.

    Note: if a monster name is not found in the database, a generic stat
    block is generated (AC 10, 1 HD, 1-6 damage, 1 attack).
    """
    # ── Parse enemies ─────────────────────────────────────────────────────────
    try:
        enemy_groups = json.loads(enemies)
        if not isinstance(enemy_groups, list):
            return {"error": "enemies must be a JSON list."}
    except json.JSONDecodeError as e:
        return {"error": f"Invalid enemies JSON: {e}"}

    # ── Load PC state ──────────────────────────────────────────────────────────
    char    = load_character()
    status  = char.get("status", {})
    classes = char.get("classes", [])

    pc_hp_cur = status.get("hp_current", 1)
    pc_hp_max = status.get("hp_max", 1)
    pc_ac     = status.get("ac", 10)

    # DEX initiative modifier (-3 to +3 mapped from AC modifier range)
    abilities  = char.get("abilities", {})
    dex        = abilities.get("dexterity") or 10
    dex_init   = {3: -3, 4: -2, 5: -2, 6: -1, 7: -1, 8: -1,
                  15: 1, 16: 1, 17: 2, 18: 3}.get(dex, 0)

    pc_init = random.randint(1, 10) + dex_init

    combatants: dict = {
        "PC": {
            "name":       char.get("name", "PC"),
            "side":       "party",
            "initiative": pc_init,
            "hp_current": pc_hp_cur,
            "hp_max":     pc_hp_max,
            "ac":         pc_ac,
            "classes":    classes,
            "is_pc":      True,
            "status":     "active",
        }
    }

    groups: dict = {}

    # ── Build enemy combatants ─────────────────────────────────────────────────
    for grp in enemy_groups:
        mname = grp.get("name", "Unknown")
        count = max(1, int(grp.get("count", 1)))
        mdata = lookup_monster(mname)

        hd_text   = mdata.get("hit_dice", "1") if mdata else "1"
        raw_ac    = mdata.get("armor_class", "10") if mdata else "10"
        damage    = mdata.get("damage", "1-6") if mdata else "1-6"
        num_atk   = mdata.get("number_of_attacks", "1") if mdata else "1"
        disp_name = mdata.get("name", mname) if mdata else mname

        try:
            base_ac = int(str(raw_ac).strip().split("/")[0])
        except ValueError:
            base_ac = 10

        try:
            n_attacks = int(str(num_atk).strip().split("/")[0])
        except ValueError:
            n_attacks = 1

        group_key = disp_name
        groups[group_key] = {
            "initial_count": count,
            "current_count":  count,
            "morale_broken":  False,
        }

        for i in range(1, count + 1):
            cid = f"{disp_name}_{i}"
            hp_roll, eff_hd = _roll_monster_hp(hd_text)
            hp  = int(grp.get("hp_override", hp_roll))
            ac  = int(grp.get("ac_override", base_ac))
            xp  = _xp_for_hd(eff_hd)

            combatants[cid] = {
                "name":        f"{disp_name} {i}",
                "side":        "enemy",
                "initiative":  random.randint(1, 10),
                "hp_current":  hp,
                "hp_max":      hp,
                "ac":          ac,
                "hd_text":     hd_text,
                "effective_hd": eff_hd,
                "damage_text": damage,
                "num_attacks": n_attacks,
                "xp":          xp,
                "is_pc":       False,
                "status":      "active",
                "group":       group_key,
            }

    # ── Sort initiative order (highest first, PC wins ties) ───────────────────
    order = sorted(
        combatants.keys(),
        key=lambda cid: (combatants[cid]["initiative"], 1 if cid == "PC" else 0),
        reverse=True,
    )

    state = {
        "encounter_name": encounter_name,
        "location":       location,
        "round":          1,
        "status":         "active",
        "combatants":     combatants,
        "initiative_order": order,
        "current_actor_index": 0,
        "groups":         groups,
        "combat_log":     [
            f"Round 1 begins. Initiative: "
            + ", ".join(
                f"{combatants[cid]['name']}({combatants[cid]['initiative']})"
                for cid in order
            )
        ],
    }

    set_active_combat(state)

    # ── Return summary ─────────────────────────────────────────────────────────
    return {
        "encounter_name":    encounter_name,
        "round":             1,
        "initiative_order": [
            {
                "id":         cid,
                "name":       combatants[cid]["name"],
                "side":       combatants[cid]["side"],
                "initiative": combatants[cid]["initiative"],
                "hp":         combatants[cid]["hp_current"],
                "ac":         combatants[cid]["ac"],
            }
            for cid in order
        ],
        "note": (
            "Combat state saved. Call attack(attacker_id, target_id, ...) "
            "in initiative order. Call get_combat_state() to review current HP "
            "at any time. Call end_combat() when the encounter concludes."
        ),
    }


@mcp.tool()
def get_combat_state() -> dict:
    """
    Return the full current combat state.

    Includes initiative order, current HP and status for every combatant,
    round number, and the last 10 entries of the combat log.

    Call this at the start of any round to verify current state before
    narrating. If no combat is active, returns an informational message.
    """
    state = get_active_combat()
    if not state:
        return {
            "active": False,
            "message": "No combat is currently active. Call start_combat() to begin an encounter.",
        }

    combatants = state.get("combatants", {})
    order      = state.get("initiative_order", [])

    return {
        "active":          True,
        "encounter_name":  state.get("encounter_name", ""),
        "location":        state.get("location", ""),
        "round":           state.get("round", 1),
        "current_actor":   order[state.get("current_actor_index", 0)] if order else "",
        "initiative_order": [
            {
                "id":         cid,
                "name":       combatants[cid]["name"],
                "side":       combatants[cid]["side"],
                "initiative": combatants[cid]["initiative"],
                "hp_current": combatants[cid]["hp_current"],
                "hp_max":     combatants[cid]["hp_max"],
                "ac":         combatants[cid]["ac"],
                "status":     combatants[cid]["status"],
            }
            for cid in order
            if cid in combatants
        ],
        "combat_log": state.get("combat_log", [])[-10:],
    }


@mcp.tool()
def attack(
    attacker_id: Annotated[
        str,
        "ID of the attacker. Use 'PC' for the player character, or the "
        "enemy ID exactly as shown in get_combat_state (e.g. 'Goblin_1').",
    ],
    target_id: Annotated[
        str,
        "ID of the target. 'PC' or an enemy ID.",
    ],
    weapon: Annotated[
        str,
        "Weapon or attack name for the log (e.g. 'longsword', 'claw', 'bite').",
    ] = "",
    damage_dice: Annotated[
        str,
        "Damage expression for PC attacks — e.g. '1d8', '1d6+2', '2d4'. "
        "Ignored when the attacker is a monster (uses monster damage from DB).",
    ] = "1d6",
    attack_bonus: Annotated[
        int,
        "Bonus added to the d20 attack roll (magic weapon, spell, position).",
    ] = 0,
    damage_bonus: Annotated[
        int,
        "Bonus added to damage (STR bonus, magic weapon, spell).",
    ] = 0,
) -> dict:
    """
    Resolve one attack roll for one attacker against one target.

    For PC attackers:
      - Finds the best available attack matrix (Fighter > Thief > Cleric > MU).
      - Looks up the target roll for the target's AC from combat_attack_matrix_entries.
      - Rolls 1d20 + attack_bonus vs that target roll.
      - On a hit, rolls damage_dice + damage_bonus.

    For monster attackers:
      - Uses the fighter_matrix at the monster's effective HD as level.
      - Rolls damage from the monster's damage_text in the combat state.
      - Monsters with multiple attacks make one roll per attack.

    Updates HP in the combat state. If a combatant reaches 0 HP it is marked
    dead and removed from the initiative order for subsequent rounds. Checks
    monster group morale when casualties exceed 50% of the group's initial count.

    Returns the full attack resolution: roll, whether it hit, damage dealt,
    remaining HP, and any morale/death results.
    """
    state = get_active_combat()
    if not state:
        return {"error": "No active combat. Call start_combat() first."}

    combatants = state["combatants"]
    if attacker_id not in combatants:
        return {"error": f"Attacker '{attacker_id}' not found in combat. "
                         f"Valid IDs: {list(combatants.keys())}"}
    if target_id not in combatants:
        return {"error": f"Target '{target_id}' not found in combat. "
                         f"Valid IDs: {list(combatants.keys())}"}

    attacker = combatants[attacker_id]
    target   = combatants[target_id]

    if attacker["status"] != "active":
        return {"error": f"{attacker['name']} is {attacker['status']} and cannot attack."}
    if target["status"] != "active":
        return {"error": f"{target['name']} is already {target['status']}."}

    target_ac = target["ac"]
    result    = {"attacker": attacker["name"], "target": target["name"],
                 "weapon": weapon or "attack"}

    # ── Determine attack rolls ─────────────────────────────────────────────────
    attacks_made = []

    if attacker["is_pc"]:
        # Best available matrix
        classes_lower = {c["class_name"].lower(): c["level"]
                         for c in attacker.get("classes", [])}
        matrix_code = "magic_user_matrix"
        matrix_level = 1
        for cls_key, mat in _CLASS_MATRIX_PRIORITY:
            if cls_key in classes_lower:
                matrix_code  = mat
                matrix_level = classes_lower[cls_key]
                break

        target_roll = get_attack_target_roll(matrix_code, matrix_level, target_ac)
        d20         = random.randint(1, 20)
        total_roll  = d20 + attack_bonus
        hit         = total_roll >= target_roll or d20 == 20
        critical    = d20 == 20
        fumble      = d20 == 1 and not hit

        # Parse and roll PC damage
        try:
            m = re.match(r"^(\d+)[dD](\d+)([+-]\d+)?$", damage_dice.strip())
            if m:
                n, s   = int(m.group(1)), int(m.group(2))
                d_mod  = int(m.group(3)) if m.group(3) else 0
                dmg_roll = sum(random.randint(1, s) for _ in range(n)) + d_mod
            else:
                # Try "A-B" notation
                m2 = re.match(r"^(\d+)-(\d+)$", damage_dice.strip())
                dmg_roll = random.randint(int(m2.group(1)), int(m2.group(2))) if m2 else random.randint(1, 6)
        except Exception:
            dmg_roll = random.randint(1, 6)

        damage_dealt = max(1, dmg_roll + damage_bonus) if hit else 0
        attacks_made.append({
            "d20": d20, "attack_bonus": attack_bonus,
            "total": total_roll, "target_roll": target_roll,
            "hit": hit, "critical": critical, "fumble": fumble,
            "damage_roll": dmg_roll, "damage_bonus": damage_bonus,
            "damage_dealt": damage_dealt,
        })
        total_damage = damage_dealt

    else:
        # Monster attacking: use fighter_matrix at effective HD
        eff_hd       = attacker.get("effective_hd", 1.0)
        matrix_level = max(1, int(eff_hd))
        n_attacks    = attacker.get("num_attacks", 1)
        dmg_text     = attacker.get("damage_text", "1-6")
        total_damage = 0

        for _ in range(n_attacks):
            target_roll = get_attack_target_roll("fighter_matrix", matrix_level, target_ac)
            d20         = random.randint(1, 20)
            total_roll  = d20 + attack_bonus
            hit         = total_roll >= target_roll or d20 == 20
            critical    = d20 == 20
            fumble      = d20 == 1 and not hit
            # One damage value per attack (take the first damage expression for multi-attack)
            dmg_parts = dmg_text.split("/")
            this_dmg_text = dmg_parts[min(len(attacks_made), len(dmg_parts) - 1)]
            rolls_list = _roll_damage(this_dmg_text)
            dmg_roll = rolls_list[0] if rolls_list else 1
            damage_dealt = max(1, dmg_roll + damage_bonus) if hit else 0
            total_damage += damage_dealt
            attacks_made.append({
                "d20": d20, "attack_bonus": attack_bonus,
                "total": total_roll, "target_roll": target_roll,
                "hit": hit, "critical": critical, "fumble": fumble,
                "damage_roll": dmg_roll, "damage_bonus": damage_bonus,
                "damage_dealt": damage_dealt,
            })

    # ── Apply damage ───────────────────────────────────────────────────────────
    new_hp   = target["hp_current"] - total_damage
    dead     = new_hp <= 0
    new_hp   = max(0, new_hp)
    combatants[target_id]["hp_current"] = new_hp
    result["total_damage"] = total_damage
    result["target_hp_remaining"] = new_hp
    result["attacks"] = attacks_made

    if dead:
        combatants[target_id]["status"] = "dead"
        result["target_status"] = "dead"
        # Remove from initiative order for next round
        state["initiative_order"] = [
            cid for cid in state["initiative_order"] if cid != target_id
        ]
    else:
        result["target_status"] = "active"

    # ── Morale check for monster groups ───────────────────────────────────────
    morale_result = None
    if dead and not target.get("is_pc", False):
        grp_key = target.get("group")
        if grp_key and grp_key in state.get("groups", {}):
            grp = state["groups"][grp_key]
            grp["current_count"] -= 1
            if (not grp["morale_broken"] and
                    grp["current_count"] <= grp["initial_count"] // 2):
                # 50% casualties: morale check (2d6 >= 8 = holds, < 8 = flees)
                morale_roll = random.randint(1, 6) + random.randint(1, 6)
                if morale_roll < 8:
                    grp["morale_broken"] = True
                    # Mark all living members of this group as fled
                    for cid, cbt in combatants.items():
                        if (cbt.get("group") == grp_key and
                                cbt["status"] == "active"):
                            cbt["status"] = "fled"
                    state["initiative_order"] = [
                        cid for cid in state["initiative_order"]
                        if combatants.get(cid, {}).get("group") != grp_key
                    ]
                    morale_result = {
                        "roll": morale_roll,
                        "result": "FLED",
                        "message": f"Morale check failed (rolled {morale_roll}, needed 8+). "
                                   f"All remaining {grp_key}s flee the battle!",
                    }
                else:
                    morale_result = {
                        "roll": morale_roll,
                        "result": "holds",
                        "message": f"Morale check passed (rolled {morale_roll}). {grp_key}s hold their ground.",
                    }

    if morale_result:
        result["morale_check"] = morale_result

    # ── Advance initiative index ───────────────────────────────────────────────
    order = state["initiative_order"]
    idx   = state["current_actor_index"]
    if order:
        idx = (idx + 1) % len(order)
        if idx == 0:
            state["round"] += 1
            state["combat_log"].append(f"Round {state['round']} begins.")
    state["current_actor_index"] = idx

    # ── Combat log entry ───────────────────────────────────────────────────────
    weapon_str = f" with {weapon}" if weapon else ""
    hit_str    = f"HIT for {total_damage} damage" if total_damage > 0 else "MISS"
    state["combat_log"].append(
        f"R{state['round']}: {attacker['name']} attacks {target['name']}{weapon_str} — "
        f"{hit_str}. {target['name']} HP: {new_hp}/{combatants[target_id]['hp_max']}."
        + (f" {morale_result['message']}" if morale_result else "")
        + (" DEAD." if dead else "")
    )

    # ── Check if combat should auto-end ───────────────────────────────────────
    living_enemies = [c for c in combatants.values()
                      if not c["is_pc"] and c["status"] == "active"]
    if not living_enemies:
        result["combat_over"] = True
        result["combat_over_message"] = (
            "All enemies are dead or fled. Call end_combat(result='victory') "
            "to award XP and close the encounter."
        )

    state["combatants"] = combatants
    set_active_combat(state)

    return result


@mcp.tool()
def end_combat(
    result: Annotated[
        str,
        "Combat outcome: 'victory', 'retreat', 'tpk', 'surrendered', 'fled'.",
    ],
    xp_override: Annotated[
        int,
        "XP to award. Pass 0 to auto-calculate from the XP values of all "
        "dead/fled enemies in the combat state.",
    ] = 0,
    notes: Annotated[
        str,
        "Any narrative notes about how the combat ended.",
    ] = "",
) -> dict:
    """
    Close the current combat encounter.

    Calculates XP earned from defeated enemies (or uses xp_override).
    For multi-class characters, XP is divided equally among classes.
    Updates class_levels XP totals in the database.
    Writes a combat summary to world_facts (category 'combat_history').
    Clears the active_combat state.

    Returns a full combat summary including enemies faced, rounds fought,
    XP awarded per class, and updated class XP totals.
    """
    state = get_active_combat()
    if not state:
        return {"error": "No active combat to end."}

    combatants = state.get("combatants", {})
    groups     = state.get("groups", {})

    # ── XP calculation ────────────────────────────────────────────────────────
    if xp_override > 0:
        total_xp = xp_override
        xp_source = "manual override"
    else:
        total_xp = sum(
            c["xp"] for c in combatants.values()
            if not c.get("is_pc", False) and c["status"] in ("dead", "fled")
        )
        xp_source = "auto-calculated from defeated enemies"

    # ── Award XP to character classes ─────────────────────────────────────────
    xp_updates = []
    if result in ("victory", "surrendered") and total_xp > 0:
        from engine.db import _get_conn as _ec, _PC_CHARACTER_ID as _pc_id
        with _ec(read_only=True) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT class_level_id, class_name, level, xp "
                "FROM class_levels WHERE character_id = ?",
                (_pc_id,),
            )
            class_rows = [dict(r) for r in cur.fetchall()]

        share = total_xp // max(1, len(class_rows))  # equal split for multi-class
        with _ec() as conn:
            for cls in class_rows:
                new_xp = cls["xp"] + share
                conn.execute(
                    "UPDATE class_levels SET xp = ? "
                    "WHERE class_level_id = ?",
                    (new_xp, cls["class_level_id"]),
                )
                xp_updates.append({
                    "class_name": cls["class_name"],
                    "level":      cls["level"],
                    "xp_before":  cls["xp"],
                    "xp_gained":  share,
                    "xp_after":   new_xp,
                })

    # ── Write combat summary to world_facts ───────────────────────────────────
    enemy_summary = [
        f"{grp}: {g['initial_count'] - g['current_count']}/{g['initial_count']} killed"
        for grp, g in groups.items()
    ]
    summary_text = (
        f"Combat: {state.get('encounter_name', 'Unknown')} | "
        f"Result: {result} | Rounds: {state.get('round', 1)} | "
        f"XP: {total_xp} | Enemies: {'; '.join(enemy_summary) or 'none'}"
        + (f" | Notes: {notes}" if notes else "")
    )
    from engine.db import _get_conn as _ec2, _CAMPAIGN_ID as _cid
    with _ec2() as conn:
        conn.execute(
            "INSERT INTO world_facts "
            "(campaign_id, category, fact_text, source_note) "
            "VALUES (?, 'combat_history', ?, 'combat_tracker')",
            (_cid, summary_text),
        )

    clear_active_combat()

    return {
        "encounter_name":  state.get("encounter_name", ""),
        "result":          result,
        "rounds_fought":   state.get("round", 1),
        "total_xp_earned": total_xp,
        "xp_source":       xp_source,
        "xp_per_class":    xp_updates,
        "enemy_summary":   enemy_summary,
        "combat_log":      state.get("combat_log", []),
        "notes":           notes,
    }


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 2B — SPELL SYSTEM
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_spell_slots() -> dict:
    """
    Return the character's memorized spells and slot availability for today.

    Reads the character's current class levels, computes total spell slots
    from classes.json (AD&D 1e tables), then cross-references with the
    spell_memory world_fact to show which slots are expended.

    Returns:
      slots_total   -- slots available per class per spell level at current level
      memorized     -- full list of memorized spells with expended status
      available     -- only the non-expended memorized spells (ready to cast)
      expended      -- spells already cast this day
      has_unmemorized_slots -- True if any available slot has no spell assigned

    Call memorize_spells() after a rest to set today's spell list.
    """
    import json as _json
    from pathlib import Path as _Path

    # Load classes.json for spell slot tables
    classes_data_path = _ROOT / "data" / "classes.json"
    with open(classes_data_path, encoding="utf-8") as f:
        classes_data = _json.load(f)

    # Load PC class levels
    from engine.db import _get_conn as _ec, _PC_CHARACTER_ID as _pc_id
    with _ec(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT class_name, level FROM class_levels WHERE character_id = ?",
            (_pc_id,),
        )
        class_levels = {r["class_name"]: r["level"] for r in cur.fetchall()}

    # Compute total slots
    slots_total: dict = {}
    for cls_name, lvl in class_levels.items():
        cls_data = classes_data.get(cls_name, {})
        slot_table = cls_data.get("spell_slots", {})
        if not slot_table:
            continue  # Fighters etc. have no spell slots
        lvl_key = str(lvl)
        if lvl_key not in slot_table:
            # Find closest level <= current
            avail = [int(k) for k in slot_table if int(k) <= lvl]
            if not avail:
                continue
            lvl_key = str(max(avail))
        row = slot_table[lvl_key]  # list of 9 values
        slots_total[cls_name] = {
            f"level_{i + 1}": row[i]
            for i in range(len(row))
            if row[i] > 0
        }

    # Load spell memory
    memory = get_spell_memory()
    memorized = memory.get("memorized", [])

    available = [s for s in memorized if not s.get("expended", False)]
    expended  = [s for s in memorized if s.get("expended", False)]

    # Compute unmemorized slots
    memorized_by_class: dict = {}
    for slot in memorized:
        key = (slot.get("class_name", ""), slot.get("spell_level", 1))
        memorized_by_class[key] = memorized_by_class.get(key, 0) + 1

    has_unmemorized = False
    for cls, levels in slots_total.items():
        cls_norm = cls.lower().replace("-", "_").replace(" ", "_")
        for lvl_key, count in levels.items():
            sp_lvl = int(lvl_key.split("_")[1])
            used = sum(
                v for (cn, sl), v in memorized_by_class.items()
                if cn.lower().replace("-", "_").replace(" ", "_") == cls_norm
                and sl == sp_lvl
            )
            if used < count:
                has_unmemorized = True

    return {
        "slots_total":           slots_total,
        "memorized":             memorized,
        "available":             available,
        "expended":              expended,
        "last_rest":             memory.get("last_rest"),
        "has_unmemorized_slots": has_unmemorized,
        "note": (
            "Call memorize_spells() after a rest to set today's spell list. "
            "Call cast_spell() to expend a memorized slot."
        ) if has_unmemorized else None,
    }


@mcp.tool()
def memorize_spells(
    spells: Annotated[
        str,
        "JSON list of spell names to memorize today. Repeat the same name "
        "to memorize it multiple times. "
        "Example: '[\"Magic Missile\", \"Magic Missile\", \"Sleep\", \"Fireball\"]'",
    ],
    class_name: Annotated[
        str,
        "Class these spells belong to — 'magic_user', 'cleric', 'illusionist', "
        "'druid'. Required if the character has multiple spellcasting classes.",
    ] = "",
) -> dict:
    """
    Set today's memorized spell list for one spellcasting class.

    Looks up each spell name in the database to confirm it exists and
    retrieve its level. Validates that the memorized list does not exceed
    the slots available at the character's current level (from classes.json).

    Replaces any existing memorization for this class. Does not affect
    memorized spells from other classes.

    Returns the full memorized list with spell details, and remaining
    available slots per level after memorization.
    """
    import json as _json

    # Parse spell list
    try:
        spell_names = _json.loads(spells)
        if not isinstance(spell_names, list):
            return {"error": "spells must be a JSON list of strings."}
    except _json.JSONDecodeError as e:
        return {"error": f"Invalid spells JSON: {e}"}

    # Resolve class name
    cls_raw  = class_name.lower().replace("-", "_").replace(" ", "_") if class_name else ""

    # Load class data for slot validation
    classes_data_path = _ROOT / "data" / "classes.json"
    with open(classes_data_path, encoding="utf-8") as f:
        classes_data = _json.load(f)

    from engine.db import _get_conn as _ec, _PC_CHARACTER_ID as _pc_id
    with _ec(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT class_name, level FROM class_levels WHERE character_id = ?",
            (_pc_id,),
        )
        class_levels = {r["class_name"]: r["level"] for r in cur.fetchall()}

    # Find the correct class key in classes.json
    target_cls_key = None
    for cls_key in class_levels:
        normalized = cls_key.lower().replace("-", "_").replace(" ", "_")
        if not cls_raw or normalized == cls_raw or cls_raw in normalized:
            cls_data = classes_data.get(cls_key, {})
            if cls_data.get("spell_slots"):
                target_cls_key = cls_key
                break

    if not target_cls_key:
        # Try spellcasting class by name in DB
        for cls_key, lvl in class_levels.items():
            cls_data = classes_data.get(cls_key, {})
            if cls_data.get("spell_slots"):
                target_cls_key = cls_key
                break

    if not target_cls_key:
        return {"error": "No spellcasting class found for this character."}

    cls_level = class_levels[target_cls_key]
    slot_table = classes_data[target_cls_key]["spell_slots"]
    lvl_key = str(cls_level)
    if lvl_key not in slot_table:
        avail = [int(k) for k in slot_table if int(k) <= cls_level]
        lvl_key = str(max(avail)) if avail else "1"
    max_slots = slot_table[lvl_key]  # list indexed by spell_level-1

    # DB class_name stored as e.g. 'magic_user'
    db_class_name = target_cls_key.lower().replace("-", "_").replace(" ", "_")

    # Look up each spell, count by level
    resolved: list[dict] = []
    errors:   list[str]  = []
    counts_by_level: dict[int, int] = {}

    for i, name in enumerate(spell_names):
        spell = lookup_spell(name, db_class_name)
        if not spell:
            spell = lookup_spell(name)  # try without class filter
        if not spell:
            errors.append(f"Spell '{name}' not found in database.")
            continue
        sp_lvl = spell.get("spell_level", 1)
        counts_by_level[sp_lvl] = counts_by_level.get(sp_lvl, 0) + 1

        # Validate slot count
        slot_limit = max_slots[sp_lvl - 1] if sp_lvl - 1 < len(max_slots) else 0
        if counts_by_level[sp_lvl] > slot_limit:
            errors.append(
                f"Cannot memorize {counts_by_level[sp_lvl]} level-{sp_lvl} spells — "
                f"only {slot_limit} slot(s) available at {target_cls_key} level {cls_level}."
            )
            continue

        resolved.append({
            "slot_id":    f"{db_class_name}_{sp_lvl}_{counts_by_level[sp_lvl]}",
            "spell_id":   spell["spell_id"],
            "name":       spell["name"],
            "class_name": db_class_name,
            "spell_level": sp_lvl,
            "school":     spell.get("school", ""),
            "expended":   False,
        })

    if errors:
        return {"error": errors, "resolved_before_error": resolved}

    # Merge with existing memory (keep other classes)
    memory    = get_spell_memory()
    existing  = [
        s for s in memory.get("memorized", [])
        if s.get("class_name", "").replace("-", "_") != db_class_name
    ]
    memory["memorized"] = existing + resolved
    set_spell_memory(memory)

    return {
        "memorized":       resolved,
        "class":           target_cls_key,
        "level":           cls_level,
        "total_memorized": len(resolved),
        "note": f"Memorized {len(resolved)} spells for {target_cls_key}. "
                "Call cast_spell() to expend a slot during play.",
    }


@mcp.tool()
def cast_spell(
    spell_name: Annotated[
        str,
        "Name of the spell to cast. Must match a non-expended slot in today's "
        "memorized list. Partial matches accepted.",
    ],
    target: Annotated[
        str,
        "Target of the spell (for the session log).",
    ] = "",
    notes: Annotated[
        str,
        "Any notes about how the spell is being used or conditions that apply.",
    ] = "",
) -> dict:
    """
    Expend one memorized spell slot and return the spell's full description.

    Finds the first non-expended slot in the memorized list that matches
    spell_name. Marks it as expended. Retrieves the complete spell record
    from the spells table including range, duration, area of effect, saving
    throw, and description.

    Returns everything needed to narrate the spell effect:
      spell_name, level, school, range, duration, area, saving_throw,
      summary_text, combat_use_text, description.

    After casting, check if any mechanical results need to be resolved:
      - If saving_throw is not empty, prompt the DM/player for a save roll
      - If the spell deals damage, use roll_dice() for the damage expression
      - If the spell affects HP, call update_character_status()
    """
    memory    = get_spell_memory()
    memorized = memory.get("memorized", [])

    # Find first matching non-expended slot
    target_slot = None
    slot_index  = -1
    for i, slot in enumerate(memorized):
        if not slot.get("expended", False):
            if spell_name.lower() in slot["name"].lower():
                target_slot = slot
                slot_index  = i
                break

    if target_slot is None:
        available_names = [s["name"] for s in memorized if not s.get("expended", False)]
        return {
            "error": f"No available slot for '{spell_name}'. "
                     f"Available spells: {available_names or ['(none — all expended or none memorized)']}"
        }

    # Mark expended
    memorized[slot_index]["expended"] = True
    memory["memorized"] = memorized
    set_spell_memory(memory)

    # Retrieve full spell data from DB
    spell_data = lookup_spell(
        target_slot["name"],
        target_slot.get("class_name"),
    )

    remaining = sum(1 for s in memorized if not s.get("expended", False))

    result = {
        "cast":        True,
        "spell_name":  target_slot["name"],
        "spell_level": target_slot.get("spell_level"),
        "class_name":  target_slot.get("class_name"),
        "target":      target,
        "notes":       notes,
        "slots_remaining_today": remaining,
    }

    if spell_data:
        result.update({
            "school":        spell_data.get("school", ""),
            "range":         spell_data.get("range_text", ""),
            "duration":      spell_data.get("duration", ""),
            "area_of_effect": spell_data.get("area_of_effect", ""),
            "components":    spell_data.get("components", ""),
            "casting_time":  spell_data.get("casting_time", ""),
            "saving_throw":  spell_data.get("saving_throw", ""),
            "summary_text":  spell_data.get("summary_text", ""),
            "combat_use":    spell_data.get("combat_use_text", ""),
            "description":   spell_data.get("description", ""),
        })

        # Mechanical reminders
        reminders = []
        if spell_data.get("saving_throw") and spell_data["saving_throw"].strip():
            reminders.append(
                f"Saving throw required: {spell_data['saving_throw']}. "
                "Use roll_dice('1d20') and compare against target's saving throw score."
            )
        if spell_data.get("combat_use_text") and "damage" in spell_data["combat_use_text"].lower():
            reminders.append(
                "This spell deals damage — use roll_dice() for the damage roll."
            )
        if reminders:
            result["mechanical_reminders"] = reminders

    return result


@mcp.tool()
def rest(
    rest_type: Annotated[
        str,
        "'long' — 8 hours of sleep: restores all spell slots and HP. "
        "'short' — 1 hour of rest: no spell recovery, no HP change.",
    ] = "long",
    calendar_note: Annotated[
        str,
        "Current in-game date/time to record (e.g. '576 CY Fireseek 5, dusk'). "
        "Leave blank to auto-increment based on last recorded time.",
    ] = "",
) -> dict:
    """
    Advance time and restore resources after a rest.

    Long rest (8 hours):
      - Restores all spell slots (clears all expended flags in spell_memory).
      - Recovers HP: 1 HP per character level (campaign ruling — fast enough
        for solo play without being trivially instant). HP cannot exceed max.
      - Advances the in-game calendar by 8 hours (stored in world_facts
        category 'calendar').

    Short rest (1 hour):
      - No spell recovery.
      - No HP recovery.
      - Advances the in-game calendar by 1 hour.

    Returns updated HP and spell slots after the rest.
    """
    import datetime as _dt

    # ── Load PC status ─────────────────────────────────────────────────────────
    from engine.db import _get_conn as _ec, _PC_CHARACTER_ID as _pc_id, _CAMPAIGN_ID as _cid
    with _ec(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT hp_current, hp_max FROM character_status WHERE character_id = ?",
            (_pc_id,),
        )
        row = cur.fetchone()

    hp_cur = row["hp_current"] if row else 0
    hp_max = row["hp_max"]     if row else 0

    # Total character levels (sum across all classes)
    with _ec(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT SUM(level) as total FROM class_levels WHERE character_id = ?",
            (_pc_id,),
        )
        lrow = cur.fetchone()
    total_levels = (lrow["total"] or 1) if lrow else 1

    result: dict = {"rest_type": rest_type}

    if rest_type.lower().startswith("long"):
        # ── HP recovery ───────────────────────────────────────────────────────
        hp_gained  = min(total_levels, hp_max - hp_cur)
        new_hp     = hp_cur + hp_gained
        with _ec() as conn:
            conn.execute(
                "UPDATE character_status SET hp_current = ? WHERE character_id = ?",
                (new_hp, _pc_id),
            )

        # ── Spell slot restoration ────────────────────────────────────────────
        memory = get_spell_memory()
        for slot in memory.get("memorized", []):
            slot["expended"] = False
        memory["last_rest"] = calendar_note or "after long rest"
        set_spell_memory(memory)

        restored_slots = sum(
            1 for s in memory.get("memorized", [])
            if not s.get("expended", False)
        )

        result.update({
            "hp_before":      hp_cur,
            "hp_after":       new_hp,
            "hp_gained":      hp_gained,
            "hp_max":         hp_max,
            "spell_slots_restored": restored_slots,
            "hours_passed":   8,
        })

    else:  # short rest
        result.update({
            "hp_before": hp_cur,
            "hp_after":  hp_cur,
            "hp_gained": 0,
            "spell_slots_restored": 0,
            "hours_passed": 1,
        })

    # ── Advance calendar ──────────────────────────────────────────────────────
    hours = result["hours_passed"]
    if calendar_note:
        new_time = calendar_note
    else:
        # Read existing calendar fact
        with _ec(read_only=True) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT fact_text FROM world_facts "
                "WHERE campaign_id = ? AND category = 'calendar' LIMIT 1",
                (_cid,),
            )
            cal_row = cur.fetchone()
        existing = cal_row["fact_text"] if cal_row else "576 CY (date unknown)"
        new_time = f"{existing} [+{hours}h]"

    with _ec() as conn:
        conn.execute(
            "DELETE FROM world_facts WHERE campaign_id = ? AND category = 'calendar'",
            (_cid,),
        )
        conn.execute(
            "INSERT INTO world_facts (campaign_id, category, fact_text, source_note) "
            "VALUES (?, 'calendar', ?, 'rest')",
            (_cid, new_time),
        )

    result["calendar_note"] = new_time
    result["note"] = (
        "HP recovery: 1 HP per character level per long rest (campaign ruling). "
        "Spell slots fully restored. Call get_spell_slots() to review."
        if rest_type.lower().startswith("long")
        else "Short rest complete. Spell slots not restored."
    )
    return result


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 3 — DUNGEON SYSTEM
# Random encounters · Wandering monster checks · Treasure generation
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def check_wandering_monster(
    dungeon_level: Annotated[
        int,
        "Current dungeon level (1–16+). Determines which monster level tables "
        "are used if an encounter is triggered.",
    ] = 1,
    chance_in_6: Annotated[
        int,
        "Number of faces on a d6 that trigger an encounter. AD&D default is 1 "
        "(1-in-6 chance per dungeon turn). Use 2 for noisy areas.",
    ] = 1,
) -> dict:
    """
    Roll a wandering monster check for one dungeon turn (10 minutes).

    Rolls 1d6. If the result is ≤ chance_in_6 an encounter is triggered and a
    full random encounter is rolled immediately (same as calling random_encounter).
    Otherwise returns {"encounter": false}.

    Also increments the persistent dungeon turn counter (world_facts category
    'dungeon_turns') so the DM can track time and light-source duration.

    Call this once per dungeon turn spent moving, searching, or doing anything
    that takes roughly 10 minutes. Do NOT call it during combat rounds.
    """
    d6        = random.randint(1, 6)
    turn_count = increment_dungeon_turn()
    triggered  = d6 <= chance_in_6

    result: dict = {
        "encounter":    triggered,
        "d6_roll":      d6,
        "chance_in_6":  chance_in_6,
        "dungeon_turn": turn_count,
        "dungeon_level": dungeon_level,
    }

    if triggered:
        enc = get_random_dungeon_encounter(dungeon_level)
        result["encounter_data"] = enc
        result["note"] = (
            f"Wandering monster triggered! {enc['count']}× {enc['monster_name']} "
            f"(table {enc['monster_level_table']}, d20={enc['d20_roll']}, "
            f"d100={enc['d100_roll']}). "
            "Call start_combat() to begin the encounter."
        )
        if enc.get("notes"):
            result["table_note"] = enc["notes"]
    else:
        result["note"] = (
            f"No encounter (rolled {d6}, needed ≤ {chance_in_6}). "
            f"Dungeon turn {turn_count} passes."
        )

    return result


@mcp.tool()
def random_encounter(
    dungeon_level: Annotated[
        int,
        "Current dungeon level (1–16+). Level 1 draws mostly from monster "
        "table I; deeper levels escalate to tables II–X.",
    ] = 1,
) -> dict:
    """
    Roll a random dungeon encounter for the given level.

    Uses the AD&D 1e two-step random encounter system:
      1. Roll d20 → select a monster level table (I–X) based on dungeon depth.
      2. Roll d100 → select a specific monster from that table.
      3. Roll number appearing.
      4. Look up the monster's full stat block.

    Returns everything needed to narrate the encounter and call start_combat().

    branch_type values:
      'monster' — standard monster; monster_stats is populated.
      'human'   — a dungeon adventuring party; see table_note for details.
      'subtable' — special result requiring a sub-roll; see table_note.

    For 'human' and 'subtable' results, narrate as appropriate and optionally
    call start_combat() with a custom enemies list.
    """
    enc = get_random_dungeon_encounter(dungeon_level)

    result = {
        "monster_name":          enc["monster_name"],
        "count":                 enc["count"],
        "number_appearing_text": enc["number_appearing_text"],
        "dungeon_level":         enc["dungeon_level"],
        "monster_level_table":   enc["monster_level_table"],
        "d20_roll":              enc["d20_roll"],
        "d100_roll":             enc["d100_roll"],
        "branch_type":           enc["branch_type"],
    }

    if enc.get("notes"):
        result["table_note"] = enc["notes"]

    stats = enc.get("monster_stats") or {}
    if stats:
        result["monster_stats"] = {
            "ac":              stats.get("armor_class"),
            "hd":              stats.get("hit_dice"),
            "move":            stats.get("move"),
            "damage":          stats.get("damage"),
            "number_attacks":  stats.get("number_of_attacks"),
            "special_attacks": stats.get("special_attacks"),
            "special_defenses": stats.get("special_defenses"),
            "treasure_type":   stats.get("treasure_type"),
            "alignment":       stats.get("alignment"),
            "intelligence":    stats.get("intelligence"),
            "size":            stats.get("size"),
        }
        result["treasure_type"] = stats.get("treasure_type", "")

    result["next_steps"] = (
        f"Encounter: {enc['count']}× {enc['monster_name']}. "
        "Describe the encounter, check for surprise (d6 each side, 1-2 = surprised), "
        "then call start_combat() with the monster name and count. "
        "After combat, call generate_treasure() with the monster's treasure_type."
    )

    return result


@mcp.tool()
def generate_treasure(
    treasure_type: Annotated[
        str,
        "Treasure type letter A–Z (e.g. 'A', 'C', 'F'). Found in the monster's "
        "stat block or on the AD&D 1e monster listing. Case-insensitive.",
    ],
    context: Annotated[
        str,
        "Optional description of where the treasure is found, e.g. "
        "'goblin lair chest', 'orc chieftain body'. Stored in the return for "
        "narrative reference only — does not affect rolls.",
    ] = "",
) -> dict:
    """
    Roll a complete AD&D 1e treasure haul for the given treasure type (A–Z).

    Each component (coins, gems, jewelry, magic items) is rolled independently
    with its published chance percentage. The treasure_types table is the
    authoritative source — roll results are fully random per AD&D 1e rules.

    Coin amounts are in the actual denomination (not thousands):
      cp/sp/ep/gp = qty × 1,000   (copper/silver/electrum/gold thousands)
      pp          = qty × 100     (platinum hundreds)

    Gems and jewelry pieces are individually typed and valued from their
    respective subtables (gem_base_value, jewelry_base_value).

    Magic items are rolled on the category determination table first, then on
    the appropriate subtable (potions, scrolls, rings, swords, armor, etc.).

    total_gp_value is the approximate GP equivalent of the entire haul.

    After reviewing the results, use add_item() to add notable items to
    inventory and update_treasury() for coins.
    """
    hoard = roll_treasure_by_type(treasure_type)

    if "error" in hoard:
        return hoard

    # Build a human-readable summary
    lines: list[str] = []

    coins = hoard.get("coins", {})
    if coins:
        coin_parts = []
        for coin_type, amount in coins.items():
            coin_parts.append(f"{amount:,} {coin_type.upper()}")
        lines.append("Coins: " + ", ".join(coin_parts))
    else:
        lines.append("Coins: none")

    gems = hoard.get("gems", [])
    if gems:
        gem_summary: dict[str, int] = {}
        for g in gems:
            gem_summary[g["type"]] = gem_summary.get(g["type"], 0) + 1
        gem_str = ", ".join(f"{cnt}× {typ}" for typ, cnt in gem_summary.items())
        total_gem_gp = sum(g["gp_value"] for g in gems)
        lines.append(f"Gems ({len(gems)}): {gem_str} — {total_gem_gp:,} gp total")
    else:
        lines.append("Gems: none")

    jewelry = hoard.get("jewelry", [])
    if jewelry:
        total_jewelry_gp = sum(j["gp_value"] for j in jewelry)
        j_str = ", ".join(f"{j['type']} ({j['gp_value']:,} gp)" for j in jewelry)
        lines.append(f"Jewelry ({len(jewelry)}): {j_str} — {total_jewelry_gp:,} gp total")
    else:
        lines.append("Jewelry: none")

    magic_items = hoard.get("magic_items", [])
    if magic_items:
        mi_str = ", ".join(item["name"] for item in magic_items)
        lines.append(f"Magic items ({len(magic_items)}): {mi_str}")
    else:
        lines.append("Magic items: none")

    hoard["summary"] = " | ".join(lines)
    hoard["total_gp_value_formatted"] = f"{hoard['total_gp_value']:,.0f} gp"

    if context:
        hoard["context"] = context

    hoard["next_steps"] = (
        "Use add_item() to record notable items (magic, gems, jewelry) in inventory. "
        "Use update_treasury() to add coins to the party treasury."
    )

    return hoard


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    mcp.run()
