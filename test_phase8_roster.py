"""
test_phase8_roster.py — Phase 8 character roster, XP grants, and class-level
management.

Covers:
  list_characters         — full roster with class summary
  grant_xp                — party XP award + level-up detection + audit log
  add_class_level         — explicit class_levels insertion (Caiya case)
  get_character_state     — accepts optional character_id / character_name

Runs against a TEMPORARY SQLite database. Never touches any campaign DB.

    python test_phase8_roster.py
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


def _build_test_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE characters (
            character_id INTEGER PRIMARY KEY, campaign_id INTEGER,
            name TEXT, character_type TEXT, race TEXT,
            alignment TEXT, notes TEXT
        );
        CREATE TABLE character_abilities (
            character_id INTEGER PRIMARY KEY,
            strength INTEGER, intelligence INTEGER, wisdom INTEGER,
            dexterity INTEGER, constitution INTEGER, charisma INTEGER,
            portrait_path TEXT, notes TEXT
        );
        CREATE TABLE character_status (
            status_id INTEGER PRIMARY KEY,
            character_id INTEGER NOT NULL,
            hp_current INTEGER, hp_max INTEGER, ac INTEGER,
            movement TEXT, attacks_per_round TEXT, status_notes TEXT,
            updated_note TEXT
        );
        CREATE TABLE class_levels (
            class_level_id INTEGER PRIMARY KEY,
            character_id INTEGER, class_name TEXT, level INTEGER, xp INTEGER
        );
        CREATE TABLE inventory (
            inventory_id INTEGER PRIMARY KEY,
            character_id INTEGER, item_id INTEGER, quantity INTEGER,
            equipped_flag INTEGER DEFAULT 0,
            location_id INTEGER, treasury_id INTEGER, notes TEXT
        );
        CREATE TABLE items (
            item_id INTEGER PRIMARY KEY, campaign_id INTEGER,
            name TEXT, item_type TEXT, magic_flag INTEGER,
            value_gp INTEGER, notes TEXT
        );
        CREATE TABLE world_facts (
            world_fact_id INTEGER PRIMARY KEY, campaign_id INTEGER,
            category TEXT, fact_text TEXT, source_note TEXT,
            source_turn_id INTEGER, created_at TEXT
        );

        -- The PC.
        INSERT INTO characters
            (character_id, campaign_id, name, character_type, race, alignment, notes)
            VALUES (1, 1, 'Theron Vale', 'pc', 'Human', 'Lawful Good',
                    'Fighter/Magic-User. Lord of Quasquetan.');
        INSERT INTO character_abilities VALUES
            (1, 17, 16, 13, 14, 15, 12, NULL, NULL);
        INSERT INTO character_status (character_id, hp_current, hp_max, ac)
            VALUES (1, 45, 52, 2);
        INSERT INTO class_levels (character_id, class_name, level, xp)
            VALUES (1, 'Fighter', 7, 35000);
        INSERT INTO class_levels (character_id, class_name, level, xp)
            VALUES (1, 'Magic-User', 7, 60000);

        -- Caiya — the user's specific add_class_level test target.
        INSERT INTO characters
            (character_id, campaign_id, name, character_type, race, notes)
            VALUES (2, 1, 'Caiya', 'npc', 'Half-Elf',
                    'Henchman thief. Rescued from the Iron Watch dungeon.');

        -- A second NPC with no class data — exercises the "no class row"
        -- error path in grant_xp and the seed path in add_class_level.
        INSERT INTO characters
            (character_id, campaign_id, name, character_type, race, notes)
            VALUES (3, 1, 'Brother Hadrian', 'npc', 'Human',
                    'Travelling cleric of Pelor. Met at Quasquetan tavern.');

        -- Fourth row: a hostile NPC with no extra rows at all.
        INSERT INTO characters
            (character_id, campaign_id, name, character_type, race, notes)
            VALUES (4, 1, 'Goblin Chieftain Skarn', 'hostile', 'Goblin',
                    NULL);
    """)
    conn.commit()
    conn.close()


class _BaseFixture(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="greyhawk_phase8_"))
        cls.db_path = cls.tmpdir / "x.db"
        _build_test_db(cls.db_path)
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

    async def _call(self, tool_name: str, args: dict) -> dict:
        from server.mcp_server import mcp
        r = await mcp.call_tool(tool_name, args)
        text = r[0].text if hasattr(r[0], "text") else str(r[0])
        return json.loads(text)


# ──────────────────────────────────────────────────────────────────────────────
# list_characters
# ──────────────────────────────────────────────────────────────────────────────

class TestListCharacters(_BaseFixture):

    async def test_returns_full_roster(self):
        result = await self._call("list_characters", {})
        self.assertEqual(result["count"], 4)
        names = [c["name"] for c in result["characters"]]
        self.assertEqual(
            names,
            ["Theron Vale", "Caiya", "Brother Hadrian",
             "Goblin Chieftain Skarn"],
        )

    async def test_pc_carries_class_summary(self):
        result = await self._call("list_characters", {})
        theron = next(c for c in result["characters"] if c["name"] == "Theron Vale")
        self.assertEqual(theron["character_type"], "pc")
        self.assertEqual(theron["classes_summary"], "Fighter 7/Magic-User 7")
        # Two class rows exposed.
        self.assertEqual(len(theron["classes"]), 2)
        # XP visible.
        fighter_row = next(c for c in theron["classes"]
                           if c["class_name"] == "Fighter")
        self.assertEqual(fighter_row["xp"], 35000)

    async def test_npc_with_no_class_shows_placeholder(self):
        result = await self._call("list_characters", {})
        caiya = next(c for c in result["characters"] if c["name"] == "Caiya")
        self.assertEqual(caiya["classes_summary"], "(no class)")
        self.assertEqual(caiya["classes"], [])

    async def test_notes_preview_truncated(self):
        # Inject a long-notes character for this test only.
        c = sqlite3.connect(self.db_path)
        c.execute(
            "INSERT INTO characters (character_id, campaign_id, name, "
            "character_type, race, notes) VALUES "
            "(99, 1, 'Verbose NPC', 'npc', 'Human', ?)",
            ("X" * 300,),
        )
        c.commit(); c.close()
        try:
            result = await self._call("list_characters", {})
            v = next(c for c in result["characters"] if c["name"] == "Verbose NPC")
            self.assertLessEqual(len(v["notes_preview"]), 120)
            self.assertTrue(v["notes_preview"].endswith("..."))
        finally:
            c = sqlite3.connect(self.db_path)
            c.execute("DELETE FROM characters WHERE character_id = 99")
            c.commit(); c.close()


# ──────────────────────────────────────────────────────────────────────────────
# get_character_state — optional ID / name lookup
# ──────────────────────────────────────────────────────────────────────────────

class TestGetCharacterState(_BaseFixture):

    async def test_default_returns_pc(self):
        """No params → the PC, like before."""
        result = await self._call("get_character_state", {})
        self.assertEqual(result["name"], "Theron Vale")
        self.assertEqual(result["str"], 17)
        self.assertEqual(result["hp_max"], 52)

    async def test_by_character_id(self):
        result = await self._call("get_character_state", {"character_id": 2})
        self.assertEqual(result["name"], "Caiya")
        self.assertEqual(result["character_type"], "npc")

    async def test_by_name_prefix(self):
        result = await self._call("get_character_state",
                                  {"character_name": "Brother"})
        self.assertEqual(result["name"], "Brother Hadrian")

    async def test_unknown_name_returns_error(self):
        result = await self._call("get_character_state",
                                  {"character_name": "Nobody"})
        self.assertIn("error", result)


# ──────────────────────────────────────────────────────────────────────────────
# add_class_level
# ──────────────────────────────────────────────────────────────────────────────

class TestAddClassLevel(_BaseFixture):

    async def test_adds_caiya_thief_level_7(self):
        """The user's exact request — Caiya as a Thief 7 with 55,200 XP."""
        result = await self._call("add_class_level", {
            "character_target": "2",
            "class_name":       "Thief",
            "level":            7,
            "xp":               55200,
        })
        self.assertTrue(result["added"], msg=result)
        self.assertEqual(result["character_id"], 2)
        self.assertEqual(result["name"], "Caiya")
        self.assertEqual(result["class_name"], "Thief")
        self.assertEqual(result["level"], 7)
        self.assertEqual(result["xp"], 55200)
        # Threshold for level 8 Thief = xp_table[7] = 70000
        self.assertEqual(result["next_level_threshold"], 70000)

        # Verify in DB.
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT * FROM class_levels WHERE character_id=2"
        ).fetchone()
        c.close()
        self.assertEqual(row["class_name"], "Thief")
        self.assertEqual(row["level"], 7)
        self.assertEqual(row["xp"], 55200)

    async def test_resolves_by_name(self):
        result = await self._call("add_class_level", {
            "character_target": "Brother Hadrian",
            "class_name":       "Cleric",
            "level":            5,
            "xp":               13500,
        })
        self.assertTrue(result["added"])
        self.assertEqual(result["character_id"], 3)

    async def test_refuses_duplicate(self):
        # First insert succeeds.
        first = await self._call("add_class_level", {
            "character_target": "Caiya",
            "class_name":       "Assassin",
            "level":            2,
            "xp":               1500,
        })
        self.assertTrue(first.get("added"))
        # Second insert with the same class fails.
        second = await self._call("add_class_level", {
            "character_target": "Caiya",
            "class_name":       "Assassin",
            "level":            3,
            "xp":               3000,
        })
        self.assertIn("error", second)
        self.assertIn("already exists", second["error"])
        self.assertIn("existing", second)

    async def test_unknown_character_errors(self):
        result = await self._call("add_class_level", {
            "character_target": "NoSuchOne",
            "class_name":       "Fighter",
        })
        self.assertIn("error", result)
        self.assertIn("not found", result["error"])

    async def test_invalid_level_errors(self):
        result = await self._call("add_class_level", {
            "character_target": "1",
            "class_name":       "Druid",
            "level":            0,
        })
        self.assertIn("error", result)


# ──────────────────────────────────────────────────────────────────────────────
# grant_xp
# ──────────────────────────────────────────────────────────────────────────────

class TestGrantXp(_BaseFixture):

    async def asyncSetUp(self):
        # Reset XP grants before each test so amounts don't accumulate.
        c = sqlite3.connect(self.db_path)
        c.execute("UPDATE class_levels SET xp = 35000 "
                  "WHERE character_id = 1 AND class_name = 'Fighter'")
        c.execute("UPDATE class_levels SET xp = 60000 "
                  "WHERE character_id = 1 AND class_name = 'Magic-User'")
        c.execute("DELETE FROM world_facts WHERE category = 'xp_log'")
        # Make sure Caiya has a class row for the level-up tests.
        c.execute("DELETE FROM class_levels WHERE character_id = 2")
        c.execute("INSERT INTO class_levels (character_id, class_name, level, xp) "
                  "VALUES (2, 'Thief', 7, 55200)")
        c.commit()
        c.close()

    async def test_grant_to_single_character_by_id(self):
        result = await self._call("grant_xp", {
            "character_targets": ["1"],
            "amount":            500,
            "event_description": "Cleared the goblin warren.",
        })
        self.assertTrue(result["granted"])
        theron = next(r for r in result["results"]
                      if r.get("character_id") == 1)
        # Theron has TWO class rows; both got +500.
        classes = {c["class_name"]: c for c in theron["classes"]}
        self.assertEqual(classes["Fighter"]["new_xp"], 35500)
        self.assertEqual(classes["Magic-User"]["new_xp"], 60500)

    async def test_grant_resolves_names_and_ids_mixed(self):
        result = await self._call("grant_xp", {
            "character_targets": ["Theron", "2"],
            "amount":            1000,
            "event_description": "Recovered the Mayor's signet.",
        })
        names = sorted(r.get("name") for r in result["results"]
                       if r.get("name"))
        self.assertIn("Theron Vale", names)
        self.assertIn("Caiya", names)

    async def test_levelup_flag_when_over_threshold(self):
        """Caiya at Thief 7 with 55,200 XP — needs 70,000 for level 8.
        A grant of 15,000 should put her at 70,200 → levelup_available."""
        result = await self._call("grant_xp", {
            "character_targets": ["Caiya"],
            "amount":            15000,
            "event_description": "Slew the dragon's seneschal.",
        })
        caiya = next(r for r in result["results"] if r.get("name") == "Caiya")
        self.assertTrue(caiya["levelup_available"], msg=caiya)
        thief = caiya["classes"][0]
        self.assertTrue(thief["levelup_available"])
        self.assertEqual(thief["new_xp"], 70200)
        self.assertEqual(thief["next_level_threshold"], 70000)
        self.assertEqual(thief["xp_to_next_level"], 0)

    async def test_no_levelup_when_under_threshold(self):
        result = await self._call("grant_xp", {
            "character_targets": ["Caiya"],
            "amount":            500,
            "event_description": "Recovered a minor relic.",
        })
        caiya = next(r for r in result["results"] if r.get("name") == "Caiya")
        self.assertFalse(caiya["levelup_available"])

    async def test_unresolved_target_surfaces_in_results(self):
        result = await self._call("grant_xp", {
            "character_targets": ["Theron", "NoSuchOne"],
            "amount":            100,
        })
        unresolved = result.get("unresolved", [])
        self.assertIn("NoSuchOne", unresolved)
        # Theron still got XP.
        theron_result = next((r for r in result["results"]
                              if r.get("name") == "Theron Vale"), None)
        self.assertIsNotNone(theron_result)
        self.assertEqual(theron_result["amount_added"], 100)

    async def test_no_class_row_returns_clear_error(self):
        # Brother Hadrian has no class_levels row in this test.
        c = sqlite3.connect(self.db_path)
        c.execute("DELETE FROM class_levels WHERE character_id = 3")
        c.commit(); c.close()
        result = await self._call("grant_xp", {
            "character_targets": ["3"],
            "amount":            500,
        })
        had = next(r for r in result["results"] if r.get("character_id") == 3)
        self.assertIn("error", had)
        self.assertIn("no class_levels row", had["error"])

    async def test_grant_writes_xp_log_audit_row(self):
        await self._call("grant_xp", {
            "character_targets": ["Theron", "Caiya"],
            "amount":            2500,
            "event_description": "Defeated the Hill Giant.",
        })
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT * FROM world_facts WHERE category = 'xp_log' "
            "ORDER BY world_fact_id DESC LIMIT 1"
        ).fetchone()
        c.close()
        self.assertIsNotNone(row, "xp_log row must be written")
        payload = json.loads(row["fact_text"])
        self.assertEqual(payload["amount"], 2500)
        self.assertEqual(payload["event"], "Defeated the Hill Giant.")
        self.assertIn(1, payload["character_ids"])
        self.assertIn(2, payload["character_ids"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
