# Installation Guide

Step-by-step setup for greyhawk-solo. Assumes you are comfortable with a
terminal and have Python installed. Takes about 10 minutes.

---

## Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.11+ (3.13 recommended) | [python.org](https://python.org/downloads) |
| Claude Desktop | Latest | [claude.ai/download](https://claude.ai/download) |
| Git | Any | For cloning |
| SQLite | 3.35+ | Bundled with Python — no separate install |

---

## Step 1 — Clone the repo

```bash
git clone https://github.com/Diogenes187/greyhawk-solo.git
cd greyhawk-solo
```

---

## Step 2 — Install the MCP library

There is one Python dependency: the MCP library.

**Without a virtual environment:**

```bash
pip install "mcp[cli]"
```

**With a virtual environment (recommended — keeps your system Python clean):**

```bash
# Create the venv inside the project folder
python -m venv .venv

# Activate it
# Windows (PowerShell)
.venv\Scripts\Activate.ps1

# Windows (cmd)
.venv\Scripts\activate.bat

# macOS / Linux
source .venv/bin/activate

# Install
pip install "mcp[cli]"
```

Note the full path to your Python executable — you will need it in Step 4.

**To find your Python path:**

```bash
# Windows (PowerShell or cmd)
where python

# With a venv active (Windows)
where python
# → C:\path\to\greyhawk-solo\.venv\Scripts\python.exe

# macOS / Linux
which python3

# With a venv active (macOS / Linux)
which python
# → /path/to/greyhawk-solo/.venv/bin/python
```

---

## Step 3 — Create your character

Run the interactive character creation script:

```bash
python create_character.py
```

The script will walk you through:

1. **Character name** — used as the filename (`saves/<name>.db`)
2. **Rolling method:**
   - `5d6 keep best 3` — recommended; higher average scores
   - `4d6 drop lowest` — classic AD&D alternative
3. **Roll and review** — all six ability scores shown with dice breakdown and
   modifier annotations; reroll as many times as you like; previous scores
   shown alongside for comparison
4. **Race and class selection** — only viable options shown based on your scores;
   level caps noted for demi-human races
5. **Character sheet preview** — final scores after racial modifiers, HP, AC,
   THAC0, all five saving throws
6. **Confirm** — on `yes`, writes `saves/<name>.db` and updates `config.json`

The script handles everything. When it finishes you will see:

```
  Aldric is ready.
  Restart Claude Desktop, then open a new chat and say:
  "Start a new campaign with Aldric. Load my character state."
```

### Optional — add full reference data

`create_character.py` creates a minimal database (schema + character data).
To add the complete AD&D 1e reference tables — 121 monsters, 401 spells,
saving throw tables, combat attack matrices, treasure types, magic items — run:

```bash
sqlite3 saves/<your_character>.db < schema/starter.sql
```

This is optional. Claude can narrate without the tables, but having them
enables mechanical lookups (random encounter generation, treasure rolls, etc.)
during play.

> **Note:** `starter.sql` is large and uses WAL mode. On Windows the load takes
> a few seconds. If it hangs, the bundled `schema/ddl.sql` (schema only, no
> reference data) is what the creation script uses internally.

### Manual setup (alternative)

If you prefer not to use the interactive script:

1. Create a blank database from the starter schema:
   ```bash
   sqlite3 saves/my_campaign.db < schema/starter.sql
   ```
2. Open `schema/new_character_template.sql` and fill in every `<<LIKE_THIS>>`
   placeholder with your character's details.
3. Apply it to your database:
   ```bash
   sqlite3 saves/my_campaign.db < schema/new_character_template.sql
   ```
4. Manually edit `config.json` to point at your database:
   ```json
   { "active_campaign_db": "saves/my_campaign.db" }
   ```

---

## Step 4 — Configure Claude Desktop

Locate your Claude Desktop configuration file:

| OS | Path |
|---|---|
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |

Open it in a text editor (create it if it doesn't exist). Add the
`mcpServers` block, substituting your actual paths:

**Windows example:**

```json
{
  "mcpServers": {
    "greyhawk-solo": {
      "command": "C:\\Users\\YourName\\greyhawk-solo\\.venv\\Scripts\\python.exe",
      "args": [
        "C:\\Users\\YourName\\greyhawk-solo\\server\\mcp_server.py"
      ]
    }
  }
}
```

**macOS / Linux example:**

```json
{
  "mcpServers": {
    "greyhawk-solo": {
      "command": "/Users/yourname/greyhawk-solo/.venv/bin/python",
      "args": [
        "/Users/yourname/greyhawk-solo/server/mcp_server.py"
      ]
    }
  }
}
```

**If you have other MCP servers already configured**, add `greyhawk-solo` as
a new entry inside the existing `mcpServers` object — do not replace the
entire file.

> **Windows path escaping:** JSON requires backslashes to be doubled:
> `C:\\Users\\...` not `C:\Users\...`

---

## Step 5 — Restart Claude Desktop

Claude Desktop must be fully quit and relaunched — closing the window is not
enough.

- **Windows:** Right-click the system tray icon → **Quit**
- **macOS:** Click the menu bar icon → **Quit Claude**

After relaunching, open a new chat. Click the tools icon (🔨) in the input bar
and confirm **greyhawk-solo** is listed with **30 tools**.

---

## Step 6 — First session

Open a new chat and say:

> *Start a new campaign with [character name]. Load my character state and tell me where I am.*

Claude will call `get_character_state`, `get_current_scene`, and
`get_recent_history`, then open the first scene. From there, just describe
what your character does.

**Resuming an existing campaign:**

> *Resume my campaign. Catch me up on where things stood.*

---

## Switching Campaigns

If you have multiple characters in `saves/`, run:

```bash
python switch_character.py
```

The script lists every database found in `saves/`, shows the character name,
class, level, and realm name for each, and lets you pick one by number. It
updates `config.json` and tells you to restart Claude Desktop.

---

## Verifying the Server

Test the MCP server from the terminal before connecting Claude Desktop:

```bash
printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}}}\n{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n' | python server/mcp_server.py
```

You should see a JSON response listing 30 tools. If you get an import error,
check that the virtual environment is activated and `mcp` is installed.

---

## Troubleshooting

**Tools don't appear in Claude Desktop after restart**

Make sure you fully quit Claude Desktop from the system tray (Windows) or menu
bar (macOS), not just closed the window. Then relaunch fresh.

---

**`ModuleNotFoundError: No module named 'mcp'`**

The MCP library isn't installed in the Python the config points to.

- Run `pip install "mcp[cli]"` in the right environment.
- If using a venv, make sure the `command` in your Claude config points to the
  venv Python, not the system Python.

---

**`sqlite3.OperationalError: no such table`**

The database doesn't have the schema applied. Run `python create_character.py`
to create a fresh database, or apply `schema/starter.sql` manually.

---

**Wrong character loads in Claude**

Check `config.json` in the project root:

```json
{ "active_campaign_db": "saves/yourname.db" }
```

If it points to the wrong file, run `python switch_character.py` and restart
Claude Desktop.

---

**`config.json` not found**

`create_character.py` writes this file automatically when you confirm a
character. If it's missing (e.g. on a fresh clone), create it manually:

```json
{ "active_campaign_db": "saves/your_character.db" }
```

Or run `python create_character.py` or `python switch_character.py` to
generate it.

---

**Character not found in database**

Verify the database has `campaign_id=1` and `character_id=1` rows. These are
created by `create_character.py` and `new_character_template.sql`. Query
quickly with:

```bash
sqlite3 saves/yourname.db "SELECT name, race FROM characters WHERE character_id=1;"
```

---

**Server starts but Claude can't connect**

- Confirm the `command` path in your Claude config is the correct Python
  executable (prints no error when run standalone).
- Confirm the `args` path to `mcp_server.py` is correct and the file exists.
- Check Claude Desktop logs:
  - Windows: `%APPDATA%\Claude\logs\`
  - macOS: `~/Library/Logs/Claude/`
