# greyhawk-solo

A single-player AD&D 1st Edition engine for **Claude Desktop**. Claude acts as
your Dungeon Master. A local MCP server handles all mechanics — dice, THAC0,
saving throws, HP, treasure — so Claude focuses entirely on narration.

Game state persists in a SQLite database. Pick up any session exactly where
you left off.

---

## What This Is

- **You type actions in Claude Desktop chat.** "I examine the door." "I attack the orc."
- **Claude narrates the scene** and calls engine tools for every mechanical outcome.
- **The engine rolls dice, tracks HP, updates the DB.** Claude never fakes a result.
- **Full AD&D 1e rules:** THAC0, five saving throw categories, class-based HD, OSRIC tables.

Designed for long-running solo campaigns with persistent domain management
(keep, troops, treasury, construction projects) as well as standard dungeon crawling.

See [INSTALL.md](INSTALL.md) for step-by-step setup instructions.

---

## Requirements

| Requirement | Notes |
|---|---|
| [Claude Desktop](https://claude.ai/download) | The chat client that connects to the MCP server |
| Python 3.11+ | 3.12 or 3.13 recommended |
| [mcp](https://pypi.org/project/mcp/) 1.27+ | `pip install "mcp[cli]"` |
| SQLite 3.35+ | Bundled with Python — no separate install needed |

No other dependencies. No API keys. No web server.

---

## Quick Start

```bash
# 1. Clone and install
git clone https://github.com/Diogenes187/greyhawk-solo.git
cd greyhawk-solo
pip install "mcp[cli]"

# 2. Create your character (interactive — rolls dice, writes saves/<name>.db)
python create_character.py

# 3. Configure Claude Desktop — see INSTALL.md for the config block

# 4. Restart Claude Desktop and open a new chat
```

Then say:
> *Start a new session. Load my character state and tell me where I am.*

---

## Installation

### 1. Clone the repo

```bash
git clone https://github.com/Diogenes187/greyhawk-solo.git
cd greyhawk-solo
```

### 2. Install the MCP library

```bash
pip install "mcp[cli]"
```

If you use a virtual environment (recommended), activate it first — remember the
path to its `python` executable, you'll need it in step 4.

### 3. Create your character

Run the interactive character creation script:

```bash
python create_character.py
```

The script will:
- Ask for your character's name
- Let you choose a rolling method: **5d6 keep best 3** (recommended) or **4d6 drop lowest** (classic)
- Roll all six ability scores with a full dice breakdown
- Show viable races and classes for your scores
- Let you reroll as many times as you like, with side-by-side comparison to the previous roll
- Apply racial modifiers and display the full character sheet
- On confirmation, write `saves/<name>.db` and update `config.json`

**Optional — load full reference data**

The creation script builds a minimal database (schema only). To add the complete
AD&D 1e reference tables — 121 monsters, 401 spells, saving throw tables, combat
matrices, treasure types, magic items — run:

```bash
sqlite3 saves/<your_character>.db < schema/starter.sql
```

This is optional for play but enables mechanical lookups during sessions.

**Prefer manual setup?** Fill in `schema/new_character_template.sql` and apply it
to a database. See the template file for instructions.

### 4. Register the MCP server with Claude Desktop

Open your Claude Desktop config file:

- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`

Add the `mcpServers` block (create the file if it doesn't exist):

```json
{
  "mcpServers": {
    "greyhawk-solo": {
      "command": "C:\\Path\\To\\python.exe",
      "args": [
        "C:\\Path\\To\\greyhawk-solo\\server\\mcp_server.py"
      ]
    }
  }
}
```

Replace both paths with your actual Python executable and repo location.

**To find your Python path:**

```bash
# Windows (PowerShell or cmd)
where python

# macOS / Linux
which python3
```

**Fully quit and relaunch Claude Desktop** after saving the config. Look for the
hammer icon in the chat input bar — the greyhawk-solo tools will be listed.

See [INSTALL.md](INSTALL.md) for detailed path examples and venv setup.

### 5. Start playing

Open a new chat in Claude Desktop and say:

> *Start a new session. Load my character state and tell me where I am.*

Claude will call `get_character_state`, `get_current_scene`, and
`get_recent_history`, then set the scene. From there, just play.

**Resuming after a break:**

> *Resume my campaign. Catch me up on where things stood.*

---

## Switching Campaigns

If you have multiple characters, run:

```bash
python switch_character.py
```

It reads each database in `saves/`, shows the character name, class, level, and
realm name, then writes your choice to `config.json`. Restart Claude Desktop
after switching.

---

## Creating a Character via Claude

You can also create a new character entirely in chat. Tell Claude:

> *I want to create a new character. Help me roll stats and pick a race and class.*

Claude will guide you through the process using the `roll_dice` tool for all
rolls, then call `create_character` to commit the final choices to a new database.

---

## MCP Tools

Claude has access to 27 tools automatically:

### Session
| Tool | What it does |
|---|---|
| `session_start` | One-call briefing: character state + current scene + last 10 turns + pending updates. Call this first, every session. |

### Read
| Tool | What it returns |
|---|---|
| `get_character_state` | Full PC stats, HP, AC, ability scores, inventory |
| `get_realm_state` | All locations, troops, treasury, livestock, key NPCs |
| `get_current_scene` | Current location, last player action, last DM response |
| `get_recent_history` | Last N turns (default 5, max 20) |
| `get_pending_updates` | Turns with state_changes notes not yet committed to DB |
| `roll_dice` | Any NdS+M expression — 1d20, 3d6+2, 7d6, etc. |

### Write
| Tool | What it does |
|---|---|
| `save_turn` | Appends player action + DM narrative to the log; returns a reminder to call `update_world_fact` / `add_npc` / `add_item` for anything new this turn |
| `update_character_status` | Changes HP, AC, or status notes |
| `update_treasury` | Adds or subtracts coins (with overdraft protection) |
| `add_location` | Records a new location in the realm |
| `update_location_status` | Changes a location's status or notes |
| `update_troop_count` | Sets or adjusts troop headcount |
| `add_troop_group` | Adds a new troop unit |
| `add_item` | Creates an item and assigns it to inventory |
| `update_world_fact` | Records a campaign fact (quests, weather, rulings) |
| `update_npc` | Updates an NPC's notes, type, or alignment |
| `add_npc` | Adds a newly encountered NPC to the database |

### Combat
| Tool | What it does |
|---|---|
| `start_combat` | Initialise an encounter: looks up monster stats, rolls HP for each individual, rolls initiative (d10 + DEX mod for PC), builds full turn order |
| `get_combat_state` | Current round, initiative order, HP and AC for every combatant |
| `attack` | Resolve one attack: THAC0 vs AC lookup, damage roll, HP update, morale check; handles PC and monster attacks |
| `end_combat` | Close the encounter, award XP, clear combat state |

### Spells
| Tool | What it does |
|---|---|
| `get_spell_slots` | Show today's memorized spell list and which slots are expended |
| `memorize_spells` | Set the memorized spell list for the day (after a long rest) |
| `cast_spell` | Expend a memorized slot and return full spell description + mechanical reminders |
| `rest` | Long rest (8h): restore all spell slots, recover 1 HP/level, advance calendar. Short rest (1h): advance time only |

### Character Setup
| Tool | What it does |
|---|---|
| `create_character` | Finalises confirmed stat choices, creates `saves/<name>.db`, updates `config.json` |

---

## Character Creation (Python API)

The `CharacterSheet` class in `engine/character.py` exposes the full creation API:

```python
from engine.character import CharacterSheet

sheet = CharacterSheet()

# Roll ability scores — 5d6 keep best 3 (default) or 4d6 drop lowest
scores = sheet.roll_ability_scores("5d6")

# Apply race (adjusts scores for racial modifiers)
sheet.apply_race("Human")

# Apply class (loads THAC0, saves, XP table, HD type)
sheet.apply_class("Fighter")

# Roll HP, calculate AC, derive THAC0 and saves
sheet.calculate_derived_stats()

sheet.name = "Aldric"
sheet.alignment = "Neutral Good"
sheet.roll_starting_gold()

print(sheet.display())  # formatted character sheet
```

**Supported races:** `Human`, `Elf`, `Half-Elf`, `Dwarf`, `Halfling`, `Half-Orc`, `Gnome`

**Supported classes:** `Fighter`, `Cleric`, `Magic-User`, `Thief`

**Rolling methods:**
- `"5d6"` — roll 5d6, keep best 3 (default, recommended)
- `"4d6"` — roll 4d6, drop lowest (classic alternative)

---

## File Layout

```
greyhawk-solo/
├── data/
│   ├── classes.json          # THAC0, saves, XP, HD by class and level (1-20)
│   └── races.json            # Racial ability modifiers and level limits
├── engine/
│   ├── character.py          # CharacterSheet: creation, leveling, JSON save/load
│   └── db.py                 # All SQLite access (zero SQL in server layer)
├── schema/
│   ├── starter.sql           # Full DDL + AD&D 1e reference data; apply once per DB
│   ├── ddl.sql               # DDL only — used internally for fast new-DB creation
│   └── new_character_template.sql  # Manual fill-in-the-blanks alternative
├── server/
│   └── mcp_server.py         # FastMCP server; 27 tools; connects to Claude Desktop
├── saves/                    # Your campaign DB goes here (git-ignored)
│   └── .gitkeep
├── create_character.py       # Interactive character creation CLI
├── switch_character.py       # Switch active campaign database
├── test_character.py         # Engine smoke test: rolls a character and prints the sheet
├── BLUEPRINT.md              # Architecture and design decisions
├── INSTALL.md                # Detailed setup guide
├── LICENSE                   # MIT
└── README.md                 # This file
```

---

## Troubleshooting

**Tools don't appear in Claude Desktop**

Fully quit Claude Desktop (system tray on Windows, menu bar on macOS) and
relaunch. A window close is not a full quit.

**`ModuleNotFoundError: No module named 'mcp'`**

Install the library: `pip install "mcp[cli]"`. If you're using a venv, make
sure the `python` path in your config points to the venv Python executable.

**`sqlite3.OperationalError: no such table`**

Run `python create_character.py` to create a fresh database, or apply
`schema/starter.sql` to an existing one.

**Character not found / wrong campaign**

Check `config.json` in the project root:
```json
{ "active_campaign_db": "saves/yourname.db" }
```
If it points to the wrong file, run `python switch_character.py` to update it.

**Verify the server starts cleanly**

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}}}
{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' | python server/mcp_server.py
```

You should see 27 tools listed in the response.

---

## Design Philosophy

The core rule: **Claude narrates, the engine decides.**

Claude never invents a dice result, adjusts HP without calling the tool, or
contradicts what's in the database. Every mechanical event — attack roll,
saving throw, treasure haul, troop casualty — flows through the MCP tools.
This keeps the campaign internally consistent across sessions, even when
Claude has no memory of what happened three weeks ago.

See `BLUEPRINT.md` for the full architecture and phase roadmap.

---

## License

MIT. See `LICENSE`.

AD&D 1e mechanical tables are derived from
[OSRIC](http://osric.us/) (Old School Reference and Index Compilation),
which is released under the Open Game License v1.0a.
This project is not affiliated with Wizards of the Coast.
