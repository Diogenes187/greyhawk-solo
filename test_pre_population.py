"""
test_pre_population.py — Phase 7 area pre-population.

Covers:
  populate_area      — pre-rolls encounters + treasure for a location
  get_area_encounters — returns pre-rolled rooms; auto-populates if absent
  get_monster_instance / update_monster_instance — per-monster HP + status
  populate_npc       — full stat block on an existing characters row
  start_combat       — uses pre-rolled HP when location has a pending instance
  Treasure lifecycle — intact -> partially_looted -> looted (no respawn)

Runs against a TEMPORARY SQLite database. Never touches any campaign DB.
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
# Test fixture — a SQLite DB with the tables Phase 7 actually touches.
# ──────────────────────────────────────────────────────────────────────────────

def _build_test_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE characters (
            character_id INTEGER PRIMARY KEY, campaign_id INTEGER,
            name TEXT, character_type TEXT, race TEXT, alignment TEXT, notes TEXT
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
        CREATE TABLE locations (
            location_id INTEGER PRIMARY KEY, campaign_id INTEGER,
            name TEXT, location_type TEXT, parent_location_id INTEGER,
            status TEXT, notes TEXT
        );
        CREATE TABLE monsters (
            monster_id INTEGER PRIMARY KEY, name TEXT UNIQUE,
            category TEXT, frequency TEXT, number_appearing TEXT,
            armor_class TEXT, move TEXT, hit_dice TEXT,
            percent_in_lair TEXT, treasure_type TEXT,
            number_of_attacks TEXT, damage TEXT, special_attacks TEXT,
            special_defenses TEXT, intelligence TEXT, alignment TEXT,
            description TEXT, notes TEXT
        );
        CREATE TABLE treasure_types (
            treasure_type_id INTEGER PRIMARY KEY,
            treasure_type TEXT UNIQUE,
            copper_1000s TEXT, silver_1000s TEXT, electrum_1000s TEXT,
            gold_1000s TEXT, platinum_100s TEXT,
            gems TEXT, jewelry TEXT, maps_or_magic TEXT,
            notes TEXT, source_file TEXT
        );
        CREATE TABLE world_facts (
            world_fact_id INTEGER PRIMARY KEY, campaign_id INTEGER,
            category TEXT, fact_text TEXT, source_note TEXT
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
            character_id INTEGER, item_id INTEGER, quantity INTEGER,
            equipped_flag INTEGER DEFAULT 0,
            location_id INTEGER, treasury_id INTEGER, notes TEXT
        );
        CREATE TABLE items (
            item_id INTEGER PRIMARY KEY, campaign_id INTEGER,
            name TEXT, item_type TEXT, magic_flag INTEGER DEFAULT 0,
            value_gp INTEGER, notes TEXT
        );
        CREATE TABLE troops (
            troop_id INTEGER PRIMARY KEY, campaign_id INTEGER,
            group_name TEXT, count INTEGER
        );

        -- Dungeon random encounter lookup tables — bare-minimum coverage so
        -- get_random_dungeon_encounter always returns SOMETHING valid for
        -- dungeon_level 1-3.
        CREATE TABLE dungeon_random_monster_level_matrix (
            id INTEGER PRIMARY KEY,
            dungeon_level_min INTEGER, dungeon_level_max INTEGER,
            roll_min INTEGER, roll_max INTEGER,
            monster_level_table TEXT
        );
        INSERT INTO dungeon_random_monster_level_matrix VALUES
            (1, 1, 10, 1, 20, 'I');

        CREATE TABLE dungeon_random_monster_table_entries (
            id INTEGER PRIMARY KEY,
            monster_level_table TEXT,
            roll_min INTEGER, roll_max INTEGER,
            result_name TEXT,
            number_appearing_text TEXT,
            branch_type TEXT, notes TEXT
        );
        INSERT INTO dungeon_random_monster_table_entries VALUES
            (1, 'I', 1, 50,  'Goblin', '2-5',  'monster', NULL);
        INSERT INTO dungeon_random_monster_table_entries VALUES
            (2, 'I', 51, 100, 'Orc',   '3-12', 'monster', NULL);

        -- A real location to populate.
        INSERT INTO locations (location_id, campaign_id, name, location_type, status)
            VALUES (1, 1, "Worker's Tunnel", 'Dungeon', 'Active');

        -- The PC and a pre-existing NPC who needs stats rolled.
        INSERT INTO characters
            (character_id, campaign_id, name, character_type, race)
            VALUES (1, 1, 'TestPC', 'pc', 'Human');
        INSERT INTO characters
            (character_id, campaign_id, name, character_type, race, notes)
            VALUES (2, 1, 'Iron Captain', 'npc', 'Human',
                    'Captain. Owes Theron a life-debt.');

        -- Minimal monster table: one entry that populate_area can use.
        INSERT INTO monsters
            (name, hit_dice, armor_class, damage, number_of_attacks,
             treasure_type, intelligence, alignment)
            VALUES ('Goblin', '1-1', '6', '1d6', '1',
                    'C', 'Average', 'Lawful Evil');
        INSERT INTO monsters
            (name, hit_dice, armor_class, damage, number_of_attacks,
             treasure_type, intelligence, alignment)
            VALUES ('Orc', '1', '6', '1d8', '1',
                    'L', 'Average', 'Lawful Evil');

        -- Minimal treasure type C (small lair haul).
        INSERT INTO treasure_types
            (treasure_type, copper_1000s, silver_1000s, gold_1000s,
             gems, jewelry, maps_or_magic)
            VALUES ('C', '20% 1d12', '30% 1d4', NULL,
                    '25% 1d6', '20% 1d3', '10% any 2');
        INSERT INTO treasure_types
            (treasure_type, copper_1000s, silver_1000s, gold_1000s,
             gems, jewelry, maps_or_magic)
            VALUES ('L', NULL, NULL, NULL, '50% 1d4', NULL, NULL);
        INSERT INTO treasure_types
            (treasure_type, copper_1000s, silver_1000s, gold_1000s,
             gems, jewelry, maps_or_magic)
            VALUES ('M', NULL, NULL, NULL, NULL, NULL, '90% any 1');
    """)
    conn.commit()
    conn.close()


class _BaseFixture(unittest.IsolatedAsyncioTestCase):
    """Shared temp-DB fixture for all Phase 7 tests."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="greyhawk_phase7_"))
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

    async def asyncSetUp(self):
        """Reset Phase-7 mutable state before every test so order doesn't
        matter. Leaves seed rows (locations, monsters, NPC base row) alone."""
        c = sqlite3.connect(self.db_path)
        # area_instances may not exist on the very first test — runtime
        # migration only creates it inside engine functions. So tolerate
        # the absence of the table.
        c.execute("CREATE TABLE IF NOT EXISTS area_instances ("
                  " area_instance_id INTEGER PRIMARY KEY, campaign_id INTEGER,"
                  " location_id INTEGER, location_name TEXT, room_label TEXT,"
                  " dungeon_level INTEGER, monster_type TEXT, monster_count INTEGER,"
                  " individual_hp_json TEXT, monster_status_json TEXT,"
                  " treasure_json TEXT, treasure_status TEXT,"
                  " encounter_status TEXT, created_date TEXT, notes TEXT)")
        c.execute("DELETE FROM area_instances")
        # Wipe rolled NPC stats but keep the base characters row for Iron
        # Captain (character_id=2) — populate_npc requires it.
        c.execute("DELETE FROM character_abilities WHERE character_id != 1")
        c.execute("DELETE FROM character_status     WHERE character_id != 1")
        c.execute("DELETE FROM class_levels         WHERE character_id != 1")
        c.execute("DELETE FROM world_facts          WHERE category = 'npc_stats'")
        c.commit()
        c.close()

    async def _call(self, tool_name: str, args: dict) -> dict:
        from server.mcp_server import mcp
        r = await mcp.call_tool(tool_name, args)
        text = r[0].text if hasattr(r[0], "text") else str(r[0])
        return json.loads(text)


# ──────────────────────────────────────────────────────────────────────────────
# populate_area + get_area_encounters
# ──────────────────────────────────────────────────────────────────────────────

class TestPopulateArea(_BaseFixture):

    async def test_populate_creates_rooms_with_individual_hp(self):
        result = await self._call("populate_area", {
            "location_name": "Worker's Tunnel",
            "dungeon_level": 1,
            "num_rooms":     3,
        })
        self.assertTrue(result.get("populated"), msg=result)
        self.assertEqual(result["room_count"], 3)
        for room in result["rooms"]:
            self.assertGreater(room["monster_count"], 0)
            self.assertEqual(len(room["individual_hp"]), room["monster_count"])
            for hp in room["individual_hp"]:
                self.assertGreaterEqual(hp, 1)

    async def test_populate_is_idempotent(self):
        """Calling populate_area twice on the same location must NOT
        produce duplicate rows or overwrite the existing pre-roll."""
        first  = await self._call("populate_area", {
            "location_name": "Crypt of Echoes", "dungeon_level": 1, "num_rooms": 2,
        })
        second = await self._call("populate_area", {
            "location_name": "Crypt of Echoes", "dungeon_level": 1, "num_rooms": 2,
        })
        # Same room count, same instance IDs.
        self.assertEqual(first["room_count"], second["room_count"])
        ids_first  = sorted(r["area_instance_id"] for r in first["rooms"])
        ids_second = sorted(r["area_instance_id"] for r in second["rooms"])
        self.assertEqual(ids_first, ids_second,
                         "populate_area must not add duplicate rows")

    async def test_get_area_encounters_auto_populates(self):
        result = await self._call("get_area_encounters", {
            "location_name": "Auto Tunnel",
            "dungeon_level": 1,
        })
        self.assertTrue(result["populated"])
        self.assertGreater(result["room_count"], 0)

    async def test_get_area_encounters_no_auto(self):
        result = await self._call("get_area_encounters", {
            "location_name": "Empty Place",
            "auto_populate": False,
        })
        self.assertFalse(result["populated"])
        self.assertEqual(result["rooms"], [])


# ──────────────────────────────────────────────────────────────────────────────
# get_monster_instance + update_monster_instance
# ──────────────────────────────────────────────────────────────────────────────

class TestMonsterInstance(_BaseFixture):

    async def asyncSetUp(self):
        await super().asyncSetUp()  # base wipes state
        await self._call("populate_area", {
            "location_name": "Goblin Hold",
            "dungeon_level": 1,
            "num_rooms":     2,
        })

    async def test_get_specific_monster(self):
        rooms = (await self._call("get_area_encounters",
                                  {"location_name": "Goblin Hold"}))["rooms"]
        target = rooms[0]
        m = await self._call("get_monster_instance", {
            "area_instance_id": target["area_instance_id"],
            "monster_index":    0,
        })
        self.assertEqual(m["monster_index"], 0)
        self.assertEqual(m["hp_current"], target["individual_hp"][0])
        self.assertEqual(m["status"], "alive")

    async def test_update_monster_hp_marks_dead_at_zero(self):
        rooms = (await self._call("get_area_encounters",
                                  {"location_name": "Goblin Hold"}))["rooms"]
        target = rooms[0]
        idx = 0
        result = await self._call("update_monster_instance", {
            "area_instance_id": target["area_instance_id"],
            "monster_index":    idx,
            "hp_current":       0,
        })
        self.assertEqual(result["individual_hp"][idx], 0)
        self.assertEqual(result["monster_status"][idx], "dead")

    async def test_encounter_clears_when_all_dead(self):
        rooms = (await self._call("get_area_encounters",
                                  {"location_name": "Goblin Hold"}))["rooms"]
        target = rooms[0]
        for i in range(target["monster_count"]):
            await self._call("update_monster_instance", {
                "area_instance_id": target["area_instance_id"],
                "monster_index":    i,
                "hp_current":       0,
            })
        # Re-read
        m = await self._call("get_monster_instance", {
            "area_instance_id": target["area_instance_id"],
            "monster_index":    0,
        })
        # Re-pull room status
        all_rooms = (await self._call("get_area_encounters",
                                      {"location_name": "Goblin Hold"}))["rooms"]
        cleared = next(r for r in all_rooms
                       if r["area_instance_id"] == target["area_instance_id"])
        self.assertEqual(cleared["encounter_status"], "cleared")

    async def test_treasure_status_lifecycle(self):
        rooms = (await self._call("get_area_encounters",
                                  {"location_name": "Goblin Hold"}))["rooms"]
        target = rooms[0]
        await self._call("update_monster_instance", {
            "area_instance_id": target["area_instance_id"],
            "treasure_status":  "partially_looted",
        })
        await self._call("update_monster_instance", {
            "area_instance_id": target["area_instance_id"],
            "treasure_status":  "looted",
        })
        # Re-fetch — should still be 'looted', and re-asking get_area_encounters
        # must NOT re-roll fresh treasure (no respawn).
        rooms2 = (await self._call("get_area_encounters",
                                   {"location_name": "Goblin Hold"}))["rooms"]
        looted_room = next(r for r in rooms2
                           if r["area_instance_id"] == target["area_instance_id"])
        self.assertEqual(looted_room["treasure_status"], "looted")
        self.assertEqual(looted_room["treasure"], target["treasure"],
                         "treasure must NOT respawn / re-roll on revisit")


# ──────────────────────────────────────────────────────────────────────────────
# start_combat integration — pre-rolled HP must win
# ──────────────────────────────────────────────────────────────────────────────

class TestStartCombatUsesPreRolledHp(_BaseFixture):

    async def test_combat_uses_pre_rolled_hp(self):
        # Populate explicitly with predictable values via direct DB seed.
        c = sqlite3.connect(self.db_path)
        c.execute("""
            INSERT INTO area_instances (
                campaign_id, location_id, location_name, room_label,
                dungeon_level, monster_type, monster_count,
                individual_hp_json, monster_status_json,
                treasure_json, treasure_status, encounter_status,
                created_date
            ) VALUES (1, 1, 'Iron Tunnel', 'Guard Post', 1, 'Goblin', 3,
                      '[7, 4, 5]', '[\"alive\",\"alive\",\"alive\"]',
                      '{}', 'intact', 'pending', '2026-04-30T00:00:00Z')
        """)
        c.commit()
        c.close()

        # Set up a PC so start_combat works.
        c = sqlite3.connect(self.db_path)
        c.execute("INSERT INTO character_abilities VALUES "
                  "(1, 14, 12, 13, 16, 13, 11, NULL, NULL)")
        c.execute("INSERT INTO character_status "
                  "(character_id, hp_current, hp_max, ac) VALUES (1, 10, 10, 5)")
        c.execute("INSERT INTO class_levels (character_id, class_name, level, xp) "
                  "VALUES (1, 'Fighter', 3, 4000)")
        c.commit()
        c.close()

        result = await self._call("start_combat", {
            "encounter_name": "Goblin ambush",
            "enemies":        json.dumps([{"name": "Goblin", "count": 3}]),
            "location":       "Iron Tunnel",
        })
        self.assertNotIn("error", result, msg=result)
        # The 3 goblin combatants must have HP exactly 7, 4, 5 in order.
        goblin_entries = [
            e for e in result["initiative_order"] if e["side"] == "enemy"
        ]
        self.assertEqual(len(goblin_entries), 3)
        actual_hp = sorted(e["hp"] for e in goblin_entries)
        self.assertEqual(actual_hp, [4, 5, 7],
                         f"Combat must use pre-rolled HP [7,4,5]; got {actual_hp}")


# ──────────────────────────────────────────────────────────────────────────────
# populate_npc
# ──────────────────────────────────────────────────────────────────────────────

class TestPopulateNpc(_BaseFixture):

    async def test_populate_npc_persists_full_stat_block(self):
        result = await self._call("populate_npc", {
            "npc_name":   "Iron Captain",
            "level":      4,
            "class_name": "Fighter",
        })
        self.assertTrue(result.get("populated"), msg=result)
        self.assertFalse(result.get("already_populated"))

        # All six abilities present, in 3..18 range.
        for k in ("strength","intelligence","wisdom","dexterity",
                  "constitution","charisma"):
            self.assertIn(k, result["abilities"])
            self.assertGreaterEqual(result["abilities"][k], 3)
            self.assertLessEqual(result["abilities"][k], 18)

        self.assertGreater(result["hp_max"], 0)
        self.assertEqual(result["class"], "Fighter")
        self.assertEqual(result["level"], 4)
        self.assertIsNotNone(result["thac0"])
        self.assertIsNotNone(result["ac"])
        self.assertGreater(len(result["equipment"]), 0)
        self.assertIsNotNone(result["carried_gp"])

        # DB-side: every related table got a row.
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row
        ab = c.execute("SELECT * FROM character_abilities WHERE character_id=2").fetchone()
        cl = c.execute("SELECT * FROM class_levels WHERE character_id=2").fetchone()
        st = c.execute("SELECT * FROM character_status WHERE character_id=2").fetchone()
        wf = c.execute(
            "SELECT * FROM world_facts WHERE category='npc_stats' "
            "AND fact_text LIKE '%\"character_id\": 2,%'"
        ).fetchone()
        c.close()
        self.assertIsNotNone(ab)
        self.assertIsNotNone(cl)
        self.assertIsNotNone(st)
        self.assertIsNotNone(wf)
        self.assertEqual(cl["class_name"], "Fighter")
        self.assertEqual(cl["level"], 4)

    async def test_populate_npc_is_idempotent(self):
        first  = await self._call("populate_npc", {
            "npc_name": "Iron Captain", "level": 4, "class_name": "Fighter",
        })
        second = await self._call("populate_npc", {
            "npc_name": "Iron Captain", "level": 4, "class_name": "Fighter",
        })
        self.assertTrue(second.get("already_populated"))
        # Stat block must match across calls.
        self.assertEqual(first["abilities"], second["abilities"])
        self.assertEqual(first["hp_max"], second["hp_max"])

    async def test_populate_npc_rejects_unknown_name(self):
        result = await self._call("populate_npc", {
            "npc_name":   "NoSuchOne",
            "level":      1,
            "class_name": "Cleric",
        })
        self.assertIn("error", result)
        self.assertIn("not found", result["error"])


# ──────────────────────────────────────────────────────────────────────────────
# Smart class-aware ability assignment (4d6 drop lowest)
# ──────────────────────────────────────────────────────────────────────────────

class TestSmartAbilityAssignment(_BaseFixture):
    """Highest rolled score must always land on the prime requisite for
    each class. Second highest must land on CON for survivability (except
    Monk, where Wisdom is the explicit secondary)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Seed three additional NPC base rows: a fighter, a cleric, a mage.
        c = sqlite3.connect(cls.db_path)
        c.executemany(
            "INSERT INTO characters "
            "(character_id, campaign_id, name, character_type, race) "
            "VALUES (?, ?, ?, 'npc', 'Human')",
            [
                (10, 1, "Sword Sergeant"),
                (11, 1, "Brother Hadrian"),
                (12, 1, "Magus Veil"),
                (13, 1, "Shadow Pell"),
                (14, 1, "Brother Monk"),
                (15, 1, "Twin Path"),
            ],
        )
        c.commit()
        c.close()

    async def asyncSetUp(self):
        # Wipe rolled stats but keep our seeded base rows.
        await super().asyncSetUp()

    def _max_keys(self, abilities: dict) -> set:
        m = max(abilities.values())
        return {k for k, v in abilities.items() if v == m}

    def _second_max(self, abilities: dict) -> int:
        return sorted(abilities.values(), reverse=True)[1]

    async def test_fighter_prime_is_strength(self):
        result = await self._call("populate_npc", {
            "npc_name": "Sword Sergeant", "level": 5, "class_name": "Fighter",
        })
        self.assertEqual(result["roll_method"], "4d6_drop_lowest")
        self.assertEqual(result["ability_priority"][0], "strength")
        ab = result["abilities"]
        self.assertEqual(ab["strength"], max(ab.values()),
                         f"Fighter prime must be STR; got {ab}")
        # Second-highest goes to CON for survivability.
        self.assertEqual(ab["constitution"], self._second_max(ab),
                         f"Fighter 2nd priority must be CON; got {ab}")

    async def test_cleric_prime_is_wisdom(self):
        result = await self._call("populate_npc", {
            "npc_name": "Brother Hadrian", "level": 5, "class_name": "Cleric",
        })
        ab = result["abilities"]
        self.assertEqual(ab["wisdom"], max(ab.values()),
                         f"Cleric prime must be WIS; got {ab}")
        self.assertEqual(ab["constitution"], self._second_max(ab),
                         f"Cleric 2nd priority must be CON; got {ab}")

    async def test_magic_user_prime_is_intelligence(self):
        result = await self._call("populate_npc", {
            "npc_name": "Magus Veil", "level": 5, "class_name": "Magic-User",
        })
        ab = result["abilities"]
        self.assertEqual(ab["intelligence"], max(ab.values()),
                         f"Magic-User prime must be INT; got {ab}")
        self.assertEqual(ab["constitution"], self._second_max(ab),
                         f"Magic-User 2nd priority must be CON; got {ab}")

    async def test_thief_prime_is_dexterity(self):
        result = await self._call("populate_npc", {
            "npc_name": "Shadow Pell", "level": 5, "class_name": "Thief",
        })
        ab = result["abilities"]
        self.assertEqual(ab["dexterity"], max(ab.values()),
                         f"Thief prime must be DEX; got {ab}")
        self.assertEqual(ab["constitution"], self._second_max(ab),
                         f"Thief 2nd priority must be CON; got {ab}")

    async def test_monk_secondary_is_wisdom(self):
        """Per spec: Monk → highest STR, second highest WIS (not CON)."""
        result = await self._call("populate_npc", {
            "npc_name": "Brother Monk", "level": 5, "class_name": "Monk",
        })
        ab = result["abilities"]
        self.assertEqual(ab["strength"], max(ab.values()),
                         f"Monk prime must be STR; got {ab}")
        self.assertEqual(ab["wisdom"], self._second_max(ab),
                         f"Monk 2nd priority must be WIS; got {ab}")

    async def test_multi_class_splits_top_scores(self):
        """Fighter/Magic-User → highest STR, 2nd INT (both primes)."""
        result = await self._call("populate_npc", {
            "npc_name": "Twin Path", "level": 4,
            "class_name": "Fighter/Magic-User",
        })
        ab = result["abilities"]
        self.assertEqual(ab["strength"], max(ab.values()),
                         f"Fighter/MU 1st prime must be STR; got {ab}")
        self.assertEqual(ab["intelligence"], self._second_max(ab),
                         f"Fighter/MU 2nd prime must be INT; got {ab}")

    async def test_4d6_drop_lowest_distribution_skews_higher_than_3d6(self):
        """4d6-drop-lowest yields ~12.24 mean vs 3d6's 10.5. Across a few
        rolls the highest should rarely fall below 14, and the lowest
        rarely below 6 — this isn't deterministic but catches a regression
        to plain 3d6 most of the time."""
        # Roll 50 NPC stat blocks; collect every score.
        from engine.db import _roll_4d6_drop_lowest
        scores = [_roll_4d6_drop_lowest() for _ in range(300)]
        # 4d6-drop-lowest mean ≈ 12.24; allow generous slack.
        mean = sum(scores) / len(scores)
        self.assertGreater(mean, 11.0,
                           f"4d6-drop-lowest mean too low ({mean:.2f}); "
                           "regression to 3d6?")
        # Range bounds
        self.assertGreaterEqual(min(scores), 3)
        self.assertLessEqual(max(scores), 18)

    async def test_mixed_group_each_class_gets_its_prime(self):
        """The user's exact ask: populate fighter/cleric/mage NPCs in one
        group and confirm each stat array looks right."""
        f = await self._call("populate_npc", {
            "npc_name": "Sword Sergeant", "level": 4, "class_name": "Fighter",
        })
        c = await self._call("populate_npc", {
            "npc_name": "Brother Hadrian", "level": 4, "class_name": "Cleric",
        })
        m = await self._call("populate_npc", {
            "npc_name": "Magus Veil", "level": 4, "class_name": "Magic-User",
        })

        self.assertEqual(f["abilities"]["strength"],     max(f["abilities"].values()))
        self.assertEqual(c["abilities"]["wisdom"],       max(c["abilities"].values()))
        self.assertEqual(m["abilities"]["intelligence"], max(m["abilities"].values()))

        # All three should be using the new method.
        for npc in (f, c, m):
            self.assertEqual(npc["roll_method"], "4d6_drop_lowest")


if __name__ == "__main__":
    unittest.main(verbosity=2)
