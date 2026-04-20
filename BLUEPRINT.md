# greyhawk-solo — Design Blueprint

A single-player AD&D 1e engine for Claude Desktop. Claude acts as DM;
a local MCP server handles all mechanics and persistent state.

---

## Design Principles

1. **Claude narrates, the engine decides.** Claude never invents dice results or
   modifies HP. Every mechanical outcome goes through the MCP tools.

2. **The database is the source of truth.** All game state lives in a single
   SQLite file. If it isn't in the DB, it didn't happen.

3. **Zero SQL in the server layer.** `server/mcp_server.py` calls functions from
   `engine/db.py`. All queries live in `db.py`.

4. **New characters are isolated.** Each character gets their own `saves/<name>.db`.
   Creation never touches any existing campaign database. `config.json` is updated
   to point at the new DB only after the player confirms their choices.

5. **OSR rules, no house rules by default.** All tables (THAC0, saves, XP, HD)
   follow AD&D 1e / OSRIC exactly. Deviations are explicit, documented, and
   stored in the `world_facts` table as `runtime_dm_behavior` entries.

---

## Architecture

```
Claude Desktop (chat)
       |
       | MCP / stdio
       v
server/mcp_server.py    -- 18 MCP tools; no SQL; no game logic
       |
       v
engine/db.py            -- All SQLite queries; read/write campaign DB
       |
       v
saves/<campaign>.db     -- SQLite; WAL mode; single file per campaign
```

Character creation has two paths — interactive CLI and in-chat MCP tool:

```
create_character.py     -- Interactive CLI; rolls dice, prompts for choices
       |                   (method: 5d6 keep best 3 or 4d6 drop lowest)
       v
engine/db.py            -- create_character_db(): validate → derive → write DB
       |
       v
saves/<name>.db         -- Fresh campaign DB; config.json updated automatically
```

```
Claude Desktop (chat)   -- Player rolls via roll_dice, confirms in conversation
       |
       v
create_character (MCP)  -- Commit step only: takes confirmed scores, same db path
```

---

## Project Layout

```
greyhawk-solo/
├── data/
│   ├── classes.json          # Fighter, Cleric, Magic-User, Thief — full 1-20 tables
│   └── races.json            # Human, Elf, Half-Elf, Dwarf, Halfling, Half-Orc, Gnome
├── engine/
│   ├── character.py          # CharacterSheet class — creation, leveling, JSON save/load
│   └── db.py                 # All DB access functions; create_character_db()
├── schema/
│   ├── starter.sql           # Full DDL + AD&D reference data; apply once per campaign DB
│   ├── ddl.sql               # DDL only — used by create_character_db() for fast DB init
│   └── new_character_template.sql  # Manual fill-in-the-blanks PC setup
├── server/
│   └── mcp_server.py         # FastMCP server; 49 tools
├── saves/                    # git-ignored — campaign DBs live here
│   └── .gitkeep
├── create_character.py       # Interactive character creation CLI
├── switch_character.py       # Switch active campaign via config.json
├── test_character.py         # Engine smoke test
├── BLUEPRINT.md              # This file
├── INSTALL.md                # Step-by-step setup guide
├── LICENSE                   # MIT
└── README.md                 # Overview and play guide
```

---

## Database Schema (key tables)

### Campaign scaffold
| Table | Purpose |
|---|---|
| `campaigns` | One row per campaign; `campaign_id=1` is the default |
| `sessions` | Numbered play sessions with dates |
| `ai_turns` | Append-only log of every player action + DM response |
| `current_scene_state` | Singleton: where we are right now |
| `world_facts` | Free-text facts by category: quests, weather, rulings |

### Character
| Table | Purpose |
|---|---|
| `characters` | All characters (PC + NPCs); `character_id=1` = PC |
| `class_levels` | One row per class per character |
| `character_status` | Mutable: HP, AC, movement, status notes |
| `character_abilities` | Ability scores (post-racial-modifier) |
| `character_spells` | Known/memorized spells per character |

### Realm
| Table | Purpose |
|---|---|
| `locations` | Named places with type, status, parent |
| `troops` | Troop groups with count, type, location, commander |
| `treasury_accounts` | Named coin accounts: GP, SP, CP, PP, gems |
| `inventory` / `items` | Items owned by character, location, or treasury |
| `livestock` | Animal counts by type and location |
| `projects` | Construction, diplomacy, and other domain projects |
| `relationships` | Directed edges between characters |

### AD&D 1e reference (read-only)
| Table | Purpose |
|---|---|
| `monsters` | Monster stat blocks |
| `spells` | Full spell catalog (MU and Cleric) |
| `saving_throw_tables` / `saving_throw_entries` | Saves by class and level band |
| `combat_attack_matrices` / `combat_attack_matrix_entries` | THAC0 vs AC lookup |
| `treasure_types` | Standard A-Z treasure type tables |
| `magic_items` | Named magic item catalog |
| `adnd_1e_gems_jewelry` | Gem/jewelry generation tables |

---

## MCP Tools

### Session tool

| Tool | Description |
|---|---|
| `session_start` | One-call briefing: character + scene + last 10 turns + pending updates. First call every session. Includes startup checklist in `briefing_notes`. |

### Read tools

| Tool | Description |
|---|---|
| `get_character_state` | Full PC stats, abilities, inventory |
| `get_realm_state` | Locations, troops, treasury, NPCs |
| `get_current_scene` | Current location + last turn context |
| `get_recent_history` | Last N turns from `ai_turns` |
| `get_pending_updates` | Turns with state_changes not yet DB-committed |
| `roll_dice` | Any NdS+M expression; never fake a result |

### Write tools

| Tool | Description |
|---|---|
| `save_turn` | Persist player action + DM narrative; update scene. Response includes `world_fact_reminder` prompting immediate `update_world_fact` / `add_npc` / `add_item` calls for anything new this turn. |
| `update_character_status` | Change HP, AC, status notes |
| `update_treasury` | Add/subtract coins with overdraft protection |
| `add_location` | Add a new location to the realm |
| `update_location_status` | Change status/notes on an existing location |
| `update_troop_count` | Set or adjust troop headcount |
| `add_troop_group` | Add a new troop unit |
| `add_item` | Create an item and assign to inventory |
| `update_world_fact` | Upsert a campaign fact (quests, weather, rulings) |
| `update_npc` | Update NPC notes, type, alignment |
| `add_npc` | Add a newly encountered NPC |

### Travel & Weather tools

| Tool | Description |
|---|---|
| `start_travel` | Begin a journey: origin, destination, terrain path, mount type; returns full travel plan with estimated days, food/water requirements |
| `travel_turn` | Resolve one day of travel: apply weather movement modifier, roll terrain encounter, track miles/resources, return day report |
| `get_travel_state` | Current journey status: miles remaining, days elapsed, current terrain, estimated days to destination |
| `get_lost` | Trigger a lost event: roll direction error and wander distance, return reorientation time and instructions |
| `generate_weather` | Roll a full daily weather result for the given season and region: precipitation, wind, temperature, visibility, movement modifier, survival check flag |
| `get_current_weather` | Return today's weather and the 3-day forecast stored in world_facts |

### Carousing & Downtime tools

| Tool | Description |
|---|---|
| `carouse` | Spend gold in taverns; XP = GP spent (always); roll d20 + spend-tier bonus for consequence (20-entry Jeff Rients table); logs to downtime_log |
| `research_spell` | Magic-User researches or copies a spell; success = 45% base + INT mod×5% + extra weeks×5%; XP on success = 100×level |
| `gather_rumors` | Spend days and coin in a settlement; quality tier 1-4 by time/gold/Cha; results stored in world_facts category='rumors' |
| `religious_observance` | Cleric performs weekly/holy_day/atonement/major_ritual; tracks missed_count and bonuses; 3 missed = lose highest spell level |
| `domain_administration` | Hold court 1-14 days; d20 + Cha mod + duration bonus → outcome tier affects NPC loyalty and troop morale |
| `recovery` | Extended bed rest: 2 HP × level per week; 7d = minor ailments cleared; 30d = all ailments (status_notes) reset |
| `craft_item` | Craft mundane/masterwork/scroll/potion/minor_magic; success = base% + INT×3% + extra periods×5%; item added to inventory on success |

### Character setup

| Tool | Description |
|---|---|
| `create_character` | Commit confirmed name/race/class/scores to a new `saves/<name>.db`; updates `config.json` |

---

## Character Creation (engine/character.py)

```python
from engine.character import CharacterSheet

sheet = CharacterSheet()
sheet.roll_ability_scores("5d6")   # or "4d6" for classic alternative
sheet.apply_race("Human")
sheet.apply_class("Fighter")
sheet.calculate_derived_stats()
sheet.name = "Aldric"
path = sheet.save("aldric")        # -> saves/aldric.json
```

Supported rolling methods (AD&D 1e / OSRIC only):
- `"5d6"` — roll 5d6, keep best 3 (default, recommended)
- `"4d6"` — roll 4d6, drop lowest (classic alternative)

---

## Dice Expression Format

The `roll_dice` tool accepts standard NdS+M notation:

| Expression | Meaning |
|---|---|
| `d20` | 1d20, no modifier |
| `1d20` | Same |
| `3d6` | Three six-sided dice |
| `3d6+2` | Three d6 plus 2 |
| `2d8-1` | Two d8 minus 1 |
| `7d6` | Fireball damage |

---

## Save File Format (JSON, new characters)

```json
{
  "version": "0.1",
  "character": {
    "name": "Aldric",
    "race": "Human",
    "class": "Fighter",
    "level": 1,
    "xp": 0,
    "xp_next_level": 2000,
    "ability_scores": { "str": 16, "int": 12, "wis": 10, "dex": 14, "con": 15, "cha": 13 },
    "hp": { "current": 9, "max": 9 },
    "thac0": 20,
    "saving_throws": { "death": 14, "wands": 16, "paralysis": 15, "breath": 17, "spells": 17 },
    "ac": 10,
    "inventory": [],
    "gold": 90.0,
    "spells": {},
    "conditions": []
  },
  "world": {
    "calendar_date": "Fireseek 1, 576 CY",
    "location": { "hex": "D8", "region": "Wild Coast", "place": "Safeton" },
    "explored_hexes": ["D8"],
    "known_locations": {},
    "known_npcs": [],
    "faction_standing": {},
    "recent_events": []
  },
  "domain": null,
  "log": []
}
```

---

## AD&D 1e Mechanics Reference

### THAC0 by Class and Level

| Class | Level 1 | Level 5 | Level 9 | Level 13 |
|---|---|---|---|---|
| Fighter | 20 | 16 | 13 | 10 |
| Cleric | 20 | 18 | 16 | 13 |
| Magic-User | 20 | 20 | 17 | 15 |
| Thief | 20 | 18 | 16 | 14 |

To hit: roll 1d20, result must equal or exceed (THAC0 - target AC).

### Saving Throw Categories

1. **Death / Poison**
2. **Wands**
3. **Paralysis / Petrification**
4. **Breath Weapons**
5. **Spells / Staves / Rods**

Roll 1d20; meet or beat the listed number to save.

### Ability Score Modifiers (OSRIC)

**Strength** (to-hit / damage / open doors d6 / bend bars %):
- 3: -3/-1/1/0 &nbsp; 8-9: 0/0/1/1 &nbsp; 16: 0/+1/3/10 &nbsp; 17: +1/+1/3/13 &nbsp; 18: +1/+2/3/16

**Constitution** (HP modifier per die):
- 3-6: -1 to -3 &nbsp; 7-14: 0 &nbsp; 15: +1 &nbsp; 16: +2 &nbsp; 17: +3 &nbsp; 18: +4

**Dexterity** (AC modifier, lower is better):
- 3: +4 &nbsp; 7-14: 0 &nbsp; 15: -1 &nbsp; 16: -2 &nbsp; 17: -3 &nbsp; 18: -4

---

## Phase Roadmap

| Phase | Status | Scope |
|---|---|---|
| Phase 1 — Core Loop | **Complete** | Character engine, DB layer, 19 MCP tools, interactive CLI |
| Phase 2 — Combat & Spells | **Complete** | Combat tracker, initiative, THAC0 attack matrix, morale, XP; spell memorization, casting, long/short rest |
| Phase 3 — Dungeon | **Complete** | Random encounter tables (d20→table, d100→monster), wandering monster check with turn counter, full treasure generation A–Z |
| Phase 4 — Domain | **Complete** | Seasonal turns, per-holding income rolls, troop upkeep, construction queue with automatic week-tracking, d20 realm events table (36 tools total) |
| Phase 5A — Travel & Weather | **Complete** | Hex-crawl travel with terrain movement rates, mount types, get_lost; daily weather by season with precipitation/wind/visibility/movement modifiers; world_facts JSON persistence (42 tools total) |
| Phase 5B — Carousing & Downtime | **Complete** | Jeff Rients d20 carousing table (XP=GP, 20 consequences); spell research (INT+time+gold formula); rumour gathering by quality tier 1-4; religious observance with missed-penalty/bonus tracking; domain administration court rolls; extended recovery; crafting (mundane/scroll/potion/minor magic) — all log to downtime_log world_fact (49 tools total) |
