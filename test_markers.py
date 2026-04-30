"""
test_markers.py — verifies the markers parsing fix.

Covers:
  1. _normalize_markers accepts every input shape (list, JSON string,
     newline-separated, comma-separated, single string, None, empty).
  2. End-to-end save_turn → verify_turn round-trip with markers=["cast:Invisibility"]
     against a temp database. Asserts verdict != "no_claims" and
     marker_count > 0 — proving the markers reached the DB and were re-parsed
     on the way out.

Runs against a TEMPORARY SQLite database in a temp directory. Never touches
any campaign DB. The active DB path is patched for the duration of the test.

    python test_markers.py
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
# Test fixture: a minimal SQLite DB carrying just the tables verify_turn reads.
# ──────────────────────────────────────────────────────────────────────────────

def _build_test_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE characters (
            character_id INTEGER PRIMARY KEY, campaign_id INTEGER, name TEXT,
            character_type TEXT, race TEXT, alignment TEXT, notes TEXT
        );
        CREATE TABLE character_status (
            character_id INTEGER PRIMARY KEY,
            hp_current INTEGER, hp_max INTEGER, ac INTEGER,
            movement INTEGER, attacks_per_round REAL, status_notes TEXT
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
            inventory_id INTEGER PRIMARY KEY, character_id INTEGER, item_id INTEGER,
            quantity INTEGER, equipped_flag INTEGER,
            location_id INTEGER, treasury_id INTEGER, notes TEXT
        );
        CREATE TABLE items (item_id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE world_facts (
            world_fact_id INTEGER PRIMARY KEY, campaign_id INTEGER,
            category TEXT, fact_text TEXT, source_turn_id INTEGER, created_at TEXT
        );
        CREATE TABLE troops (
            troop_id INTEGER PRIMARY KEY, campaign_id INTEGER, group_name TEXT,
            count INTEGER, troop_type TEXT, location_id INTEGER,
            commander_character_id INTEGER, notes TEXT
        );
        INSERT INTO characters (character_id, campaign_id, name)
            VALUES (1, 1, 'TestPC');
        INSERT INTO character_status (character_id, hp_current, hp_max)
            VALUES (1, 23, 31);
        -- Spell memory with Invisibility marked expended so verify_turn
        -- confirms cast:Invisibility instead of just flagging it unverified.
        INSERT INTO world_facts (campaign_id, category, fact_text)
            VALUES (1, 'spell_memory',
                    '{"2": {"slots": [{"spell": "Invisibility", "expended": true}]}}');
    """)
    conn.commit()
    conn.close()


# ──────────────────────────────────────────────────────────────────────────────
# Test cases
# ──────────────────────────────────────────────────────────────────────────────

class TestNormalizeMarkers(unittest.TestCase):
    """_normalize_markers must accept every input shape an MCP client may send."""

    def setUp(self):
        from server.mcp_server import _normalize_markers
        self.normalize = _normalize_markers

    def test_list_passes_through(self):
        self.assertEqual(
            self.normalize(["cast:Invisibility", "hp:31>23"]),
            ["cast:Invisibility", "hp:31>23"],
        )

    def test_json_array_string(self):
        self.assertEqual(
            self.normalize('["cast:Invisibility", "hp:31>23"]'),
            ["cast:Invisibility", "hp:31>23"],
        )

    def test_newline_separated_string(self):
        self.assertEqual(
            self.normalize("cast:Invisibility\nhp:31>23"),
            ["cast:Invisibility", "hp:31>23"],
        )

    def test_comma_separated_string(self):
        self.assertEqual(
            self.normalize("cast:Invisibility, hp:31>23"),
            ["cast:Invisibility", "hp:31>23"],
        )

    def test_single_marker_string(self):
        self.assertEqual(
            self.normalize("cast:Invisibility"),
            ["cast:Invisibility"],
        )

    def test_none_returns_empty(self):
        self.assertEqual(self.normalize(None), [])

    def test_empty_string_returns_empty(self):
        self.assertEqual(self.normalize(""), [])
        self.assertEqual(self.normalize("   "), [])

    def test_empty_list_returns_empty(self):
        self.assertEqual(self.normalize([]), [])

    def test_strips_whitespace_and_drops_empties(self):
        self.assertEqual(
            self.normalize(["  cast:X  ", "", "  ", "hp:1>0"]),
            ["cast:X", "hp:1>0"],
        )

    def test_non_string_entries_dropped(self):
        self.assertEqual(
            self.normalize([1, "cast:X", None, "hp:1>0"]),
            ["cast:X", "hp:1>0"],
        )

    def test_apostrophes_in_list(self):
        """The reported bug case: apostrophes must survive a list pass-through."""
        markers = ["location_changed:Worker's tunnel", "cast:Invisibility"]
        self.assertEqual(self.normalize(markers), markers)

    def test_apostrophes_in_json_string(self):
        """A JSON-array string with apostrophes inside string values."""
        s = '["location_changed:Worker\'s tunnel", "cast:Invisibility"]'
        self.assertEqual(
            self.normalize(s),
            ["location_changed:Worker's tunnel", "cast:Invisibility"],
        )

    def test_apostrophes_in_python_repr(self):
        """A Python list-repr string (single-quoted, escaped apostrophes)."""
        # Real Python repr: ['location_changed:Worker\\'s tunnel', 'cast:Invisibility']
        s = "['location_changed:Worker\\'s tunnel', 'cast:Invisibility']"
        self.assertEqual(
            self.normalize(s),
            ["location_changed:Worker's tunnel", "cast:Invisibility"],
        )

    def test_comma_split_preserves_apostrophes_inside_value(self):
        """Conservative comma split must NOT shred 'Worker's tunnel, west cellar'."""
        # The whole string is one marker whose value happens to contain a comma.
        # Because "west cellar" doesn't start with a known prefix, we keep it whole.
        s = "location_changed:Worker's tunnel, west cellar"
        self.assertEqual(self.normalize(s), [s])

    def test_double_wrapped_string_unwraps(self):
        """An accidentally double-quoted single marker still works."""
        self.assertEqual(self.normalize('"cast:Invisibility"'), ["cast:Invisibility"])

    def test_unrecognizable_input_raises(self):
        """A dict / int / etc. must NOT silently drop to []."""
        with self.assertRaises(ValueError):
            self.normalize({"cast": "X"})
        with self.assertRaises(ValueError):
            self.normalize(42)

    def test_list_with_one_comma_separated_element_flattens(self):
        """If the AI sends ['cast:X, hp:1>0'] by accident, we split it."""
        self.assertEqual(
            self.normalize(["cast:X, hp:1>0"]),
            ["cast:X", "hp:1>0"],
        )


class TestSaveAndVerifyRoundTrip(unittest.TestCase):
    """End-to-end: a marker list passed in must come back via verify_turn."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir  = Path(tempfile.mkdtemp(prefix="greyhawk_test_markers_"))
        cls.db_path = cls.tmpdir / "test.db"
        _build_test_db(cls.db_path)

        # Patch the path resolver so every _get_conn() call hits our temp DB.
        from engine import db as engine_db
        cls._patcher = patch.object(
            engine_db, "_resolve_db_path", return_value=cls.db_path
        )
        cls._patcher.start()

    @classmethod
    def tearDownClass(cls):
        cls._patcher.stop()
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _save_turn_with_markers(self, raw_markers):
        """Mimic save_turn's normalization → write → verify path."""
        from server.mcp_server import _normalize_markers
        from engine.db import (
            write_ai_turn, update_current_scene,
            db_verify_turn, db_update_turn_verification,
        )

        clean = _normalize_markers(raw_markers)
        structured = {"markers": clean} if clean else {}

        turn_id = write_ai_turn(
            player_action="Cast invisibility on myself.",
            dm_response="You shimmer and vanish from sight.",
            model_name="test",
            structured_response_json=(
                json.dumps(structured) if structured else None
            ),
        )
        update_current_scene(
            turn_id=turn_id,
            player_action="Cast invisibility on myself.",
            dm_response="You shimmer and vanish from sight.",
            structured_state=structured or None,
        )
        verification = db_verify_turn(turn_id=turn_id)
        db_update_turn_verification(turn_id, json.dumps(verification))
        return turn_id, verification

    def test_list_input_roundtrips_to_confirmed(self):
        """The originally reported bug: passing a list must NOT yield no_claims."""
        _turn_id, result = self._save_turn_with_markers(["cast:Invisibility"])

        self.assertNotEqual(
            result.get("verdict"), "no_claims",
            f"Markers list was lost on the way to the DB. Full result: {result}",
        )
        self.assertEqual(result.get("marker_count"), 1)
        # Spell memory had Invisibility marked expended, so verify should confirm.
        self.assertEqual(
            len(result.get("confirmed", [])), 1,
            f"Expected 1 confirmed entry; got {result}",
        )
        self.assertEqual(result["confirmed"][0]["type"], "cast")
        # Debug fields must be present
        self.assertIn("debug", result)
        self.assertEqual(result["debug"]["markers_in_db_type"], "list")
        self.assertEqual(
            result["debug"]["markers_in_db_raw"], ["cast:Invisibility"]
        )

    def test_json_string_input_also_roundtrips(self):
        _turn_id, result = self._save_turn_with_markers('["cast:Invisibility"]')
        self.assertNotEqual(result.get("verdict"), "no_claims")
        self.assertEqual(result.get("marker_count"), 1)

    def test_newline_string_input_also_roundtrips(self):
        _turn_id, result = self._save_turn_with_markers(
            "cast:Invisibility\nhp:31>23"
        )
        self.assertNotEqual(result.get("verdict"), "no_claims")
        self.assertEqual(result.get("marker_count"), 2)

    def test_no_markers_returns_no_claims(self):
        _turn_id, result = self._save_turn_with_markers(None)
        self.assertEqual(result.get("verdict"), "no_claims")
        self.assertEqual(result.get("marker_count", 0), 0)
        # Debug should still surface what was (not) found.
        self.assertEqual(result["debug"]["markers_in_db_type"], "missing")

    def test_apostrophe_markers_roundtrip_through_db(self):
        """The exact case the user asked about — apostrophes in a list must
        round-trip cleanly through structured_response_json."""
        markers = ["location_changed:Worker's tunnel", "cast:Invisibility"]
        _turn_id, result = self._save_turn_with_markers(markers)
        self.assertEqual(result.get("marker_count"), 2)
        self.assertEqual(
            result["debug"]["markers_in_db_raw"],
            ["location_changed:Worker's tunnel", "cast:Invisibility"],
            "Apostrophes must survive JSON serialization through the DB.",
        )
        # location_changed should resolve as unverified (scene_location was
        # not passed) and cast:Invisibility should confirm.
        self.assertEqual(len(result.get("confirmed", [])), 1)
        self.assertEqual(result["confirmed"][0]["type"], "cast")


class TestSchemaAndLiveDispatch(unittest.IsolatedAsyncioTestCase):
    """
    End-to-end against the actual @mcp.tool() registration. Proves:
      1. The markers parameter IS in the schema (not just the docstring).
      2. Schema declares array of string with default [] and a description.
      3. A live tool dispatch with markers=["test:value"] reports
         markers_received_raw_type='list' (proving the parameter survived
         the JSON-RPC boundary and Pydantic validation).
    """

    @classmethod
    def setUpClass(cls):
        cls.tmpdir  = Path(tempfile.mkdtemp(prefix="greyhawk_test_live_"))
        cls.db_path = cls.tmpdir / "test.db"
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

    async def test_markers_is_registered_in_input_schema(self):
        """save_turn's input schema must include markers as array of string
        with default [] and a non-empty description."""
        from server.mcp_server import mcp
        tools = await mcp.list_tools()
        save_turn = next(t for t in tools if t.name == "save_turn")

        self.assertIn("markers", save_turn.inputSchema["properties"],
                      "markers parameter is missing from the registered schema")

        m = save_turn.inputSchema["properties"]["markers"]
        self.assertEqual(m.get("type"), "array",
                         f"markers schema is not array; got {m}")
        self.assertEqual(m.get("items", {}).get("type"), "string",
                         f"markers items not string; got {m}")
        self.assertEqual(m.get("default"), [],
                         f"markers default is not []; got {m}")
        self.assertTrue(m.get("description", "").strip(),
                        "markers schema is missing a description")
        # markers MUST be optional, not required
        self.assertNotIn("markers", save_turn.inputSchema.get("required", []))

    async def test_minimal_call_markers_arrive_as_list(self):
        """The minimal save_turn call: just the two required params plus
        markers. markers_received_raw_type MUST be 'list', NOT 'NoneType'."""
        import json as _json
        from server.mcp_server import mcp

        result = await mcp.call_tool("save_turn", {
            "player_action": "test",
            "dm_narrative":  "test",
            "markers":       ["test:value"],
        })
        text = result[0].text if hasattr(result[0], "text") else str(result[0])
        payload = _json.loads(text)

        self.assertEqual(
            payload.get("markers_received_raw_type"), "list",
            f"markers were dropped before reaching the function. Full payload:\n"
            f"{_json.dumps(payload, indent=2)}",
        )
        self.assertEqual(payload.get("markers_received_count"), 1)
        self.assertEqual(payload.get("markers_normalized"), ["test:value"])

    async def test_minimal_call_with_apostrophes_arrives_intact(self):
        """The user's specific apostrophe case via the live tool dispatch."""
        import json as _json
        from server.mcp_server import mcp

        result = await mcp.call_tool("save_turn", {
            "player_action": "test",
            "dm_narrative":  "test",
            "markers":       ["location_changed:Worker's tunnel",
                              "cast:Invisibility"],
        })
        text = result[0].text if hasattr(result[0], "text") else str(result[0])
        payload = _json.loads(text)

        self.assertEqual(payload["markers_received_raw_type"], "list")
        self.assertEqual(payload["markers_received_count"], 2)
        self.assertEqual(
            payload["markers_normalized"],
            ["location_changed:Worker's tunnel", "cast:Invisibility"],
        )

    async def test_markers_str_pipe_delimited_workaround(self):
        """The exact workaround the user requested: pipe-delimited string
        delivers two markers that the array path failed to deliver."""
        import json as _json
        from server.mcp_server import mcp

        result = await mcp.call_tool("save_turn", {
            "player_action": "test",
            "dm_narrative":  "test",
            "markers_str":   "cast:Charm Person|hp:41>38",
        })
        text = result[0].text if hasattr(result[0], "text") else str(result[0])
        payload = _json.loads(text)

        # The string ingress path arrives intact.
        self.assertEqual(payload["markers_str_received_raw_type"], "str")
        self.assertEqual(payload["markers_str_pieces_after_split"], 2)

        # Both markers landed in clean_markers and were verified.
        self.assertEqual(
            payload["markers_normalized"],
            ["cast:Charm Person", "hp:41>38"],
        )
        self.assertNotEqual(
            payload["verification"]["verdict"], "no_claims",
            "markers_str must produce a verifiable turn, not no_claims",
        )
        self.assertEqual(payload["verification"]["marker_count"], 2)

    async def test_markers_str_with_apostrophe(self):
        """Pipe-delimited string with an apostrophe inside one marker value."""
        import json as _json
        from server.mcp_server import mcp

        result = await mcp.call_tool("save_turn", {
            "player_action": "test",
            "dm_narrative":  "test",
            "markers_str":   "location_changed:Worker's tunnel|cast:Invisibility",
        })
        text = result[0].text if hasattr(result[0], "text") else str(result[0])
        payload = _json.loads(text)
        self.assertEqual(
            payload["markers_normalized"],
            ["location_changed:Worker's tunnel", "cast:Invisibility"],
        )

    async def test_markers_array_takes_precedence_when_both_passed(self):
        """If both ingress paths have content, the array wins."""
        import json as _json
        from server.mcp_server import mcp

        result = await mcp.call_tool("save_turn", {
            "player_action": "test",
            "dm_narrative":  "test",
            "markers":       ["cast:FromArray"],
            "markers_str":   "cast:FromString|hp:1>0",
        })
        text = result[0].text if hasattr(result[0], "text") else str(result[0])
        payload = _json.loads(text)
        self.assertEqual(payload["markers_normalized"], ["cast:FromArray"])

    async def test_markers_str_falls_back_when_array_empty(self):
        """Array empty + markers_str non-empty → markers_str is used.
        Models the actual production scenario where the array drops out."""
        import json as _json
        from server.mcp_server import mcp

        result = await mcp.call_tool("save_turn", {
            "player_action": "test",
            "dm_narrative":  "test",
            "markers":       [],
            "markers_str":   "cast:Charm Person|hp:41>38",
        })
        text = result[0].text if hasattr(result[0], "text") else str(result[0])
        payload = _json.loads(text)
        self.assertEqual(
            payload["markers_normalized"],
            ["cast:Charm Person", "hp:41>38"],
        )

    async def test_both_empty_yields_no_claims(self):
        """No markers in either path → verdict no_claims."""
        import json as _json
        from server.mcp_server import mcp

        result = await mcp.call_tool("save_turn", {
            "player_action": "test",
            "dm_narrative":  "test",
        })
        text = result[0].text if hasattr(result[0], "text") else str(result[0])
        payload = _json.loads(text)
        self.assertEqual(payload["verification"]["verdict"], "no_claims")
        self.assertEqual(payload["markers_normalized"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
