"""
test_npc_fixes.py — verifies the three live-gameplay bug fixes.

Bug 1 (markers special-character investigation): every special-character
   variant survives the in-process MCP call as a list. The drop happens at
   the JSON-RPC stdio transport layer, not in our handler. The markers_str
   pipe-delimited workaround already covers it. Documented via
   test_special_chars_survive_in_process.

Bug 2 (add_npc notes binding): aligns with update_npc — notes is now
   `str | None = None`, written verbatim when provided, NULL when omitted.

Bug 3 (update_npc_class clobbering notes): the tool no longer accepts a
   notes parameter at all; characters.notes is left untouched on every
   call. Notes changes must flow through update_npc.

Runs against a TEMPORARY SQLite database. Never touches any campaign DB.

    python test_npc_fixes.py
"""
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))


# ──────────────────────────────────────────────────────────────────────────────
# Test fixture — a minimal SQLite DB with the tables these fixes touch.
# ──────────────────────────────────────────────────────────────────────────────

def _build_test_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE characters (
            character_id INTEGER PRIMARY KEY, campaign_id INTEGER,
            name TEXT, character_type TEXT, race TEXT, alignment TEXT, notes TEXT
        );
        CREATE TABLE character_status (
            character_id INTEGER PRIMARY KEY,
            hp_current INTEGER, hp_max INTEGER, ac INTEGER,
            movement INTEGER, attacks_per_round REAL, status_notes TEXT
        );
        CREATE TABLE class_levels (
            class_level_id INTEGER PRIMARY KEY,
            character_id INTEGER, class_name TEXT, level INTEGER, xp INTEGER
        );
        CREATE TABLE relationships (
            relationship_id INTEGER PRIMARY KEY,
            source_character_id INTEGER, target_character_id INTEGER,
            relationship_type TEXT, notes TEXT
        );
        CREATE TABLE world_facts (
            world_fact_id INTEGER PRIMARY KEY, campaign_id INTEGER,
            category TEXT, fact_text TEXT, source_note TEXT,
            source_turn_id INTEGER, created_at TEXT
        );
        CREATE TABLE ai_turns (
            turn_id INTEGER PRIMARY KEY,
            player_action TEXT, dm_response TEXT,
            response_id TEXT, previous_response_id TEXT,
            model_name TEXT, created_at TEXT,
            turn_packet_json TEXT, structured_response_json TEXT,
            validation_errors_json TEXT
        );
        CREATE TABLE current_scene_state (
            id INTEGER PRIMARY KEY, current_turn_id INTEGER,
            current_player_action TEXT, current_dm_response TEXT,
            structured_state_json TEXT, updated_at TEXT
        );
        CREATE TABLE inventory (
            inventory_id INTEGER PRIMARY KEY,
            character_id INTEGER, item_id INTEGER, quantity INTEGER
        );
        CREATE TABLE items (item_id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE troops (
            troop_id INTEGER PRIMARY KEY, campaign_id INTEGER,
            group_name TEXT, count INTEGER
        );
        INSERT INTO characters (character_id, campaign_id, name, character_type)
            VALUES (1, 1, 'TestPC', 'pc');
    """)
    conn.commit()
    conn.close()


# ──────────────────────────────────────────────────────────────────────────────
# Bug 1 — special-character investigation
# ──────────────────────────────────────────────────────────────────────────────

class TestBug1SpecialCharsInProcess(unittest.IsolatedAsyncioTestCase):
    """In-process every special-character marker arrives as list. The
    transport-layer drop is upstream of our handler."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="greyhawk_npc_"))
        cls.db_path = cls.tmpdir / "x.db"
        _build_test_db(cls.db_path)
        from engine import db as engine_db
        cls._patcher = patch.object(
            engine_db, "_resolve_db_path", return_value=cls.db_path
        )
        cls._patcher.start()

    @classmethod
    def tearDownClass(cls):
        cls._patcher.stop()
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    async def _markers_call(self, markers):
        from server.mcp_server import mcp
        r = await mcp.call_tool("save_turn", {
            "player_action": "p", "dm_narrative": "d", "markers": markers,
        })
        text = r[0].text if hasattr(r[0], "text") else str(r[0])
        return json.loads(text)

    async def test_plain_strings_survive_as_list(self):
        """The user's specific question: do plain strings survive?"""
        p = await self._markers_call(["nospecialchars"])
        self.assertEqual(p["markers_received_raw_type"], "list")
        self.assertEqual(p["markers_received_count"], 1)

    async def test_apostrophe_survives_as_list(self):
        p = await self._markers_call(["location_changed:Worker's tunnel"])
        self.assertEqual(p["markers_received_raw_type"], "list")
        self.assertEqual(p["markers_received_count"], 1)

    async def test_quotes_survive_as_list(self):
        p = await self._markers_call(['npc_added:"The Quoted One"'])
        self.assertEqual(p["markers_received_raw_type"], "list")

    async def test_em_dash_survives_as_list(self):
        p = await self._markers_call(["location_changed:Quasquetan — north wall"])
        self.assertEqual(p["markers_received_raw_type"], "list")


# ──────────────────────────────────────────────────────────────────────────────
# Bug 2 — add_npc notes binding
# ──────────────────────────────────────────────────────────────────────────────

class TestBug2AddNpcNotes(unittest.IsolatedAsyncioTestCase):
    """add_npc must persist the notes parameter when provided."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="greyhawk_npc_b2_"))
        cls.db_path = cls.tmpdir / "x.db"
        _build_test_db(cls.db_path)
        from engine import db as engine_db
        cls._patcher = patch.object(
            engine_db, "_resolve_db_path", return_value=cls.db_path
        )
        cls._patcher.start()

    @classmethod
    def tearDownClass(cls):
        cls._patcher.stop()
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _read_notes(self, name):
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT notes FROM characters WHERE name = ?", (name,)
        ).fetchone()
        c.close()
        return row["notes"] if row else None

    async def _add_npc(self, **kwargs):
        from server.mcp_server import mcp
        r = await mcp.call_tool("add_npc", kwargs)
        text = r[0].text if hasattr(r[0], "text") else str(r[0])
        return json.loads(text)

    async def test_notes_persisted_on_create(self):
        """The exact bug: pass notes, expect it written, not NULL."""
        notes = "Sells rare gems; knows secrets about the Iron Watch."
        result = await self._add_npc(
            name="Merchant Grel", race="Human", notes=notes,
        )
        self.assertTrue(result.get("created"))
        self.assertEqual(result.get("notes"), notes)
        self.assertEqual(self._read_notes("Merchant Grel"), notes)

    async def test_notes_omitted_means_null(self):
        """Omitting notes leaves the column NULL — same behavior as before."""
        await self._add_npc(name="Silent Guard", race="Human")
        self.assertIsNone(self._read_notes("Silent Guard"))

    async def test_notes_empty_string_means_null(self):
        """Empty string is normalized to NULL (no empty-string rows)."""
        await self._add_npc(name="Empty Notes Guy", notes="")
        self.assertIsNone(self._read_notes("Empty Notes Guy"))

    async def test_long_notes_with_apostrophes_persisted(self):
        """Long notes with apostrophes survive intact."""
        notes = ("Master smith of the Worker's Quarter. Specializes in "
                 "blade reforging. Owes Theron 200gp from a dice game.")
        await self._add_npc(name="Smithy Pell", notes=notes)
        self.assertEqual(self._read_notes("Smithy Pell"), notes)

    async def test_signature_alignment_with_update_npc(self):
        """add_npc and update_npc must accept notes the same way."""
        from server.mcp_server import mcp
        tools = await mcp.list_tools()
        add = next(t for t in tools if t.name == "add_npc")
        upd = next(t for t in tools if t.name == "update_npc")
        # Both should accept notes as anyOf[string, null] (str | None)
        self.assertEqual(
            add.inputSchema["properties"]["notes"].get("anyOf"),
            upd.inputSchema["properties"]["notes"].get("anyOf"),
            "add_npc.notes schema must match update_npc.notes schema",
        )


# ──────────────────────────────────────────────────────────────────────────────
# Bug 3 — update_npc_class must NOT touch characters.notes
# ──────────────────────────────────────────────────────────────────────────────

class TestBug3UpdateNpcClassPreservesNotes(unittest.IsolatedAsyncioTestCase):
    """Class/level corrections must never clobber rich narrative notes."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="greyhawk_npc_b3_"))
        cls.db_path = cls.tmpdir / "x.db"
        _build_test_db(cls.db_path)

        # Seed an NPC with rich narrative notes that we DO NOT want clobbered.
        cls.rich_notes = (
            "Captain of the Iron Watch. Met Theron at the Quasquetan tavern. "
            "Owes a life-debt after the goblin ambush. Carries his late "
            "wife's signet ring. Suspects the Lord Mayor of corruption."
        )
        c = sqlite3.connect(cls.db_path)
        c.execute(
            "INSERT INTO characters (campaign_id, name, character_type, notes) "
            "VALUES (1, 'Iron Captain', 'npc', ?)",
            (cls.rich_notes,),
        )
        c.execute(
            "INSERT INTO class_levels (character_id, class_name, level, xp) "
            "VALUES (2, 'Fighter', 4, 8000)"
        )
        c.commit()
        c.close()

        # Patch BOTH path resolvers — engine/db uses _resolve_db_path,
        # but the direct-edit tools (update_npc_class) go through
        # mcp_server._active_db_path → config.json. Both need to point
        # at the temp DB.
        from engine import db as engine_db
        from server import mcp_server as srv
        cls._patchers = [
            patch.object(engine_db, "_resolve_db_path", return_value=cls.db_path),
            patch.object(srv, "_active_db_path", return_value=cls.db_path),
        ]
        for p in cls._patchers:
            p.start()

    @classmethod
    def tearDownClass(cls):
        for p in cls._patchers:
            p.stop()
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _read_notes(self, character_id):
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT notes FROM characters WHERE character_id = ?",
            (character_id,),
        ).fetchone()
        c.close()
        return row["notes"] if row else None

    def _read_class(self, character_id):
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT class_name, level FROM class_levels "
            "WHERE character_id = ? LIMIT 1", (character_id,),
        ).fetchone()
        c.close()
        return (row["class_name"], row["level"]) if row else (None, None)

    async def test_update_npc_class_does_not_clobber_notes(self):
        """The exact bug: class/level update must leave notes untouched."""
        from server.mcp_server import mcp
        before = self._read_notes(2)
        self.assertEqual(before, self.rich_notes)

        await mcp.call_tool("update_npc_class", {
            "npc_character_id": 2,
            "new_class": "Ranger",
            "new_level": 5,
            "reason":    "Re-classed after backstory revision.",
        })

        # Class actually changed
        cls_name, lvl = self._read_class(2)
        self.assertEqual(cls_name, "Ranger")
        self.assertEqual(lvl, 5)
        # Notes UNCHANGED
        after = self._read_notes(2)
        self.assertEqual(after, self.rich_notes,
                         "Rich narrative notes were clobbered by update_npc_class")

    async def test_notes_parameter_no_longer_in_schema(self):
        """The new_notes parameter must be gone entirely so the AI cannot
        accidentally pass it."""
        from server.mcp_server import mcp
        tools = await mcp.list_tools()
        update_npc_class = next(t for t in tools if t.name == "update_npc_class")
        props = update_npc_class.inputSchema["properties"]
        self.assertNotIn("new_notes", props,
                         f"new_notes must be removed from update_npc_class; "
                         f"got params {list(props.keys())}")

    async def test_class_only_change_still_logs_to_edit_log(self):
        """Confirm the audit trail still records class/level edits."""
        from server.mcp_server import mcp
        await mcp.call_tool("update_npc_class", {
            "npc_character_id": 2,
            "new_level":        6,
            "reason":           "Levelled up after dragon fight.",
        })
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT fact_text FROM world_facts "
            "WHERE category = 'edit_log' "
            "ORDER BY world_fact_id DESC LIMIT 1"
        ).fetchone()
        c.close()
        self.assertIsNotNone(row, "edit_log entry must exist")
        self.assertIn("update_npc_class", row["fact_text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
