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

  DUNGEON
  check_wandering_monster -- Roll 1-in-6 wandering monster check (one dungeon turn)
  random_encounter        -- Roll random encounter for given dungeon level
  generate_treasure       -- Roll complete treasure haul for type A-Z

  DOMAIN
  get_domain_state        -- Full realm snapshot: holdings, troops, treasury, projects
  domain_turn             -- Advance one season: income, upkeep, construction, event
  add_construction_project -- Queue a new building with cost and completion weeks
  collect_income          -- Roll income for all active holdings for N months
  pay_upkeep              -- Deduct troop upkeep for N months from treasury
  realm_event             -- Roll on the d20 realm events table

  TRAVEL & WEATHER
  start_travel            -- Begin a journey: origin, destination, terrain path, mount
  travel_turn             -- Resolve one day of travel with weather and encounter checks
  get_travel_state        -- Current journey status: miles, days, terrain, estimate
  get_lost                -- Trigger lost event: direction error, wander distance, recovery
  generate_weather        -- Roll daily weather for season and region
  get_current_weather     -- Return today's weather and 3-day forecast

  CAROUSING & DOWNTIME
  carouse                 -- Spend gold, roll Jeff Rients d20 table, earn XP, apply consequence
  research_spell          -- Magic-User researches/copies a spell; INT + time + gold = success chance
  gather_rumors           -- Spend days in settlement; roll for rumour quantity and quality tier
  religious_observance    -- Cleric fulfils deity obligations; tracks penalties/bonuses
  domain_administration   -- Hold court; Cha roll affects NPC loyalty and troop morale
  recovery                -- Extended bed rest for serious injuries; enhanced HP + ailment clearing
  craft_item              -- Spend time and materials to produce mundane or minor magic items

  LOYALTY & AGING
  get_loyalty_state       -- All NPC/troop loyalty scores; auto-initializes from DB on first call
  loyalty_check           -- 2d6 vs loyalty score for a specific entity; returns outcome tier
  adjust_loyalty          -- Modify score ±N for gifts, betrayals, promotions, deaths
  henchman_morale_event   -- Monthly 2d6 morale roll for every named NPC henchman
  advance_time            -- Advance calendar N days; check aging thresholds; flag overdue observances
  aging_check             -- Apply ability score changes at middle_age/old/venerable threshold
  get_character_age       -- Current age, race thresholds, years to next aging check

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
    # Phase 5A — travel & weather
    db_start_travel, db_travel_turn, db_get_lost,
    db_generate_weather, db_get_current_weather,
    _get_world_fact_json, _BASE_MOVE_MPD,
    # Phase 5B — carousing & downtime
    db_carouse,
    db_research_spell,
    db_gather_rumors,
    db_religious_observance,
    db_domain_administration,
    db_recovery,
    db_craft_item,
    # Phase 5C — loyalty & aging
    db_get_loyalty_state,
    db_loyalty_check,
    db_adjust_loyalty,
    db_henchman_morale_event,
    db_advance_time,
    db_aging_check,
    db_get_character_age,
    # Phase 5D — siege mechanics
    db_start_siege,
    db_siege_turn,
    db_artillery_fire,
    db_assault,
    db_get_siege_state,
    db_negotiate_surrender,
    # Phase 4 — domain
    get_full_domain_state,
    db_add_construction_project,
    db_collect_income,
    db_pay_upkeep,
    db_roll_realm_event,
    db_create_domain_turn,
    db_advance_construction,
    _credit_treasury,
    _record_ledger_entry,
    _REALM_EVENTS,
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
# PHASE 4 — DOMAIN MANAGEMENT
# Domain turns · Income · Upkeep · Construction · Realm Events
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_domain_state() -> dict:
    """
    Return the complete domain status snapshot.

    Covers:
      - All holdings (locations) with their income range and active status
      - All troop groups with headcount, location, and monthly upkeep cost
      - All treasury accounts with current balances and a GP total
      - All construction projects — both established and in-progress — with
        weeks_remaining and cost_per_week for projects in the construction queue
      - Estimated monthly income range (sum of active holding rates)
      - Total monthly troop upkeep
      - Estimated net monthly range (income minus upkeep)
      - Last domain turn record

    Call this at the start of every domain management session and after any
    major transaction. Does not modify any state.
    """
    return get_full_domain_state()


@mcp.tool()
def add_construction_project(
    name: Annotated[
        str,
        "Full name of the project, e.g. 'Rillford Market Hall'.",
    ],
    project_type: Annotated[
        str,
        "Category: Keep, Tower, Road, Mill, Workshop, Inn, Civic, Stable, "
        "Lodge, Causeway, School, or any descriptive type.",
    ],
    cost_gp: Annotated[
        int,
        "Total gold piece cost of the project.",
    ],
    weeks_total: Annotated[
        int,
        "Estimated construction time in weeks. "
        "Typical ranges: Tower 8–12 weeks, Keep 16–24 weeks, "
        "Workshop 4–8 weeks, Road (per league) 2–4 weeks.",
    ],
    location_name: Annotated[
        str,
        "Name of the existing location where this project is being built, "
        "e.g. 'Quasquetan'. Leave blank if the location is new or unknown.",
    ] = "",
    notes: Annotated[
        str,
        "Brief description of the project's purpose or special features.",
    ] = "",
) -> dict:
    """
    Queue a new construction project.

    Inserts a row into the projects table (status = 'Funded/In Progress') and
    registers the project in the construction queue (world_facts category
    'construction_queue') with its time and cost tracking data.

    The construction queue is consumed by domain_turn(), which advances all
    projects by the season's week count and marks completed ones as
    'Established/Completed'. Cost is NOT automatically deducted here — use
    update_treasury() to record the capital expenditure separately.

    Returns the new project's full record including its project_id.
    """
    # Resolve location_id from name
    location_id: int | None = None
    if location_name.strip():
        from engine.db import _get_conn as _ec, _CAMPAIGN_ID as _cid
        with _ec(read_only=True) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT location_id FROM locations "
                "WHERE campaign_id = ? AND LOWER(name) LIKE LOWER(?) LIMIT 1",
                (_cid, f"%{location_name.strip()}%"),
            )
            row = cur.fetchone()
            if row:
                location_id = row["location_id"]

    project = db_add_construction_project(
        name        = name,
        location_id = location_id,
        project_type= project_type,
        cost_gp     = cost_gp,
        weeks_total = weeks_total,
        notes       = notes,
    )

    project["note"] = (
        f"Project '{name}' queued. {weeks_total} weeks to completion at "
        f"~{project['cost_per_week']} gp/week. "
        "Run domain_turn() each season to advance all projects. "
        "Use update_treasury() to record the capital cost now if already funded."
    )
    return project


@mcp.tool()
def collect_income(
    months: Annotated[
        int,
        "Number of months to collect income for (1 = monthly, 3 = seasonal). "
        "Each active holding rolls its income range independently per month.",
    ] = 1,
    credit_treasury: Annotated[
        bool,
        "If true (default), automatically add the total income to the primary "
        "treasury (treasury_id=1). Set false to review before crediting.",
    ] = True,
) -> dict:
    """
    Roll and record income from all active domain holdings.

    Each active location rolls its monthly income range (based on location_type)
    independently for each month requested. The total is logged in
    domain_income_expenses and optionally credited to the primary treasury.

    Income rates by type (monthly, rough range):
      Keep ~120–220 gp · City ~150–350 gp · District ~70–130 gp
      Mill ~55–105 gp · Farm ~45–85 gp · Workshop ~25–55 gp
      Lodge ~15–35 gp · Inn ~25–55 gp · Civic ~10–30 gp

    Call this once per game month or as part of domain_turn() for a full
    seasonal cycle. Returns a per-holding breakdown with individual rolls.
    """
    result = db_collect_income(months=months)

    if credit_treasury:
        _credit_treasury(result["total_gp"])
        result["treasury_credited"] = True
        result["treasury_note"] = (
            f"{result['total_gp']:,} gp credited to primary treasury."
        )
    else:
        result["treasury_credited"] = False
        result["treasury_note"] = (
            "Treasury NOT updated. Call update_treasury() to credit manually."
        )

    result["note"] = (
        f"Income collected: {result['total_gp']:,} gp from "
        f"{result['holdings_rolled']} holdings over {months} month(s)."
    )
    return result


@mcp.tool()
def pay_upkeep(
    months: Annotated[
        int,
        "Number of months of upkeep to pay (1 = monthly, 3 = seasonal). "
        "All troop groups are charged their full rate.",
    ] = 1,
) -> dict:
    """
    Calculate and deduct troop upkeep for the given number of months.

    Each troop group is charged its per-type monthly rate:
      Ogres 15 gp · Elves 5 gp · Mounted Humans 6 gp · Dwarves/Gnomes 4 gp
      Human Soldiers 3 gp · Hobgoblins 2 gp · Halflings 2 gp
      Goblins/Orcs/Laborers 1 gp · Constructs 0 gp

    Deducts the total from the primary treasury (treasury_id=1) and records
    the transaction in domain_income_expenses. The treasury will not go below
    zero — if the realm is insolvent the shortfall is noted but not enforced
    mechanically (the DM handles the narrative consequences).

    Returns a per-group breakdown with individual costs and the total charged.
    """
    result = db_pay_upkeep(months=months)

    result["note"] = (
        f"Upkeep paid: {result['total_gp_charged']:,} gp for "
        f"{result['troop_groups_charged']} troop groups over {months} month(s). "
        f"Monthly upkeep baseline: {result['total_monthly_upkeep']:,} gp."
    )
    return result


@mcp.tool()
def realm_event(
    force_roll: Annotated[
        int,
        "Force a specific d20 result (1–20) instead of rolling randomly. "
        "Leave at 0 to roll normally.",
    ] = 0,
) -> dict:
    """
    Roll one random realm event on the d20 domain events table.

    The 20-entry table covers the full range of peacetime domain developments:
      Positive (rolls 1–10): Harvest bonuses, trade windfalls, settlers, festivals,
        skilled craftsmen, mercenary offers, diplomatic openings.
      Negative (rolls 11–19): Harsh weather, bandits, monster raids, plague,
        crop failure, spy activity, unrest, rival claimants.
      Special (roll 20): Great Fortune — roll twice, apply both results.

    The result includes a mechanical_key that describes what action to take:
      income_bonus_*   → call collect_income or update_treasury for the bonus
      income_loss_*    → call update_treasury to deduct the penalty
      gain_d4_laborers → call add_troop_group
      construction_speed_up_2_weeks → call domain_turn with extra weeks
      lose_d4_troops_* → call update_troop_count
      narrative_only   → no mechanical change; narrate the event
      roll_twice       → call realm_event() twice more

    This tool rolls the event and describes it; mechanical application is your
    responsibility — the event result tells you what to do.
    """
    if force_roll and 1 <= force_roll <= 20:
        roll = force_roll
        event = next(
            (e for e in _REALM_EVENTS if e[0] == roll),
            (roll, "Quiet Season", "Nothing notable occurs. The realm rests.", "narrative_only"),
        )
        event_dict = {
            "roll":           roll,
            "title":          event[1],
            "description":    event[2],
            "mechanical_key": event[3],
            "forced":         True,
        }
    else:
        event_dict = db_roll_realm_event()
        event_dict["forced"] = False

    # Resolve secondary dice for mechanical effects
    key = event_dict["mechanical_key"]
    effect_roll: int | None = None
    effect_gp:   int | None = None

    if "d6x50" in key:
        effect_roll = random.randint(1, 6)
        effect_gp   = effect_roll * 50
    elif "d4x30" in key:
        effect_roll = random.randint(1, 4)
        effect_gp   = effect_roll * 30
    elif "d6x30" in key:
        effect_roll = random.randint(1, 6)
        effect_gp   = effect_roll * 30
    elif "d4x25" in key:
        effect_roll = random.randint(1, 4)
        effect_gp   = effect_roll * 25
    elif "d6x20" in key:
        effect_roll = random.randint(1, 6)
        effect_gp   = effect_roll * 20
    elif "d4x50" in key:
        effect_roll = random.randint(1, 4)
        effect_gp   = effect_roll * 50
    elif "d4x40" in key:
        effect_roll = random.randint(1, 4)
        effect_gp   = effect_roll * 40
    elif "d6x15" in key:
        effect_roll = random.randint(1, 6)
        effect_gp   = effect_roll * 15
    elif "d4_laborers" in key:
        effect_roll = random.randint(1, 4)
    elif "d4_troops" in key:
        effect_roll = random.randint(1, 4)
    elif "10pct" in key:
        effect_roll = 10   # percentage

    if effect_roll is not None:
        event_dict["effect_roll"] = effect_roll
    if effect_gp is not None:
        event_dict["effect_gp"] = effect_gp

    # Generate next-step instructions
    instructions: list[str] = []
    if "income_bonus" in key and effect_gp:
        instructions.append(
            f"Call update_treasury(account='Quasquetan Treasury', delta_gp={effect_gp}) "
            f"to credit the {effect_gp} gp windfall."
        )
    elif "income_loss" in key and effect_gp:
        instructions.append(
            f"Call update_treasury(account='Quasquetan Treasury', delta_gp=-{effect_gp}) "
            f"to deduct the {effect_gp} gp loss."
        )
    elif "10pct" in key:
        instructions.append(
            "Income this season is reduced by 10%. Adjust collect_income total accordingly."
        )
    elif "gain_d4_laborers" in key:
        instructions.append(
            f"{effect_roll} new laborers arrive. Call add_troop_group() to record them."
        )
    elif "construction_speed_up" in key:
        instructions.append(
            "Call domain_turn() with extra_weeks=2 to apply the early completion bonus."
        )
    elif "lose_d4_troops" in key:
        instructions.append(
            f"{effect_roll} troops fall ill. Call update_troop_count() to reduce a "
            "relevant garrison by that amount."
        )
    elif key == "roll_twice":
        instructions.append(
            "Roll twice more using realm_event() and apply both results "
            "(re-roll if you get 20 again)."
        )
    elif key == "narrative_only":
        instructions.append("No mechanical change required — narrate the event.")

    if instructions:
        event_dict["instructions"] = instructions

    return event_dict


@mcp.tool()
def domain_turn(
    season_label: Annotated[
        str,
        "Label for this domain turn, e.g. 'Coldeven 576 CY' or 'Spring 577'. "
        "Used as the turn_label in domain_turns.",
    ],
    start_date: Annotated[
        str,
        "In-game start date of this season, e.g. '1 Readying 576 CY'.",
    ] = "",
    end_date: Annotated[
        str,
        "In-game end date of this season, e.g. '28 Coldeven 576 CY'.",
    ] = "",
    weeks_in_season: Annotated[
        int,
        "Number of construction weeks this turn covers. Standard season = 13 weeks. "
        "Use a higher number if extra construction speed applies (realm event, etc.).",
    ] = 13,
    roll_event: Annotated[
        bool,
        "If true (default), roll one realm event at the end of the turn.",
    ] = True,
) -> dict:
    """
    Advance the domain by one full season.

    Performs all five domain turn steps in sequence:

    1. Creates a new domain_turns record for the season.
    2. Collects income from all active holdings for 3 months. Credits treasury.
    3. Pays troop upkeep for 3 months. Debits treasury.
    4. Advances all construction projects by weeks_in_season weeks. Projects that
       reach 0 weeks_remaining are marked 'Established/Completed' automatically.
    5. Rolls one realm event (if roll_event=True).

    Returns a complete season report: income breakdown, upkeep breakdown, net
    treasury change, project progress, and the realm event. The treasury is
    updated for income and upkeep automatically. All transactions are recorded
    in domain_income_expenses.

    Call get_domain_state() after this to see the updated realm snapshot.
    """
    SEASON_MONTHS = 3
    report: dict = {
        "season_label":    season_label,
        "start_date":      start_date,
        "end_date":        end_date,
        "weeks_in_season": weeks_in_season,
    }

    # ── Step 1: Create turn record ─────────────────────────────────────────────
    turn_id = db_create_domain_turn(season_label, start_date, end_date)
    report["domain_turn_id"] = turn_id

    # ── Step 2: Collect income ─────────────────────────────────────────────────
    income = db_collect_income(months=SEASON_MONTHS)
    _credit_treasury(income["total_gp"])
    _record_ledger_entry(
        entry_type     = "income",
        amount_gp      = income["total_gp"],
        description    = f"Seasonal income — {season_label}",
        domain_turn_id = turn_id,
    )
    report["income"] = {
        "total_gp":        income["total_gp"],
        "holdings_rolled": income["holdings_rolled"],
        "breakdown":       income["breakdown"],
    }

    # ── Step 3: Pay upkeep ─────────────────────────────────────────────────────
    upkeep = db_pay_upkeep(months=SEASON_MONTHS)
    _record_ledger_entry(
        entry_type     = "expense",
        amount_gp      = upkeep["total_gp_charged"],
        description    = f"Seasonal upkeep — {season_label}",
        domain_turn_id = turn_id,
    )
    report["upkeep"] = {
        "total_gp_charged":     upkeep["total_gp_charged"],
        "monthly_baseline_gp":  upkeep["total_monthly_upkeep"],
        "breakdown":            upkeep["breakdown"],
    }

    # ── Step 4: Advance construction ──────────────────────────────────────────
    construction = db_advance_construction(weeks=weeks_in_season)
    report["construction"] = construction

    for completed in construction.get("completed", []):
        _record_ledger_entry(
            entry_type     = "project_complete",
            amount_gp      = 0,
            description    = f"Project completed: {completed['name']}",
            domain_turn_id = turn_id,
            project_id     = completed["project_id"],
        )

    # ── Step 5: Realm event ────────────────────────────────────────────────────
    if roll_event:
        event = db_roll_realm_event()
        # Apply simple automatic effects
        auto_gp_delta = 0
        if "income_bonus" in event["mechanical_key"]:
            for r_entry, title, desc, key in _REALM_EVENTS:
                if key == event["mechanical_key"]:
                    # Already rolled by db_roll_realm_event but we need the bonus
                    break
        report["realm_event"] = event
    else:
        report["realm_event"] = None

    # ── Summary ───────────────────────────────────────────────────────────────
    net_gp = income["total_gp"] - upkeep["total_gp_charged"]
    report["net_gp_this_season"] = net_gp
    report["completed_projects"] = [c["name"] for c in construction.get("completed", [])]
    report["summary"] = (
        f"Season {season_label}: "
        f"Income {income['total_gp']:,} gp | "
        f"Upkeep {upkeep['total_gp_charged']:,} gp | "
        f"Net {net_gp:+,} gp | "
        f"{len(construction.get('advanced', []))} projects progressed, "
        f"{len(construction.get('completed', []))} completed."
    )
    if roll_event and report["realm_event"]:
        report["summary"] += f" Realm event: {report['realm_event']['title']}."

    return report


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 5A — TRAVEL & WEATHER SYSTEM
# Hex-crawl travel · Daily resolution · Weather generation · Getting lost
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def generate_weather(
    season: Annotated[
        str,
        "Current Greyhawk season: 'winter', 'spring', 'summer', or 'autumn'. "
        "Winter = Sunsebb/Fireseek, Spring = Readying/Coldeven, "
        "Summer = Wealsun/Reaping/Goodmonth, Autumn = Harvester/Patchwall.",
    ],
    date_str: Annotated[
        str,
        "In-game date for context, e.g. 'Fireseek 12, 576 CY'. "
        "Stored with the weather record but does not affect rolls.",
    ] = "",
) -> dict:
    """
    Roll today's weather and a 3-day forecast for the Vesve frontier region.

    Generates temperature, precipitation, wind, visibility, movement modifier,
    and survival check flag for the Vesve Forest frontier near Whyestil Lake
    (cold temperate, lake-effect, northern Flanaess).

    Weather affects travel: movement_modifier multiplies daily mileage.
      1.0 = full movement  0.75 = light rain/fog  0.5 = heavy rain or snow
      0.25 = thunderstorm or blizzard conditions  0.0 = storm halts travel

    Conditions that require survival checks (extreme cold below 10°F in winter)
    are flagged. The party must succeed on a CON check or suffer fatigue penalties.

    Stores today's weather as world_facts 'current_weather' and the 3-day
    forecast as 'weather_forecast'. Calling this again replaces both.
    """
    return db_generate_weather(season=season, date_str=date_str)


@mcp.tool()
def get_current_weather() -> dict:
    """
    Return today's stored weather conditions and the 3-day forecast.

    If no weather has been generated yet, returns an error with a hint.
    Call generate_weather() first to set today's conditions.

    Returns:
      - temperature_f, temperature_desc
      - precipitation_label, wind_label
      - visibility_miles
      - movement_modifier (multiply base daily miles by this)
      - halts_travel (True = severe storm, party cannot move)
      - survival_check_required (True = extreme cold, CON check needed)
      - conditions_summary (single-line human-readable)
      - forecast_3_days: list of simplified next-3-day conditions
    """
    return db_get_current_weather()


@mcp.tool()
def start_travel(
    origin: Annotated[
        str,
        "Name or description of the starting location, e.g. 'Quasquetan'.",
    ],
    destination: Annotated[
        str,
        "Name or description of the destination, e.g. 'Rillford'.",
    ],
    total_miles: Annotated[
        int,
        "Total journey distance in miles. Use the Greyhawk map or known distances. "
        "Typical: Quasquetan–Rillford ~12 mi, cross-country hex ~6 mi per hex.",
    ],
    terrain_path: Annotated[
        str,
        "Terrain type(s) for the journey. Single type: 'forest'. "
        "Multiple segments: 'road:8,forest:20,hills:12' (terrain:miles pairs). "
        "Valid types: road, plains, hills, forest, mountains, swamp, marsh.",
    ] = "plains",
    mount_type: Annotated[
        str,
        "Travel mode: 'foot', 'light_horse', or 'heavy_horse'. "
        "Determines base movement rate per terrain.",
    ] = "foot",
    notes: Annotated[
        str,
        "Optional notes about this journey (purpose, party size, special conditions).",
    ] = "",
) -> dict:
    """
    Begin a new overland journey and return the full travel plan.

    Calculates for each terrain segment:
      - Miles per day (base movement rate by terrain and mount type)
      - Days required (ceiling of miles ÷ daily rate)
      - Hexes crossed (at 6 miles per hex)

    Summarises total food (days' rations per person) and water (pints per person)
    needed for the journey. Horse fodder is noted for mounted parties.

    Movement rates (miles/day):
      Foot:        Road 24 · Plains 18 · Hills/Forest 12 · Mountains/Swamp 6
      Light Horse: Road 48 · Plains 36 · Hills 18 · Forest 12 · Mountains/Swamp 4–6
      Heavy Horse: Road 36 · Plains 27 · Hills 15 · Forest 12 · Mountains/Swamp 3–6

    Stores the travel state in world_facts. Call travel_turn() once per day
    to resolve each day's movement, weather effects, encounters, and getting lost.

    Only one journey can be active at a time. Starting a new journey overwrites
    any existing travel state.
    """
    return db_start_travel(
        origin       = origin,
        destination  = destination,
        terrain_path = terrain_path,
        mount_type   = mount_type,
        total_miles  = total_miles,
        notes        = notes,
    )


@mcp.tool()
def travel_turn() -> dict:
    """
    Resolve one day of overland travel.

    Performs all daily travel steps:
    1. Reads current weather — applies movement_modifier to base daily miles.
       movement_modifier=0 (blizzard/storm) halts travel entirely.
    2. Determines today's terrain from the leading journey segment.
    3. Rolls for getting lost (terrain-based chance, doubled in severe weather).
       Forest 30%, Mountains 25%, Swamp 40%, Hills 15%, Plains 5%, Road 0%.
    4. Rolls for random encounter (terrain-based chance per day).
       Road 1-in-12 · Plains/Hills/Mountains 1-in-6 · Forest/Swamp 2-in-6.
    5. Advances the journey by actual miles traveled. Detects segment transitions.
    6. Consumes 1 food-day per person.
    7. Marks the journey complete when all terrain segments are exhausted.

    Returns:
      - actual_miles_today, total_miles_traveled, miles_remaining
      - days_elapsed, days_remaining_estimate
      - got_lost (bool) + lost_result (direction, hexes off, reorient hours)
      - encounter (monster name, count, stats) if triggered
      - halted_by_weather (bool)
      - survival_check_required (bool)
      - journey_complete (bool) — update current scene location when True

    Call this once per in-game day. Call generate_weather() each morning
    before travel_turn() to roll fresh conditions for the day.
    """
    return db_travel_turn()


@mcp.tool()
def get_travel_state() -> dict:
    """
    Return the current journey status without advancing any state.

    Shows:
      - origin / destination
      - mount_type
      - terrain_segments remaining (with miles_remaining each)
      - total_miles, miles_traveled, miles_remaining
      - days_elapsed, total_days_estimate
      - food_days_needed / food_days_consumed / food_days_remaining
      - encounters_log (all encounters this trip)
      - weather_delays_days and lost_extra_days accumulated
      - active (False means journey is complete or not started)

    Returns an error if no travel state exists. Call start_travel() first.
    """
    state = _get_world_fact_json("travel_state")
    if not state:
        return {
            "error":  "No travel state found.",
            "hint":   "Call start_travel() to begin a journey.",
            "active": False,
        }

    # Compute derived fields
    miles_remaining = sum(
        s.get("miles_remaining", 0) for s in state.get("terrain_segments", [])
    )
    mount_type = state.get("mount_type", "foot")
    # Estimate current terrain
    segs = state.get("terrain_segments", [])
    current_terrain = segs[0]["terrain"] if segs else "unknown"
    base_move = _BASE_MOVE_MPD.get(mount_type, _BASE_MOVE_MPD["foot"]).get(current_terrain, 18)
    days_rem  = max(0, int(miles_remaining / base_move + 0.99)) if miles_remaining > 0 else 0

    state["miles_remaining"]        = miles_remaining
    state["days_remaining_estimate"] = days_rem
    state["food_days_remaining"]    = (
        state.get("food_days_needed", 0) - state.get("food_days_consumed", 0)
    )
    return state


@mcp.tool()
def get_lost(
    terrain: Annotated[
        str,
        "Current terrain type where the party got lost: "
        "road, plains, hills, forest, mountains, swamp, or marsh.",
    ],
    weather_condition: Annotated[
        str,
        "Active weather condition that contributed, e.g. 'heavy_snow', 'fog', "
        "'heavy_rain'. Leave blank for clear conditions.",
    ] = "",
) -> dict:
    """
    Resolve a getting-lost event.

    Rolls to determine:
      - Direction the party has drifted (d8 compass rose: N, NE, E, SE, S, SW, W, NW)
      - Hexes off course (1–3 hexes, each 6 miles)
      - Hours required to reorient (2–8 hours; 8 hours = full day lost)

    Getting-lost base chances by terrain (% per day without a ranger/good map):
      Forest 30% · Swamp 40% · Marsh 35% · Mountains 25% · Hills 15% · Plains 5%
    Severe weather (movement_modifier ≤ 0.5) doubles the chance.

    Use this tool whenever:
      - travel_turn() returns got_lost=True (to get the specific direction/extent)
      - The DM decides the party is lost due to narrative circumstances
      - A navigation roll fails during hex travel

    Returns direction, hexes off course, miles off course, hours to reorient,
    extra days lost (0 or 1), and a description for narration.
    """
    return db_get_lost(terrain=terrain, weather_condition=weather_condition)


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 5B — CAROUSING & DOWNTIME ACTIVITIES
# ══════════════════════════════════════════════════════════════════════════════


@mcp.tool()
def carouse(
    gold_spent: Annotated[
        int,
        "Gold pieces spent on the evening's entertainment (minimum 1). "
        "This exact amount is deducted from the primary treasury. "
        "XP equal to gold spent is always awarded. "
        "Spend tiers add a bonus to the d20 consequence roll: "
        "25 gp = +1, 50 gp = +2, 100 gp = +3, 200 gp = +4, 500 gp = +5. "
        "Higher rolls trend toward colourful but manageable; lower rolls toward trouble.",
    ],
    calendar_note: Annotated[
        str,
        "Current in-game date/time to record, e.g. 'Coldeven 14, 576 CY'. "
        "If omitted, '+1d' is appended to the existing calendar entry.",
    ] = "",
) -> dict:
    """
    Carousing downtime activity — Jeff Rients style.

    The character spends an evening (or three) in the taverns and back alleys of
    the nearest settlement. Gold spent is deducted from the primary treasury and
    converted 1:1 into XP — always, regardless of what the dice decide.

    Then a d20 is rolled (modified by gold spend tier) to determine the night's
    consequence. Results 1-10 range from public embarrassment to serious trouble.
    Results 11-20 are colourful, beneficial, or at worst mixed.

    Returns:
    - gold_spent, treasury_before/after
    - xp_awarded and per-class XP breakdown
    - d20 raw roll, spend_bonus, final_roll
    - consequence_type, consequence (narrative text), mechanical_effect, severity
    - extra_rolls (damage dice, debt amounts, winnings, etc. pre-rolled)
    - calendar (updated in-game date)
    - dm_note with instructions for applying mechanical effects

    Jeff Rients carousing XP rule: GP spent = XP earned, period. The night out
    is its own reward. The hangover/enemy/tattoo is the game.
    """
    return db_carouse(gold_spent=gold_spent, calendar_note=calendar_note)


@mcp.tool()
def research_spell(
    spell_name: Annotated[
        str,
        "Name of the spell being researched or copied into the spellbook.",
    ],
    spell_level: Annotated[
        int,
        "Level of the spell (1-9). Determines minimum research time, "
        "expected gold cost (100 gp x level x weeks), and XP awarded on success.",
    ],
    days: Annotated[
        int,
        "Number of days spent on research. Minimum viable: spell_level days. "
        "Typical: spell_level weeks (e.g. level 3 spell = 21 days). "
        "Extra weeks beyond the minimum add +5% to success chance each.",
    ],
    gold_spent: Annotated[
        int,
        "Gold pieces spent on materials, components, and library access. "
        "Guideline: 100 gp x spell_level x weeks worked. "
        "Underfunding does not directly penalise success but is tracked.",
    ],
    calendar_note: Annotated[
        str,
        "Current in-game date after the research period, e.g. 'Coldeven 28, 576 CY'. "
        "If omitted, '+N days' is appended to the existing calendar entry.",
    ] = "",
) -> dict:
    """
    Magic-User researches a new spell or copies one from a discovered scroll/tome.

    Success chance formula:
      Base 45% + (Intelligence modifier × 5%) + (extra weeks over minimum × 5%)
      Capped at 5% minimum / 95% maximum.

    On success:
    - Spell is noted as added to spellbook (DM calls update_world_fact to record it)
    - XP awarded: 100 × spell_level
    - Calendar advances by days spent

    On failure:
    - Half the time invested (research notes exist; retry possible)
    - All gold spent (materials consumed)
    - No XP

    The spell is not memorized upon research — use memorize_spells after the
    next long rest to prepare it.

    Returns: spell_name, spell_level, success, roll, success_chance_pct,
    days_spent, gold_spent, expected_cost_gp, xp_awarded, calendar, dm_note.
    """
    return db_research_spell(
        spell_name=spell_name,
        spell_level=spell_level,
        days=days,
        gold_spent=gold_spent,
        calendar_note=calendar_note,
    )


@mcp.tool()
def gather_rumors(
    settlement: Annotated[
        str,
        "Name of the settlement where rumours are gathered "
        "(e.g. 'Rillford', 'Quasquetan', 'the border fort').",
    ],
    days: Annotated[
        int,
        "Days spent in taverns, markets, and common rooms. "
        "1 day = quality 1-2 gossip; 4+ days = quality 3 intelligence; "
        "8+ days = quality 4 reliable sources.",
    ],
    gold_spent: Annotated[
        int,
        "Gold pieces spent on drinks, bribes, and introductions. "
        "0 gp = free gossip only; 50+ gp = quality 3; 100+ gp = quality 4. "
        "Deducted from primary treasury.",
    ],
    calendar_note: Annotated[
        str,
        "In-game date after the investigation period. "
        "If omitted, '+N days' appended to existing calendar.",
    ] = "",
) -> dict:
    """
    Spend time and coin in a settlement gathering rumours and intelligence.

    Quality tiers:
    - Quality 1: Common tavern gossip — colourful but often distorted or wrong.
    - Quality 2: Credible traveller reports — details may be off but leads are real.
    - Quality 3: Reliable local sources — specific, actionable, mostly accurate.
    - Quality 4: Solid intelligence from insiders — treat as confirmed fact.

    Charisma modifier extends the maximum quality tier available.

    All gathered rumours are stored in world_facts (category='rumors') for
    future reference. XP: 10 per day of investigation.

    Returns: settlement, days_spent, gold_spent, rumors_learned, rumors list
    (each with quality and text), max_quality, xp_awarded, calendar, dm_note.
    """
    return db_gather_rumors(
        settlement=settlement,
        days=days,
        gold_spent=gold_spent,
        calendar_note=calendar_note,
    )


@mcp.tool()
def religious_observance(
    deity: Annotated[
        str,
        "Name of the deity whose rites are being observed "
        "(e.g. 'Trithereon', 'Vecna', 'Pelor', 'St. Cuthbert').",
    ],
    observance_type: Annotated[
        str,
        "Type of religious duty being performed: "
        "'weekly' — standard prayers (1 day, 50 XP, clears 1 missed mark); "
        "'holy_day' — seasonal feast day rites (1 day, 200 XP, +1 saves 7 days); "
        "'atonement' — formal penance for offences against the deity (2 days, 100 XP, clears ALL penalties); "
        "'major_ritual' — full ceremony with sacrifice or great deed (3 days, 300 XP, divine favour 14 days).",
    ],
    calendar_note: Annotated[
        str,
        "Current in-game date, e.g. 'Coldeven 7, 576 CY'. "
        "If omitted, the appropriate days are appended to the existing calendar.",
    ] = "",
) -> dict:
    """
    Cleric fulfils obligations to their patron deity.

    Tracking: missed observances accumulate in world_facts
    (category='religious_obligations'). If missed_count reaches 3 or more, the
    cleric loses their highest memorized spell level until atonement is performed.

    Performing any observance reduces missed_count by 1 (except atonement, which
    resets it to 0). The appropriate bonus is granted immediately.

    Bonuses:
    - weekly: prayer_bonus_24h — +1 to next Wisdom check
    - holy_day: holy_day_bonus_7d — +1 to all saving throws for 7 days
    - atonement: atonement_cleared — all penalties removed; standing restored
    - major_ritual: divine_favour_14d — +1 morale to all followers 14 days;
      +1 to the cleric's next turn undead attempt

    Vital for Aelric (Vecna) — missed Vecna rites carry especially severe
    consequences given the Eye's ongoing demands.

    Returns: deity, observance_type, penalty_before/after, missed_before/after,
    bonus_granted, xp_awarded, description, calendar, dm_note.
    """
    return db_religious_observance(
        deity=deity,
        observance_type=observance_type,
        calendar_note=calendar_note,
    )


@mcp.tool()
def domain_administration(
    days: Annotated[
        int,
        "Days spent holding court and administering the domain (1-14). "
        "3+ days grants +1 roll bonus; 7+ days grants an additional +1.",
    ],
    focus: Annotated[
        str,
        "Focus of the court session: "
        "'general' — petitions, disputes, all quarters (default); "
        "'military' — troop readiness, supply, deployment; "
        "'economic' — guild reports, trade routes, treasury; "
        "'diplomatic' — emissaries, envoys, foreign relations; "
        "'justice' — crimes, punishments, outstanding disputes.",
    ] = "general",
    calendar_note: Annotated[
        str,
        "In-game date after the court session. "
        "If omitted, '+N days' appended to existing calendar.",
    ] = "",
) -> dict:
    """
    Theron holds court and administers the realm of Quasquetan.

    Mechanic: d20 + Charisma modifier + duration bonus (max +2 for 7+ days).
    Result tiers:
    - 18-20 (excellent): NPC loyalty +1; treasury efficiency +10%; petition resolved.
    - 14-17 (good): NPCs satisfied; one intelligence item surfaces through channels.
    - 9-13 (adequate): Routine. No complications.
    - 5-8 (poor): One NPC quietly disgruntled; surfaces as complication later.
    - 1-4 (crisis): Serious dispute erupted; requires follow-up action next session.

    XP: 20 × days × outcome_multiplier (0 for poor/crisis, 1-3 for adequate-excellent).

    Critical for maintaining the loyalty of key NPCs (Aldric, Mira, Fingolfin,
    the Greenreach lords) and troop morale across Theron's extended realm.

    Returns: days_spent, focus, d20_roll, modifier, final_roll, outcome_tier,
    outcome, npc_mood, troop_mood, bonus_effect, xp_awarded, calendar, dm_note.
    """
    return db_domain_administration(
        days=days,
        focus=focus,
        calendar_note=calendar_note,
    )


@mcp.tool()
def recovery(
    injury_description: Annotated[
        str,
        "Brief description of the injury, ailment, or condition being treated "
        "(e.g. 'severe stab wounds', 'mummy rot', 'magical exhaustion after Eye use', "
        "'broken ribs from giant blow').",
    ],
    days_resting: Annotated[
        int,
        "Days of complete bed rest (1-90). "
        "7+ days: minor ailments cleared. "
        "14+ days: moderate ailments cleared. "
        "30+ days: ALL ailments cleared (status_notes reset to null). "
        "HP recovery: 2 HP × character level per week "
        "(vs. 1 HP × level per night for normal rest).",
    ],
    calendar_note: Annotated[
        str,
        "In-game date after recovery, e.g. 'Planting 5, 576 CY'. "
        "If omitted, '+N days' appended to existing calendar.",
    ] = "",
) -> dict:
    """
    Extended rest for serious injuries or magical ailments beyond normal healing.

    Normal rest: 1 HP per character level per night.
    Recovery rest: 2 HP per character level per week (bed rest, no strenuous activity).

    Partial weeks still recover HP at the normal 1/level/day rate.

    Ailment clearing:
    - 7+ days: minor ailments (light wounds, fatigue, light poison)
    - 14+ days: moderate ailments (serious wounds, disease, heavy exhaustion)
    - 30+ days: ALL ailments — status_notes field cleared entirely

    Magical conditions (curses, lycanthropy, charm, energy drain, mummy rot)
    require Remove Curse / Cure Disease / Restoration — bed rest alone cannot
    cure them, but rest IS still required for HP recovery.

    XP: 5 per day (time-cost only — the character is not adventuring).

    Returns: injury_description, days_resting, hp_before, hp_after, hp_recovered,
    ailments_cleared, recovery_note, xp_awarded, calendar, dm_note.
    """
    return db_recovery(
        injury_description=injury_description,
        days_resting=days_resting,
        calendar_note=calendar_note,
    )


@mcp.tool()
def craft_item(
    item_name: Annotated[
        str,
        "Name of the item being crafted (e.g. 'Iron shortbow', "
        "'Scroll of Fireball', 'Antitoxin potion', 'Ring of Feather Falling').",
    ],
    item_type: Annotated[
        str,
        "Category of item being crafted — determines base success chance and minimum time: "
        "'mundane' — standard equipment (90% success, 1+ days); "
        "'masterwork' — exceptional quality mundane item (70% success, 7+ days); "
        "'scroll' — spell scroll (65% success, 3+ days, requires caster); "
        "'potion' — magical potion (60% success, 7+ days, requires caster); "
        "'minor_magic' — minor enchanted item (45% success, 14+ days, requires caster).",
    ],
    materials_gp: Annotated[
        int,
        "Gold pieces spent on materials and components. Deducted from primary treasury. "
        "On failure, half the materials are recovered (refunded).",
    ],
    days: Annotated[
        int,
        "Days spent crafting. Extra days beyond the type minimum add +5% success chance "
        "per additional period equal to the minimum (e.g. for scroll: each extra 3 days = +5%).",
    ],
    calendar_note: Annotated[
        str,
        "In-game date after crafting. If omitted, '+N days' appended to existing calendar.",
    ] = "",
) -> dict:
    """
    Craft a mundane or minor magical item from raw materials.

    Success chance: base% + (Intelligence modifier × 3%) + (extra periods × 5%).
    Capped at 5% minimum / 98% maximum.

    On success:
    - Item is immediately added to the PC's inventory (items + inventory tables)
    - XP awarded: type base XP + materials_gp ÷ 10
    - Calendar advances by days spent

    On failure:
    - Half the materials are lost (half refunded to treasury)
    - No XP, no item
    - Retry is always allowed (start fresh with new materials)

    Item types and use cases:
    - mundane: replacement gear, trade goods, tools
    - masterwork: +1 non-magical to hit/damage (DM ruling), trade at premium
    - scroll: one-shot spell use; caster must know the spell
    - potion: consumable magical effect; requires alchemical knowledge
    - minor_magic: permanent minor enchantment; subject to campaign rulings

    Returns: item_name, item_type, days_spent, materials_gp, success_chance,
    roll, success, item_added, xp_awarded, calendar, note, dm_note.
    """
    return db_craft_item(
        item_name=item_name,
        item_type=item_type,
        materials_gp=materials_gp,
        days=days,
        calendar_note=calendar_note,
    )


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 5C — LOYALTY & AGING SYSTEM
# ══════════════════════════════════════════════════════════════════════════════


@mcp.tool()
def get_loyalty_state() -> dict:
    """
    Return loyalty scores for every named NPC and troop group in the realm.

    On first call, the system auto-initializes loyalty scores from the existing
    relationships and troops tables — no manual setup required. Ruk, Pell,
    Red Eye, Elowen, Gisir, and all other named henchmen are initialized from
    their relationship notes.

    Loyalty score scale (2-12, matching a 2d6 roll):
    - 12: Unshakeable — cannot be bought, intimidated, or broken
    - 10-11: Devoted — will follow into mortal danger without question
    - 8-9: Steadfast — reliable under pressure; holds firm in adversity
    - 6-7: Reliable — performs duties faithfully; wavering only under extreme duress
    - 4-5: Wavering — conditional on success, pay, and good treatment
    - 2-3: At risk — actively looking for a way out or a better offer

    Theron's Charisma 17 grants a +2 bonus to all initial loyalty scores.

    Returns: lists of NPC and troop records, at_risk list, dm_note with
    instructions for running loyalty checks.
    """
    return db_get_loyalty_state()


@mcp.tool()
def loyalty_check(
    entity_name: Annotated[
        str,
        "Name of the NPC or troop group to check (e.g. 'Ruk', 'Pell', "
        "'Quasquetan Goblins'). Partial name match is supported.",
    ],
    situation: Annotated[
        str,
        "Description of the triggering situation, e.g. "
        "'ordered into a dragon's lair', 'wages two weeks late', "
        "'comrade killed on last mission', 'asked to betray a friend'.",
    ],
    modifier: Annotated[
        int,
        "Situational modifier to the 2d6 roll (-3 to +3). "
        "Negative: dangerous/distressing order, poor conditions, fear. "
        "Positive: good pay, recent victory, personal respect for the PC. "
        "Default 0.",
    ] = 0,
    calendar_note: Annotated[
        str,
        "Current in-game date for the event log.",
    ] = "",
) -> dict:
    """
    Roll a 2d6 loyalty check for a specific NPC or troop group.

    Triggered by:
    - Dangerous or morally objectionable orders
    - Unpaid or late wages
    - Mistreatment, public humiliation, or being put in unnecessary danger
    - Deaths of companions or comrades
    - Major realm setbacks (defeats, disasters)
    - Offers from rivals or enemies

    Mechanic: roll 2d6 + modifier, compare to loyalty score.
    - roll ≤ score-2: Strong pass — complete loyalty, no hesitation
    - roll = score-1 or score: Pass — complies, possibly with reluctance
    - roll = score+1: Grumbling — obeys but complains; record the grievance
    - roll = score+2 or +3: Demands — refuses without concession
    - roll > score+3: Desertion risk — loyalty score drops by 1; immediate action needed
    - Natural 12: Always note-worthy, even for loyal characters

    Returns: dice, roll, modifier, adjusted_roll, outcome_tier, consequence,
    score_before/after, passed flag, dm_note.
    """
    return db_loyalty_check(
        entity_name=entity_name,
        situation=situation,
        modifier=modifier,
        calendar_note=calendar_note,
    )


@mcp.tool()
def adjust_loyalty(
    entity_name: Annotated[
        str,
        "Name of the NPC or troop group whose loyalty is being adjusted.",
    ],
    delta: Annotated[
        int,
        "Amount to change the loyalty score. Positive = improve, negative = worsen. "
        "Typical magnitudes: "
        "+1 for a meaningful gift, public praise, or promotion; "
        "+2 for a life-saving act or major favour; "
        "-1 for ignoring a demand, a comrade's death, or broken promise; "
        "-2 for betrayal of trust or public humiliation; "
        "-3 for serious mistreatment (rare).",
    ],
    reason: Annotated[
        str,
        "Narrative reason for the adjustment, e.g. "
        "'Rewarded with magic item after dungeon raid', "
        "'Fellow goblin killed by trap Ruk warned about', "
        "'Promoted to captain of eastern watch'.",
    ],
    calendar_note: Annotated[
        str,
        "Current in-game date for the event log.",
    ] = "",
) -> dict:
    """
    Directly modify a loyalty score based on events in the campaign.

    Call this after:
    - Giving a gift, bonus pay, or magic item
    - Promoting or publicly honouring a henchman
    - A comrade being killed (especially if preventable)
    - A broken promise or ignored demand
    - A major victory the henchman contributed to
    - An act of betrayal or public humiliation

    Score is capped at 2 (minimum) and 12 (maximum).
    All adjustments are logged in the entity's adjustment_history.

    Returns: entity_name, reason, score_before, delta, score_after,
    status_before/after, at_risk flag, dm_note.
    """
    return db_adjust_loyalty(
        entity_name=entity_name,
        delta=delta,
        reason=reason,
        calendar_note=calendar_note,
    )


@mcp.tool()
def henchman_morale_event(
    month_label: Annotated[
        str,
        "Label for the month being resolved, e.g. 'Coldeven 576 CY' or "
        "'Readying — third month of campaign'.",
    ],
    global_modifier: Annotated[
        int,
        "Modifier applied to every henchman's roll this month (-3 to +3). "
        "Positive modifiers: recent victory (+1), wages paid on time (+1), "
        "excellent domain administration (+1), PC present and visible (+1). "
        "Negative modifiers: recent defeat (-1), unpaid wages (-2), "
        "PC absent for extended period (-1), realm under threat (-1). "
        "Stack up to the -3/+3 cap.",
    ] = 0,
    calendar_note: Annotated[
        str,
        "Current in-game date for the event log.",
    ] = "",
) -> dict:
    """
    Monthly morale roll for every named NPC henchman.

    Roll 2d6 + global_modifier for each NPC. Results:
    - 12: Increased devotion (+1 loyalty permanently) — good scene opportunity
    - 10-11: Steady — no change, reliable as always
    - 8-9: Mild grumbling — minor complaint worth noting
    - 6-7: Demands — a specific raise, recognition, or concession required
    - 4-5: Troubled — loyalty drops by 1; something is wrong, investigate
    - 2-3: Crisis — loyalty drops by 1; loyalty_check required or desertion likely

    Call this once per in-game month as part of domain administration.
    The global_modifier captures the overall mood of the realm that month.

    Returns: per-NPC roll reports with event labels, summary of who needs
    attention (demands, at_risk, crisis), and dm_note.
    """
    return db_henchman_morale_event(
        month_label=month_label,
        global_modifier=global_modifier,
        calendar_note=calendar_note,
    )


@mcp.tool()
def advance_time(
    days: Annotated[
        int,
        "Number of in-game days to advance. "
        "Use this for travel (journey complete), downtime periods, "
        "seasonal turns, or any significant time skip. "
        "For very long skips (years), use multiple calls or large day counts.",
    ],
    calendar_note: Annotated[
        str,
        "New in-game date after the time advance, e.g. 'Planting 1, 576 CY'. "
        "If provided, this replaces the calendar entry exactly. "
        "If omitted, '+N days' is appended to the existing entry.",
    ] = "",
) -> dict:
    """
    Advance the campaign calendar and check for aging and obligation triggers.

    This is the canonical time-advancement tool. Use it for:
    - Long dungeon expeditions (days of travel)
    - Downtime between adventures
    - Seasonal domain turns (90 days per season)
    - Any skip of a week or more

    Side effects automatically checked:
    1. PC aging: current age updated from days elapsed. If an age threshold
       is crossed (middle_age / old / venerable), aging_check_needed=True
       is returned and aging_check() should be called immediately.
    2. Religious observances: overdue observances (missed_count >= 3)
       are flagged in overdue_observances.

    Theron Vale is an Elf (starting age ~120). His middle_age threshold is
    350 years — aging will not affect him within a normal campaign. The
    system is fully functional for Human NPCs, Aelric, and future characters.

    Returns: days_advanced, calendar, age_before/after, aging_stage,
    thresholds_crossed, aging_check_needed, overdue_observances, dm_note.
    """
    return db_advance_time(days=days, calendar_note=calendar_note)


@mcp.tool()
def aging_check(
    character_id: Annotated[
        int,
        "character_id of the character crossing an age threshold. "
        "The PC is always character_id=1.",
    ],
    threshold_stage: Annotated[
        str,
        "The aging threshold just crossed: "
        "'middle_age' — Strength -1, Constitution -1, Wisdom +1; "
        "'old' — Strength -2, Dexterity -1, Constitution -1, Wisdom +1; "
        "'venerable' — Strength -1, Dexterity -1, Constitution -1, Wisdom +1. "
        "These are cumulative: a character reaching venerable from young "
        "eventually accumulates Str -4, Dex -2, Con -3, Wis +3 total.",
    ],
) -> dict:
    """
    Apply ability score changes when a character crosses an age threshold.

    Called immediately after advance_time() returns aging_check_needed=True.
    Permanently modifies the character_abilities table in the database.

    Aging effects per AD&D 1e DMG:
    - Middle age: Str -1, Con -1, Wis +1
    - Old:        Str -2, Dex -1, Con -1, Wis +1  (in addition to middle age)
    - Venerable:  Str -1, Dex -1, Con -1, Wis +1  (in addition to old)

    Ability scores cannot drop below 3 from aging.
    Wisdom gains make aged characters useful as advisors even as their
    physical stats decline — classic AD&D design intent.

    Returns: threshold_stage, ability_changes dict (before/after/delta per stat),
    full abilities_before and abilities_after, dm_note.
    """
    return db_aging_check(
        character_id=character_id,
        threshold_stage=threshold_stage,
    )


@mcp.tool()
def get_character_age(
    character_id: Annotated[
        int,
        "character_id of the character to check. PC is always 1.",
    ] = 1,
) -> dict:
    """
    Return the character's current age, race-based thresholds, and time
    to the next aging check.

    Auto-initializes the aging record if it doesn't exist yet.

    For Theron Vale (Elf, character_id=1):
    - Starting age: ~120 years (young adult for an elf)
    - Middle age threshold: 350 years (~230 campaign years away)
    - Natural lifespan max: 1200-2000 years
    - Aging will not be a mechanical concern within a normal campaign

    For human NPCs, henchmen, and characters like Aelric:
    - Middle age: 40 years; Old: 60; Venerable: 90
    - A 30-year campaign with a 10-year-old starting character could reach middle age

    Returns: current_age, race, aging_stage, thresholds dict, thresholds_passed,
    years_to_next_check, natural_lifespan_max, ability_changes_applied, dm_note.
    """
    return db_get_character_age(character_id=character_id)


# ══════════════════════════════════════════════════════════════════════════════
# SIEGE MECHANICS  (Phase 5D)
# ══════════════════════════════════════════════════════════════════════════════


@mcp.tool()
def start_siege(
    target_location: Annotated[
        str,
        "Name of the location being besieged (e.g. 'Quasquetan', 'Iron Marsh Tower').",
    ],
    fortification_type: Annotated[
        str,
        "Type of fortification: palisade, tower, keep, castle, city_walls, "
        "fortified_mill, or fortress. Determines wall/gate hit points and resist rating.",
    ],
    role: Annotated[
        str,
        "PC's side: 'attacker' or 'defender'.",
    ],
    attacker_name: Annotated[
        str,
        "Label for the attacking force (e.g. 'Theron's host', 'Orcish warband').",
    ],
    attacker_count: Annotated[
        int,
        "Effective troop strength of the attacking force.",
    ],
    attacker_supplies: Annotated[
        int,
        "Weeks of supplies the attackers have (each 100 troops consume 1 week/week).",
    ],
    defender_name: Annotated[
        str,
        "Label for the defending garrison (e.g. 'Quasquetan garrison', 'Iron Watch').",
    ],
    defender_count: Annotated[
        int,
        "Effective troop strength of the defending garrison.",
    ],
    defender_supplies: Annotated[
        int,
        "Weeks of supplies the defenders have.",
    ],
    artillery: Annotated[
        list,
        "List of artillery pieces. Each entry is a dict with keys: "
        "name (str), type (str: stone_caster | light_catapult | heavy_catapult | "
        "ballista | trebuchet), condition (str: operational | damaged). "
        "stone_caster entries automatically record Brak, Hurn, and Tollug as crew "
        "with +2 to-hit and +1d6 damage bonus. Pass [] if no artillery.",
    ],
    calendar_note: Annotated[
        str,
        "Campaign date or note for the log (e.g. 'Planting 4, 576 CY').",
    ] = "",
) -> dict:
    """
    Initiate a siege. Overwrites any prior siege state.

    Use this to open a formal siege against a fortified location.
    The fortification_type determines starting wall/gate integrity and resist rating.

    Stone-caster artillery automatically registers Brak, Hurn, and Tollug as crew
    (Theron's trained ogre artillery crew) with +2 to-hit and +1d6 bonus damage.

    After starting the siege, advance it with:
    - siege_turn()       — weekly attrition, disease, sallies, morale
    - artillery_fire()   — daily bombardment to reduce wall/gate integrity
    - assault()          — direct storm of walls or gate
    - negotiate_surrender() — attempt to end the siege diplomatically

    Returns wall/gate starting integrity, both sides' troop counts, and a
    reminder of which tools to call next.
    """
    return db_start_siege(
        target_location=target_location,
        fortification_type=fortification_type,
        role=role,
        attacker_name=attacker_name,
        attacker_count=attacker_count,
        attacker_supplies=attacker_supplies,
        defender_name=defender_name,
        defender_count=defender_count,
        defender_supplies=defender_supplies,
        artillery=artillery,
        calendar_note=calendar_note,
    )


@mcp.tool()
def siege_turn(
    mining: Annotated[
        bool,
        "True if the attacker has sappers actively digging under the walls this week. "
        "Each week of successful mining contributes to eventual wall collapse.",
    ] = False,
    calendar_note: Annotated[
        str,
        "Campaign date or note for the log (e.g. 'Planting 11, 576 CY — Week 2 of siege').",
    ] = "",
) -> dict:
    """
    Resolve one week of siege operations.

    Rolls for all weekly siege events:
    - Supply attrition (each side loses supply_weeks based on troop count)
    - Disease outbreak (1-in-6 per side; 5% casualties if it hits)
    - Defender sally attempt (if morale ≥ 7)
    - Relief force arrival check
    - Morale degradation from casualties and starvation
    - Mining progress (if mining=True)

    Call this once per game week during a siege. Use artillery_fire() between
    turns for daily bombardment, and assault() when ready to storm.

    Returns: week number, supply state, disease outcomes, sally result,
    morale for both sides, mining progress, and any critical events.
    """
    return db_siege_turn(mining=mining, calendar_note=calendar_note)


@mcp.tool()
def artillery_fire(
    engine_name: Annotated[
        str,
        "Name of the artillery piece to fire. Must match a name registered in start_siege(). "
        "For Theron's stone-caster, use 'Stone-caster (ogre-operated)' or the name you gave it.",
    ],
    target: Annotated[
        str,
        "What to target: 'walls' (reduces wall_integrity), 'gate' (reduces gate_integrity), "
        "or 'defenders' (direct casualties against garrison).",
    ] = "walls",
    volleys: Annotated[
        int,
        "Number of shots fired this day. Catapults typically fire 1-3 per day depending "
        "on crew rest. Stone-casters (ogre crew) can sustain 2-3 per day.",
    ] = 1,
    calendar_note: Annotated[
        str,
        "Campaign date or note (e.g. 'Planting 5, 576 CY — Day 2 bombardment').",
    ] = "",
) -> dict:
    """
    Resolve one day of artillery bombardment against a fortified target.

    Each volley rolls THAC0 vs AC 8 (walls/gate). Hit = damage rolled, reduced
    by the fortification's resist rating. The stone-caster operated by Brak,
    Hurn, and Tollug gets +2 to-hit and +1d6 bonus damage per hit.

    When wall_integrity drops below 30, a breach opens (assault without scaling
    ladders becomes possible). When gate_integrity drops below 20, the gate is
    destroyed.

    Returns: per-volley hit/miss/damage breakdown, updated wall/gate integrity,
    breach status, and engine condition (may be damaged by counter-battery fire).
    """
    return db_artillery_fire(
        engine_name=engine_name,
        target=target,
        volleys=volleys,
        calendar_note=calendar_note,
    )


@mcp.tool()
def assault(
    breach_point: Annotated[
        str,
        "Where to assault: 'walls' (requires breach or scaling_ladders=True), "
        "'gate' (requires battering_ram=True or gate_integrity < 20), "
        "or 'breach' (if a breach exists in the walls).",
    ] = "walls",
    waves: Annotated[
        int,
        "Number of assault waves (1-3). More waves = more casualties on both sides, "
        "but higher chance of success. Each wave after the first suffers additional "
        "defender penalties.",
    ] = 1,
    scaling_ladders: Annotated[
        bool,
        "True if attackers are using scaling ladders against intact walls. "
        "Allows assault without a breach but adds +5% attacker casualties per wave.",
    ] = False,
    battering_ram: Annotated[
        bool,
        "True if a battering ram is targeting the gate. Adds +3% attacker casualties "
        "from concentrated defensive fire.",
    ] = False,
    calendar_note: Annotated[
        str,
        "Campaign date or note (e.g. 'Planting 18, 576 CY — The storm begins').",
    ] = "",
) -> dict:
    """
    Resolve a direct assault on the fortification's walls, gate, or breach.

    Requires at least one of: an open breach (wall_integrity < 30),
    scaling_ladders=True, or battering_ram=True (gate assault).

    Each wave rolls attacker vs defender casualties based on fort type, wall
    condition, troop numbers, and assault method. Success is determined by
    whether attackers break through before their morale collapses.

    Returns: wave-by-wave casualty reports, assault outcome (success/repelled),
    defender morale impact, and any gate/breach changes.
    """
    return db_assault(
        breach_point=breach_point,
        waves=waves,
        scaling_ladders=scaling_ladders,
        battering_ram=battering_ram,
        calendar_note=calendar_note,
    )


@mcp.tool()
def get_siege_state() -> dict:
    """
    Return the full current siege status.

    Returns a complete snapshot of the active siege including:
    - Wall and gate integrity percentages
    - Both sides: troop count, casualties, strength %, supply weeks, morale
    - Breach points (if any)
    - Whether the last assault was repelled
    - Last 5 siege events log
    - Artillery pieces (name, type, condition, shots fired, crew)

    Returns an error dict if no siege is currently active.

    Call this at the start of each siege-related scene to orient the DM narration.
    """
    return db_get_siege_state()


@mcp.tool()
def negotiate_surrender(
    terms_offered: Annotated[
        str,
        "Brief description of the terms being proposed to the defender "
        "(e.g. 'Quarter for all, garrison may leave with swords', "
        "'Surrender weapons and swear fealty', 'Unconditional — no quarter'). "
        "The terms should reflect the PC's intent; the roll determines acceptance.",
    ],
    calendar_note: Annotated[
        str,
        "Campaign date or note (e.g. 'Planting 25, 576 CY — Parley under truce flag').",
    ] = "",
) -> dict:
    """
    Attempt to negotiate the defender's surrender.

    Rolls d20 + modifiers to determine whether the defender accepts terms.
    Modifiers account for: attacker numerical superiority, wall/gate damage,
    defender supply state, defender morale, and PC Charisma.

    Result tiers (Jeff Rients-style):
    - 20+: unconditional surrender (siege ends)
    - 16-19: honourable terms accepted (siege ends)
    - 12-15: hard terms — defender stalls, negotiating for time
    - 8-11: terms refused
    - 4-7: offer insulted — defender morale briefly rises
    - 1-3: betrayal — ambush or attack under the truce flag

    Returns: roll breakdown, modifier notes, result tier, and DM guidance
    for resolving the outcome. If siege_ends=True, call end_combat/update_troop_count
    to close out the engagement.
    """
    return db_negotiate_surrender(
        terms_offered=terms_offered,
        calendar_note=calendar_note,
    )


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    mcp.run()
