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
  get_character_state   -- Full sheet incl. inventory (caps when inventory is large; prefer get_character_stats)
  get_character_stats   -- Core stats only — name/race/classes/abilities/HP/AC/THAC0/saves, no inventory or spells
  get_saving_throws     -- Just the 5 save target numbers (~150 bytes)
  get_combat_summary    -- Compact combat card: THAC0, AC, HP, attacks_per_round, equipped weapons (~250 bytes)
  get_realm_state       -- All locations, troops, treasury, active projects
  roll_dice             -- Parse and roll any dice expression (e.g. "3d6+2")
  get_current_scene     -- Current location, active NPCs, recent events
  get_recent_history    -- Last N turns from ai_turns table
  get_pending_updates   -- Turns with state_changes notes not yet DB-committed

  WRITE
  save_turn             -- Write player action + DM narrative to the database
  update_character_status  -- Change HP, AC, status notes on Theron
  update_treasury          -- Add/subtract coins or gems from a treasury account
  add_treasury_account     -- Insert a new treasury account (vault, cache, hoard)
  list_treasury_accounts   -- Discovery: every treasury account with balances and location
  add_location             -- Insert a new location into the realm
  update_location_status   -- Change status/notes on an existing location
  update_troop_count       -- Set or adjust count on a troop group
  add_troop_group          -- Insert a new troop group (optionally with commander)
  add_livestock            -- Insert a new livestock row at a location
  remove_livestock         -- Delete a livestock row entirely (audit-logged)
  insert_row               -- Generic INSERT into any user table (companion to direct_db_edit)
  restore_from_edit_log    -- Inverse of any audit-logged edit; revives, reverts, or undoes inserts
  add_item                 -- Create an item and assign it to inventory (with combat fields, character_target)
  equip_item               -- Set a character's inventory item into a slot
  list_equipped            -- Compact slot-by-slot loadout for mid-combat reference
  list_inventory           -- Per-character inventory with magic_only/equipped_only/summary_only filters
  search_inventory         -- Substring item-name search; returns compact 5-field summaries
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
  check_aerial_encounter  -- Roll 1-in-6 aerial check; rolls AD&D 1e flying-creature table on hit
  roll_reaction           -- 2d6 + Cha + situation on the AD&D 1e reaction table; logs to reaction_log
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

import ast
import json
import os
import random
import re
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Callable

from pydantic import BeforeValidator, Field


# ══════════════════════════════════════════════════════════════════════════════
# MCP DEBUG LOGGING
#
# Persistent observability for the MCP request path. Originally added in
# Phase 14 to chase a long-standing rumor that non-empty `markers` arrays
# arrived at save_turn as None. Day 1141 live testing on three parallel
# ingress formats (markers / markers_json / markers_str) proved the array
# survives intact in the production client/transport — the "drop" was
# upstream and was no longer reproducible. Phase 15 locked the array as
# the only ingress path and demoted this to standard logging.
#
# Two channels are still wired:
#   normalize_markers — every Pydantic BeforeValidator call: raw_type,
#                       raw_length, raw_repr, normalized result. Cheap
#                       insurance against a regression of the array-drop
#                       bug; if it ever resurfaces we'll have the data
#                       immediately rather than burning another
#                       investigation cycle.
#   save_turn         — one line per save_turn call: markers_type, count,
#                       clean_count, scene metadata.
#
# Logs land in <project_root>/logs/mcp_debug.log as one JSON object per
# line — tail-friendly, grep-friendly, easy to script. The logger is
# best-effort: any IO failure is caught silently so a broken log file
# can never break a tool call.
# ══════════════════════════════════════════════════════════════════════════════

_DEBUG_LOG_PATH = Path(__file__).parent.parent / "logs" / "mcp_debug.log"
_DEBUG_LOG_LOCK = threading.Lock()


def _debug_repr(value: Any, max_len: int = 800) -> str:
    """
    Best-effort string rep that always succeeds and never explodes the log.

    repr() handles None/list/dict/str cleanly. Long strings are truncated
    to keep individual log lines readable. Anything that raises in __repr__
    falls back to the type name plus the exception message.
    """
    try:
        s = repr(value)
    except Exception as e:
        return f"<unrepr-able {type(value).__name__}: {e!r}>"
    if len(s) > max_len:
        return s[:max_len] + f"...<+{len(s) - max_len} chars truncated>"
    return s


def _log_mcp_debug(channel: str, payload: dict) -> None:
    """
    Append one JSON-line entry to logs/mcp_debug.log.

    Best-effort — any failure (no logs/ dir, permission denied, JSON encode
    error from a stray non-serializable value) is swallowed silently. The
    debug logger MUST NOT break a save_turn call; the markers bug we're
    chasing already burns turns when it fires, and a broken logger would
    burn them again.

    Each entry is prefixed with an ISO-8601 UTC timestamp + the channel
    name (e.g. 'normalize_markers.entry', 'save_turn.entry',
    'save_turn.merged'). The payload is forced through a json-safe
    encoding pass (stringifying anything that doesn't serialize) so a
    surprise type never tanks the line.
    """
    try:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        # Make the payload safe to json-encode by stringifying anything
        # that doesn't survive the default encoder.
        safe_payload = {}
        for k, v in (payload or {}).items():
            try:
                json.dumps(v)
                safe_payload[k] = v
            except (TypeError, ValueError):
                safe_payload[k] = _debug_repr(v)
        line = json.dumps({
            "ts":      ts,
            "pid":     os.getpid(),
            "channel": channel,
            "data":    safe_payload,
        }, ensure_ascii=False)

        # Make sure the directory exists. Best-effort — if we can't create
        # it, the open() below will fail and the whole call no-ops.
        try:
            _DEBUG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        with _DEBUG_LOG_LOCK:
            with open(_DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        # Never propagate. Logging is observability, not control flow.
        pass


# ──────────────────────────────────────────────────────────────────────────────
# MARKERS INPUT NORMALIZATION
# ──────────────────────────────────────────────────────────────────────────────
#
# The save_turn `markers` parameter is exposed to MCP clients as a strict
# `array of string` schema. But because some clients (and some models) still
# serialize array arguments as Python repr, JSON-string literals, or single
# concatenated strings, a Pydantic BeforeValidator coerces every plausible
# input shape into a clean list[str] before validation runs. That keeps the
# schema simple and authoritative while remaining tolerant to the messy
# reality of model output.
# ──────────────────────────────────────────────────────────────────────────────

_MARKER_PREFIXES = {
    "cast", "item_added", "item_used", "hp",
    "spent", "gained", "npc_added", "location_changed", "troop_change",
    # Phase 20: extended vocabulary for non-inventory state.
    "livestock_added", "troop_added", "project_added", "location_added",
}

CANONICAL_MARKER_FORMAT_HELP = (
    'markers MUST be a JSON array of strings (e.g. ["cast:Invisibility", '
    '"hp:41>38"]). One marker per state change. Apostrophes and other '
    "special characters are safe inside the strings — pass them raw, no "
    "escaping needed. Pick the prefix that matches what changed — the "
    "verifier routes by prefix to the right table and emits the correct "
    "suggested_call. Using item_added: for livestock or troop_added: for "
    "an NPC will produce the wrong remediation suggestion.\n"
    "\n"
    "Inventory / character state:\n"
    "  cast:[spell name]\n"
    "  item_added:[name]      item_used:[name]\n"
    "  hp:[old]>[new]\n"
    "  spent:[amount]gp       gained:[amount]gp\n"
    "  npc_added:[name]\n"
    "  location_changed:[name]\n"
    "  troop_change:[group]:[old]>[new]\n"
    "\n"
    "Domain / realm state (Phase 20):\n"
    "  livestock_added:[animal_type]:[count]:[location]\n"
    "  troop_added:[group_name]:[count]:[location]\n"
    "  project_added:[project name]\n"
    "  location_added:[location name]"
)


def _looks_like_marker(s: str) -> bool:
    """Quick heuristic: does this string start with a known marker prefix?"""
    if not s or ":" not in s:
        return False
    return s.split(":", 1)[0].strip().lower() in _MARKER_PREFIXES


def _split_string_into_markers(s: str) -> list[str]:
    """
    Turn ONE string into a list of marker strings, applying fallbacks in
    order: JSON array → Python repr (ast.literal_eval) → newline split →
    conservative comma split → single-element wrap.

    The comma split only fires when every comma-separated piece looks like a
    valid marker prefix — so values that legitimately contain commas
    (e.g. "Worker's tunnel, west cellar") aren't shredded.
    """
    if not isinstance(s, str):
        return []
    s = s.strip()
    if not s:
        return []

    # Strip an outer pair of matched wrapping quotes that some clients add
    # when they over-serialize a string argument (e.g. '"cast:X"').
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        inner = s[1:-1]
        if s[0] not in inner:  # only strip if no quote is interior
            s = inner.strip()
            if not s:
                return []

    # 1. JSON array literal — apostrophes are safe inside JSON strings.
    if s.startswith("[") and s.endswith("]"):
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                cleaned = [str(m).strip() for m in parsed
                           if isinstance(m, str) and str(m).strip()]
                if cleaned:
                    return cleaned
        except (json.JSONDecodeError, TypeError):
            pass
        # 2. Python repr fallback — handles single-quoted lists like
        #    "['cast:X', 'hp:1>0']" and Python-escaped apostrophes such as
        #    "['Worker\\'s tunnel']" that are invalid as JSON.
        try:
            parsed = ast.literal_eval(s)
            if isinstance(parsed, list):
                cleaned = [str(m).strip() for m in parsed
                           if isinstance(m, str) and str(m).strip()]
                if cleaned:
                    return cleaned
        except (ValueError, SyntaxError):
            pass

    # 3. Newline split — apostrophes inside a marker value never include
    #    newlines, so this is always safe.
    if "\n" in s:
        parts = [p.strip() for p in s.replace("\r", "").split("\n") if p.strip()]
        if parts:
            return parts

    # 4. Conservative comma split — fires only when EVERY piece looks like a
    #    real marker. "location_changed:Worker's tunnel, west cellar" stays
    #    intact because "west cellar" has no marker prefix.
    if "," in s:
        pieces = [p.strip() for p in s.split(",") if p.strip()]
        if pieces and all(_looks_like_marker(p) for p in pieces):
            return pieces

    # 5. Single marker — wrap as a one-element list. Never returns [] for a
    #    non-empty input string.
    return [s]


def _normalize_markers(raw: Any) -> list[str]:
    """
    Coerce any plausible MCP input into list[str].

    Used as a Pydantic BeforeValidator on the save_turn markers parameter,
    and exposed for direct tests.

    Acceptance order:
      None / ""              → []
      list                   → recursively flatten each element
      str                    → _split_string_into_markers (5-step fallback)
      anything else          → ValueError (surfaces to the AI as a
                                Pydantic validation error — never silent)

    Standard logging: every invocation logs the raw input value and
    resulting normalized list to logs/mcp_debug.log under channel
    'normalize_markers'. This is the EARLIEST point in the pipeline that
    user code can intercept the markers parameter — Pydantic calls a
    BeforeValidator before doing any type coercion. If a future session
    ever shows None/empty here while the wire payload was non-empty,
    the bug is upstream of Pydantic (FastMCP transport, MCP client, or
    the model's serialization) and we have the data to prove it.
    """
    raw_type     = type(raw).__name__
    raw_repr     = _debug_repr(raw)
    raw_length   = (len(raw)
                    if isinstance(raw, (list, str, tuple, dict))
                    else None)

    try:
        if raw is None:
            normalized = []
        elif isinstance(raw, list):
            normalized = []
            for item in raw:
                if isinstance(item, str):
                    normalized.extend(_split_string_into_markers(item))
                # silently drop non-strings inside a list — schema
                # validation will already have flagged the type.
        elif isinstance(raw, str):
            normalized = _split_string_into_markers(raw)
        else:
            _log_mcp_debug("normalize_markers.invalid", {
                "raw_type":    raw_type,
                "raw_repr":    raw_repr,
                "raw_length":  raw_length,
            })
            raise ValueError(
                f"markers must be a JSON array of strings (or a string). "
                f"Received {raw_type}: {raw!r}. " + CANONICAL_MARKER_FORMAT_HELP
            )
    except ValueError:
        raise
    except Exception as e:
        _log_mcp_debug("normalize_markers.exception", {
            "raw_type":   raw_type,
            "raw_repr":   raw_repr,
            "raw_length": raw_length,
            "exception":  f"{type(e).__name__}: {e!r}",
        })
        raise

    _log_mcp_debug("normalize_markers", {
        "raw_type":          raw_type,
        "raw_length":        raw_length,
        "raw_repr":          raw_repr,
        "normalized_count":  len(normalized),
        "normalized":        normalized,
    })
    return normalized


# Pydantic-friendly Annotated type alias used by save_turn. Schema is
# {"type":"array","items":{"type":"string"}} — single, unambiguous, with
# the description visible to the model.
MarkersField = Annotated[
    list[str],
    BeforeValidator(_normalize_markers),
    Field(description=(
        "Array of structured state-change markers — one string per change. "
        'Example: ["cast:Invisibility", "hp:41>38", '
        '"location_changed:Worker\'s tunnel"]. Apostrophes and other special '
        "characters are safe inside the strings (no escaping needed). "
        "Prefixes: cast:[spell], item_added:[name], item_used:[name], "
        "hp:[old]>[new], spent:[N]gp, gained:[N]gp, npc_added:[name], "
        "location_changed:[name], troop_change:[group]:[old]>[new]. "
        "Pass [] (or omit) only when nothing changed; a turn with no "
        "markers returns verdict='no_claims'."
    )),
]


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 24 — RESPONSE CAP + GENERALIZED SUMMARY PATTERN
#
# Several read tools could return >50KB on large campaigns:
#   get_world_facts()              ~900 KB on Ramun (575 facts, 302 categories)
#   get_character_state()          ~114 KB on Ramun (full PC sheet + inventory)
#   get_realm_state()              ~66 KB on Ramun
#   get_world_facts(category=X)    ~286 KB for the largest single category
#   get_recent_history(n=20)       ~20 KB at the cap edge
#   get_domain_state()             ~22 KB at the edge
#   get_loyalty_state()            ~17 KB, close to cap
#
# The MCP harness used to dump >30KB responses to a temp file and ask the
# caller to read it back in chunks — lossy and forced manual pagination.
# This pass enforces a 30 KB cap at the tool layer, with two policies:
#
#   (A) Degrade-to-summary — for list-shaped tools. When oversize, swap to
#       a thin-shape summary and append _response_meta with a hint about
#       how to filter further. The list_inventory.summary_only flag added
#       in Phase 13 is the explicit opt-in; this is the implicit safety net.
#
#   (B) Hard error — for atomic records like get_character_state. The
#       response IS the data; there's no sensible summary. Caller must
#       reach for finer-grained tools (list_inventory(magic_only=True),
#       list_equipped, get_spell_slots, etc.) instead.
#
# Both policies surface a _response_meta dict with original_kb /
# returned_kb / hint so the caller can see what happened and what to do.
# ══════════════════════════════════════════════════════════════════════════════

DEFAULT_RESPONSE_CAP_BYTES = 30_000   # 30 KB — empirically: above this the
                                       # MCP harness's truncate-to-file path
                                       # fires, which is what we're avoiding.


def _payload_bytes(payload: Any) -> int:
    """Serialized JSON byte size of a payload. default=str for non-JSON values."""
    try:
        return len(json.dumps(payload, default=str, ensure_ascii=False)
                   .encode("utf-8"))
    except (TypeError, ValueError):
        # If something is genuinely un-serializable, treat as oversize so
        # we fall through to the safety policy.
        return 10**9


def _cap_response(
    payload: dict,
    summary_fn: "Callable[[dict], dict] | None" = None,
    max_bytes: int = DEFAULT_RESPONSE_CAP_BYTES,
    error_hint: str = "",
    tool_name: str = "",
) -> dict:
    """
    Cap a tool's response payload to max_bytes (default 30 KB).

      Under cap                  → return as-is.
      Over cap + summary_fn      → return summary_fn(payload) with
                                    _response_meta {degraded: true,
                                    original_kb, returned_kb, hint}.
                                    Policy (A): degrade-to-summary.
      Over cap + no summary_fn   → return {"error": ..., _response_meta}
                                    with error_hint included.
                                    Policy (B): hard error, force the
                                    caller to use finer-grained tools.

    Logs every cap-fired event to logs/mcp_debug.log channel
    'response_capped' so we have a long-term record of which tools the
    cap is biting and how often.
    """
    original = _payload_bytes(payload)
    if original <= max_bytes:
        return payload

    if summary_fn is not None:
        try:
            summarized = summary_fn(payload)
        except Exception as e:
            # If the summary_fn itself blows up, fall through to the
            # hard-error path. Don't let a buggy summarizer take down
            # the whole tool call.
            _log_mcp_debug("response_capped.summary_fn_failed", {
                "tool":           tool_name,
                "original_kb":    round(original / 1024, 1),
                "exception":      f"{type(e).__name__}: {e!r}",
            })
            summary_fn = None

    if summary_fn is not None:
        returned = _payload_bytes(summarized)
        # Defensive: if the "summary" is somehow STILL over cap, log and
        # truncate harder by stripping all but top-level keys.
        if returned > max_bytes:
            _log_mcp_debug("response_capped.summary_still_oversize", {
                "tool":           tool_name,
                "original_kb":    round(original / 1024, 1),
                "summary_kb":     round(returned / 1024, 1),
            })
        if not isinstance(summarized, dict):
            summarized = {"_summary": summarized}
        summarized["_response_meta"] = {
            "degraded":     True,
            "original_kb":  round(original / 1024, 1),
            "returned_kb":  round(returned / 1024, 1),
            "cap_kb":       round(max_bytes / 1024, 1),
            "hint": (
                error_hint or
                "Response auto-degraded to summary shape. Pass an "
                "explicit summary parameter (e.g. summary_only=True) "
                "or use filters to silence this warning."
            ),
        }
        _log_mcp_debug("response_capped.degraded", {
            "tool":         tool_name,
            "original_kb":  round(original / 1024, 1),
            "returned_kb":  round(returned / 1024, 1),
        })
        return summarized

    # No summary_fn: hard error.
    _log_mcp_debug("response_capped.error", {
        "tool":         tool_name,
        "original_kb":  round(original / 1024, 1),
    })
    return {
        "error": (
            f"Response is {round(original/1024, 1)} KB, exceeds the "
            f"{round(max_bytes/1024)} KB cap. " +
            (error_hint or "Use a more focused tool to fetch the data you need.")
        ),
        "_response_meta": {
            "degraded":     False,
            "rejected":     True,
            "original_kb":  round(original / 1024, 1),
            "cap_kb":       round(max_bytes / 1024, 1),
            "hint":         error_hint or "Use finer-grained tools.",
        },
    }


# ── Tool-specific summary functions ──────────────────────────────────────────

def _summarize_world_facts_categories(payload: dict) -> dict:
    """
    For get_world_facts() with no category filter, when the full payload
    is oversize: drop the fact bodies entirely and return just the
    category index — a {category_name: count} map. The caller then
    picks one and re-calls with category=X for the bodies.
    """
    by_category = payload.get("by_category", {}) or {}
    return {
        "count":               payload.get("count"),
        "categories_summary":  {
            cat: len(facts)
            for cat, facts in by_category.items()
        },
        "categories":          sorted(by_category.keys()),
        "hint": (
            "Full fact bodies dropped because the payload exceeded the "
            "response cap. Re-call get_world_facts(category=X) with a "
            "specific category to retrieve its facts. Categories with "
            "very high counts (e.g. edit_log) may still trigger the cap "
            "and degrade to fact previews."
        ),
    }


def _summarize_world_facts_single_category(payload: dict) -> dict:
    """
    For get_world_facts(category=X) when one category alone is oversize
    (e.g. edit_log on Ramun: ~200 entries x ~1 KB each = 200 KB).

    Two-stage trim:
      1. Truncate every fact_text to a 200-char preview.
      2. Cap the number of entries per category at MAX_ENTRIES_PER_CAT
         (most-recent first by id) so even a category with thousands of
         entries fits under the response cap.

    Caller fetches specific older entries via direct SQL on
    world_facts(world_fact_id).
    """
    MAX_ENTRIES_PER_CAT = 25

    by_category = payload.get("by_category", {}) or {}
    summarized: dict = {}
    truncated_counts: dict = {}
    for cat, facts in by_category.items():
        # Most recent first: sort by id descending
        sorted_facts = sorted(
            facts,
            key=lambda f: (f.get("id") or 0),
            reverse=True,
        )
        kept = sorted_facts[:MAX_ENTRIES_PER_CAT]
        if len(sorted_facts) > MAX_ENTRIES_PER_CAT:
            truncated_counts[cat] = {
                "shown":   len(kept),
                "total":   len(sorted_facts),
                "dropped": len(sorted_facts) - len(kept),
            }
        previews = []
        for f in kept:
            body = f.get("fact", "") or ""
            preview = body if len(body) <= 200 else (body[:197] + "...")
            previews.append({
                "id":              f.get("id"),
                "fact_preview":    preview,
                "source_note":     f.get("source_note"),
            })
        summarized[cat] = previews
    out = {
        "count":          payload.get("count"),
        "categories":     payload.get("categories", []),
        "by_category":    summarized,
        "hint": (
            f"Fact bodies truncated to 200-char previews and per-category "
            f"entries limited to the most recent {MAX_ENTRIES_PER_CAT}. "
            "Use direct SQL on world_facts (world_fact_id) for the full "
            "body of any specific entry, or for entries older than the "
            "shown window."
        ),
    }
    if truncated_counts:
        out["truncated_counts"] = truncated_counts
    return out


def _summarize_realm_state(payload: dict) -> dict:
    """
    For get_realm_state(): drop the verbose `notes` field on every list
    row and the troop/treasury detail dicts; keep counts, names, and
    the summary roll-ups. Caller can fetch full per-table detail via
    list_treasury_accounts, get_loyalty_state, get_domain_state, etc.
    """
    def _strip(rows: list, drop: set) -> list:
        out = []
        for row in (rows or []):
            if isinstance(row, dict):
                out.append({k: v for k, v in row.items() if k not in drop})
            else:
                out.append(row)
        return out

    return {
        "locations": _strip(payload.get("locations", []), {"notes"}),
        "troops":    _strip(payload.get("troops", []), {"notes"}),
        "treasury":  _strip(payload.get("treasury", []), {"notes"}),
        "livestock": _strip(payload.get("livestock", []), {"notes"}),
        "key_npcs":  _strip(payload.get("key_npcs", []),
                            {"notes", "background", "description"}),
        "treasury_summary": payload.get("treasury_summary"),
        "troop_summary":    payload.get("troop_summary"),
        "hint": (
            "All `notes` fields stripped to fit under the response cap. "
            "Use the dedicated list_* / get_* tools for full detail on "
            "any single sub-table."
        ),
    }


def _summarize_recent_history(payload: list) -> list:
    """
    For get_recent_history(): truncate each turn's player_action and
    dm_response to 300-char previews. turn_id stays intact so the caller
    can fetch the full turn via direct SQL on ai_turns if needed.
    """
    summarized = []
    for turn in (payload or []):
        if not isinstance(turn, dict):
            summarized.append(turn)
            continue
        out = dict(turn)
        for k in ("player_action", "dm_response"):
            v = out.get(k) or ""
            if len(v) > 300:
                out[k] = v[:297] + "..."
        summarized.append(out)
    return summarized


def _summarize_domain_state(payload: dict) -> dict:
    """For get_domain_state(): strip notes from every nested list."""
    def _strip(rows: list, drop: set) -> list:
        out = []
        for row in (rows or []):
            if isinstance(row, dict):
                out.append({k: v for k, v in row.items() if k not in drop})
            else:
                out.append(row)
        return out
    out = dict(payload)
    out["holdings"]          = _strip(payload.get("holdings", []), {"notes"})
    out["troops"]             = _strip(payload.get("troops", []), {"notes"})
    out["treasury_accounts"]  = _strip(payload.get("treasury_accounts", []),
                                       {"notes"})
    out["projects"]           = _strip(payload.get("projects", []), {"notes"})
    out["hint"] = (
        "All `notes` fields stripped to fit under the response cap. "
        "Use list_treasury_accounts or direct SQL for full notes."
    )
    return out


def _summarize_loyalty_state(payload: dict) -> dict:
    """For get_loyalty_state(): keep names + scores, drop verbose notes."""
    def _strip_notes(rows: list) -> list:
        out = []
        for row in (rows or []):
            if isinstance(row, dict):
                kept = {k: v for k, v in row.items()
                        if k not in {"notes", "history"}}
                out.append(kept)
            else:
                out.append(row)
        return out
    return {
        "npcs":          _strip_notes(payload.get("npcs", [])),
        "troops":        _strip_notes(payload.get("troops", [])),
        "at_risk":       payload.get("at_risk", []),
        "total_tracked": payload.get("total_tracked"),
        "dm_note":       payload.get("dm_note"),
        "hint": (
            "Notes / loyalty history dropped to fit under the response cap. "
            "Use direct SQL on the relevant table for the full record."
        ),
    }


def _summarize_inventory(payload: dict) -> dict:
    """
    For list_inventory() when the full shape is oversize: degrade to the
    Phase 13 summary_only=True shape — the 5 fields downstream tools
    actually need (inventory_id, name, slot, magic_flag, equipped).
    """
    items = payload.get("items", []) or []
    summary_items = [{
        "inventory_id": it.get("inventory_id"),
        "name":         it.get("name"),
        "slot":         it.get("slot"),
        "magic_flag":   it.get("magic_flag"),
        "equipped":     it.get("equipped"),
    } for it in items]
    return {
        "character_id":  payload.get("character_id"),
        "magic_only":    payload.get("magic_only"),
        "equipped_only": payload.get("equipped_only"),
        "summary_only":  True,
        "count":         payload.get("count"),
        "items":         summary_items,
        "hint": (
            "Auto-degraded to summary_only=True shape because full payload "
            "exceeded the response cap. Pass summary_only=True to silence, "
            "or use magic_only / equipped_only / search_inventory to narrow."
        ),
    }


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
    add_livestock            as db_add_livestock,
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
    # Phase 6 — turn verification
    db_verify_turn,
    db_update_turn_verification,
    # Phase 7 — area pre-population
    db_populate_area,
    db_get_area_encounters,
    db_get_monster_instance,
    db_update_monster_instance,
    db_find_pre_rolled_for_combat,
    db_populate_npc,
    # Phase 34 — full-stat enforcement
    verify_combatant_stats,
    db_regenerate_combatant_stats,
    _REQUIRED_COMBATANT_FIELDS,
    _build_full_monster_stats_block,
    _monster_thac0_from_hd,
    _monster_saves_from_hd,
    _monster_morale_value,
    # Phase 8 — character roster, XP grants, class-level management
    db_grant_xp,
    db_add_class_level,
    db_list_characters,
    _resolve_character,
    # Phase 9 — aerial encounters & reaction rolls
    db_check_aerial_encounter,
    db_roll_reaction,
    # Phase 10 — treasury account create/list
    db_add_treasury_account,
    db_list_treasury_accounts,
    coins_to_gp_equivalent,
    format_coin_total,
    # Phase 29 — trade circuit tracker
    db_add_trade_circuit,
    db_list_trade_circuits,
    db_collect_circuit_income,
    db_get_circuit_ledger,
    db_check_circuits_due,
    # Phase 31 — spellbook
    db_get_spellbook,
    db_add_spell_to_book,
    db_remove_spell_from_book,
    # Phase 12 — equipment slot system
    db_equip_item,
    db_list_equipped,
    db_list_inventory,
    _INVENTORY_SLOTS,
    _WEAPON_TYPES,
    _ensure_inventory_slot_column,
    _ensure_items_combat_columns,
    # Phase 13 — inventory tool refinements
    db_search_inventory,
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
def get_character_state(
    character_id: Annotated[
        int,
        "Optional character_id to fetch. Pass 0 (default) to fetch the PC. "
        "Use list_characters to discover IDs.",
    ] = 0,
    character_name: Annotated[
        str,
        "Optional name (or unique prefix) of the character to fetch — "
        "alternative to character_id. Case-insensitive prefix match. "
        "Ignored when character_id is non-zero.",
    ] = "",
) -> dict:
    """
    Return a complete character state from the database.

    Default behavior (no parameters) returns the PC. Pass character_id or
    character_name to fetch any other character's full sheet — useful for
    henchmen, hirelings, NPCs in the party, prisoners, or any tracked
    character that needs stats inspected.

    Includes: name, race, class levels with XP, current and max HP, AC,
    movement, attacks per round, all six ability scores, full equipped and
    carried inventory with magic item flags, and status notes.

    Call this at session start, when checking henchmen morale, before
    resolving combat for a non-PC, or any time you need to verify a
    character's current stats.
    """
    explicit_lookup = bool(
        (character_id and int(character_id) > 0) or character_name
    )

    if character_id and int(character_id) > 0:
        cid = _resolve_character(int(character_id))
    elif character_name:
        cid = _resolve_character(character_name)
    else:
        cid = None

    # Lookup failed — but the caller asked for a specific character. Surface
    # the error rather than silently returning the PC.
    if explicit_lookup and cid is None:
        return {"error": (f"Character not found for "
                          f"id={character_id!r}, name={character_name!r}. "
                          "Use list_characters to discover available IDs.")}

    if cid is not None:
        char = load_character(character_id=cid)
        if not char:
            return {"error": f"Character not found for "
                             f"id={character_id!r}, name={character_name!r}"}
    else:
        # Default — the PC.
        char = load_character()
        if not char:
            return {"error": "PC not found in database."}

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

    return _cap_response(
        char,
        summary_fn=None,    # atomic record — no summary makes sense
        tool_name="get_character_state",
        error_hint=(
            "Use finer-grained tools instead: get_character_stats"
            "(character_target) for the core sheet (name / race / classes / "
            "abilities / HP / AC / THAC0 / saves) without inventory — that's "
            "the right tool for session start. list_equipped(character_target) "
            "for the slot loadout, list_inventory(character_target, "
            "magic_only=True) for magic items, list_inventory(character_target, "
            "summary_only=True) for the full inventory in trim shape, "
            "get_spell_slots(character_target) for memorized spells. "
            "The HP/AC/abilities header is small enough to fit; only the "
            "inventory roll-up bloats this response."
        ),
    )


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: get_character_stats
# ══════════════════════════════════════════════════════════════════════════════
#
# Core stats sheet — the right tool for "what are my numbers?" at session
# start, replacing get_character_state which now caps because inventory has
# grown too large. Returns identity (name / race / alignment), class roster
# (name / level / xp / THAC0 / saves per class), all six abilities, HP / AC /
# movement / attacks_per_round / status_notes. NO inventory, NO spells —
# fetch those via list_equipped / list_inventory / get_spell_slots when
# needed. Designed to fit comfortably under the 30 KB cap on any character.
# ══════════════════════════════════════════════════════════════════════════════

# Cache classes.json once at import. Keys are the canonical Title-Case
# class names ('Fighter', 'Cleric', 'Magic-User', 'Thief'). The DB stores
# class_name with various conventions (`magic_user`, `Magic-User`,
# `magicuser`); _normalize_class_for_data_lookup matches them robustly.
try:
    with open(_ROOT / "data" / "classes.json", encoding="utf-8") as _f:
        _CLASSES_DATA: dict = json.load(_f)
except Exception:
    _CLASSES_DATA = {}


def _normalize_class_for_data_lookup(stored_name: str) -> str | None:
    """
    Map a class name as stored in the class_levels table to the canonical
    classes.json key (Title-Case). Returns None when no match exists.
    """
    if not stored_name:
        return None
    needle = stored_name.lower().replace("-", "").replace("_", "").replace(" ", "")
    for key in _CLASSES_DATA.keys():
        candidate = key.lower().replace("-", "").replace("_", "").replace(" ", "")
        if candidate == needle or needle in candidate or candidate in needle:
            return key
    return None


def _thac0_for_class_level(class_name: str, level: int) -> int | None:
    """
    Look up THAC0 for a class + level from classes.json, falling back
    sensibly if the exact level isn't in the table (use closest <= level).
    Returns None when the class isn't found.
    """
    canonical = _normalize_class_for_data_lookup(class_name)
    if not canonical:
        return None
    table = _CLASSES_DATA.get(canonical, {}).get("thac0_by_level", {}) or {}
    if not table:
        return None
    if str(level) in table:
        return int(table[str(level)])
    # Fall back to closest level <= requested
    avail = sorted([int(k) for k in table if k.isdigit() and int(k) <= level])
    if avail:
        return int(table[str(avail[-1])])
    return None


def _saves_for_class_level(class_name: str, level: int) -> dict | None:
    """
    Look up saves dict ({'death':N, 'wands':N, 'paralysis':N, 'breath':N,
    'spells':N}) for a class + level. Same fallback logic as THAC0.
    """
    canonical = _normalize_class_for_data_lookup(class_name)
    if not canonical:
        return None
    table = _CLASSES_DATA.get(canonical, {}).get("saves_by_level", {}) or {}
    if not table:
        return None
    if str(level) in table:
        return dict(table[str(level)])
    avail = sorted([int(k) for k in table if k.isdigit() and int(k) <= level])
    if avail:
        return dict(table[str(avail[-1])])
    return None


# ── Phase 35: monster / NPC visual reference helpers ─────────────────────────
# Every tool that surfaces a creature appends a `visual_refs` field at the
# bottom of its response: one Google-image-search link per unique creature
# name, blank-line separator. Tools that don't surface a creature omit it.

def _visual_ref_url(name: str) -> str:
    """Build the Google image-search URL for one creature/NPC name."""
    encoded = (name or "").strip().replace(" ", "+")
    return f"https://www.google.com/search?q={encoded}+1e+D%26D&tbm=isch"


def _visual_ref_line(name: str) -> str:
    """Format one '🎨 [Name visual](url)' line."""
    return f"🎨 [{(name or '').strip()} visual]({_visual_ref_url(name)})"


def _visual_refs_block(names) -> str | None:
    """
    Build the `visual_refs` block from a sequence of creature/NPC names.

      - Dedups case-insensitively, first-seen order wins.
      - Returns None when no names survive (caller omits the field).
      - Output starts with a blank line and contains one link per unique
        name, so when concatenated after other narration the spec's
        "separated by a blank line" rule is satisfied.

    Pass an empty list (or all-blank strings) to get None back — that is
    how tools signal "no creature in this response".
    """
    if not names:
        return None
    if isinstance(names, str):
        names = [names]
    seen: set[str] = set()
    uniq: list[str] = []
    for n in names:
        if n is None:
            continue
        nm = str(n).strip()
        if not nm:
            continue
        key = nm.lower()
        if key in seen:
            continue
        seen.add(key)
        uniq.append(nm)
    if not uniq:
        return None
    return "\n" + "\n".join(_visual_ref_line(n) for n in uniq)


@mcp.tool()
def get_character_stats(
    character_target: Annotated[
        str,
        "Optional name (case-insensitive prefix match) or numeric "
        "character_id. Leave blank to default to the PC. Same resolution "
        "as the rest of the character_target tools.",
    ] = "",
) -> dict:
    """
    Return ONLY the core stats — no inventory, no spells.

    The right tool for session start: 'what are my numbers?'. Designed to
    fit comfortably under the 30 KB response cap regardless of how big
    the inventory has grown. Use list_equipped / list_inventory /
    get_spell_slots for the rest.

    Returns:
      character_id, name, race, alignment, character_type
      classes:    [{class_name, level, xp, thac0, saves}, ...]
      abilities:  {str, int, wis, dex, con, cha}
      hp_current, hp_max
      ac
      thac0_best, saves_best  — multi-class rollup (lowest THAC0,
                                lowest target per save category across
                                all of this character's classes)
      movement, attacks_per_round, status_notes

    Errors clearly when:
      - character_target doesn't resolve
      - the character has no character_status row (HP/AC tracking missing)
    """
    cid: int | None = None
    if (character_target or "").strip():
        cid = _resolve_character(character_target)
        if cid is None:
            return {
                "error": (
                    f"character_target {character_target!r} did not resolve "
                    "— use list_characters to discover available names/ids."
                ),
            }
    from engine.db import (_get_conn as _ec,
                           _PC_CHARACTER_ID as _pc_id)
    target_id = cid if cid is not None else _pc_id

    with _ec(read_only=True) as conn:
        cur = conn.cursor()
        # ── Identity ────────────────────────────────────────────────────────
        cur.execute(
            "SELECT character_id, name, character_type, race, alignment, notes "
            "FROM characters WHERE character_id = ?",
            (target_id,),
        )
        ch = cur.fetchone()
        if not ch:
            return {
                "error": f"No character row with character_id={target_id}.",
            }
        # ── Class levels ───────────────────────────────────────────────────
        cur.execute(
            "SELECT class_name, level, xp FROM class_levels "
            "WHERE character_id = ? ORDER BY class_name",
            (target_id,),
        )
        cls_rows = [dict(r) for r in cur.fetchall()]
        # ── Status ─────────────────────────────────────────────────────────
        cur.execute(
            "SELECT hp_current, hp_max, ac, movement, attacks_per_round, "
            "       status_notes "
            "FROM character_status WHERE character_id = ?",
            (target_id,),
        )
        status = cur.fetchone()
        if status is None:
            return {
                "error": (
                    f"character_id={target_id} has no character_status row "
                    "— stats unavailable. Initialize via update_character_status."
                ),
            }
        status = dict(status)
        # ── Abilities ──────────────────────────────────────────────────────
        cur.execute(
            "SELECT strength, intelligence, wisdom, dexterity, constitution, "
            "       charisma "
            "FROM character_abilities WHERE character_id = ?",
            (target_id,),
        )
        abi = cur.fetchone()
    abi = dict(abi) if abi else {}

    # ── Per-class derived stats (THAC0 + saves) ───────────────────────────
    classes_out = []
    thac0_values: list[int] = []
    saves_acc: dict[str, list[int]] = {}
    for c in cls_rows:
        cname = c.get("class_name") or ""
        lvl   = int(c.get("level") or 1)
        thac0 = _thac0_for_class_level(cname, lvl)
        saves = _saves_for_class_level(cname, lvl) or {}
        classes_out.append({
            "class_name": cname,
            "level":      lvl,
            "xp":         c.get("xp"),
            "thac0":      thac0,
            "saves":      saves,
        })
        if isinstance(thac0, int):
            thac0_values.append(thac0)
        for cat, target in saves.items():
            saves_acc.setdefault(cat, []).append(int(target))

    # Multi-class rollup: best (lowest) THAC0, best (lowest) save target per category.
    thac0_best = min(thac0_values) if thac0_values else None
    saves_best = {cat: min(vals) for cat, vals in saves_acc.items()} or None

    payload = {
        "character_id":      target_id,
        "name":              ch["name"],
        "race":              ch["race"],
        "alignment":         ch["alignment"],
        "character_type":    ch["character_type"],
        "classes":           classes_out,
        "abilities": {
            "str": abi.get("strength"),
            "int": abi.get("intelligence"),
            "wis": abi.get("wisdom"),
            "dex": abi.get("dexterity"),
            "con": abi.get("constitution"),
            "cha": abi.get("charisma"),
        },
        "hp_current":        status.get("hp_current"),
        "hp_max":            status.get("hp_max"),
        "ac":                status.get("ac"),
        "thac0_best":        thac0_best,
        "saves_best":        saves_best,
        "movement":          status.get("movement"),
        "attacks_per_round": status.get("attacks_per_round"),
        "status_notes":      status.get("status_notes"),
    }
    # Cap as a safety net only — this tool is designed to be small. If a
    # character somehow has a 50-class roster the cap will fire with a
    # helpful error directing the caller to list_characters.
    return _cap_response(
        payload,
        summary_fn=None,
        tool_name="get_character_stats",
        error_hint=(
            "Stats payload unexpectedly oversized — usually caused by a "
            "character with an enormous class roster or unusual notes. "
            "Use list_characters to inspect the basic row, or direct SQL "
            "on character_status / character_abilities for the raw fields."
        ),
    )


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: get_saving_throws
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_saving_throws(
    character_target: Annotated[
        str,
        "Optional name (case-insensitive prefix match) or numeric "
        "character_id. Leave blank to default to the PC.",
    ] = "",
) -> dict:
    """
    Return ONLY the five AD&D 1e saving throw target numbers.

    Compact specialty tool for "what does Caiya save at vs. spells?"
    style lookups. ~150 bytes — fits anywhere. For multi-class
    characters, returns the BEST (lowest) target per category across
    all classes.

    Returns:
      character_id, name
      saves: {death, wands, paralysis, breath, spells} — best of each
      saves_by_class: per-class breakdown {class_name: {death, ...}}
        so the caller can see which class is providing the best save
        in each category if relevant.

    Errors clearly when the target doesn't resolve or has no class_levels.
    """
    cid: int | None = None
    if (character_target or "").strip():
        cid = _resolve_character(character_target)
        if cid is None:
            return {
                "error": (
                    f"character_target {character_target!r} did not resolve "
                    "— use list_characters to discover available names/ids."
                ),
            }
    from engine.db import (_get_conn as _ec,
                           _PC_CHARACTER_ID as _pc_id)
    target_id = cid if cid is not None else _pc_id

    with _ec(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT name FROM characters WHERE character_id = ?",
            (target_id,),
        )
        nm = cur.fetchone()
        if not nm:
            return {"error": f"No character with character_id={target_id}."}
        cur.execute(
            "SELECT class_name, level FROM class_levels "
            "WHERE character_id = ? ORDER BY class_name",
            (target_id,),
        )
        cls_rows = [dict(r) for r in cur.fetchall()]

    if not cls_rows:
        return {
            "error": (
                f"character_id={target_id} has no class_levels rows — "
                "saves cannot be derived. Use add_class_level to "
                "populate at least one class."
            ),
        }

    saves_by_class: dict = {}
    saves_acc: dict[str, list[int]] = {}
    for c in cls_rows:
        cname = c.get("class_name") or ""
        lvl   = int(c.get("level") or 1)
        saves = _saves_for_class_level(cname, lvl) or {}
        if saves:
            saves_by_class[cname] = saves
            for cat, target in saves.items():
                saves_acc.setdefault(cat, []).append(int(target))

    saves_best = ({cat: min(v) for cat, v in saves_acc.items()}
                  if saves_acc else None)

    return {
        "character_id":   target_id,
        "name":           nm["name"],
        "saves":          saves_best,
        "saves_by_class": saves_by_class,
    }


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: get_combat_summary
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_combat_summary(
    character_target: Annotated[
        str,
        "Optional name (case-insensitive prefix match) or numeric "
        "character_id. Leave blank to default to the PC.",
    ] = "",
) -> dict:
    """
    Return a compact combat card — ~250 bytes — for use mid-combat or
    for "what are Caiya's swing numbers?" lookups.

    Returns:
      character_id, name
      thac0           -- best (lowest) across the character's classes
      ac, hp_current, hp_max
      attacks_per_round
      mainhand        -- {name, damage_dice, damage_bonus, to_hit_bonus,
                          weapon_type} for the currently equipped
                          mainhand weapon (or null when no mainhand)
      offhand         -- same shape for offhand (or null)

    No abilities, no saves, no inventory roll-up — fetch those via
    get_character_stats / get_saving_throws / list_inventory.
    """
    cid: int | None = None
    if (character_target or "").strip():
        cid = _resolve_character(character_target)
        if cid is None:
            return {
                "error": (
                    f"character_target {character_target!r} did not resolve "
                    "— use list_characters to discover available names/ids."
                ),
            }
    from engine.db import (_get_conn as _ec,
                           _PC_CHARACTER_ID as _pc_id)
    target_id = cid if cid is not None else _pc_id

    with _ec(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT name FROM characters WHERE character_id = ?",
            (target_id,),
        )
        nm_row = cur.fetchone()
        if not nm_row:
            return {"error": f"No character with character_id={target_id}."}
        cur.execute(
            "SELECT class_name, level FROM class_levels "
            "WHERE character_id = ?",
            (target_id,),
        )
        cls_rows = [dict(r) for r in cur.fetchall()]
        cur.execute(
            "SELECT hp_current, hp_max, ac, attacks_per_round "
            "FROM character_status WHERE character_id = ?",
            (target_id,),
        )
        st_row = cur.fetchone()
        # Equipped mainhand + offhand (if any) with their combat fields
        cur.execute(
            "SELECT inv.slot, i.name, i.damage_dice, i.damage_bonus, "
            "       i.to_hit_bonus, i.weapon_type "
            "FROM inventory inv JOIN items i ON i.item_id = inv.item_id "
            "WHERE inv.character_id = ? "
            "  AND inv.slot IN ('mainhand', 'offhand')",
            (target_id,),
        )
        slot_rows = [dict(r) for r in cur.fetchall()]

    # Best (lowest) THAC0 across all classes
    thac0_values: list[int] = []
    for c in cls_rows:
        t = _thac0_for_class_level(
            c.get("class_name") or "",
            int(c.get("level") or 1),
        )
        if isinstance(t, int):
            thac0_values.append(t)
    thac0_best = min(thac0_values) if thac0_values else None

    st = dict(st_row) if st_row else {}
    by_slot = {r["slot"]: r for r in slot_rows}
    def _weapon_card(slot: str) -> dict | None:
        r = by_slot.get(slot)
        if not r:
            return None
        return {
            "name":         r["name"],
            "damage_dice":  r["damage_dice"],
            "damage_bonus": r["damage_bonus"] or 0,
            "to_hit_bonus": r["to_hit_bonus"] or 0,
            "weapon_type":  r["weapon_type"],
        }

    return {
        "character_id":      target_id,
        "name":              nm_row["name"],
        "thac0":             thac0_best,
        "ac":                st.get("ac"),
        "hp_current":        st.get("hp_current"),
        "hp_max":            st.get("hp_max"),
        "attacks_per_round": st.get("attacks_per_round"),
        "mainhand":          _weapon_card("mainhand"),
        "offhand":           _weapon_card("offhand"),
    }


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: get_realm_state
# ══════════════════════════════════════════════════════════════════════════════

_REALM_SECTIONS = {"all", "locations", "troops", "treasury", "livestock", "npcs"}


@mcp.tool()
def get_realm_state(
    section: Annotated[
        str,
        "Which slice to return. One of: 'all' (default — every section, "
        "may auto-degrade on rich campaigns), 'locations', 'troops', "
        "'treasury', 'livestock', 'npcs'. Single-section calls return "
        "just that list plus the relevant summary roll-up (e.g. "
        "section='treasury' returns {treasury, treasury_summary}). Use "
        "the focused sections on rich campaigns to avoid the auto-cap.",
    ] = "all",
) -> dict:
    """
    Return state of the realm (locations / troops / treasury / livestock / NPCs).

    section='all' (default) returns every section. On large campaigns
    this may exceed the 30 KB cap and auto-degrade to a summary shape
    (notes stripped). For surgical access prefer section='troops' /
    'treasury' / etc — each single-section call carries far less data
    and stays well under the cap.

    Always returns the relevant summary roll-up alongside the requested
    section: treasury_summary with treasury, troop_summary with troops.
    """
    sec = (section or "all").strip().lower()
    if sec not in _REALM_SECTIONS:
        return {
            "error": f"section must be one of {sorted(_REALM_SECTIONS)}; "
                     f"got {section!r}.",
            "allowed_sections": sorted(_REALM_SECTIONS),
        }

    realm = load_realm()
    if not realm:
        return {"error": "Realm data not found in database."}

    # Summary roll-ups — always computed, included with their related section.
    _t_accts   = realm.get("treasury", [])
    pp_total   = sum(a.get("pp", 0) or 0 for a in _t_accts)
    gp_total   = sum(a.get("gp", 0) or 0 for a in _t_accts)
    sp_total   = sum(a.get("sp", 0) or 0 for a in _t_accts)
    cp_total   = sum(a.get("cp", 0) or 0 for a in _t_accts)
    gems_total = sum(a.get("gems_gp_value", 0) or 0 for a in _t_accts)
    _coin_equiv = coins_to_gp_equivalent(pp_total, gp_total, 0, sp_total, cp_total)
    treasury_summary = {
        "pp":                  pp_total,
        "gp":                  gp_total,
        "ep":                  0,
        "sp":                  sp_total,
        "cp":                  cp_total,
        "gems_gp_value":       gems_total,
        "total_gp_equivalent": _coin_equiv + gems_total,
        "formatted_total":     format_coin_total(pp_total, gp_total, 0, sp_total, cp_total),
    }
    troop_total = sum(t.get("count", 0) or 0 for t in realm.get("troops", []))
    troop_summary = {"total_troops": troop_total}

    if sec == "all":
        realm["treasury_summary"] = treasury_summary
        realm["troop_summary"]    = troop_summary
        return _cap_response(
            realm,
            summary_fn=_summarize_realm_state,
            tool_name="get_realm_state",
        )

    # Single-section response.
    section_payload: dict = {"section": sec}
    if sec == "locations":
        section_payload["locations"] = realm.get("locations", [])
    elif sec == "troops":
        section_payload["troops"]        = realm.get("troops", [])
        section_payload["troop_summary"] = troop_summary
    elif sec == "treasury":
        section_payload["treasury"]         = realm.get("treasury", [])
        section_payload["treasury_summary"] = treasury_summary
    elif sec == "livestock":
        section_payload["livestock"] = realm.get("livestock", [])
    elif sec == "npcs":
        section_payload["key_npcs"] = realm.get("key_npcs", [])

    return _cap_response(
        section_payload,
        summary_fn=_summarize_realm_state,
        tool_name=f"get_realm_state(section={sec})",
    )


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: roll_dice
# ══════════════════════════════════════════════════════════════════════════════

_DICE_RE = re.compile(
    r"^\s*(?:(\d+)\s*[dD])?\s*(\d+)\s*([+-]\s*\d+)?\s*$"
)

def _parse_dice(expression: str) -> tuple[int, int, int, int]:
    """
    Parse a dice expression into (num_dice, die_sides, modifier, multiplier).
    Accepts:
      "d20", "1d20", "3d6", "3d6+2", "2d8-1"     standard
      "2d6*50", "2d6×50", "1d6 * 100"            multiplication (gold etc.)
      "2d6*50+10"                                 multiply then add modifier
      "20"                                        flat number (no dice)

    Multiplication semantics: result = (sum_of_dice + modifier) * multiplier.
    The multiplier defaults to 1.

    Returns (num_dice, sides, modifier, multiplier). Raises ValueError on bad
    input.
    """
    # Normalize unicode multiply sign to ASCII '*' so the regex stays simple.
    expr = expression.strip().replace("×", "*").replace("×", "*")

    # Handle bare integer (no dice)
    if re.match(r"^\d+$", expr):
        return (0, 0, int(expr), 1)

    # Standard dice with optional modifier and optional trailing *N multiplier.
    m = re.match(
        r"^(\d+)?[dD](\d+)\s*([+-]\s*\d+)?\s*(?:\*\s*(\d+))?$",
        expr.replace(" ", ""),
    )
    if not m:
        raise ValueError(f"Cannot parse dice expression: '{expression}'")

    num     = int(m.group(1)) if m.group(1) else 1
    sides   = int(m.group(2))
    mod_str = (m.group(3) or "0").replace(" ", "")
    mod     = int(mod_str)
    mult    = int(m.group(4)) if m.group(4) else 1

    if num < 1:
        raise ValueError("Number of dice must be at least 1.")
    if sides < 2:
        raise ValueError("Die must have at least 2 sides.")
    if num > 100:
        raise ValueError("Maximum 100 dice per roll.")
    if mult < 1:
        raise ValueError("Multiplier must be at least 1.")
    if mult > 10000:
        raise ValueError("Multiplier capped at 10,000.")

    return (num, sides, mod, mult)


@mcp.tool()
def roll_dice(
    expression: Annotated[
        str,
        "Dice expression to evaluate. Examples: '1d20', 'd6', '3d6+2', "
        "'2d8-1', '7d6'. Multiplication: '2d6*50' (rolls 2d6 then ×50; "
        "common for gold haul / encumbrance), '1d6×100' (Unicode × is "
        "also accepted), '2d6*50+10' (modifier first, then multiply). "
        "Use standard NdS+M[*K] notation.",
    ],
    label: Annotated[
        str,
        "Optional label for this roll, e.g. 'attack vs goblin', 'fireball damage'.",
    ] = "",
) -> dict:
    """
    Roll any standard dice expression and return the full breakdown.

    ALWAYS use this tool for mechanical outcomes — attack rolls, damage,
    saving throws, random encounters, ability checks, morale, gold haul.
    Never invent dice results. The engine is the source of truth.

    Multiplication: NdS[+M]*K rolls NdS, applies the modifier, then
    multiplies the result by K. Useful for AD&D 1e gold rolls (e.g.
    treasure type 'A' has '2d6*1000 gp' for the copper line) and any
    other "roll then scale" mechanic. Both '*' and '×' are accepted.

    Returns:
      expression       -- the expression as given
      label            -- the label as given
      num_dice         -- number of dice rolled
      die_sides        -- sides on each die
      modifier         -- flat modifier applied
      multiplier       -- final multiplier (default 1)
      individual_rolls -- list of each die result
      subtotal         -- sum of dice (no modifier, no multiplier)
      pre_multiply     -- subtotal + modifier (the value before *K)
      total            -- pre_multiply * multiplier (final answer)
      natural_20       -- true if 1d20 and result was 20 (attack rolls)
      natural_1        -- true if 1d20 and result was 1 (fumble)
    """
    try:
        num, sides, mod, mult = _parse_dice(expression)
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
            "multiplier":       mult,
            "individual_rolls": [],
            "subtotal":         0,
            "pre_multiply":     mod,
            "total":            mod * mult,
            "natural_20":       False,
            "natural_1":        False,
        }

    rolls    = [random.randint(1, sides) for _ in range(num)]
    subtotal = sum(rolls)
    pre_mult = subtotal + mod
    total    = pre_mult * mult

    return {
        "expression":       expression,
        "label":            label,
        "num_dice":         num,
        "die_sides":        sides,
        "modifier":         mod,
        "multiplier":       mult,
        "individual_rolls": rolls,
        "subtotal":         subtotal,
        "pre_multiply":     pre_mult,
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

    Every response carries a contract_footer field — a one-line reminder
    that the DM contract is active. The DM should treat that footer as
    binding for the response it is about to write.
    """
    scene = load_current_scene()

    # Enrich with recent history for context even when a scene record exists
    recent = load_recent_ai_turns(limit=3)
    last_action   = recent[-1]["player_action"]  if recent else ""
    last_response = recent[-1]["dm_response"]    if recent else ""

    # Phase 35: if there's an active combat, surface a visual_refs block for
    # each unique enemy group so the DM has the image search ready while
    # narrating. No combat / no creature in scene state ⇒ no link.
    creature_names: list[str] = []
    active = get_active_combat()
    if active:
        groups = active.get("groups") or {}
        creature_names.extend(sorted(groups.keys()))

    if scene:
        scene["last_player_action"]  = last_action
        scene["last_dm_response_preview"] = last_response[:300] if last_response else ""
        scene["contract_footer"] = _DM_CONTRACT_FOOTER
        block = _visual_refs_block(creature_names)
        if block:
            scene["visual_refs"] = block
        return scene

    # No scene record yet — build a default from DB context
    out = {
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
        "contract_footer":             _DM_CONTRACT_FOOTER,
    }
    block = _visual_refs_block(creature_names)
    if block:
        out["visual_refs"] = block
    return out


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
        "Free-text prose summary of what happened this turn (for the human-readable "
        "history log only). Verification does NOT parse this — use the markers "
        "parameter for state changes.",
    ] = "",
    markers: MarkersField = [],
    markers_str: Annotated[
        str,
        Field(description=(
            "DEPRECATED — accepted for backward compatibility but IGNORED. "
            "The contents are not merged into markers. Use the markers "
            "array instead. Sending a non-empty value raises a "
            "deprecation_warning in the response so callers learn to "
            "drop the field. Will be removed in a future engine pass."
        )),
    ] = "",
    markers_json: Annotated[
        str,
        Field(description=(
            "DEPRECATED — accepted for backward compatibility but IGNORED. "
            "The contents are not merged into markers. Use the markers "
            "array instead. Sending a non-empty value raises a "
            "deprecation_warning in the response so callers learn to "
            "drop the field. Will be removed in a future engine pass."
        )),
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
    Returns the new turn_id, verification result, and any unverified or
    conflicting state changes that need follow-up tool calls.

    ════════════════════════════════════════════════════════════════════════
    STATE-CHANGE MARKERS  (REQUIRED FOR VERIFICATION)
    ════════════════════════════════════════════════════════════════════════

    Every turn that mutates game state MUST declare its changes via markers.
    Prose in scene_notes is NOT parsed — verify_turn reads only the
    structured markers. A turn with no markers returns verdict 'no_claims'
    (silence ≠ verified).

    ┌──────────────────────────────────────────────────────────────────────┐
    │ HOW TO PASS MARKERS — single canonical format                        │
    ├──────────────────────────────────────────────────────────────────────┤
    │ markers = array of strings, one marker per state change.             │
    │                                                                      │
    │   markers=["cast:Charm Person", "hp:41>38",                          │
    │            "item_added:Bone token"]                                  │
    │                                                                      │
    │ Phase 14 instrumented three formats (markers, markers_json,          │
    │ markers_str) to chase a long-standing bug where non-empty arrays     │
    │ were rumored to drop in transport. Live testing on Day 1141          │
    │ proved the array survives intact in this client/transport — the     │
    │ "drop" was upstream and is no longer reproducible. Phase 15 locks   │
    │ the array as the only ingress path and removes the workarounds      │
    │ to eliminate the silent-precedence data-loss risk that arose when    │
    │ both formats were passed in the same call.                           │
    │                                                                      │
    │ The Pydantic BeforeValidator is still tolerant of single strings,    │
    │ JSON-array literals, and Python repr — passed through to the same    │
    │ _normalize_markers splitter — so an off-format value still has a     │
    │ chance to land. Empty array (or omitted) means nothing changed.      │
    └──────────────────────────────────────────────────────────────────────┘

    One marker per state change. Use these exact prefixes:

      cast:[spell name]                A spell was expended this turn.
                                       Example: "cast:Magic Missile"

      item_added:[item name]           PC gained an item.
                                       Example: "item_added:Potion of Healing"

      item_used:[item name]            PC consumed / lost / dropped an item.
                                       Example: "item_used:Torch"

      hp:[old]>[new]                   HP changed. Both integers.
                                       Example: "hp:31>23"

      spent:[amount]gp                 Gold deducted. Integer, no commas.
                                       Example: "spent:500gp"

      gained:[amount]gp                Gold added. Integer, no commas.
                                       Example: "gained:200gp"

      npc_added:[name]                 New NPC appeared.
                                       Example: "npc_added:Merchant Grel"

      location_changed:[name]          Party moved. Also pass scene_location
                                       so verify can confirm.
                                       Example: "location_changed:north wall"

      troop_change:[group]:[old]>[new] Troop count changed. Both integers.
                                       Example: "troop_change:Iron Watch:120>108"

    Phase 20 — domain/realm state additions. Pick the prefix that matches
    what changed; the verifier routes by prefix to the right table and
    emits the correct suggested_call. Using item_added: for livestock or
    troop_added: for an NPC produces the wrong remediation suggestion:

      livestock_added:[type]:[count]:[location]
                                       New livestock at a location.
                                       Example: "livestock_added:Sheep:65:Daral-Ra'ahd Estate"

      troop_added:[group]:[count]:[location]
                                       New troop group recruited/arrived.
                                       Example: "troop_added:Wyvern Cohort:30:The Moathouse"

      project_added:[name]             New construction project queued.
                                       Example: "project_added:Moathouse Granary"

      location_added:[name]            New location discovered/claimed/built.
                                       Example: "location_added:Pale Chapel Fief"

    Always pair markers with the underlying tool call (update_character_status,
    cast_spell, update_treasury, add_livestock, add_troop_group, etc.). The
    marker is a *claim*; the tool call is the *write*. verify_turn checks
    the claim against the write.
    ════════════════════════════════════════════════════════════════════════
    """
    # ── Markers normalization (Phase 15: single ingress path) ─────────────
    # The Pydantic BeforeValidator (_normalize_markers) has already done
    # the heavy lifting — it accepts list[str], a single string, JSON
    # array literals, Python repr, newline/comma-separated strings, and
    # None, and produces a clean list[str]. All we do here is filter
    # empties and pass the rest to verify_turn.
    clean_markers = [
        m.strip() for m in (markers or [])
        if isinstance(m, str) and m.strip()
    ]

    # ── Phase 16: deprecated-stub warnings (replace silent drop) ─────────
    # markers_str / markers_json are kept in the schema so old client code
    # doesn't crash, but their contents are ignored entirely — never merged
    # into clean_markers (that was the silent-precedence bug from turn 74).
    # If the caller sent a non-empty value we surface a loud warning in
    # the response so the model learns to drop the field.
    deprecation_warnings: list[str] = []
    if (markers_str or "").strip():
        deprecation_warnings.append(
            "markers_str is deprecated and ignored — use the markers "
            "array instead. Live testing on turns 72-74 confirmed the "
            "array path is reliable; the markers_str workaround is no "
            "longer needed and will be removed in a future engine pass."
        )
    if (markers_json or "").strip():
        deprecation_warnings.append(
            "markers_json is deprecated and ignored — use the markers "
            "array instead. Live testing on turns 72-74 confirmed the "
            "array path is reliable; the markers_json workaround is no "
            "longer needed and will be removed in a future engine pass."
        )

    # Standard logging: one line per save_turn so we can spot a
    # regression of the "non-empty array arrives as None" bug
    # immediately if it ever resurfaces. Cross-reference with the
    # 'normalize_markers' channel entries to confirm what Pydantic saw.
    _log_mcp_debug("save_turn", {
        "markers_type":             type(markers).__name__,
        "markers_count":            len(markers or []),
        "clean_count":              len(clean_markers),
        "scene_location_set":       bool(scene_location),
        "scene_notes_length":       len(scene_notes or ""),
        "markers_str_length":       len(markers_str or ""),
        "markers_json_length":      len(markers_json or ""),
        "deprecation_warning_count": len(deprecation_warnings),
    })

    structured: dict = {}
    if scene_location:
        structured["location"] = scene_location
    if scene_notes:
        structured["state_changes"] = scene_notes
    if clean_markers:
        structured["markers"] = clean_markers

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

    # ── Auto-verify: parse markers, cross-check DB ────────────────────────────
    verification = db_verify_turn(turn_id=turn_id)
    db_update_turn_verification(turn_id, json.dumps(verification))

    result: dict = {
        "saved":        True,
        "turn_id":      turn_id,
        "location":     scene_location or "(unchanged)",
        "notes_stored": bool(scene_notes),
        "verification": verification,
        # Cheap diagnostics so the DM can confirm markers arrived. If
        # markers_received_raw_type ever shows "NoneType" with a
        # markers_received_count of 0 on a turn the model claims to
        # have populated, the Phase-14-era array-drop bug has
        # resurfaced — pull logs/mcp_debug.log channel
        # 'normalize_markers' for the raw input the validator saw.
        "markers_received_raw_type": type(markers).__name__,
        "markers_received_count":    len(markers or []),
        "markers_normalized":        clean_markers,
        "world_fact_reminder": (
            "Check this turn for anything that should be written to the database "
            "immediately — do not let it live only in chat history. Call "
            "update_world_fact for: named NPCs encountered or mentioned, items "
            "acquired or lost, decisions made, quests opened or closed, rulings "
            "established, alliances or hostilities formed. Call add_npc for any "
            "new named character. Call add_item for any new item the PC now carries."
        ),
    }

    # If markers came in with content but everything got filtered out
    # (whitespace-only strings, non-string members), surface the
    # canonical format help so the AI knows how to retry without
    # burning another turn.
    if markers and not clean_markers:
        result["markers_format_help"] = CANONICAL_MARKER_FORMAT_HELP

    # Phase 16: surface deprecated-stub warnings loudly. Either
    # `deprecation_warning` (single) or `deprecation_warnings` (multiple)
    # appears in the response so the model can't miss it.
    if deprecation_warnings:
        if len(deprecation_warnings) == 1:
            result["deprecation_warning"] = deprecation_warnings[0]
        else:
            result["deprecation_warnings"] = deprecation_warnings

    # Surface conflicts prominently so the DM sees them immediately
    if verification.get("conflicts"):
        result["CONFLICTS_DETECTED"] = [
            c["suggested_call"] for c in verification["conflicts"]
            if c.get("suggested_call")
        ]
    if verification.get("unverified"):
        result["unverified_claims"] = [
            u.get("suggested_call", u.get("claim"))
            for u in verification["unverified"]
        ]

    return result


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: get_recent_history
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_recent_history(
    n: Annotated[
        int,
        "Number of recent turns to return. Default 5, max 20. Alias of "
        "limit; keep for back-compat.",
    ] = 5,
    limit: Annotated[
        int,
        "Number of recent turns to return. Default 5, max 20. Preferred "
        "over n. When both are passed, limit wins.",
    ] = 0,
) -> list[dict]:
    """
    Return the last N turns from the ai_turns table in chronological order.

    Either parameter (n or limit) can be used; limit is the preferred name
    going forward (matches the rest of the read-tool family). Both are
    clamped to 1..20 — the tool will never return more than 20 turns.

    Each turn includes: turn_id, player_action, dm_response, model_name,
    created_at. Use this to re-establish context when resuming a session,
    resolve continuity questions, or review what was narrated recently.

    Keep small (5-10) for normal session resumption. Use larger values
    only when the player explicitly asks about earlier events. The cap
    auto-degrades by truncating each turn's text to 300-char previews
    if the result is still too large.
    """
    # `limit` wins when explicitly passed (>0); else fall back to `n`.
    eff = limit if (limit and limit > 0) else n
    eff = max(1, min(int(eff or 5), 20))  # clamp 1..20
    turns = load_recent_ai_turns(limit=eff)
    # Cap at 30 KB; auto-degrade by truncating each turn's
    # player_action / dm_response to 300-char previews. This tool returns
    # a list (not a dict), so we apply the cap manually rather than
    # routing through _cap_response (which is dict-shaped).
    if _payload_bytes(turns) > DEFAULT_RESPONSE_CAP_BYTES:
        original_kb = round(_payload_bytes(turns) / 1024, 1)
        summarized = _summarize_recent_history(turns)
        returned_kb = round(_payload_bytes(summarized) / 1024, 1)
        _log_mcp_debug("response_capped.degraded", {
            "tool":         "get_recent_history",
            "original_kb":  original_kb,
            "returned_kb":  returned_kb,
        })
        # Append a meta sentinel as the last list element so callers
        # iterating the list see it without a separate response shape.
        return summarized + [{
            "_response_meta": {
                "degraded":     True,
                "original_kb":  original_kb,
                "returned_kb":  returned_kb,
                "cap_kb":       round(DEFAULT_RESPONSE_CAP_BYTES / 1024),
                "hint": (
                    "Each turn's player_action and dm_response truncated "
                    "to 300-char previews to fit under cap. Use direct "
                    "SQL on ai_turns(turn_id) for the full body of any "
                    "specific turn."
                ),
            },
        }]
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
    character_target: Annotated[
        str,
        "Optional name (case-insensitive prefix match) or numeric "
        "character_id of the target character. Leave blank to default "
        "to the PC. Lets henchmen / hirelings / NPC party members have "
        "their HP / AC / status updated through the same tool — "
        "previously the only path was direct_db_edit on character_status.",
    ] = "",
) -> dict:
    """
    Update a character's mutable combat status in the database.

    Call this whenever HP changes (combat damage, healing, rest), AC changes
    (armor removed, magical effects), or status conditions change. Only
    fields you provide are written — all others remain untouched.

    Defaults to the PC. Pass character_target='Caiya' (or any tracked
    henchman / hireling name, or a numeric character_id) to update a
    different character. Errors clearly if the name doesn't resolve.

    Returns the full updated status row as confirmation.
    """
    try:
        cid: int | None = None
        if (character_target or "").strip():
            cid = _resolve_character(character_target)
            if cid is None:
                return {
                    "error": (
                        f"character_target {character_target!r} did not "
                        "resolve — use list_characters to discover "
                        "available names/ids."
                    ),
                }
        # db_update_character_status defaults character_id to the PC when
        # we pass nothing.
        if cid is None:
            result = db_update_character_status(
                hp_current=hp_current, hp_max=hp_max,
                ac=ac, status_notes=status_notes,
            )
        else:
            result = db_update_character_status(
                character_id=cid,
                hp_current=hp_current, hp_max=hp_max,
                ac=ac, status_notes=status_notes,
            )
        return {"updated": True, "character_id": cid, **result}
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
    ep_delta: Annotated[
        int,
        "Electrum pieces to add or subtract. AD&D 1e: 1 EP = 0.5 GP. "
        "Often skipped — set when looted treasure includes electrum.",
    ] = 0,
    gems_delta: Annotated[int, "Gem value in GP to add or subtract."] = 0,
    new_account_name: Annotated[
        str,
        "Optional new name for this treasury account. Leave blank to "
        "leave the name unchanged. The new name must be non-empty and "
        "must not collide with another treasury account in the campaign "
        "(case-insensitive). Pass all-zero deltas with new_account_name "
        "to perform a pure rename.",
    ] = "",
) -> dict:
    """
    Add or subtract coins/gems from a treasury account, and optionally
    rename the account.

    Supports all five AD&D 1e coin denominations:
      pp_delta — platinum (1 PP = 5 GP)
      gp_delta — gold     (1 GP = 1 GP)
      ep_delta — electrum (1 EP = 0.5 GP)
      sp_delta — silver   (1 SP = 0.05 GP, NOT 0.1 — that is D&D 5e)
      cp_delta — copper   (1 CP = 0.005 GP, NOT 0.01 — that is D&D 5e)
    Plus gems_delta — gp-valued gem total tracked separately.

    Use negative deltas to spend money (e.g. gp_delta=-800 for an 800 gp
    construction cost). The tool validates that no denomination goes
    below zero and returns an error instead of allowing overdraft.

    Renaming: pass new_account_name to rename the row in the same call.
    Replaces the off-piste 'go through direct_db_edit' workflow for the
    standard 'we should call this The Moathouse Vault now' kind of fix.
    Validation: non-empty, no case-insensitive collision with another
    treasury account.

    Returns the account's full updated balances (pp, gp, ep, sp, cp,
    gems_gp_value) plus total_gp_equivalent and a formatted_total string
    such as
      '9,465.3 gp equivalent (457 pp, 3465 gp, 0 ep, 5 sp, 60 cp)'.
    """
    try:
        result = db_update_treasury(
            account_name,
            gp_delta=gp_delta, sp_delta=sp_delta,
            cp_delta=cp_delta, pp_delta=pp_delta,
            ep_delta=ep_delta, gems_delta=gems_delta,
            new_account_name=(new_account_name or None),
        )
        return {"updated": True, **result}
    except ValueError as e:
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: add_treasury_account
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def add_treasury_account(
    account_name: Annotated[
        str,
        "Name of the new treasury account. Must be unique within the "
        "campaign (case-insensitive); duplicate names are rejected. "
        "Examples: 'Moathouse Vault', 'Saltmarsh Cellar', "
        "'Dragon Hoard - Cinderpeak'.",
    ],
    location_name: Annotated[
        str,
        "Optional location to link this treasury to (case-insensitive "
        "prefix match against the locations table). Use for vaults that "
        "physically sit in a keep, cellar, lair, or settlement. Leave "
        "blank for an unsited account (e.g. coin a henchman is carrying).",
    ] = "",
    gp: Annotated[int, "Starting gold pieces. Default 0."] = 0,
    sp: Annotated[int, "Starting silver pieces. Default 0."] = 0,
    cp: Annotated[int, "Starting copper pieces. Default 0."] = 0,
    pp: Annotated[int, "Starting platinum pieces. Default 0."] = 0,
    ep: Annotated[
        int,
        "Starting electrum pieces. AD&D 1e: 1 EP = 0.5 GP. Default 0.",
    ] = 0,
    gems_gp_value: Annotated[
        int,
        "Total gem value in GP (sum of all gems stored). Default 0.",
    ] = 0,
    notes: Annotated[
        str,
        "Free-text description: contents beyond coins/gems, security, "
        "who knows about it, access conditions. Optional.",
    ] = "",
) -> dict:
    """
    Create a new treasury account.

    Accepts opening balances in all five AD&D 1e coin denominations
    (pp, gp, ep, sp, cp) plus a gp-valued gem total.

    Use this when the party establishes a new vault, claims a hoard, or
    splits an existing pile into a separately-tracked stash. The new
    account immediately becomes a valid target for update_treasury.

    Validates:
      - account_name is non-empty and unique in the campaign
      - location_name (if given) resolves to a real location row

    Returns the new treasury_id, the full row (with linked location
    name), the total_gp_equivalent and the formatted_total string as
    confirmation. On error returns {"error": "..."}.
    """
    try:
        result = db_add_treasury_account(
            account_name=account_name,
            location_name=(location_name or None),
            gp=gp, sp=sp, cp=cp, pp=pp, ep=ep,
            gems_gp_value=gems_gp_value,
            notes=(notes or None),
        )
        return {"created": True, **result}
    except ValueError as e:
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: list_treasury_accounts
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def list_treasury_accounts() -> dict:
    """
    Return every treasury account in the active campaign.

    Discovery tool: each entry includes treasury_id, account_name, all
    coin balances (gp/sp/cp/pp), gems_gp_value, linked location (if any),
    and notes (with a 120-char preview). Also returns the campaign-wide
    GP total across all accounts.

    Use at session start to see what vaults exist, before calling
    update_treasury when you don't remember exact account names, or when
    auditing total wealth across multiple stashes.
    """
    return db_list_treasury_accounts()


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 29 — TRADE CIRCUIT TRACKER
# Recurring merchant-circuit income on predictable cycles. Each circuit has a
# fixed cycle in days, an income range, and an optional linked treasury that
# gets credited when a return is logged. domain_turn auto-runs check_circuits_due
# and includes the result in its season report.
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def add_trade_circuit(
    name: Annotated[
        str,
        "Unique name for this circuit. Case-insensitive duplicates are "
        "rejected. Examples: 'Chendl Circuit', 'Eastern Circuit', "
        "'Riverbend Salt Run'.",
    ],
    cycle_days: Annotated[
        int,
        "How many days between returns (the time between one departure and "
        "the next return at the home depot). Common: 30 for monthly, "
        "60 for bi-monthly, 90 for seasonal. Must be a positive integer.",
    ],
    income_min_gp: Annotated[
        int,
        "Lower bound of the per-return income range in gp. Used for "
        "projection and to communicate variance, not for auto-rolling — "
        "the actual amount is passed in by the caller of "
        "collect_circuit_income.",
    ] = 0,
    income_max_gp: Annotated[
        int,
        "Upper bound of the per-return income range in gp. Must be >= "
        "income_min_gp. Defaults to 0 for circuits whose income is fully "
        "variable and not yet characterised.",
    ] = 0,
    description: Annotated[
        str,
        "Free-text description: route, principal goods, factor or wagonmaster, "
        "any complications. Optional but strongly recommended for canon.",
    ] = "",
    treasury_account_name: Annotated[
        str,
        "Name (case-insensitive prefix match) of an EXISTING treasury "
        "account that should be credited automatically when "
        "collect_circuit_income is called. Leave blank to defer linking. "
        "If non-blank and the name does not match an existing account, "
        "the call is rejected — add the account first via "
        "add_treasury_account.",
    ] = "",
    last_return_day: Annotated[
        int,
        "Campaign day of the most recent return for this circuit (0 if "
        "the circuit is being established now and the next due day should "
        "be cycle_days from day 0). Defaults to 0.",
    ] = 0,
    notes: Annotated[
        str,
        "Free-text notes: weather considerations, escort arrangements, "
        "trustworthiness of the factor, etc. Optional.",
    ] = "",
) -> dict:
    """
    Register a new trade circuit.

    Validates name uniqueness, the income range (max >= min, both
    non-negative), and the linked treasury account if one was given.
    Calculates next_due_day = last_return_day + cycle_days automatically.

    Returns the new circuit_id and the full row as confirmation.
    """
    try:
        result = db_add_trade_circuit(
            name                  = name,
            cycle_days            = int(cycle_days),
            income_min_gp         = int(income_min_gp),
            income_max_gp         = int(income_max_gp),
            description           = (description or None),
            treasury_account_name = (treasury_account_name or None),
            last_return_day       = int(last_return_day),
            notes                 = (notes or None),
        )
        return {"created": True, **result}
    except ValueError as e:
        return {"error": str(e)}


@mcp.tool()
def list_trade_circuits(
    current_day: Annotated[
        int,
        "Campaign day used to compute days_overdue. Pass -1 to auto-resolve "
        "from the PC's campaign_days_elapsed (the standard advance_time "
        "counter). Default -1.",
    ] = -1,
) -> dict:
    """
    List every trade circuit in the campaign with status fields.

    Each entry includes: circuit_id, name, description, cycle_days, income
    range, last_return_day, next_due_day, treasury_account_name, status
    ('active' | 'inactive'), last_amount_gp (from the most-recent ledger
    row, if any), days_overdue (0 if not overdue), and
    projected_annual_income_gp (mid-of-range × cycles per year).

    Sorted by active-first then next_due_day ascending so the most-pressing
    circuits surface at the top.
    """
    cd = None if current_day is None or current_day < 0 else int(current_day)
    return db_list_trade_circuits(current_day=cd)


@mcp.tool()
def collect_circuit_income(
    circuit_name: Annotated[
        str,
        "Name (case-insensitive exact, then prefix) of the trade circuit "
        "whose return is being logged. Examples: 'Chendl Circuit', 'Eastern'.",
    ],
    current_day: Annotated[
        int,
        "Campaign day on which the return arrived. Used to set "
        "last_return_day and to compute next_due_day = current_day + "
        "cycle_days.",
    ],
    amount_gp: Annotated[
        int,
        "Actual gp amount returned this cycle (caller rolls within the "
        "circuit's income range, applies modifiers, and passes the final "
        "number here). Must be non-negative.",
    ],
    notes: Annotated[
        str,
        "Free-text notes for this specific return: weather, raids, "
        "exceptional sales, factor's report, etc. Optional.",
    ] = "",
) -> dict:
    """
    Log a circuit return.

    Three things happen atomically (within one engine call):
      1. A new circuit_ledger row is written.
      2. The circuit's last_return_day is set to current_day and
         next_due_day is recomputed as current_day + cycle_days.
      3. If a treasury account is linked, update_treasury is called to
         credit the deposit.

    If the treasury credit fails (e.g. the linked account was renamed or
    deleted), the ledger row is still kept and the response includes a
    treasury_error field so the caller can reconcile manually instead of
    losing the deposit.

    Returns deposit confirmation, treasury_after balances, and the new
    next_due_day.
    """
    try:
        result = db_collect_circuit_income(
            circuit_name = circuit_name,
            current_day  = int(current_day),
            amount_gp    = int(amount_gp),
            notes        = (notes or None),
        )
        return {"logged": True, **result}
    except ValueError as e:
        return {"error": str(e)}


@mcp.tool()
def get_circuit_ledger(
    circuit_name: Annotated[
        str,
        "Name (case-insensitive exact, then prefix) of one circuit to filter "
        "to. Leave blank to see ledger entries for ALL circuits in the "
        "campaign.",
    ] = "",
    limit: Annotated[
        int,
        "Maximum number of ledger rows to return (newest-first). Default "
        "20, capped at 500.",
    ] = 20,
) -> dict:
    """
    Chronological log of circuit returns (newest-first), optionally filtered
    to a single circuit.

    Returns:
      - entries: list of {ledger_id, circuit_id, circuit_name, day,
        amount_gp, treasury_account, notes, created_at}.
      - totals_by_circuit: a mapping of circuit_id -> {circuit_name,
        total_gp, entries} computed across the FULL ledger (not capped by
        limit), so the caller always sees lifetime running totals even
        with a small limit.
    """
    try:
        return db_get_circuit_ledger(
            circuit_name = (circuit_name or None),
            limit        = int(limit),
        )
    except ValueError as e:
        return {"error": str(e)}


@mcp.tool()
def check_circuits_due(
    current_day: Annotated[
        int,
        "Campaign day to compare against. Pass -1 to auto-resolve from "
        "the PC's campaign_days_elapsed (the standard advance_time "
        "counter). Default -1.",
    ] = -1,
    lookahead_days: Annotated[
        int,
        "How many days ahead to scan for upcoming circuits. Default 30.",
    ] = 30,
) -> dict:
    """
    Surface trade circuits that need attention.

    Returns:
      - overdue:  active circuits whose next_due_day is in the past
        (each entry carries a days_overdue field). Flag these prominently
        to the player at session start.
      - upcoming: active circuits coming due within the next
        lookahead_days days (each entry carries days_until_due).

    Inactive circuits are excluded from both lists.

    Called automatically by domain_turn at the end of every season; the
    caller may also invoke it directly at session start as a reminder.
    """
    cd = None if current_day is None or current_day < 0 else int(current_day)
    return db_check_circuits_due(
        current_day    = cd,
        lookahead_days = int(lookahead_days),
    )


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
    new_name: Annotated[
        str,
        "Optional new canonical name for this location. Leave blank to "
        "keep the existing name. The new name must be non-empty and "
        "must not collide with another location in the campaign "
        "(case-insensitive). Use when a working-handle gets formalized "
        "or a place's canonical name changes through play.",
    ] = "",
) -> dict:
    """
    Change the status (and optionally notes / canonical name) of a location.

    Call this when construction completes ('Under Construction' -> 'Active'),
    a location is captured or destroyed, or its operational state changes.
    Pass new_name to also rename the location in the same call.

    Returns the updated location row as confirmation.
    """
    try:
        result = db_update_location_status(
            name=name, new_status=new_status,
            notes=notes if notes else None,
            new_name=(new_name or None),
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
    new_group_name: Annotated[
        str,
        "Optional new name for this troop group. Leave blank to keep the "
        "existing name. The new name must be non-empty and must not "
        "collide with another troop group in the campaign (case-"
        "insensitive). Pass new_count and delta as None with "
        "new_group_name to perform a pure rename.",
    ] = "",
) -> dict:
    """
    Set or adjust the headcount for a troop group, and optionally rename
    the group.

    Use delta for incremental changes (casualties, desertions, new recruits).
    Use new_count to correct to a known value from hard-copy records.
    Count cannot go below zero. Either new_count or delta may be provided
    (not both); when both are None, only the rename happens.

    Returns the updated troop row as confirmation.
    """
    try:
        result = db_update_troop_count(
            group_name=group_name, new_count=new_count, delta=delta,
            new_group_name=(new_group_name or None),
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
    commander_character_id: Annotated[
        int,
        "Optional character_id of the commanding officer. Validated against "
        "the characters table. Pass 0 (default) to skip — commander_name "
        "may be used instead. If both are given, this id wins.",
    ] = 0,
    commander_name: Annotated[
        str,
        "Optional commander name (case-insensitive prefix match against the "
        "characters table). Convenient when you don't have the id handy. "
        "Ignored if commander_character_id is non-zero.",
    ] = "",
) -> dict:
    """
    Add a new troop group to the realm.

    Use this when Theron hires mercenaries, recruits new soldiers, gains allied
    forces, or when a new type of unit needs to be tracked separately.

    A commander can be linked at insert time via commander_character_id (exact
    id, validated) or commander_name (prefix match against characters). When
    one is given the troop's commander_character_id FK is set and the
    commander's name is included in the returned row.

    Returns the new troop_id and full row (with commander, if linked) as
    confirmation.
    """
    try:
        result = db_add_troop_group(
            group_name=group_name, troop_type=troop_type,
            count=count, location_name=location_name, notes=notes,
            commander_character_id=(int(commander_character_id) or None),
            commander_name=(commander_name or None),
        )
        return {"created": True, **result}
    except ValueError as e:
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: add_livestock
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def add_livestock(
    animal_type: Annotated[
        str,
        "Type of animal — free text, e.g. 'Sheep', 'Pigs', 'Beef Cattle', "
        "'Dairy Cattle', 'Chickens', 'Sheepdog', 'Horses', 'Goats'.",
    ],
    count: Annotated[int, "Headcount. Must be >= 0."],
    location_name: Annotated[
        str,
        "Location where this stock lives (case-insensitive prefix match "
        "against the locations table).",
    ],
    notes: Annotated[
        str,
        "Optional details: breed, productive purpose, working assignments "
        "(e.g. 'Velyander breed', 'Gran March premium dairy', 'Moss the "
        "sheepdog working with Dellon'). Free text.",
    ] = "",
) -> dict:
    """
    Insert a new livestock row at a domain location.

    Use when a farm is acquired, a new herd is purchased, an animal cohort
    is split off for separate tracking, or when backfilling canon. The
    livestock table is read by get_realm_state and informs domain
    bookkeeping (agricultural sales, smokehouse partnerships, etc.).

    Returns the new livestock_id and full row (with the joined location_name)
    as confirmation. On error returns {"error": "..."}.
    """
    try:
        result = db_add_livestock(
            animal_type=animal_type,
            count=count,
            location_name=location_name,
            notes=(notes or None),
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
        "Where to put the item: 'character' (the PC's personal inventory), "
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
    equipped: Annotated[
        bool,
        "Legacy: marks the item equipped without assigning a specific slot. "
        "Prefer the slot parameter; equipped is honored only when slot is empty.",
    ] = False,
    carry_notes: Annotated[str, "How it's carried: 'Worn', 'In pack', 'Sheathed', etc."] = "",
    # ── Phase 12 structured combat fields ──
    damage_dice: Annotated[
        str,
        "Damage dice expression for weapons, e.g. '1d6', '2d4', '1d8'. "
        "Leave blank for non-weapons.",
    ] = "",
    damage_bonus: Annotated[
        int,
        "Flat +N to damage rolls (magic enhancement, ability bonus baked in, etc.).",
    ] = 0,
    to_hit_bonus: Annotated[
        int,
        "Flat +N to attack rolls.",
    ] = 0,
    weapon_type: Annotated[
        str,
        "Weapon category. Accepted values (no others — the validator "
        "rejects anything else, including natural weapon names like "
        "'shortsword' or 'longbow'):\n"
        "  one_handed  — sword, mace, hammer, dagger held single-handed\n"
        "  two_handed  — greatsword, polearm, two-handed axe, longbow\n"
        "                (longbow is two-handed because it requires two\n"
        "                hands to draw, NOT 'ranged'; 'ranged' is for\n"
        "                projectile category, see below)\n"
        "  off_hand    — secondary weapon when dual-wielding (a dagger\n"
        "                in the off hand for thieves, etc.)\n"
        "  ranged      — bow / crossbow / sling category — covers any\n"
        "                weapon whose primary use is firing projectiles\n"
        "  thrown      — javelin, throwing dagger, hand axe — meant to\n"
        "                be hurled\n"
        "  none        — explicit non-weapon (alias for blank). Use for\n"
        "                armor, shields, gear that has no attack profile.\n"
        "Leave blank for non-weapons (same as 'none').",
    ] = "",
    armor_class_bonus: Annotated[
        int,
        "AC contribution. For armor: the AD&D AC value the piece provides "
        "(e.g. 6 for chainmail). For misc gear: a +N defensive bonus.",
    ] = 0,
    slot: Annotated[
        str,
        "Equip the new item directly into this slot (character-only). One of: "
        "mainhand, offhand, head, body, cloak, belt, boots, gloves, ring1, "
        "ring2, neck, back. Leave blank to stow. Auto-vacates any prior "
        "occupant of the slot.",
    ] = "",
    character_target: Annotated[
        str,
        "Optional name (case-insensitive prefix match) or numeric character_id "
        "of the recipient when assign_to='character'. Leave blank to default "
        "to the PC. Lets henchmen, hirelings, or any tracked NPC receive the "
        "item directly without a follow-up reassignment. Rejects with a "
        "'character_target did not resolve' error if the name/id is unknown.",
    ] = "",
    auto_vacate: Annotated[
        bool,
        "When slot is set and another item already holds that slot for "
        "this character: True (default) silently displaces it (matches "
        "equip_item); False raises a slot-conflict error listing the "
        "current occupant so the caller can decide how to resolve it.",
    ] = True,
) -> dict:
    """
    Create a new item and place it in an inventory.

    Use when the PC acquires new gear, loots a defeated enemy, commissions
    equipment, or when a realm asset (siege engine, stored supplies) needs
    to be tracked.

    Combat fields (damage_dice / damage_bonus / to_hit_bonus / weapon_type /
    armor_class_bonus) are written into the items table so they can drive
    attack/AC math without re-parsing the notes field.

    weapon_type — accepted values (anything else is rejected by the
    validator; do NOT pass natural-language names like 'shortsword' or
    'longbow'):
      one_handed  — sword, mace, hammer, dagger held single-handed
      two_handed  — greatsword, polearm, two-handed axe, longbow
                    (longbow is two-handed because it takes two hands
                    to draw — NOT 'ranged'; 'ranged' is the projectile
                    category, see below)
      off_hand    — secondary weapon when dual-wielding (a thief's
                    off-hand dagger, etc.)
      ranged      — bow / crossbow / sling — any weapon whose primary
                    use is firing projectiles
      thrown      — javelin, throwing dagger, hand axe — meant to be
                    hurled
      none        — explicit non-weapon (alias for blank). Use for
                    armor, shields, gear with no attack profile.
    Leave blank for non-weapons (treated identically to 'none').

    slot equips the item directly at insert time (only when assign_to=
    'character'). Pass it for known-equipped purchases like 'a +1 cloak
    of protection put straight on'.

    character_target lets non-PC characters (henchmen, hirelings, NPCs
    in the party) receive the item directly. Only meaningful when
    assign_to='character'.

    Returns item_id, inventory_id, character_id (when applicable), and
    assignment details as confirmation.
    """
    # 'none' is an explicit alias for blank weapon_type — the user-facing
    # docstring lists it as accepted, so the validator must treat it as
    # equivalent to the empty string.
    wt_clean = (weapon_type or "").strip()
    if wt_clean.lower() == "none":
        wt_clean = ""
    try:
        result = db_add_item(
            name=name, item_type=item_type, magic_flag=magic_flag,
            value_gp=value_gp, notes=notes, assign_to=assign_to,
            location_name=location_name or None,
            treasury_name=treasury_name or None,
            equipped=equipped, carry_notes=carry_notes,
            damage_dice=(damage_dice or None),
            damage_bonus=damage_bonus,
            to_hit_bonus=to_hit_bonus,
            weapon_type=(wt_clean or None),
            armor_class_bonus=armor_class_bonus,
            slot=(slot or None),
            character_target=(character_target or None),
            auto_vacate=bool(auto_vacate),
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
    new_name: Annotated[
        str | None,
        "Optional new canonical name for this character. Useful when a "
        "placeholder ('the wounded soldier') resolves to a real name, or "
        "canonical spellings get standardized. Must be non-empty and "
        "must not collide with another character in the campaign "
        "(case-insensitive). Omit (or leave None) to keep the current name.",
    ] = None,
) -> dict:
    """
    Update an NPC's record in the database.

    Call this when an NPC's status changes during play: Ruk takes casualties,
    Red Rider's prisoner status changes, a new NPC reveals their alignment,
    an ally turns hostile, or someone dies. The notes field is the primary
    place to record narrative state.

    Renaming: pass new_name to also rename the character in the same call.
    Replaces the off-piste 'go through direct_db_edit' workflow when a
    placeholder gets resolved to a proper name.

    Returns the full updated character row as confirmation.
    """
    try:
        result = db_update_npc(
            name=name, notes=notes, character_type=character_type,
            race=race, alignment=alignment, new_name=new_name,
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
    race: Annotated[
        str | None,
        "Race (Human, Elf, Goblin, etc.). Omit if unknown.",
    ] = None,
    character_type: Annotated[
        str,
        "Type: 'NPC', 'Prisoner', 'Ally', 'Hostile', 'Construct', 'Unknown'.",
    ] = "NPC",
    notes: Annotated[
        str | None,
        "Description, backstory, role in the campaign, known abilities or items. "
        "When provided this is written verbatim to characters.notes — same "
        "binding as update_npc.notes. Omit to leave the column NULL.",
    ] = None,
    relationship_to_theron: Annotated[
        str | None,
        "If this NPC has a relationship with Theron, describe it here: "
        "'Hired Soldier', 'Enemy', 'Quest Giver', 'Merchant', 'Prisoner', etc. "
        "Omit if no relationship entry is needed.",
    ] = None,
    relationship_notes: Annotated[
        str | None,
        "Additional context for the relationship (circumstances of meeting, etc.).",
    ] = None,
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
# PHASE 32 — DM CONTRACT ENFORCEMENT
# The AI DM drifts over a session. The contract lives in world_facts
# (category='dm_contract') and is auto-installed on first read. session_start
# prepends it; get_current_scene appends a footer reminder; the `contract`
# tool fetches it on demand when the player types "contract".
# ══════════════════════════════════════════════════════════════════════════════

_DM_CONTRACT_DEFAULT = (
    "DM CONTRACT (read before every response):\n"
    "(a) Describe the world only — never write Ramun's/the player's actions "
    "or decisions.\n"
    "(b) Real dice only. Roll stats (4d6 drop lowest), HD, saves with actual "
    "thresholds stated aloud before rolling.\n"
    "(c) Brevity default. One sentence answers simple questions. No "
    "paragraphs unless drama demands it.\n"
    "(d) No scope creep. No improvised subplots, tropes, or invented lore. "
    "If uncertain, ask.\n"
    "(e) Player silence = player thinking. Describe the scene and wait. "
    "Do not fill in.\n"
    "(f) If player says \"contract\" — stop, re-read this, correct course, "
    "acknowledge what drifted."
)

# Phrases that uniquely identify a SUPERSEDED default contract. Used by the
# auto-upgrade path in _get_or_install_dm_contract to swap legacy rows over
# to the current default via update_world_fact(overwrite_category=True).
# Each entry is the exact opening of a discontinued (a)-(g) item line; a
# stored contract is treated as legacy when ANY of these phrases is present
# AND the canonical header is also present (so custom admin overrides that
# happen to mention "NPC names" aren't blown away).
_DM_CONTRACT_LEGACY_PHRASES: tuple[str, ...] = (
    "No new NPC names without player permission.",
)

_DM_CONTRACT_HEADER = "=== DM CONTRACT ==="
_DM_CONTRACT_FOOTER = (
    "[Contract active — no player verbs, no improvised names, real dice only]"
)


def _is_legacy_dm_contract(text: str) -> bool:
    """
    True if `text` looks like a superseded engine default rather than a
    custom admin override. Requires the canonical header AND at least one
    discontinued-line phrase from _DM_CONTRACT_LEGACY_PHRASES.
    """
    if not text:
        return False
    if "DM CONTRACT (read before every response):" not in text:
        return False
    return any(p in text for p in _DM_CONTRACT_LEGACY_PHRASES)


def _get_or_install_dm_contract() -> str:
    """
    Return the active DM contract text from world_facts.

    Three paths:

    1. No row present — auto-install the current default (no manual .db
       edit or migration script needed). Inserted via update_world_fact
       with source_note 'auto-installed Phase 33 default'.

    2. Stored row matches a known legacy default (superseded engine
       version detected via _is_legacy_dm_contract) — replace it with
       the current default via update_world_fact(overwrite_category=True).
       This is how a contract revision (e.g. dropping the NPC-names
       rule) propagates to existing campaigns without an external
       migration step or any direct .db edit.

    3. Stored row exists and is NOT a known legacy default — leave it
       alone. Custom admin overrides survive untouched.

    Returns the most recently inserted fact_text for category
    ='dm_contract' (after any auto-install / auto-upgrade has run).
    """
    from engine.db import (_get_conn as _ec, _CAMPAIGN_ID as _cid,
                           update_world_fact as _uwf)
    with _ec(read_only=True) as conn:
        row = conn.execute(
            "SELECT fact_text FROM world_facts "
            "WHERE campaign_id = ? AND category = 'dm_contract' "
            "ORDER BY world_fact_id DESC LIMIT 1",
            (_cid,),
        ).fetchone()

    stored = (row["fact_text"] if row else "") or ""

    # Path 2: stored row is a recognised legacy default — auto-upgrade.
    if stored and _is_legacy_dm_contract(stored):
        try:
            _uwf(
                category="dm_contract",
                fact_text=_DM_CONTRACT_DEFAULT,
                source_note="auto-upgraded Phase 33 default (legacy row replaced)",
                overwrite_category=True,
            )
        except Exception:
            # Couldn't write — still return the new text so the caller
            # sees the correct contract this session.
            pass
        return _DM_CONTRACT_DEFAULT

    # Path 3: custom override / already-current default — keep as-is.
    if stored.strip():
        return stored

    # Path 1: no row yet — auto-install. Best-effort: a write that fails
    # (read-only DB, locked) still leaves the caller with a usable string.
    try:
        _uwf(
            category="dm_contract",
            fact_text=_DM_CONTRACT_DEFAULT,
            source_note="auto-installed Phase 33 default",
            overwrite_category=False,
        )
    except Exception:
        pass
    return _DM_CONTRACT_DEFAULT


def _formatted_dm_contract() -> str:
    """Return the contract with the canonical header on top, ready to display."""
    return f"{_DM_CONTRACT_HEADER}\n{_get_or_install_dm_contract()}"


@mcp.tool()
def contract() -> dict:
    """
    Fetch and display the active DM contract.

    This is the player's reset button. When they type "contract" in chat,
    call this tool, re-read the rules, and acknowledge IN YOUR NEXT
    RESPONSE which rule you drifted from before continuing play.

    The contract is stored in world_facts under category 'dm_contract'.
    First call on any campaign auto-installs the default contract — no
    setup step required. An admin may rewrite the contract for a campaign
    by calling update_world_fact with category='dm_contract' and
    overwrite_category=True.

    Returns:
      contract          — the contract text only (no header)
      formatted         — the contract with the '=== DM CONTRACT ===' header
                          prepended, suitable for direct display
      header            — the literal header string
      footer            — the one-line scene footer reminder
      reminder_for_dm   — explicit instruction on how to handle this call
    """
    text = _get_or_install_dm_contract()
    return {
        "contract":  text,
        "formatted": f"{_DM_CONTRACT_HEADER}\n{text}",
        "header":    _DM_CONTRACT_HEADER,
        "footer":    _DM_CONTRACT_FOOTER,
        "reminder_for_dm": (
            "Player invoked the contract reset. Read every rule (a)-(g), "
            "identify which rule you most recently drifted from, "
            "acknowledge it explicitly in your next response, then "
            "course-correct."
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: session_start
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def session_start(
    full: Annotated[
        bool,
        "When False (default), returns the LEAN startup briefing — core "
        "character stats (no inventory), current scene, and the last 5 "
        "turns. Designed to fit comfortably under the response cap on "
        "any campaign size. When True, also includes the last 10 turns, "
        "pending_updates, and the verbose briefing_notes. Pass full=True "
        "only when you need the comprehensive view — most session starts "
        "are fine with the lean default.",
    ] = False,
) -> dict:
    """
    Call this FIRST at the start of every session, before any narration.

    Two modes:
      full=False (default — LEAN):
        character       -- Core stats (name/race/classes/abilities/HP/AC/
                           THAC0/saves) — same shape as get_character_stats
        scene           -- Current location and last turn context
        recent_history  -- Last 5 turns in chronological order
        briefing_notes  -- One-line reminder

      full=True (COMPREHENSIVE):
        character       -- Same lean stats (no full inventory; use
                           list_inventory / list_equipped for that)
        scene           -- Same
        recent_history  -- Last 10 turns
        pending_updates -- Turns whose state_changes have not yet been
                           committed to the database
        briefing_notes  -- Full DM checklist

    Inventory and spells are deliberately excluded from both modes —
    fetch via list_equipped / list_inventory / get_spell_slots if needed.

    After receiving this briefing you should:
      1. Pass full=True if you need pending_updates — and resolve any
         entries before starting (call the appropriate write tools).
      2. Orient the player: tell them where they are, what just happened,
         what immediate situation they face.
      3. Never invent a state that contradicts what this briefing contains.
    """
    # ── Character: lean shape (same as get_character_stats) ───────────────────
    # Pulled inline rather than calling get_character_stats so we don't
    # double-cap and so we keep one DB-connection round trip.
    from engine.db import (_get_conn as _ec,
                           _PC_CHARACTER_ID as _pc_id)
    with _ec(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT character_id, name, character_type, race, alignment "
            "FROM characters WHERE character_id = ?",
            (_pc_id,),
        )
        ch = cur.fetchone()
        cur.execute(
            "SELECT class_name, level, xp FROM class_levels "
            "WHERE character_id = ? ORDER BY class_name",
            (_pc_id,),
        )
        cls_rows = [dict(r) for r in cur.fetchall()]
        cur.execute(
            "SELECT hp_current, hp_max, ac, movement, attacks_per_round, "
            "       status_notes "
            "FROM character_status WHERE character_id = ?",
            (_pc_id,),
        )
        status_row = cur.fetchone()
        cur.execute(
            "SELECT strength, intelligence, wisdom, dexterity, constitution, "
            "       charisma "
            "FROM character_abilities WHERE character_id = ?",
            (_pc_id,),
        )
        abi = cur.fetchone()

    if not ch:
        char: dict = {"error": "Character not found in database."}
    else:
        st  = dict(status_row) if status_row else {}
        abi = dict(abi) if abi else {}
        classes_out: list[dict] = []
        thac0_values: list[int] = []
        saves_acc: dict[str, list[int]] = {}
        for c in cls_rows:
            cname = c.get("class_name") or ""
            lvl   = int(c.get("level") or 1)
            thac0 = _thac0_for_class_level(cname, lvl)
            saves = _saves_for_class_level(cname, lvl) or {}
            classes_out.append({
                "class_name": cname, "level": lvl, "xp": c.get("xp"),
                "thac0": thac0, "saves": saves,
            })
            if isinstance(thac0, int):
                thac0_values.append(thac0)
            for cat, target in saves.items():
                saves_acc.setdefault(cat, []).append(int(target))
        char = {
            "character_id":      ch["character_id"],
            "name":              ch["name"],
            "race":              ch["race"],
            "alignment":         ch["alignment"],
            "character_type":    ch["character_type"],
            "classes":           classes_out,
            "abilities": {
                "str": abi.get("strength"),
                "int": abi.get("intelligence"),
                "wis": abi.get("wisdom"),
                "dex": abi.get("dexterity"),
                "con": abi.get("constitution"),
                "cha": abi.get("charisma"),
            },
            "hp_current":        st.get("hp_current"),
            "hp_max":            st.get("hp_max"),
            "ac":                st.get("ac"),
            "thac0_best":        min(thac0_values) if thac0_values else None,
            "saves_best":        ({cat: min(v) for cat, v in saves_acc.items()}
                                  if saves_acc else None),
            "movement":          st.get("movement"),
            "attacks_per_round": st.get("attacks_per_round"),
            "status_notes":      st.get("status_notes"),
        }

    # ── Scene + recent history ────────────────────────────────────────────────
    scene = load_current_scene() or {}
    history = load_recent_ai_turns(limit=10 if full else 5)
    if history:
        scene["last_player_action"]       = history[-1]["player_action"]
        scene["last_dm_response_preview"] = (history[-1]["dm_response"] or "")[:300]

    # ── DM contract: prepended to every session_start response ───────────────
    # First call on a fresh campaign auto-installs the default contract.
    # Stays at the TOP of the dict so it lands above scene/character/history
    # when the response is rendered in insertion order.
    contract_text = _get_or_install_dm_contract()
    dm_contract = {
        "header": _DM_CONTRACT_HEADER,
        "text":   contract_text,
        "formatted": f"{_DM_CONTRACT_HEADER}\n{contract_text}",
    }

    if not full:
        return {
            "dm_contract":     dm_contract,
            "mode":            "lean",
            "character":       char,
            "scene":           scene,
            "recent_history":  history,
            "briefing_notes": (
                "Lean session_start. Pass full=True for pending_updates "
                "and the full DM checklist. Inventory / spells not "
                "included — use list_equipped / list_inventory / "
                "get_spell_slots. Read dm_contract.formatted before "
                "writing any narration."
            ),
        }

    # full=True: comprehensive briefing
    pending = db_get_pending_updates(limit=30)
    return {
        "dm_contract":     dm_contract,
        "mode":            "full",
        "character":       char,
        "scene":           scene,
        "recent_history":  history,
        "pending_updates": pending,
        "briefing_notes": (
            "SESSION STARTUP CHECKLIST — complete before narrating: "
            "(0) Read dm_contract.formatted at the top of this response "
            "and honour it for the rest of the session. "
            "(1) If pending_updates is non-empty, commit each unresolved "
            "state change now using the appropriate write tools. "
            "(2) Orient the player from the scene and recent_history "
            "context. (3) After each turn, call save_turn then act on its "
            "world_fact_reminder — write every named NPC, item, decision, "
            "and quest development to the database immediately. Nothing "
            "important should exist only in chat history. Inventory and "
            "spells deliberately excluded — use list_equipped / "
            "list_inventory / get_spell_slots when you need them."
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

        # ── Phase 7: consult pre-rolled area_instances ────────────────────────
        # Two ways to get pre-rolled HP into this group, in priority order:
        #   (a) `individual_hp`: an explicit list passed by the caller
        #       (typically by start_combat_from_area, see below).
        #   (b) Auto-lookup by (location, monster name) when the location
        #       has a pending pre-rolled instance.
        prerolled_hp:           list[int] | None = None
        prerolled_instance_id:  int        | None = None
        if isinstance(grp.get("individual_hp"), list) and grp["individual_hp"]:
            prerolled_hp = [int(h) for h in grp["individual_hp"]]
        elif grp.get("area_instance_id"):
            try:
                inst = db_get_monster_instance(int(grp["area_instance_id"]), 0)
                if not inst.get("error"):
                    encounters = db_get_area_encounters(
                        location, auto_populate=False
                    )
                    for room in encounters.get("rooms", []):
                        if room["area_instance_id"] == int(grp["area_instance_id"]):
                            prerolled_hp = list(room.get("individual_hp") or [])
                            prerolled_instance_id = room["area_instance_id"]
                            break
            except Exception:
                prerolled_hp = None
        elif location:
            inst = db_find_pre_rolled_for_combat(
                location_name=location, monster_type=disp_name,
            )
            if inst:
                try:
                    hp_list = json.loads(inst.get("individual_hp_json") or "[]")
                except (json.JSONDecodeError, TypeError):
                    hp_list = []
                if hp_list and len(hp_list) >= count:
                    prerolled_hp = [int(h) for h in hp_list[:count]]
                    prerolled_instance_id = inst.get("area_instance_id")

        # Phase 34: if this group is backed by a pre-rolled area_instance row,
        # auto-repair any pre-Phase-34 row that's missing monster_stats_json
        # so verify_combatant_stats returns ok before we ever resolve dice.
        prerolled_stats_block: list[dict] | None = None
        if prerolled_instance_id is not None:
            verify = verify_combatant_stats(prerolled_instance_id)
            if not verify.get("ok"):
                fixed = db_regenerate_combatant_stats(prerolled_instance_id)
                if not fixed.get("ok"):
                    return {
                        "error": (
                            f"Stats incomplete for {disp_name} "
                            f"(area_instance_id={prerolled_instance_id}) — "
                            f"run populate_npc first. "
                            f"Repair failed: {fixed.get('error')}"
                        ),
                        "blocked_by_contract": True,
                    }
                prerolled_stats_block = fixed.get("stats") or None
            else:
                # Pull the existing block out of the row for fast access.
                from engine.db import (_get_conn as _ec_block,
                                       _area_instances_table_exists as _aiex)
                with _ec_block(read_only=True) as conn:
                    if _aiex(conn):
                        try:
                            r = conn.execute(
                                "SELECT monster_stats_json FROM area_instances "
                                "WHERE area_instance_id = ?",
                                (prerolled_instance_id,),
                            ).fetchone()
                            if r and r["monster_stats_json"]:
                                prerolled_stats_block = json.loads(
                                    r["monster_stats_json"]
                                )
                        except Exception:
                            prerolled_stats_block = None

        # Adhoc groups (no pre-rolled row): roll a full stats block now so
        # every combatant carries complete fields into the active_combat
        # blob. This is what verify_combatant_stats and attack() rely on.
        adhoc_stats_block: list[dict] | None = None
        if prerolled_stats_block is None:
            adhoc_stats_block = _build_full_monster_stats_block(
                monster_name=disp_name, count=count,
                hd_text_override=hd_text,
                ac_override=(grp.get("ac_override") if grp.get("ac_override") else None),
            )

        for i in range(1, count + 1):
            cid = f"{disp_name}_{i}"
            # Resolve the per-individual full stat dict for this monster.
            stat_block = None
            if prerolled_stats_block and i - 1 < len(prerolled_stats_block):
                stat_block = prerolled_stats_block[i - 1] or {}
            elif adhoc_stats_block and i - 1 < len(adhoc_stats_block):
                stat_block = adhoc_stats_block[i - 1] or {}
            stat_block = dict(stat_block or {})

            # Caller overrides win over rolled values.
            if "hp_override" in grp:
                stat_block["hp_current"] = int(grp["hp_override"])
                stat_block["hp_max"]     = int(grp["hp_override"])
            if "ac_override" in grp:
                stat_block["ac"] = int(grp["ac_override"])
            # If we used a pre-rolled HP list (mid-combat / damaged state),
            # honour it over the freshly-rolled hp_current.
            if prerolled_hp is not None and i - 1 < len(prerolled_hp):
                stat_block["hp_current"] = int(prerolled_hp[i - 1])

            eff_hd = stat_block.get("effective_hd", 1.0)
            xp     = _xp_for_hd(eff_hd)

            # Final per-combatant verification — every required field
            # present and > 0. If anything is missing here something is
            # genuinely wrong with the build path; refuse to start combat.
            missing = [
                f for f in _REQUIRED_COMBATANT_FIELDS
                if stat_block.get(f) is None
                or (isinstance(stat_block.get(f), (int, float))
                    and stat_block.get(f) <= 0)
            ]
            if missing:
                return {
                    "error": (
                        f"Stats incomplete for {disp_name} {i} — run "
                        f"populate_npc first. Missing: {missing}"
                    ),
                    "blocked_by_contract": True,
                    "missing_fields":       missing,
                }

            combatants[cid] = {
                "name":        f"{disp_name} {i}",
                "side":        "enemy",
                "initiative":  random.randint(1, 10),
                "hp_current":  int(stat_block["hp_current"]),
                "hp_max":      int(stat_block["hp_max"]),
                "ac":          int(stat_block["ac"]),
                "thac0":       int(stat_block["thac0"]),
                "save_death":     int(stat_block["save_death"]),
                "save_wands":     int(stat_block["save_wands"]),
                "save_paralysis": int(stat_block["save_paralysis"]),
                "save_breath":    int(stat_block["save_breath"]),
                "save_spells":    int(stat_block["save_spells"]),
                "morale":      int(stat_block["morale"]),
                "hd_text":     stat_block.get("hd_text", hd_text),
                "effective_hd": float(stat_block.get("effective_hd", eff_hd)),
                "damage_text": stat_block.get("damage", damage),
                "num_attacks": int(stat_block.get("num_attacks", n_attacks)),
                "xp":          xp,
                "is_pc":       False,
                "status":      "active",
                "group":       group_key,
                # Tracks back to the pre-rolled row so attacks can update it.
                "area_instance_id":      prerolled_instance_id,
                "monster_index":         (i - 1) if prerolled_instance_id else None,
                "from_pre_rolled":       prerolled_hp is not None,
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
    # Phase 35: one visual ref per unique enemy group (six orcs = one Orc
    # link, not six). The PC entry is excluded — visual refs are for the
    # monsters/NPCs the DM has to describe, not the player.
    enemy_group_names = sorted(groups.keys())
    response: dict = {
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
    block = _visual_refs_block(enemy_group_names)
    if block:
        response["visual_refs"] = block
    return response


@mcp.tool()
def get_combat_state(
    summary_only: Annotated[
        bool,
        "When True, returns the compact combat card: round number, "
        "current_actor, and a thin initiative_order with only "
        "{id, name, hp_current, initiative}. Drops AC, hp_max, side, "
        "status, and combat_log. ~10× smaller; ideal for a quick "
        "'whose turn is it / who's hurt' check between rounds.",
    ] = False,
) -> dict:
    """
    Return current combat state.

    Default: full state with initiative order, every combatant's HP / AC /
    status, and the last 10 combat_log entries.

    summary_only=True: compact card with just round / current_actor /
    {id, name, hp_current, initiative} for each combatant. Use this for
    quick mid-round "whose turn / who's hurt" lookups; drop into the
    full call when you actually need stat blocks.

    If no combat is active, returns {"active": false} with an informational
    message — same in either mode.
    """
    state = get_active_combat()
    if not state:
        return {
            "active": False,
            "message": "No combat is currently active. Call start_combat() to begin an encounter.",
        }

    combatants = state.get("combatants", {})
    order      = state.get("initiative_order", [])
    current_actor = order[state.get("current_actor_index", 0)] if order else ""

    if summary_only:
        return {
            "active":         True,
            "summary_only":   True,
            "encounter_name": state.get("encounter_name", ""),
            "round":          state.get("round", 1),
            "current_actor":  current_actor,
            "initiative_order": [
                {
                    "id":         cid,
                    "name":       combatants[cid]["name"],
                    "hp_current": combatants[cid]["hp_current"],
                    "initiative": combatants[cid]["initiative"],
                }
                for cid in order
                if cid in combatants
            ],
        }

    return {
        "active":          True,
        "encounter_name":  state.get("encounter_name", ""),
        "location":        state.get("location", ""),
        "round":           state.get("round", 1),
        "current_actor":   current_actor,
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

    # ── Phase 34: stat-enforcement gate ───────────────────────────────────────
    # Block dice resolution against any enemy combatant whose stat block in
    # the active_combat state is missing required fields. PCs are exempt —
    # their stats live in characters / character_status and are validated
    # by their own pipelines. The required fields for an enemy are the
    # full set in _REQUIRED_COMBATANT_FIELDS.
    def _missing_required(c: dict) -> list[str]:
        if c.get("is_pc"):
            # PC entries only need hp/ac for the d20 math here.
            out = []
            if c.get("hp_current") is None or c.get("hp_current") < 0:
                out.append("hp_current")
            if c.get("ac") is None:
                out.append("ac")
            return out
        return [
            f for f in _REQUIRED_COMBATANT_FIELDS
            if c.get(f) is None
            or (isinstance(c.get(f), (int, float)) and c.get(f) <= 0
                and f != "hp_current")  # hp_current=0 means dead, which is ok upstream
        ]

    miss_attacker = _missing_required(attacker)
    miss_target   = _missing_required(target)
    if miss_attacker:
        return {
            "error": (
                f"Stats incomplete for {attacker['name']} — run "
                f"populate_npc first. Missing: {miss_attacker}"
            ),
            "blocked_by_contract": True,
            "missing_fields":       miss_attacker,
        }
    if miss_target:
        return {
            "error": (
                f"Stats incomplete for {target['name']} — run "
                f"populate_npc first. Missing: {miss_target}"
            ),
            "blocked_by_contract": True,
            "missing_fields":       miss_target,
        }

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

# ── PHASE 31 — SPELLBOOK (long-term known spells, distinct from today's load)

@mcp.tool()
def get_spellbook(
    character_target: Annotated[
        str,
        "Optional name (case-insensitive prefix match) or numeric "
        "character_id of the caster. Leave blank to default to the PC. "
        "Returns an error if the target has no spellcasting class.",
    ] = "",
    spell_level: Annotated[
        int,
        "Optional level filter (1-9). Pass -1 (or 0) to return all levels.",
    ] = -1,
    spell_class: Annotated[
        str,
        "Optional class filter (case-insensitive exact match against the "
        "spell_class column). Examples: 'Magic-User', 'Illusionist', "
        "'Cleric', 'Druid'. Leave blank for all classes.",
    ] = "",
) -> dict:
    """
    Return a character's spellbook — the spells they KNOW and could
    memorize — grouped by spell level.

    This is the long-term known-spells list, NOT today's prepared load.
    For today's memorized loadout and remaining slot availability, call
    get_spell_slots.

    Each entry includes id, spell_name, spell_level, spell_class, source,
    and notes. by_level groups the rows into {spell_level, count, spells}
    blocks in ascending level order.
    """
    try:
        lvl = None if spell_level is None or spell_level < 1 else int(spell_level)
        return db_get_spellbook(
            character_target=(character_target or None),
            spell_level=lvl,
            spell_class=(spell_class or None),
        )
    except ValueError as e:
        return {"error": str(e)}


@mcp.tool()
def add_spell_to_book(
    spell_name: Annotated[
        str,
        "Name of the spell, e.g. 'Magic Missile', 'Cure Light Wounds'. "
        "Required.",
    ],
    spell_level: Annotated[
        int,
        "Spell level 1-9 in the spell's class list. Required.",
    ],
    spell_class: Annotated[
        str,
        "Which casting class lists this spell. Examples: 'Magic-User', "
        "'Illusionist', 'Cleric', 'Druid'. Required so multiclass "
        "characters can keep their spellbooks cleanly separated.",
    ],
    source: Annotated[
        str,
        "Where this spell came from, e.g. 'starting spells', 'researched', "
        "'copied from scroll', 'gifted by Tenser'. Optional.",
    ] = "",
    notes: Annotated[
        str,
        "Free-text notes for this row: scribed component cost, page "
        "reference, special trigger conditions. Optional.",
    ] = "",
    character_target: Annotated[
        str,
        "Optional name or character_id of the spellbook owner. Blank = PC. "
        "The target must have at least one arcane or divine spellcasting "
        "class (Magic-User, Illusionist, Cleric, Druid, Ranger, Paladin, "
        "Bard); fighters / thieves / non-casters are rejected.",
    ] = "",
) -> dict:
    """
    Add a spell to a character's spellbook.

    Validates that:
      - spell_name and spell_class are non-empty
      - spell_level is in 1-9
      - the target character has at least one spellcasting class
      - the same (spell_name, spell_class) is not already in the book
        for this character — duplicates return
        {"already_known": True, "warning": "..."} instead of inserting
        a second row.

    Same spell on two different class lists IS allowed — Cleric 1
    'Bless' and Magic-User 1 'Bless' (if a homebrew variant existed)
    would coexist as separate rows.

    Returns the new row on success.
    """
    try:
        return db_add_spell_to_book(
            spell_name=spell_name,
            spell_level=int(spell_level),
            spell_class=spell_class,
            source=(source or None),
            notes=(notes or None),
            character_target=(character_target or None),
        )
    except ValueError as e:
        return {"error": str(e)}


@mcp.tool()
def remove_spell_from_book(
    spell_name: Annotated[
        str,
        "Name of the spell to remove (case-insensitive exact match).",
    ],
    confirm: Annotated[
        str,
        "Must be the literal string 'yes' to actually delete. Any other "
        "value (including blank, the default) returns a preview shape "
        "with no write so the caller can verify before committing.",
    ] = "",
    spell_class: Annotated[
        str,
        "Required ONLY when the same spell_name exists in multiple class "
        "lists for this character (e.g. Cleric and Druid both have it). "
        "Leave blank when the spell is unambiguous.",
    ] = "",
    character_target: Annotated[
        str,
        "Optional spellbook owner. Blank = PC.",
    ] = "",
) -> dict:
    """
    Delete a row from a character's spellbook.

    Safety gate: confirm='yes' is required for the deletion to actually
    happen. Any other confirm value returns a preview showing
    would_delete and a note — no write occurs.

    Ambiguity gate: if multiple rows in the book share spell_name (the
    same spell on two class lists for a multiclass caster), the call
    is rejected with the candidate list so the caller can re-call
    with spell_class to disambiguate.

    Returns {"removed": True, "deleted": {...row...}} on success.
    """
    try:
        return db_remove_spell_from_book(
            spell_name=spell_name,
            confirm=confirm,
            character_target=(character_target or None),
            spell_class=(spell_class or None),
        )
    except ValueError as e:
        return {"error": str(e)}


@mcp.tool()
def get_spell_slots(
    character_target: Annotated[
        str,
        "Optional name (case-insensitive prefix match) or numeric "
        "character_id of the spellcaster. Leave blank to default to the "
        "PC. Each character has independent spell memory keyed by "
        "character_id under world_facts category 'spell_memory' (PC) or "
        "'spell_memory_<id>' (others).",
    ] = "",
) -> dict:
    """
    Return a character's memorized spells and slot availability for today.

    Defaults to the PC. Pass character_target to inspect any other
    spellcasting character's slots (multi-class henchmen, hireling
    magic-users, etc.). Returns an error if the target doesn't resolve
    or has no spellcasting class.

    Reads the character's current class levels, computes total spell slots
    from classes.json (AD&D 1e tables), then cross-references with the
    per-character spell_memory world_fact to show which slots are expended.

    Returns:
      character_id  -- the resolved character_id (PC or henchman)
      slots_total   -- slots available per class per spell level at current level
      memorized     -- full list of memorized spells with expended status
      available     -- only the non-expended memorized spells (ready to cast)
      expended      -- spells already cast this day
      has_unmemorized_slots -- True if any available slot has no spell assigned
    """
    import json as _json

    # Resolve character_target → character_id
    cid: int | None = None
    if (character_target or "").strip():
        cid = _resolve_character(character_target)
        if cid is None:
            return {
                "error": (
                    f"character_target {character_target!r} did not resolve "
                    "— use list_characters to discover available names/ids."
                ),
            }
    from engine.db import _get_conn as _ec, _PC_CHARACTER_ID as _pc_id
    target_id = cid if cid is not None else _pc_id

    # Load classes.json for spell slot tables
    classes_data_path = _ROOT / "data" / "classes.json"
    with open(classes_data_path, encoding="utf-8") as f:
        classes_data = _json.load(f)

    with _ec(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT class_name, level FROM class_levels WHERE character_id = ?",
            (target_id,),
        )
        class_levels = {r["class_name"]: r["level"] for r in cur.fetchall()}

    # Compute total slots
    slots_total: dict = {}
    for cls_name, lvl in class_levels.items():
        cls_data = classes_data.get(cls_name, {})
        slot_table = cls_data.get("spell_slots", {})
        if not slot_table:
            continue  # Fighters / Thieves etc. have no spell slots
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

    # Load spell memory for THIS character
    memory = get_spell_memory(character_id=target_id)
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
        "character_id":          target_id,
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
    character_target: Annotated[
        str,
        "Optional name (case-insensitive prefix match) or numeric "
        "character_id of the spellcaster. Leave blank to default to the "
        "PC. Each character maintains independent memorized spells.",
    ] = "",
) -> dict:
    """
    Set today's memorized spell list for one spellcasting class.

    Defaults to the PC. Pass character_target to memorize spells for any
    other tracked spellcaster (henchman magic-user, hireling cleric).
    Spell memory is stored per-character — memorizing for Caiya does not
    affect the PC's spell list.

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

    # Resolve character_target → character_id
    cid: int | None = None
    if (character_target or "").strip():
        cid = _resolve_character(character_target)
        if cid is None:
            return {
                "error": (
                    f"character_target {character_target!r} did not resolve "
                    "— use list_characters to discover available names/ids."
                ),
            }
    from engine.db import _get_conn as _ec, _PC_CHARACTER_ID as _pc_id
    target_id = cid if cid is not None else _pc_id

    # Resolve class name
    cls_raw  = class_name.lower().replace("-", "_").replace(" ", "_") if class_name else ""

    # Load class data for slot validation
    classes_data_path = _ROOT / "data" / "classes.json"
    with open(classes_data_path, encoding="utf-8") as f:
        classes_data = _json.load(f)

    with _ec(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT class_name, level FROM class_levels WHERE character_id = ?",
            (target_id,),
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

    # Merge with existing memory (keep other classes); per-character.
    memory    = get_spell_memory(character_id=target_id)
    existing  = [
        s for s in memory.get("memorized", [])
        if s.get("class_name", "").replace("-", "_") != db_class_name
    ]
    memory["memorized"] = existing + resolved
    set_spell_memory(memory, character_id=target_id)

    return {
        "memorized":       resolved,
        "character_id":    target_id,
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
    character_target: Annotated[
        str,
        "Optional name (case-insensitive prefix match) or numeric "
        "character_id of the caster. Leave blank to default to the PC. "
        "The caster's spell memory is consulted (per-character), and the "
        "expended flag is written back to that same per-character store.",
    ] = "",
) -> dict:
    """
    Expend one memorized spell slot and return the spell's full description.

    Defaults to the PC. Pass character_target to cast from any other
    spellcaster's memorized list (henchman / hireling magic-user, etc.).
    Each character has independent spell memory.

    Finds the first non-expended slot in the caster's memorized list that
    matches spell_name. Marks it as expended. Retrieves the complete spell
    record from the spells table including range, duration, area of
    effect, saving throw, and description.

    Returns everything needed to narrate the spell effect:
      caster character_id, spell_name, level, school, range, duration,
      area, saving_throw, summary_text, combat_use_text, description.

    After casting, check if any mechanical results need to be resolved:
      - If saving_throw is not empty, prompt the DM/player for a save roll
      - If the spell deals damage, use roll_dice() for the damage expression
      - If the spell affects HP, call update_character_status()
    """
    # Resolve character_target → character_id
    cid: int | None = None
    if (character_target or "").strip():
        cid = _resolve_character(character_target)
        if cid is None:
            return {
                "error": (
                    f"character_target {character_target!r} did not resolve "
                    "— use list_characters to discover available names/ids."
                ),
            }
    from engine.db import _PC_CHARACTER_ID as _pc_id
    target_id = cid if cid is not None else _pc_id

    memory    = get_spell_memory(character_id=target_id)
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
            "error": f"No available slot for '{spell_name}' on character_id={target_id}. "
                     f"Available spells: {available_names or ['(none — all expended or none memorized)']}",
            "character_id": target_id,
        }

    # Mark expended (per-character write)
    memorized[slot_index]["expended"] = True
    memory["memorized"] = memorized
    set_spell_memory(memory, character_id=target_id)

    # Retrieve full spell data from DB
    spell_data = lookup_spell(
        target_slot["name"],
        target_slot.get("class_name"),
    )

    remaining = sum(1 for s in memorized if not s.get("expended", False))

    result = {
        "cast":        True,
        "character_id": target_id,
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
    character_target: Annotated[
        str,
        "Optional name (case-insensitive prefix match) or numeric "
        "character_id of the resting character. Leave blank to default "
        "to the PC. HP recovery and spell-slot restoration both flow "
        "through the resolved character_id, so each party member rests "
        "independently. The calendar advance is global (one shared "
        "calendar) so calling rest once for the whole party works fine "
        "if each member calls it in turn — calendar only advances on "
        "the first call's calendar_note.",
    ] = "",
) -> dict:
    """
    Advance time and restore resources after a rest.

    Defaults to the PC. Pass character_target to rest any other tracked
    spellcaster / HP-tracked character. Each character's spell memory is
    independent; resting Caiya restores HER expended slots, not the PC's.

    Long rest (8 hours):
      - Restores all spell slots (clears all expended flags in
        spell_memory for the target character).
      - Recovers HP: 1 HP per character level (campaign ruling — fast
        enough for solo play without being trivially instant). HP
        cannot exceed max.
      - Advances the in-game calendar by 8 hours (stored in world_facts
        category 'calendar').

    Short rest (1 hour):
      - No spell recovery.
      - No HP recovery.
      - Advances the in-game calendar by 1 hour.

    Returns updated HP and spell slots after the rest.
    """
    import datetime as _dt

    # Resolve character_target → character_id
    cid: int | None = None
    if (character_target or "").strip():
        cid = _resolve_character(character_target)
        if cid is None:
            return {
                "error": (
                    f"character_target {character_target!r} did not resolve "
                    "— use list_characters to discover available names/ids."
                ),
            }

    from engine.db import _get_conn as _ec, _PC_CHARACTER_ID as _pc_id, _CAMPAIGN_ID as _cid
    target_id = cid if cid is not None else _pc_id

    # ── Load target character status ──────────────────────────────────────────
    with _ec(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT hp_current, hp_max FROM character_status WHERE character_id = ?",
            (target_id,),
        )
        row = cur.fetchone()

    hp_cur = row["hp_current"] if row else 0
    hp_max = row["hp_max"]     if row else 0

    # Total character levels (sum across all classes)
    with _ec(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT SUM(level) as total FROM class_levels WHERE character_id = ?",
            (target_id,),
        )
        lrow = cur.fetchone()
    total_levels = (lrow["total"] or 1) if lrow else 1

    result: dict = {"rest_type": rest_type, "character_id": target_id}

    if rest_type.lower().startswith("long"):
        # ── HP recovery (per-character) ───────────────────────────────────────
        hp_gained  = min(total_levels, hp_max - hp_cur)
        new_hp     = hp_cur + hp_gained
        with _ec() as conn:
            conn.execute(
                "UPDATE character_status SET hp_current = ? WHERE character_id = ?",
                (new_hp, target_id),
            )

        # ── Spell slot restoration (per-character) ────────────────────────────
        memory = get_spell_memory(character_id=target_id)
        for slot in memory.get("memorized", []):
            slot["expended"] = False
        memory["last_rest"] = calendar_note or "after long rest"
        set_spell_memory(memory, character_id=target_id)

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

    # Phase 35: one visual ref per unique creature surfaced. 'human' /
    # 'subtable' branches have no canonical stat-block name to link, so we
    # only attach a ref when we actually rolled a named monster.
    if enc.get("branch_type") == "monster" and enc.get("monster_name"):
        block = _visual_refs_block([enc["monster_name"]])
        if block:
            result["visual_refs"] = block

    return result


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: check_aerial_encounter
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def check_aerial_encounter(
    elevation: Annotated[
        str,
        "Elevation tier: 'low' (under 1000 ft — giant eagles, hippogriffs, "
        "griffons, wyverns, gargoyles), 'medium' (1000–5000 ft — dragons, "
        "manticores, pegasi, chimera, giant hawks), or 'high' (above 5000 ft "
        "— rocs, storm giants, air elementals, invisible stalkers). "
        "Determines which AD&D 1e aerial encounter table is rolled.",
    ] = "low",
    terrain: Annotated[
        str,
        "Terrain below the party: forest, mountains, plains, coast, swamp, "
        "hills, or desert. Falls back to a generic table for the elevation "
        "if the terrain isn't explicitly tabled.",
    ] = "plains",
    chance_in_6: Annotated[
        int,
        "Number of d6 faces that trigger an encounter. AD&D default is 1 "
        "(1-in-6). Use 2 over migratory paths, dragon territory, or open-sky "
        "flight on hippogriff/pegasus mounts. Clamped to 1–6.",
    ] = 1,
) -> dict:
    """
    Roll a 1-in-6 (or chance_in_6) check for an aerial encounter.

    On miss returns {"encounter": false} with a "sky is clear" note. On hit
    rolls a creature on the AD&D 1e aerial encounter table for the given
    elevation × terrain, plus number appearing, altitude descriptor, and
    approach direction. The reaction_roll_eligible flag is True when the
    creature is intelligent enough that parley/reaction is meaningful;
    follow up with roll_reaction() to determine disposition.

    Call once per hour of overland flight, or whenever the party scans the
    skies in known territory. Pairs with check_wandering_monster (ground)
    and start_combat (if the encounter turns hostile).
    """
    return db_check_aerial_encounter(
        elevation=elevation,
        terrain=terrain,
        chance_in_6=chance_in_6,
    )


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: roll_reaction
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def roll_reaction(
    creature_name: Annotated[
        str,
        "Name of the creature, NPC, or group whose disposition is being "
        "rolled. Used in the interpretation text and persisted to "
        "world_facts category 'reaction_log'.",
    ],
    charisma_modifier: Annotated[
        int,
        "Charisma reaction adjustment of the speaker (the PC negotiating). "
        "AD&D 1e Cha mods range from -5 (Cha 3) to +4 (Cha 18+). Default 0.",
    ] = 0,
    situation_modifier: Annotated[
        int,
        "Circumstantial adjustment in [-3, +3]. Positive: gifts, shared "
        "language, common enemy, charm spell active, peaceful approach. "
        "Negative: drawn weapons, recent insult, faction rivalry, prior "
        "wrong done. Out-of-range values are clamped.",
    ] = 0,
) -> dict:
    """
    Roll 2d6 + Cha mod + situation mod on the AD&D 1e reaction table.

    Result tiers (final 2d6 + modifiers):
      ≤ 2  → Immediate attack (no parlay possible)
      3–5  → Hostile, likely to attack
      6–8  → Uncertain / wary
      9–11 → Indifferent / neutral
      12+  → Friendly, willing to talk

    Returns the full breakdown (both d6 results, modifiers, final), the
    disposition tier, and a one-line behavioral interpretation. Logs every
    roll to world_facts (category 'reaction_log') for an audit trail of
    social encounter outcomes.

    Use for: NPC first meetings, creatures encountered without surprise,
    parley attempts, charm-spell follow-ups, and any moment the party's
    welcome is in question. This tool is the mechanical backbone for
    every social encounter — call it instead of inventing dispositions.
    """
    return db_roll_reaction(
        creature_name=creature_name,
        charisma_modifier=charisma_modifier,
        situation_modifier=situation_modifier,
    )


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

_DOMAIN_SECTIONS = {"all", "holdings", "troops", "treasury", "projects", "summary"}


@mcp.tool()
def get_domain_state(
    section: Annotated[
        str,
        "Which slice to return. One of: 'all' (default — every section, "
        "may auto-degrade on rich campaigns), 'holdings', 'troops', "
        "'treasury', 'projects', 'summary' (just the income / upkeep / "
        "net range roll-ups + last_domain_turn — the cheapest call, "
        "ideal for monthly-bookkeeping checks).",
    ] = "all",
) -> dict:
    """
    Return domain state (holdings / troops / treasury / projects / summary).

    section='all' (default) returns every section. On rich campaigns this
    may exceed the 30 KB cap and auto-degrade. For surgical access prefer
    section='holdings' / 'projects' / 'summary' / etc — single-section
    calls are far smaller.

    Always returns the income / upkeep / net rollup fields alongside the
    requested section so the financial picture stays visible.
    """
    sec = (section or "all").strip().lower()
    if sec not in _DOMAIN_SECTIONS:
        return {
            "error": f"section must be one of {sorted(_DOMAIN_SECTIONS)}; "
                     f"got {section!r}.",
            "allowed_sections": sorted(_DOMAIN_SECTIONS),
        }

    full = get_full_domain_state()

    if sec == "all":
        return _cap_response(
            full,
            summary_fn=_summarize_domain_state,
            tool_name="get_domain_state",
        )

    # Always include the small monetary roll-ups so the caller has the
    # bottom-line financial context regardless of which section they ask for.
    rollup = {
        "treasury_total_gp":            full.get("treasury_total_gp"),
        "treasury_total_gp_equivalent": full.get("treasury_total_gp_equivalent"),
        "treasury_formatted_total":     full.get("treasury_formatted_total"),
        "monthly_income_range":         full.get("monthly_income_range"),
        "monthly_upkeep_gp":            full.get("monthly_upkeep_gp"),
        "monthly_net_range":            full.get("monthly_net_range"),
        "last_domain_turn":             full.get("last_domain_turn"),
    }

    section_payload: dict = {"section": sec, **rollup}
    if sec == "holdings":
        section_payload["holdings"] = full.get("holdings", [])
    elif sec == "troops":
        section_payload["troops"] = full.get("troops", [])
    elif sec == "treasury":
        section_payload["treasury_accounts"] = full.get("treasury_accounts", [])
    elif sec == "projects":
        section_payload["projects"] = full.get("projects", [])
    # 'summary' — rollup only, no list. Already included above.

    return _cap_response(
        section_payload,
        summary_fn=_summarize_domain_state,
        tool_name=f"get_domain_state(section={sec})",
    )


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

    INCOME RESOLUTION ORDER:
      1. world_facts category 'income_canon' (if present) — overrides
         per-holding rates. Use this for trade-circuit-driven economies
         where the per-location income table dramatically underestimates
         actual revenue. Set via update_world_fact(category='income_canon',
         fact_text=...) — JSON object with monthly_gross_gp, or free text
         containing 'GROSS MONTHLY INCOME: ~N gp'.
      2. Per-holding rolls (the default) — each active location rolls its
         monthly income range from the engine table, independently per
         month. Income rates by type (monthly, rough range):
           Keep ~120–220 gp · City ~150–350 gp · District ~70–130 gp
           Mill ~55–105 gp · Farm ~45–85 gp · Workshop ~25–55 gp
           Lodge ~15–35 gp · Inn ~25–55 gp · Civic ~10–30 gp

    The result includes an 'income_source' field describing which path
    fired. Call this once per game month or as part of domain_turn().
    The total is logged in domain_income_expenses regardless of source
    and optionally credited to the primary treasury.
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

    src = result.get("income_source", "per-holding rolls")
    if "income_canon" in src:
        result["note"] = (
            f"Income collected: {result['total_gp']:,} gp over {months} "
            f"month(s) per income_canon "
            f"(world_fact_id={result.get('world_fact_id')})."
        )
    else:
        result["note"] = (
            f"Income collected: {result['total_gp']:,} gp from "
            f"{result['holdings_rolled']} holdings over {months} month(s) "
            f"(per-holding rolls — no income_canon set)."
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
        "total_gp":         income["total_gp"],
        "income_source":    income.get("income_source"),
        "holdings_rolled":  income["holdings_rolled"],
        "monthly_gross_gp": income.get("monthly_gross_gp"),
        "monthly_net_gp":   income.get("monthly_net_gp"),
        "breakdown":        income["breakdown"],
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

    # ── Step 6: Trade circuit due check (Phase 29) ─────────────────────────────
    # Best-effort — if the migration hasn't run yet (very fresh DB) or the
    # campaign has no circuits, we still want the rest of the report to land.
    try:
        circuits_due = db_check_circuits_due(current_day=None, lookahead_days=30)
        report["circuits"] = {
            "current_day":    circuits_due["current_day"],
            "overdue_count":  circuits_due["overdue_count"],
            "upcoming_count": circuits_due["upcoming_count"],
            "overdue":        circuits_due["overdue"],
            "upcoming":       circuits_due["upcoming"],
        }
    except Exception as e:
        report["circuits"] = {"error": str(e)}

    # ── Summary ───────────────────────────────────────────────────────────────
    net_gp = income["total_gp"] - upkeep["total_gp_charged"]
    report["net_gp_this_season"] = net_gp
    report["completed_projects"] = [c["name"] for c in construction.get("completed", [])]

    # Treasury rollup AFTER income/upkeep have been applied — gives the player
    # a single bottom-line read on every coin denomination plus the
    # gp-equivalent total.
    try:
        post_state = get_full_domain_state()
        report["treasury_after_season"] = {
            "treasury_total_pp":            post_state.get("treasury_total_pp"),
            "treasury_total_gp":            post_state.get("treasury_total_gp"),
            "treasury_total_ep":            post_state.get("treasury_total_ep"),
            "treasury_total_sp":            post_state.get("treasury_total_sp"),
            "treasury_total_cp":            post_state.get("treasury_total_cp"),
            "treasury_total_gp_equivalent": post_state.get("treasury_total_gp_equivalent"),
            "treasury_formatted_total":     post_state.get("treasury_formatted_total"),
        }
    except Exception as e:
        report["treasury_after_season"] = {"error": str(e)}

    report["summary"] = (
        f"Season {season_label}: "
        f"Income {income['total_gp']:,} gp | "
        f"Upkeep {upkeep['total_gp_charged']:,} gp | "
        f"Net {net_gp:+,} gp | "
        f"{len(construction.get('advanced', []))} projects progressed, "
        f"{len(construction.get('completed', []))} completed."
    )
    tas = report.get("treasury_after_season") or {}
    if isinstance(tas, dict) and tas.get("treasury_formatted_total"):
        report["summary"] += f" Treasury after: {tas['treasury_formatted_total']}."
    if roll_event and report["realm_event"]:
        report["summary"] += f" Realm event: {report['realm_event']['title']}."
    circ = report.get("circuits") or {}
    if isinstance(circ, dict) and (circ.get("overdue_count") or 0) > 0:
        report["summary"] += f" ⚠ {circ['overdue_count']} circuit(s) OVERDUE."
    elif isinstance(circ, dict) and (circ.get("upcoming_count") or 0) > 0:
        report["summary"] += f" {circ['upcoming_count']} circuit(s) due within 30 days."

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
    character_target: Annotated[
        str,
        "Optional name (case-insensitive prefix match) or numeric "
        "character_id of the carousing character. Leave blank to default "
        "to the PC. Lets henchmen / hirelings / NPC party members carouse "
        "and earn the XP themselves — gold debit still comes from the "
        "shared primary treasury, but XP lands in the carouser's "
        "class_levels rows.",
    ] = "",
) -> dict:
    """
    Carousing downtime activity — Jeff Rients style.

    Defaults to the PC. Pass character_target to have any tracked
    character carouse instead — XP equal to the gold spent goes to that
    character's class_levels rows. Treasury debit comes from the primary
    party purse regardless.

    The character spends an evening (or three) in the taverns and back alleys of
    the nearest settlement. Gold spent is deducted from the primary treasury and
    converted 1:1 into XP — always, regardless of what the dice decide.

    Then a d20 is rolled (modified by gold spend tier) to determine the night's
    consequence. Results 1-10 range from public embarrassment to serious trouble.
    Results 11-20 are colourful, beneficial, or at worst mixed.

    Returns:
    - character_id (the resolved carouser)
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
    cid: int | None = None
    if (character_target or "").strip():
        cid = _resolve_character(character_target)
        if cid is None:
            return {
                "error": (
                    f"character_target {character_target!r} did not resolve "
                    "— use list_characters to discover available names/ids."
                ),
            }
    if cid is None:
        return db_carouse(gold_spent=gold_spent, calendar_note=calendar_note)
    return db_carouse(
        gold_spent=gold_spent, calendar_note=calendar_note,
        character_id=cid,
    )


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
    character_target: Annotated[
        str,
        "Optional name (case-insensitive prefix match) or numeric "
        "character_id of the researching Magic-User. Leave blank to "
        "default to the PC. The Intelligence score consulted for the "
        "success-chance bonus is the researcher's; XP on success goes "
        "to that character's class_levels. Gold debit still comes from "
        "the shared primary treasury.",
    ] = "",
) -> dict:
    """
    Magic-User researches a new spell or copies one from a discovered scroll/tome.

    Defaults to the PC. Pass character_target to have a henchman /
    hireling Magic-User do the research — their INT drives the
    success-chance bonus, and any XP award lands in their class_levels.

    Success chance formula:
      Base 45% + (Intelligence modifier × 5%) + (extra weeks over minimum × 5%)
      Capped at 5% minimum / 95% maximum.

    On success:
    - Spell is noted as added to spellbook (DM calls update_world_fact to record it)
    - XP awarded: 100 × spell_level (to the resolved researcher)
    - Calendar advances by days spent

    On failure:
    - Half the time invested (research notes exist; retry possible)
    - All gold spent (materials consumed)
    - No XP

    The spell is not memorized upon research — use memorize_spells after the
    next long rest to prepare it.

    Returns: character_id, spell_name, spell_level, success, roll,
    success_chance_pct, days_spent, gold_spent, expected_cost_gp,
    intelligence (the researcher's score), xp_awarded, calendar, dm_note.
    """
    cid: int | None = None
    if (character_target or "").strip():
        cid = _resolve_character(character_target)
        if cid is None:
            return {
                "error": (
                    f"character_target {character_target!r} did not resolve "
                    "— use list_characters to discover available names/ids."
                ),
            }
    if cid is None:
        return db_research_spell(
            spell_name=spell_name,
            spell_level=spell_level,
            days=days,
            gold_spent=gold_spent,
            calendar_note=calendar_note,
        )
    return db_research_spell(
        spell_name=spell_name,
        spell_level=spell_level,
        days=days,
        gold_spent=gold_spent,
        calendar_note=calendar_note,
        character_id=cid,
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
    character_target: Annotated[
        str,
        "Optional name (case-insensitive prefix match) or numeric "
        "character_id of the recovering character. Leave blank to default "
        "to the PC. Lets henchmen / hirelings / NPC party members run "
        "their own bed-rest recovery — Caiya patching up after a moathouse "
        "fight, a hireling laid up with mummy rot, etc. The 5xp/day "
        "symbolic award is PC-only; non-PC targets can be granted XP "
        "explicitly via grant_xp.",
    ] = "",
) -> dict:
    """
    Extended rest for serious injuries or magical ailments beyond normal healing.

    Defaults to the PC. Pass character_target to recover any tracked
    character (henchman, hireling, NPC party member). Errors clearly
    if the target name/id can't be resolved or has no character_status row.

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

    XP: 5 per day for the PC only (time-cost). Non-PC targets get
    xp_awarded=0 with an xp_note explaining how to award via grant_xp.

    Returns: character_id, injury_description, days_resting, hp_before,
    hp_after, hp_recovered, ailments_cleared, recovery_note, xp_awarded,
    xp_note (when applicable), calendar, dm_note.
    """
    cid: int | None = None
    if (character_target or "").strip():
        cid = _resolve_character(character_target)
        if cid is None:
            return {
                "error": (
                    f"character_target {character_target!r} did not resolve "
                    "— use list_characters to discover available names/ids."
                ),
            }
    try:
        if cid is None:
            return db_recovery(
                injury_description=injury_description,
                days_resting=days_resting,
                calendar_note=calendar_note,
            )
        return db_recovery(
            injury_description=injury_description,
            days_resting=days_resting,
            calendar_note=calendar_note,
            character_id=cid,
        )
    except ValueError as e:
        return {"error": str(e)}


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
    return _cap_response(
        db_get_loyalty_state(),
        summary_fn=_summarize_loyalty_state,
        tool_name="get_loyalty_state",
    )


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
# TURN VERIFICATION  (Phase 6)
# ══════════════════════════════════════════════════════════════════════════════


@mcp.tool()
def verify_turn(
    turn_id: Annotated[
        int,
        "Turn ID to verify. Omit (or pass 0) to verify the most recent turn.",
    ] = 0,
) -> dict:
    """
    Cross-check the structured markers from a saved turn against current
    database state.

    Called automatically by save_turn after every write — you only need to
    invoke directly when re-auditing an older turn.

    Reads the markers list that save_turn stored on the turn (NOT scene_notes
    prose). For every marker, looks up the relevant table and reports whether
    the DB matches.

    Marker formats it understands (see save_turn for the canonical reference):
        cast:[spell name]
        item_added:[name]               item_used:[name]
        hp:[old]>[new]
        spent:[amount]gp                gained:[amount]gp
        npc_added:[name]
        location_changed:[name]
        troop_change:[group]:[old]>[new]

    Verdict values:
        "no_claims"        — turn had zero markers; verification did not run.
                             This is distinct from "clean" — silence is not
                             the same as verified. Whenever state actually
                             changed and you see this, you forgot the markers.
        "clean"            — every marker matched DB state.
        "needs_attention"  — at least one marker is unverified or malformed.
        "conflict"         — at least one marker contradicts DB state. A tool
                             call was missed; resolve before the next turn.

    Returns: turn_id, verdict, marker_count, confirmed[], unverified[],
    conflicts[], malformed[] (only if any markers failed to parse). Each
    unverified/conflict entry includes a suggested_call: the exact tool
    invocation to close the gap.
    """
    return db_verify_turn(turn_id=turn_id if turn_id else None)


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
# TOOL: list_campaigns
# ══════════════════════════════════════════════════════════════════════════════

def _read_campaign_info(db_path: Path) -> dict:
    """Extract PC name and class levels from a campaign DB (read-only)."""
    info = {
        "filename":  db_path.name,
        "path":      f"saves/{db_path.name}",
        "character": None,
        "classes":   None,
        "error":     None,
    }
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        try:
            row = cur.execute(
                "SELECT name FROM characters WHERE character_id = 1"
            ).fetchone()
            if row:
                info["character"] = row[0]
        except Exception:
            pass
        try:
            rows = cur.execute(
                "SELECT class_name, level FROM class_levels "
                "WHERE character_id = 1 ORDER BY class_name"
            ).fetchall()
            if rows:
                info["classes"] = " / ".join(
                    f"{r['class_name']} {r['level']}" for r in rows
                )
        except Exception:
            pass
        conn.close()
    except sqlite3.OperationalError as e:
        info["error"] = f"Cannot open: {e}"
    return info


def _active_campaign_rel() -> str:
    """Return the active_campaign_db value from config.json (normalised), or ''."""
    config_path = _ROOT / "config.json"
    if not config_path.exists():
        return ""
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        return cfg.get("active_campaign_db", "").replace("\\", "/")
    except Exception:
        return ""


@mcp.tool()
def list_campaigns() -> dict:
    """
    List every campaign database in saves/ with its character name and class.

    Returns a dict with:
      campaigns -- list of {filename, path, character, classes, active}
                   where classes is a string like "Fighter 7 / Magic-User 7".
      count     -- number of .db files found.

    Use before switch_campaign so the player can see what's available.
    Does not modify anything on disk.
    """
    saves_dir = _ROOT / "saves"
    db_files = sorted(saves_dir.glob("*.db"))
    active_rel = _active_campaign_rel()

    campaigns = []
    for db_path in db_files:
        info = _read_campaign_info(db_path)
        info["active"] = (info["path"] == active_rel)
        campaigns.append(info)

    return {"campaigns": campaigns, "count": len(campaigns)}


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: switch_campaign
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def switch_campaign(
    identifier: Annotated[
        str,
        "Either a database filename ('aelric_silvertongue.db'), a relative "
        "path ('saves/aelric_silvertongue.db'), the DB stem ('aelric_silvertongue'), "
        "or the character's name ('Aelric Silvertongue', 'aelric'). Matching "
        "is case-insensitive and falls back to substring search if an exact "
        "match is not found.",
    ],
) -> dict:
    """
    Point config.json at a different campaign database.

    The engine re-reads config.json on every connection, so the switch is
    hot — the very next tool call (e.g. get_character_state) will target
    the new database with no Claude Desktop restart required. The
    databases themselves are not modified.

    Returns on success:
      character -- PC name from the newly-selected DB
      classes   -- Class levels string (e.g. "Fighter 7 / Magic-User 7")
      database  -- Relative DB path now stored in config.json
      note      -- Confirmation that the change is already live

    Returns an error dict if no matching DB is found, the identifier is
    ambiguous, or the selected DB cannot be read.
    """
    saves_dir = _ROOT / "saves"
    db_files = sorted(saves_dir.glob("*.db"))
    if not db_files:
        return {"error": "No .db files found in saves/."}

    raw = identifier.strip()
    if not raw:
        return {"error": "Identifier is empty."}

    needle = raw.replace("\\", "/")
    if needle.lower().startswith("saves/"):
        needle = needle[len("saves/"):]
    needle_lower = needle.lower()

    # 1. Exact filename / stem match
    matches: list[Path] = []
    for db_path in db_files:
        if needle_lower in (db_path.name.lower(), db_path.stem.lower()):
            matches = [db_path]
            break

    # 2. Exact character-name match
    if not matches:
        for db_path in db_files:
            info = _read_campaign_info(db_path)
            char = (info.get("character") or "").lower()
            if char and needle_lower == char:
                matches = [db_path]
                break

    # 3. Substring match across both filename stem and character name
    if not matches:
        for db_path in db_files:
            info = _read_campaign_info(db_path)
            char = (info.get("character") or "").lower()
            if needle_lower in db_path.stem.lower() or (char and needle_lower in char):
                matches.append(db_path)

    if not matches:
        return {
            "error":     f"No campaign matching '{identifier}' found in saves/.",
            "available": [p.name for p in db_files],
        }
    if len(matches) > 1:
        return {
            "error":   f"'{identifier}' matches multiple campaigns; be more specific.",
            "matched": [p.name for p in matches],
        }

    selected = matches[0]
    info = _read_campaign_info(selected)
    if info.get("error"):
        return {"error": f"Selected DB unreadable: {info['error']}"}

    rel = f"saves/{selected.name}"

    # Preserve any other config keys when rewriting
    config_path = _ROOT / "config.json"
    cfg: dict = {}
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    cfg["active_campaign_db"] = rel
    config_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")

    return {
        "character": info.get("character"),
        "classes":   info.get("classes"),
        "database":  rel,
        "note":      (
            "config.json updated — the switch is already live. The next "
            "tool call will target the new database; no Claude Desktop "
            "restart is needed."
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: backup_campaign
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def backup_campaign(
    label: Annotated[
        str,
        "Optional short label appended to the backup filename "
        "(e.g. 'pre_dragon_fight'). Non-alphanumeric chars are sanitised. "
        "Leave blank for a plain timestamp-only name.",
    ] = "",
) -> dict:
    """
    Snapshot the currently active campaign DB to saves/backups/.

    Uses SQLite's online backup API so the copy is consistent even with the
    MCP server's connection open (WAL mode is respected). The backup
    directory is created on demand.

    Filename format:  <stem>_<YYYYMMDD-HHMMSS>[_<label>].db  (UTC timestamp)

    Returns:
      source     -- relative path of the active DB that was backed up
      backup     -- relative path of the new backup file
      bytes      -- size of the backup file
      timestamp  -- UTC timestamp used in the filename

    Call before risky moments: big combats, domain turns, one-way downtime
    activities. Restoring is a manual swap of the file back into saves/
    plus a switch_campaign call.
    """
    active_rel = _active_campaign_rel()
    if not active_rel:
        return {"error": "No active campaign found in config.json."}

    src = (_ROOT / active_rel).resolve()
    if not src.exists():
        return {"error": f"Active DB missing on disk: {active_rel}"}

    backups_dir = _ROOT / "saves" / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    safe_label = re.sub(r"[^A-Za-z0-9_-]+", "_", label.strip()).strip("_")
    suffix = f"_{safe_label}" if safe_label else ""
    dest = backups_dir / f"{src.stem}_{ts}{suffix}.db"

    try:
        src_conn  = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        dest_conn = sqlite3.connect(dest)
        with dest_conn:
            src_conn.backup(dest_conn)
        src_conn.close()
        dest_conn.close()
    except sqlite3.Error as e:
        if dest.exists():
            try: dest.unlink()
            except Exception: pass
        return {"error": f"Backup failed: {e}"}

    return {
        "source":    active_rel,
        "backup":    f"saves/backups/{dest.name}",
        "bytes":     dest.stat().st_size,
        "timestamp": ts,
    }


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: search_history
# ══════════════════════════════════════════════════════════════════════════════

def _snippet(text: str, needle: str, window: int = 60) -> str:
    """Return a short snippet of `text` centred on the first case-insensitive
    occurrence of `needle`, with ellipses on either side as needed."""
    if not text:
        return ""
    lo = text.lower().find(needle.lower())
    if lo < 0:
        return text[: window * 2] + ("…" if len(text) > window * 2 else "")
    start = max(0, lo - window)
    end   = min(len(text), lo + len(needle) + window)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return prefix + text[start:end] + suffix


@mcp.tool()
def search_history(
    query: Annotated[
        str,
        "Keyword or phrase to search for in past turns. Matching is "
        "case-insensitive and substring-based (SQL LIKE). Searches the "
        "player's action, the DM's narration, AND the structured markers "
        "field (where cast:X, item_added:Y etc. live) — so spell names, "
        "item names, and NPC names that only appear in markers are now "
        "findable.",
    ],
    limit: Annotated[
        int,
        "Maximum number of matching turns to return. Results are ordered "
        "most-recent-first.",
    ] = 20,
) -> dict:
    """
    Search the full ai_turns history for a keyword (e.g. an NPC name, place,
    item, spell) and return matching turns with snippets.

    Complements get_recent_history, which only returns the last N turns.
    Use when the player references something from earlier in the campaign
    ("that merchant in Hommlet", "the ring we found in the crypt", "when
    did Ramun cast Cloudkill") and you need to ground the recollection in
    what actually happened.

    Search coverage:
      - ai_turns.player_action
      - ai_turns.dm_response
      - ai_turns.structured_response_json (catches markers like cast:X)

    Returns:
      query                  -- the search string used
      count                  -- number of matching turns returned
      total_turns_searched   -- size of the ai_turns table (sanity check)
      matches                -- list of {turn_id, created_at, matched_in,
                                player_snippet, dm_snippet, marker_match}
                                where matched_in is 'player', 'dm',
                                'markers', or any combination joined by '+'.
    """
    q = (query or "").strip()
    if not q:
        return {"error": "Query is empty."}
    if limit < 1 or limit > 200:
        return {"error": "limit must be between 1 and 200."}

    active_rel = _active_campaign_rel()
    if not active_rel:
        return {"error": "No active campaign found in config.json."}
    db_path = (_ROOT / active_rel).resolve()
    if not db_path.exists():
        return {"error": f"Active DB missing on disk: {active_rel}"}

    like = f"%{q}%"
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        # Total turn count for sanity-check in the response.
        total = conn.execute("SELECT COUNT(*) FROM ai_turns").fetchone()[0]
        rows = conn.execute(
            "SELECT turn_id, player_action, dm_response, "
            "       structured_response_json, created_at "
            "FROM ai_turns "
            "WHERE player_action            LIKE ? COLLATE NOCASE "
            "   OR dm_response              LIKE ? COLLATE NOCASE "
            "   OR structured_response_json LIKE ? COLLATE NOCASE "
            "ORDER BY turn_id DESC LIMIT ?",
            (like, like, like, limit),
        ).fetchall()
        conn.close()
    except sqlite3.Error as e:
        return {"error": f"Search failed: {e}"}

    matches = []
    ql = q.lower()
    for r in rows:
        pa = r["player_action"] or ""
        dm = r["dm_response"]   or ""
        sj = r["structured_response_json"] or ""
        in_p = ql in pa.lower()
        in_d = ql in dm.lower()
        in_m = ql in sj.lower()
        where_parts = []
        if in_p: where_parts.append("player")
        if in_d: where_parts.append("dm")
        if in_m: where_parts.append("markers")
        # Try to extract the matched marker so the response is informative.
        marker_match = ""
        if in_m and sj:
            try:
                struct = json.loads(sj) or {}
                for m in (struct.get("markers") or []):
                    if isinstance(m, str) and ql in m.lower():
                        marker_match = m
                        break
            except (json.JSONDecodeError, TypeError):
                pass
        matches.append({
            "turn_id":        r["turn_id"],
            "created_at":     r["created_at"],
            "matched_in":     "+".join(where_parts) or "unknown",
            "player_snippet": _snippet(pa, q) if in_p else "",
            "dm_snippet":     _snippet(dm, q) if in_d else "",
            "marker_match":   marker_match,
        })

    return {
        "query":                q,
        "count":                len(matches),
        "total_turns_searched": total,
        "matches":              matches,
    }


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: get_world_facts
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_world_facts(
    category: Annotated[
        str,
        "Optional category filter (e.g. 'geography', 'factions', 'rumours'). "
        "Leave blank to return every fact grouped by category — but on a "
        "rich campaign that's hundreds of KB and will always auto-degrade. "
        "Always prefer a specific category for surgical access.",
    ] = "",
    search: Annotated[
        str,
        "Optional case-insensitive substring to match against fact_text. "
        "Combines with category — pass both to scope to a category AND "
        "filter by content. Empty string means no substring filter. "
        "Especially useful for the unbounded edit_log category and "
        "long-running rumour categories.",
    ] = "",
    limit: Annotated[
        int,
        "Maximum facts to return after filters apply. 0 (default) means "
        "no row-count limit at the SQL layer (the response cap may still "
        "auto-degrade further). Use a small limit (10-50) for surveying "
        "a long category without auto-degrading.",
    ] = 0,
    fact_ids: Annotated[
        str,
        "Direct-lookup escape hatch for 'is fact #N still alive?' style "
        "questions. Comma-separated list of integer world_fact_ids "
        "(e.g. '842,907,1041') OR a JSON array string. When non-empty, "
        "category / search / limit are IGNORED — the result returns "
        "exactly the rows whose world_fact_id is in this set, plus a "
        "missing_ids list naming any IDs that didn't resolve (so the "
        "caller can distinguish 'fact deleted' from 'fact in another "
        "campaign'). Bypasses the substring-search index entirely; "
        "useful when search_index gaps are suspected.",
    ] = "",
) -> dict:
    """
    Return campaign world facts stored via update_world_fact.

    World facts are the DM's persistent notes about the setting — canon
    details, secrets, established truths — that need to survive across
    sessions. The world_facts table grows without bound; never call
    unfiltered on a rich campaign — always pass at least one of category /
    search / limit / fact_ids.

    Filters compose (except fact_ids, which is exclusive):
      - category narrows to one category
      - search filters by fact_text substring (case-insensitive)
      - limit caps the row count
      - fact_ids does direct PK lookup, ignoring everything else

    Returns:
      count          -- number of facts returned
      categories     -- list of category names present in the result
      by_category    -- {category: [{id, fact, source_note}, ...]}
      filters_applied -- echo of category / search / limit / fact_ids
      missing_ids    -- (only present when fact_ids was used) IDs that
                        didn't resolve — so the caller can distinguish
                        a deleted row from an existing one

    Empty result is not an error — it just means nothing matched.
    """
    active_rel = _active_campaign_rel()
    if not active_rel:
        return {"error": "No active campaign found in config.json."}
    db_path = (_ROOT / active_rel).resolve()
    if not db_path.exists():
        return {"error": f"Active DB missing on disk: {active_rel}"}

    cat       = (category or "").strip()
    needle    = (search or "").strip()
    limit_int = max(0, int(limit or 0))

    # ── fact_ids direct-lookup path ──────────────────────────────────────────
    fid_raw = (fact_ids or "").strip()
    requested_ids: list[int] = []
    if fid_raw:
        # Try JSON array first; fall back to comma-separated parse.
        try:
            decoded = json.loads(fid_raw)
            if isinstance(decoded, list):
                requested_ids = [int(x) for x in decoded
                                 if isinstance(x, (int, str)) and str(x).strip().isdigit()]
        except (json.JSONDecodeError, TypeError, ValueError):
            requested_ids = []
        if not requested_ids:
            for part in fid_raw.split(","):
                p = part.strip()
                if p.isdigit():
                    requested_ids.append(int(p))
        if not requested_ids:
            return {
                "error": (
                    f"fact_ids parsed to no integer ids: {fid_raw!r}. Use "
                    "either a comma-separated list ('842,907') or a JSON "
                    "array ('[842, 907]')."
                ),
            }

    if requested_ids:
        placeholders = ",".join("?" for _ in requested_ids)
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT world_fact_id, category, fact_text, source_note "
                f"FROM world_facts "
                f"WHERE campaign_id = 1 AND world_fact_id IN ({placeholders}) "
                f"ORDER BY world_fact_id",
                requested_ids,
            ).fetchall()
            conn.close()
        except sqlite3.Error as e:
            return {"error": f"Read failed: {e}"}

        by_category: dict[str, list[dict]] = {}
        found_ids: set[int] = set()
        for r in rows:
            found_ids.add(int(r["world_fact_id"]))
            by_category.setdefault(r["category"], []).append({
                "id":          r["world_fact_id"],
                "fact":        r["fact_text"],
                "source_note": r["source_note"],
            })
        missing = [i for i in requested_ids if i not in found_ids]
        # No cap path on direct lookup — the caller specifically asked for
        # these N rows and presumably knows it's a short list.
        return {
            "count":       len(rows),
            "categories":  sorted(by_category.keys()),
            "by_category": by_category,
            "missing_ids": missing,
            "filters_applied": {"fact_ids": requested_ids},
        }

    # ── Standard category / search / limit path ─────────────────────────────
    where = ["campaign_id = 1"]
    params: list = []
    if cat:
        where.append("category = ? COLLATE NOCASE")
        params.append(cat)
    if needle:
        where.append("LOWER(fact_text) LIKE LOWER(?)")
        params.append(f"%{needle}%")
    order = "category, world_fact_id" if not cat else "world_fact_id"
    sql = (
        "SELECT world_fact_id, category, fact_text, source_note "
        "FROM world_facts "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY {order}"
    )
    if limit_int > 0:
        sql += f" LIMIT {limit_int}"

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, tuple(params)).fetchall()
        conn.close()
    except sqlite3.Error as e:
        return {"error": f"Read failed: {e}"}

    by_category: dict[str, list[dict]] = {}
    for r in rows:
        by_category.setdefault(r["category"], []).append({
            "id":          r["world_fact_id"],
            "fact":        r["fact_text"],
            "source_note": r["source_note"],
        })

    payload = {
        "count":       len(rows),
        "categories":  sorted(by_category.keys()),
        "by_category": by_category,
        "filters_applied": {
            "category": cat or None,
            "search":   needle or None,
            "limit":    limit_int or None,
        },
    }
    # Two summary policies based on whether the caller asked for everything
    # or for a single category. Unfiltered → categories_summary. Single
    # category → fact_text previews + per-category 25-row cap.
    summary_fn = (_summarize_world_facts_categories if not cat
                  else _summarize_world_facts_single_category)
    return _cap_response(
        payload,
        summary_fn=summary_fn,
        tool_name=(
            f"get_world_facts(category={cat or '<all>'}"
            f"{', search=' + needle if needle else ''}"
            f"{', limit=' + str(limit_int) if limit_int else ''})"
        ),
    )


# ══════════════════════════════════════════════════════════════════════════════
# DIRECT-EDIT HELPERS (shared by update_class_level / update_inventory_item /
# remove_inventory_item / update_npc_class / direct_db_edit)
# ══════════════════════════════════════════════════════════════════════════════

def _active_db_path() -> Path | None:
    """Return the absolute Path of the active campaign DB, or None."""
    rel = _active_campaign_rel()
    if not rel:
        return None
    p = (_ROOT / rel).resolve()
    return p if p.exists() else None


def _open_writable_active() -> sqlite3.Connection:
    """Open a writable connection to the active campaign DB. Raises on failure."""
    p = _active_db_path()
    if not p:
        raise RuntimeError("No active campaign DB resolvable from config.json.")
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> list[dict]:
    """Return PRAGMA table_info rows as dicts. Empty list if table unknown."""
    try:
        return [dict(r) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    except sqlite3.Error:
        return []


def _log_edit(
    conn: sqlite3.Connection,
    tool_name: str,
    table: str,
    row_id,
    changes: list[dict],
    note: str = "",
) -> None:
    """
    Append an edit-log entry to world_facts (category='edit_log').

    `changes` is a list of {"field": str, "old": jsonable, "new": jsonable}.
    Caller must be inside an open transaction — this does not commit.
    """
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    parts = [f"{c['field']}: {c['old']!r} -> {c['new']!r}" for c in changes]
    summary = f"[{ts}] {tool_name} on {table}#{row_id}: " + "; ".join(parts)
    if note:
        summary += f" (note: {note})"
    payload = json.dumps({
        "timestamp": ts,
        "tool":      tool_name,
        "table":     table,
        "row_id":    row_id,
        "changes":   changes,
        "note":      note,
    }, default=str)
    conn.execute(
        "INSERT INTO world_facts (campaign_id, category, fact_text, source_note) "
        "VALUES (1, 'edit_log', ?, ?)",
        (summary, payload),
    )


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: update_class_level
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def update_class_level(
    character_id: Annotated[
        int,
        "The character_id whose class row is being edited. PC is 1; NPCs "
        "have their own character_id values.",
    ],
    class_name: Annotated[
        str,
        "Existing class_name to target (e.g. 'Fighter', 'Magic-User'). Must "
        "match an existing row for this character_id.",
    ],
    new_level: Annotated[
        int,
        "New level. Pass -1 to leave unchanged.",
    ] = -1,
    new_xp: Annotated[
        int,
        "New XP value. Pass -1 to leave unchanged.",
    ] = -1,
    rename_to: Annotated[
        str,
        "If non-empty, rename the class (e.g. dual-class correction). "
        "Leave blank to keep the current class_name.",
    ] = "",
    reason: Annotated[
        str,
        "Short reason for the edit, written to the edit_log.",
    ] = "",
) -> dict:
    """
    Directly correct a row in class_levels (level / xp / class name).

    Use for data-correction or canon overrides when the regular game loop
    would not naturally produce the needed state. Every change is recorded
    in world_facts under category 'edit_log'.
    """
    try:
        conn = _open_writable_active()
    except RuntimeError as e:
        return {"error": str(e)}

    try:
        row = conn.execute(
            "SELECT class_level_id, class_name, level, xp FROM class_levels "
            "WHERE character_id = ? AND class_name = ?",
            (character_id, class_name),
        ).fetchone()
        if not row:
            return {
                "error": (
                    f"No class_levels row for character_id={character_id} "
                    f"with class_name={class_name!r}."
                ),
            }

        updates: list[tuple[str, object]] = []
        changes:  list[dict]              = []
        if new_level >= 0 and new_level != row["level"]:
            updates.append(("level", new_level))
            changes.append({"field": "level", "old": row["level"], "new": new_level})
        if new_xp >= 0 and new_xp != row["xp"]:
            updates.append(("xp", new_xp))
            changes.append({"field": "xp", "old": row["xp"], "new": new_xp})
        if rename_to and rename_to != row["class_name"]:
            updates.append(("class_name", rename_to))
            changes.append({"field": "class_name", "old": row["class_name"], "new": rename_to})

        if not updates:
            return {"ok": True, "unchanged": True, "row_id": row["class_level_id"]}

        set_clause = ", ".join(f"{k} = ?" for k, _ in updates)
        params     = [v for _, v in updates] + [row["class_level_id"]]
        with conn:
            conn.execute(
                f"UPDATE class_levels SET {set_clause} WHERE class_level_id = ?",
                params,
            )
            _log_edit(conn, "update_class_level", "class_levels",
                      row["class_level_id"], changes, reason)
        return {"ok": True, "row_id": row["class_level_id"], "changes": changes}
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: update_inventory_item
# ══════════════════════════════════════════════════════════════════════════════

# Fields that live on the inventory table itself.
_INVENTORY_EDITABLE = {
    "quantity", "equipped_flag", "slot", "notes",
    "character_id", "location_id", "treasury_id", "item_id",
}

# Fields that live on the items table — update_inventory_item routes these
# to the linked items row instead of the inventory row.
_ITEM_EDITABLE_VIA_INVENTORY = {
    "name", "item_type", "magic_flag", "value_gp",
    "damage_dice", "damage_bonus", "to_hit_bonus",
    "weapon_type", "armor_class_bonus",
    "item_notes",   # alias for items.notes (to disambiguate from inventory.notes)
}

_INVENTORY_INT_FIELDS = {
    "quantity", "equipped_flag", "character_id", "location_id",
    "treasury_id", "item_id",
}
_ITEM_INT_FIELDS = {
    "magic_flag", "value_gp", "damage_bonus", "to_hit_bonus",
    "armor_class_bonus",
}


@mcp.tool()
def update_inventory_item(
    inventory_id: Annotated[
        int,
        "inventory_id (primary key) of the record to update.",
    ],
    field: Annotated[
        str,
        "Column to update. Inventory fields: quantity, equipped_flag, slot, "
        "notes, character_id, location_id, treasury_id, item_id. Item fields "
        "(routed to the linked items row): name, item_type, magic_flag, "
        "value_gp, damage_dice, damage_bonus, to_hit_bonus, weapon_type, "
        "armor_class_bonus, item_notes.",
    ],
    new_value: Annotated[
        str,
        "New value as a string. Integer fields are parsed as ints; for NULL "
        "pass the literal string 'null'. For booleans (magic_flag, "
        "equipped_flag) use '1' / '0'. slot must be one of: mainhand, "
        "offhand, head, body, cloak, belt, boots, gloves, ring1, ring2, "
        "neck, back, or 'null' to unequip.",
    ],
    reason: Annotated[
        str,
        "Short reason for the edit, written to the edit_log.",
    ] = "",
) -> dict:
    """
    Update one field on a single inventory or item row.

    Field-routing: inventory-table fields update the inventory row directly;
    item-table fields update the linked items row (so a single tool serves
    both layers). To change multiple fields, call this tool once per field.
    The companion tool remove_inventory_item deletes an inventory row
    entirely. Every edit is recorded in world_facts under category
    'edit_log'.

    Slot semantics: when field='slot' and the new value is non-null, the
    invariant 'equipped_flag = 1 IFF slot IS NOT NULL' is enforced — the
    inventory row's equipped_flag is auto-synced. Setting slot='null'
    likewise clears equipped_flag. To re-slot an item with auto-vacate of
    the previous occupant, prefer the equip_item tool.
    """
    is_inv_field  = field in _INVENTORY_EDITABLE
    is_item_field = field in _ITEM_EDITABLE_VIA_INVENTORY
    if not (is_inv_field or is_item_field):
        return {
            "error": f"Field {field!r} is not editable via this tool.",
            "allowed_inventory_fields": sorted(_INVENTORY_EDITABLE),
            "allowed_item_fields":      sorted(_ITEM_EDITABLE_VIA_INVENTORY),
        }

    # Coerce the new_value to its native type.
    parsed: object
    if new_value.strip().lower() == "null":
        parsed = None
    elif field in _INVENTORY_INT_FIELDS or field in _ITEM_INT_FIELDS:
        try:
            parsed = int(new_value)
        except ValueError:
            return {"error": f"{field} requires an integer, got {new_value!r}."}
    else:
        parsed = new_value

    # Slot validation (inventory.slot only).
    if field == "slot" and parsed is not None:
        if parsed not in _INVENTORY_SLOTS:
            return {
                "error": f"slot must be one of {sorted(_INVENTORY_SLOTS)} or 'null'; got {new_value!r}.",
            }

    # weapon_type validation (items.weapon_type).
    if field == "weapon_type" and parsed is not None:
        if parsed not in _WEAPON_TYPES:
            return {
                "error": f"weapon_type must be one of {sorted(_WEAPON_TYPES)} or 'null'; got {new_value!r}.",
            }

    # Make sure the schema has the new columns before we touch them.
    _ensure_inventory_slot_column()
    _ensure_items_combat_columns()

    try:
        conn = _open_writable_active()
    except RuntimeError as e:
        return {"error": str(e)}

    try:
        row = conn.execute(
            "SELECT * FROM inventory WHERE inventory_id = ?",
            (inventory_id,),
        ).fetchone()
        if not row:
            return {"error": f"No inventory row with inventory_id={inventory_id}."}

        if is_inv_field:
            old = row[field]
            if old == parsed:
                return {"ok": True, "unchanged": True, "inventory_id": inventory_id}

            with conn:
                conn.execute(
                    f"UPDATE inventory SET {field} = ? WHERE inventory_id = ?",
                    (parsed, inventory_id),
                )
                # Sync the slot/equipped_flag invariant when either changes.
                if field == "slot":
                    conn.execute(
                        "UPDATE inventory SET equipped_flag = ? "
                        "WHERE inventory_id = ?",
                        (1 if parsed is not None else 0, inventory_id),
                    )
                _log_edit(conn, "update_inventory_item", "inventory",
                          inventory_id,
                          [{"field": field, "old": old, "new": parsed}],
                          reason)
            return {
                "ok":           True,
                "inventory_id": inventory_id,
                "table":        "inventory",
                "field":        field,
                "old":          old,
                "new":          parsed,
            }

        # ── Item-table routing ───────────────────────────────────────────────
        item_id = row["item_id"]
        # 'item_notes' is the param-level alias for items.notes (to keep it
        # disambiguated from inventory.notes at the tool boundary).
        sql_field = "notes" if field == "item_notes" else field

        item_row = conn.execute(
            "SELECT * FROM items WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        if not item_row:
            return {"error": f"Linked items row item_id={item_id} not found."}

        old = item_row[sql_field]
        if old == parsed:
            return {
                "ok":           True,
                "unchanged":    True,
                "inventory_id": inventory_id,
                "item_id":      item_id,
            }

        with conn:
            conn.execute(
                f"UPDATE items SET {sql_field} = ? WHERE item_id = ?",
                (parsed, item_id),
            )
            _log_edit(conn, "update_inventory_item", "items",
                      item_id,
                      [{"field": sql_field, "old": old, "new": parsed}],
                      reason)
        return {
            "ok":           True,
            "inventory_id": inventory_id,
            "item_id":      item_id,
            "table":        "items",
            "field":        sql_field,
            "old":          old,
            "new":          parsed,
        }
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: remove_inventory_item
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def remove_inventory_item(
    inventory_id: Annotated[
        int,
        "inventory_id of the record to delete.",
    ],
    reason: Annotated[
        str,
        "Why the record is being removed (written to edit_log). Strongly "
        "recommended so the deletion can be audited later.",
    ] = "",
) -> dict:
    """
    Delete a single inventory row entirely.

    Intended for fixing duplicates or spurious entries. The referenced
    items row in `items` is left intact so other inventory rows or loot
    records pointing at it keep working. The deleted row's prior
    contents are captured in the edit_log so it can be reconstructed.
    """
    try:
        conn = _open_writable_active()
    except RuntimeError as e:
        return {"error": str(e)}

    try:
        row = conn.execute(
            "SELECT * FROM inventory WHERE inventory_id = ?",
            (inventory_id,),
        ).fetchone()
        if not row:
            return {"error": f"No inventory row with inventory_id={inventory_id}."}

        snapshot = dict(row)
        changes  = [{"field": "__row__", "old": snapshot, "new": None}]
        with conn:
            conn.execute("DELETE FROM inventory WHERE inventory_id = ?", (inventory_id,))
            _log_edit(conn, "remove_inventory_item", "inventory",
                      inventory_id, changes, reason)
        return {"ok": True, "inventory_id": inventory_id, "deleted_row": snapshot}
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: remove_livestock
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def remove_livestock(
    livestock_id: Annotated[
        int,
        "livestock_id of the row to delete.",
    ],
    reason: Annotated[
        str,
        "Why the row is being removed (written to edit_log). Strongly "
        "recommended so the deletion can be audited later — e.g. 'sold "
        "30 sheep to Renna smokehouse', 'wolf attack at Olvert',  "
        "'consolidated into single dairy herd row'.",
    ] = "",
) -> dict:
    """
    Delete a single livestock row entirely.

    Intended for spinning down stock that's been sold off, slaughtered,
    or consolidated into another row. For partial losses prefer
    direct_db_edit on the count column so the row history stays intact.

    The deleted row's prior contents are captured in the edit_log
    (animal_type, count, location_id, notes) so it can be reconstructed
    from the audit trail if needed.
    """
    try:
        conn = _open_writable_active()
    except RuntimeError as e:
        return {"error": str(e)}

    try:
        row = conn.execute(
            "SELECT * FROM livestock WHERE livestock_id = ?",
            (livestock_id,),
        ).fetchone()
        if not row:
            return {"error": f"No livestock row with livestock_id={livestock_id}."}

        snapshot = dict(row)
        changes  = [{"field": "__row__", "old": snapshot, "new": None}]
        with conn:
            conn.execute(
                "DELETE FROM livestock WHERE livestock_id = ?",
                (livestock_id,),
            )
            _log_edit(conn, "remove_livestock", "livestock",
                      livestock_id, changes, reason)
        return {
            "ok":           True,
            "livestock_id": livestock_id,
            "deleted_row":  snapshot,
        }
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: update_npc_class
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def update_npc_class(
    npc_character_id: Annotated[
        int,
        "character_id of the NPC (from the characters table, where "
        "character_type = 'npc'). Rejects PC entries (character_type='pc').",
    ],
    new_class: Annotated[
        str,
        "New class_name. If the NPC has no class_levels row, one is inserted. "
        "Leave blank to leave class unchanged.",
    ] = "",
    new_level: Annotated[
        int,
        "New level. Pass -1 to leave unchanged (or let an inserted row use 1).",
    ] = -1,
    new_notes: Annotated[
        str,
        "Replacement value for characters.notes. IGNORED unless "
        "preserve_notes is explicitly set to False. Default behavior is "
        "to preserve existing rich narrative notes. To actually overwrite, "
        "pass new_notes='your text' AND preserve_notes=False.",
    ] = "",
    preserve_notes: Annotated[
        bool,
        "Safety flag. True (default) — characters.notes is NEVER touched, "
        "even if new_notes is passed. False — new_notes (if non-empty) is "
        "written to characters.notes. The explicit-opt-in pattern prevents "
        "rich narrative notes from being silently clobbered by class/level "
        "edits.",
    ] = True,
    reason: Annotated[
        str,
        "Short reason for the edit, written to the edit_log.",
    ] = "",
) -> dict:
    """
    Directly correct an NPC's class and/or level (and optionally notes).

    Operates on a single class_levels row per NPC. If the NPC has multiple
    class rows (rare), the first (lowest class_level_id) is edited. Every
    change is recorded in world_facts under category 'edit_log'.

    Notes safety:
      By default this tool will NOT touch characters.notes — that field
      holds rich narrative state (relationships, life-debts, suspicions,
      personal effects) that must never be silently replaced by a brief
      class/level summary. To intentionally overwrite notes through this
      tool, the caller must pass BOTH new_notes='replacement text' AND
      preserve_notes=False. Otherwise notes is preserved verbatim.

      For routine notes updates use update_npc, which is the canonical
      path for narrative state.
    """
    try:
        conn = _open_writable_active()
    except RuntimeError as e:
        return {"error": str(e)}

    try:
        pc_row = conn.execute(
            "SELECT character_id, character_type, notes, name "
            "FROM characters WHERE character_id = ?",
            (npc_character_id,),
        ).fetchone()
        if not pc_row:
            return {"error": f"No characters row with character_id={npc_character_id}."}
        if (pc_row["character_type"] or "").lower() == "pc":
            return {"error": "Refusing to edit a PC via update_npc_class. Use update_class_level instead."}

        changes: list[dict] = []

        # ── class / level ────────────────────────────────────────────────
        class_row = conn.execute(
            "SELECT class_level_id, class_name, level FROM class_levels "
            "WHERE character_id = ? ORDER BY class_level_id LIMIT 1",
            (npc_character_id,),
        ).fetchone()

        with conn:
            if new_class or new_level >= 0:
                if class_row:
                    cls_updates: list[tuple[str, object]] = []
                    if new_class and new_class != class_row["class_name"]:
                        cls_updates.append(("class_name", new_class))
                        changes.append({"field": "class_levels.class_name",
                                        "old": class_row["class_name"], "new": new_class})
                    if new_level >= 0 and new_level != class_row["level"]:
                        cls_updates.append(("level", new_level))
                        changes.append({"field": "class_levels.level",
                                        "old": class_row["level"], "new": new_level})
                    if cls_updates:
                        set_clause = ", ".join(f"{k} = ?" for k, _ in cls_updates)
                        params     = [v for _, v in cls_updates] + [class_row["class_level_id"]]
                        conn.execute(
                            f"UPDATE class_levels SET {set_clause} WHERE class_level_id = ?",
                            params,
                        )
                elif new_class:
                    lvl = new_level if new_level >= 0 else 1
                    cur = conn.execute(
                        "INSERT INTO class_levels (character_id, class_name, level, xp) "
                        "VALUES (?, ?, ?, 0)",
                        (npc_character_id, new_class, lvl),
                    )
                    changes.append({"field": "class_levels (inserted)",
                                    "old": None,
                                    "new": {"class_name": new_class, "level": lvl,
                                            "class_level_id": cur.lastrowid}})

            # ── notes ────────────────────────────────────────────────────
            # Default behavior is preserve_notes=True — characters.notes is
            # NEVER touched, even if new_notes is passed. The caller must
            # explicitly pass preserve_notes=False to opt into a notes write.
            notes_skipped = False
            if new_notes and preserve_notes:
                notes_skipped = True  # Surface to caller in result.
            elif new_notes and not preserve_notes:
                want = None if new_notes.strip().lower() == "null" else new_notes
                if want != pc_row["notes"]:
                    conn.execute(
                        "UPDATE characters SET notes = ? WHERE character_id = ?",
                        (want, npc_character_id),
                    )
                    changes.append({"field": "characters.notes",
                                    "old": pc_row["notes"], "new": want})

            if not changes:
                result = {"ok": True, "unchanged": True,
                          "npc_character_id": npc_character_id}
                if notes_skipped:
                    result["notes_preserved"] = True
                    result["hint"] = ("new_notes was provided but preserve_notes "
                                      "is True; pass preserve_notes=False to "
                                      "actually overwrite. Or use update_npc.")
                return result

            _log_edit(conn, "update_npc_class", "class_levels",
                      npc_character_id, changes, reason)

        result = {"ok": True, "npc_character_id": npc_character_id,
                  "name": pc_row["name"], "changes": changes,
                  "notes_preserved": preserve_notes}
        if notes_skipped:
            result["hint"] = ("new_notes was provided but preserve_notes is "
                              "True; pass preserve_notes=False to overwrite, "
                              "or call update_npc for narrative updates.")
        return result
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: direct_db_edit
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def direct_db_edit(
    table: Annotated[
        str,
        "Target table name. Must be a real table in the active DB; system "
        "tables (sqlite_*) are rejected.",
    ],
    id_field: Annotated[
        str,
        "Primary-key column name for the target table (e.g. 'character_id', "
        "'item_id', 'class_level_id'). Must be a real column.",
    ],
    id_value: Annotated[
        int,
        "Value of id_field identifying the exact row to update.",
    ],
    updates_json: Annotated[
        str,
        "JSON-encoded object mapping column names to new values "
        "(e.g. '{\"level\": 8, \"xp\": 95000}'). String/int/float/null "
        "supported. Every key must be a real column. The primary-key "
        "column itself may not be changed.",
    ],
    reason: Annotated[
        str,
        "Short reason for the edit, written to the edit_log. Strongly "
        "recommended for any canon override.",
    ] = "",
    confirm: Annotated[
        bool,
        "Two-stage write protection. When False (default), the tool "
        "returns a PREVIEW — the current row contents plus the proposed "
        "diff — and DOES NOT WRITE. The caller must verify the row is "
        "the intended target, then re-call with confirm=True to commit. "
        "Prevents accidental overwrites caused by passing the wrong "
        "id_value (the bug that produced the spellbook-notes mis-attach "
        "incident). Pass confirm=True directly when you're certain — "
        "e.g. from a chained call where the id was just looked up.",
    ] = False,
) -> dict:
    """
    Override any single row in the active campaign DB.

    Swiss-army tool for data correction when no specific tool fits.

    Validation runs in this order:
      1. Table exists and is not a system table.
      2. id_field is a column of that table.
      3. Every key in updates_json is a column of that table.
      4. The primary-key column is not among the keys (no row-renumbering).
      5. A single row with id_field = id_value exists.

    Two-stage write (confirm gate):
      - confirm=False (default): returns {"preview": True, "current_row":
        {...full row...}, "proposed_changes": [{field, old, new}, ...],
        "note": "..."}. NO WRITE happens. Caller verifies the row is
        what they intended, then re-calls with confirm=True.
      - confirm=True: same validation, then commits. Returns
        {"ok": True, "changes": [...]}.

    The update is wrapped in a transaction together with the edit_log
    insert, so partial writes cannot occur. Every old/new value pair is
    recorded in world_facts under category 'edit_log'.
    """
    # Parse updates first (cheapest failure).
    try:
        updates = json.loads(updates_json)
    except Exception as e:
        return {"error": f"updates_json is not valid JSON: {e}"}
    if not isinstance(updates, dict) or not updates:
        return {"error": "updates_json must decode to a non-empty object."}

    if not table or table.startswith("sqlite_"):
        return {"error": f"Refusing to edit table {table!r}."}

    try:
        conn = _open_writable_active()
    except RuntimeError as e:
        return {"error": str(e)}

    try:
        # ── Validate table exists ────────────────────────────────────────
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
            (table,),
        ).fetchone()
        if not exists:
            return {"error": f"Table {table!r} does not exist."}

        # ── Validate columns and primary key ─────────────────────────────
        cols_info = _table_columns(conn, table)
        col_names = {c["name"] for c in cols_info}
        pk_cols   = {c["name"] for c in cols_info if c["pk"]}
        if id_field not in col_names:
            return {"error": f"{id_field!r} is not a column of {table!r}.",
                    "columns": sorted(col_names)}
        unknown = [k for k in updates if k not in col_names]
        if unknown:
            return {"error": f"Unknown column(s) for {table!r}: {unknown}",
                    "columns": sorted(col_names)}
        forbidden = [k for k in updates if k in pk_cols]
        if forbidden:
            return {"error": f"Primary-key columns cannot be updated: {forbidden}."}

        # ── Load existing row ────────────────────────────────────────────
        row = conn.execute(
            f"SELECT * FROM {table} WHERE {id_field} = ?",
            (id_value,),
        ).fetchone()
        if not row:
            return {"error": f"No row in {table} with {id_field}={id_value}."}

        changes = []
        applied = {}
        for k, v in updates.items():
            old = row[k]
            if old != v:
                changes.append({"field": k, "old": old, "new": v})
                applied[k] = v

        if not applied:
            return {"ok": True, "unchanged": True, "table": table, "id": id_value}

        # ── Confirm gate: preview-and-stop unless confirm=True ───────────
        if not confirm:
            return {
                "preview":          True,
                "table":            table,
                "id_field":         id_field,
                "id":               id_value,
                "current_row":      dict(row),
                "proposed_changes": changes,
                "note": (
                    "Preview only — NO WRITE happened. Verify "
                    f"{id_field}={id_value} is the row you meant to edit "
                    "(the current_row contents are above), then re-call "
                    "with confirm=True to commit. This guard exists "
                    "because earlier mis-attach bugs (e.g. spellbook "
                    "notes ending up on a different inventory_id) were "
                    "all caused by writing to the wrong id without "
                    "verifying first."
                ),
            }

        # confirm=True: commit.
        set_clause = ", ".join(f"{k} = ?" for k in applied)
        params     = list(applied.values()) + [id_value]
        with conn:
            conn.execute(
                f"UPDATE {table} SET {set_clause} WHERE {id_field} = ?",
                params,
            )
            _log_edit(conn, "direct_db_edit", table, id_value, changes, reason)

        return {
            "ok":       True,
            "table":    table,
            "id_field": id_field,
            "id":       id_value,
            "changes":  changes,
        }
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: insert_row
# ══════════════════════════════════════════════════════════════════════════════
#
# Generic INSERT primitive — companion to direct_db_edit. Lets callers
# populate any table without waiting on a bespoke add_* tool. Mirrors
# direct_db_edit's validation harness (real table, real columns, no
# system tables) and writes a `__row__` snapshot to edit_log so
# restore_from_edit_log can DELETE the row to undo.
#
# Tables with dedicated, semantically-rich add_* helpers (add_npc,
# add_location, add_item, add_troop_group, add_livestock,
# add_treasury_account, add_construction_project) should still go
# through those tools — they handle FK resolution, validation, and
# auto-vacate behaviors that this primitive doesn't replicate. Use
# insert_row for new tables, one-off backfills, or canon corrections
# that don't fit any existing add_* shape.
# ══════════════════════════════════════════════════════════════════════════════

# Tables insert_row refuses to touch — written by dedicated audit/
# history paths or by other primitives that maintain invariants this
# tool can't see (the inventory CHECK constraint is the obvious case
# but DOES go through here since it's a real, useful table — caller
# sees the SQLite IntegrityError if they violate it).
_INSERT_FORBIDDEN_TABLES = {
    "world_facts",         # use update_world_fact
    "ai_turns",            # use save_turn
    "current_scene_state", # use save_turn
    "domain_income_expenses",
    "sqlite_master", "sqlite_sequence",
}


@mcp.tool()
def insert_row(
    table: Annotated[
        str,
        "Target table name. Must be a real, non-system table. Tables "
        "owned by dedicated audit/history paths (world_facts, ai_turns, "
        "current_scene_state, domain_income_expenses, sqlite_*) are "
        "refused — use the matching dedicated tool instead.",
    ],
    fields_json: Annotated[
        str,
        "JSON-encoded object mapping column names to values, e.g. "
        '\'{"name": "Goat", "count": 4, "location_id": 13}\'. Every key '
        "must be a real column of the target table. Pass the primary "
        "key explicitly to control the assigned id; omit it to let the "
        "DB auto-assign and read it back from `inserted_pk` in the "
        "response. Unknown columns are rejected up front (with the full "
        "column list returned) so a typo doesn't reach the INSERT.",
    ],
    reason: Annotated[
        str,
        "Short reason for the insert, written to the edit_log. Strongly "
        "recommended for any canon backfill or correction so the "
        "operation can be unwound by restore_from_edit_log later.",
    ] = "",
) -> dict:
    """
    Generic INSERT into any user table.

    Companion to direct_db_edit (UPDATE) and remove_inventory_item /
    remove_livestock (DELETE). Closes the CRUD primitive set so a new
    table no longer requires a bespoke add_* tool to populate.

    Validation runs before the write:
      1. fields_json parses to a non-empty object.
      2. table exists, is not a system table, is not in the
         _INSERT_FORBIDDEN_TABLES set.
      3. Every key in fields_json is a real column of that table.

    Notably NOT enforced (intentionally let through to the DB):
      - NOT NULL constraints — surfaces as a SQLite IntegrityError so
        the caller learns which column they missed.
      - CHECK constraints (e.g. inventory's exactly-one-FK rule) —
        same.
      - FK references — same.
    All such failures roll back the transaction and return
    {"error": "..."} with the SQLite message intact.

    The successful insert writes an edit_log entry of shape
    [{"field":"__row__", "old":None, "new":{...inserted_fields...}}]
    so restore_from_edit_log can DELETE the row to undo the operation.

    Returns:
      {"ok": True, "table": ..., "inserted_pk": <new_id>,
       "inserted_fields": {...}, "edit_log_id": <log id>}
      {"error": "..."} on any validation or constraint failure.
    """
    # Parse fields_json first (cheapest failure).
    try:
        fields = json.loads(fields_json)
    except Exception as e:
        return {"error": f"fields_json is not valid JSON: {e}"}
    if not isinstance(fields, dict) or not fields:
        return {"error": "fields_json must decode to a non-empty object."}

    if not table or table.startswith("sqlite_"):
        return {"error": f"Refusing to insert into {table!r}."}
    if table in _INSERT_FORBIDDEN_TABLES:
        return {
            "error": f"Refusing to insert into {table!r}: it is owned "
                     "by a dedicated audit/history tool. Use the "
                     "matching dedicated tool instead.",
            "forbidden_tables": sorted(_INSERT_FORBIDDEN_TABLES),
        }

    try:
        conn = _open_writable_active()
    except RuntimeError as e:
        return {"error": str(e)}

    try:
        # Validate table exists.
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
            (table,),
        ).fetchone()
        if not exists:
            return {"error": f"Table {table!r} does not exist."}

        # Validate every field is a real column.
        cols_info = _table_columns(conn, table)
        col_names = {c["name"] for c in cols_info}
        unknown   = [k for k in fields if k not in col_names]
        if unknown:
            return {
                "error":   f"Unknown column(s) for {table!r}: {unknown}",
                "columns": sorted(col_names),
            }

        # Build the INSERT.
        ins_cols   = list(fields.keys())
        col_list   = ", ".join(ins_cols)
        placeholders = ", ".join("?" for _ in ins_cols)
        values     = [fields[c] for c in ins_cols]

        try:
            with conn:
                cur = conn.execute(
                    f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})",
                    values,
                )
                # Resolve the new PK so callers can chain operations.
                pk_col = _resolve_pk_column(conn, table)
                inserted_pk = (
                    fields[pk_col] if (pk_col and pk_col in fields)
                    else cur.lastrowid
                )
                # Snapshot includes the resolved PK so a subsequent
                # restore can re-insert at the same id.
                snapshot = dict(fields)
                if pk_col and pk_col not in snapshot:
                    snapshot[pk_col] = inserted_pk
                _log_edit(conn, "insert_row", table, inserted_pk, [{
                    "field": "__row__",
                    "old":   None,
                    "new":   snapshot,
                }], reason)
                # Recover the just-written edit_log id for the response.
                edit_log_id = conn.execute(
                    "SELECT MAX(world_fact_id) FROM world_facts "
                    "WHERE category = 'edit_log'"
                ).fetchone()[0]
        except sqlite3.IntegrityError as e:
            return {
                "error": f"INSERT failed: {e}",
                "hint": (
                    "Likely a NOT NULL / CHECK / FOREIGN KEY constraint. "
                    "Pass all required columns and confirm referenced "
                    "FKs exist. Inspect the table schema with "
                    "PRAGMA table_info or use a dedicated add_* tool "
                    "if one exists."
                ),
            }
        except sqlite3.Error as e:
            return {"error": f"INSERT failed: {e}"}

        return {
            "ok":              True,
            "table":           table,
            "inserted_pk":     inserted_pk,
            "inserted_fields": snapshot,
            "edit_log_id":     edit_log_id,
        }
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: restore_from_edit_log
# ══════════════════════════════════════════════════════════════════════════════
#
# Inverse of direct_db_edit / remove_inventory_item / remove_livestock /
# update_inventory_item / update_class_level / any tool that calls
# _log_edit. Reads the original edit_log entry's source_note JSON, figures
# out whether it was a DELETE (single change, field='__row__') or an
# UPDATE (per-field changes), and re-applies the prior state. The
# restoration itself is recorded in edit_log so it can be unwound, and
# every step happens inside a single transaction so a partial failure
# can't leave the DB inconsistent.
# ══════════════════════════════════════════════════════════════════════════════

# Tables that restore_from_edit_log refuses to touch — would cascade in
# unpredictable ways or undo schema/audit infrastructure itself.
_RESTORE_FORBIDDEN_TABLES = {
    "world_facts",        # would let you re-write the audit trail
    "ai_turns",           # historical record, never restore
    "current_scene_state",
    "domain_income_expenses",
    "sqlite_master", "sqlite_sequence",
}


def _resolve_pk_column(conn: sqlite3.Connection, table: str) -> str | None:
    """Return the single primary-key column name, or None if not exactly one."""
    cols = _table_columns(conn, table)
    pks = [c["name"] for c in cols if c.get("pk")]
    if len(pks) == 1:
        return pks[0]
    return None


@mcp.tool()
def restore_from_edit_log(
    edit_log_id: Annotated[
        int,
        "world_fact_id of the edit_log entry to invert. Use "
        "get_world_facts(category='edit_log') or query world_facts "
        "directly to find candidates. The entry's source_note JSON is "
        "what gets parsed.",
    ],
    reason: Annotated[
        str,
        "Why the row is being restored (written to the new edit_log "
        "entry). Strongly recommended — restoration is the kind of "
        "operation that always benefits from explanation.",
    ] = "",
) -> dict:
    """
    Restore a row from a prior edit_log entry.

    Promotes the edit_log from a forensics-only artifact into a usable
    safety net. Routes by the structure of the original entry's
    `changes` array:

      DELETE-style — exactly one change with field='__row__' and
      new=None. Re-INSERTs the snapshot. Tries the original primary
      key first; on PK collision falls back to inserting without the
      PK column (the DB picks a new id, returned as `restored_pk` so
      the caller can update any references).

      UPDATE-style — per-field changes with `old` / `new` values.
      Reverts each field to its `old` value via UPDATE on the original
      row_id. Errors if the row no longer exists (an UPDATE-restore
      can't recreate a row that was subsequently deleted — chain by
      restoring the DELETE first).

    The restoration writes its own edit_log entry (tool name
    'restore_from_edit_log') with the inverse changes recorded, so the
    restore itself can be unwound by another restore_from_edit_log
    call if needed.

    Refused tables (would corrupt audit/history): world_facts,
    ai_turns, current_scene_state, domain_income_expenses, sqlite_*.

    Returns one of:
      {"ok": True, "mode": "update_revert", "table": ..., "row_id": ..., "fields_restored": [...]}
      {"ok": True, "mode": "delete_reinsert", "table": ..., "row_id": ..., "restored_pk": ...}
      {"ok": True, "unchanged": True, ...}   # row already matches snapshot
      {"error": "..."}                        # any failure, transactional rollback
    """
    try:
        conn = _open_writable_active()
    except RuntimeError as e:
        return {"error": str(e)}

    try:
        # ── Read the original edit_log entry ────────────────────────────────
        log_row = conn.execute(
            "SELECT world_fact_id, category, source_note "
            "FROM world_facts WHERE world_fact_id = ?",
            (edit_log_id,),
        ).fetchone()
        if not log_row:
            return {"error": f"No world_facts row with id={edit_log_id}."}
        if log_row["category"] != "edit_log":
            return {
                "error": f"world_fact_id={edit_log_id} is in category "
                         f"{log_row['category']!r}, not 'edit_log'.",
            }
        source_note = log_row["source_note"]
        if not source_note:
            return {
                "error": (
                    f"edit_log entry {edit_log_id} has no source_note JSON "
                    "— probably a pre-Phase-15 entry written by a tool that "
                    "didn't save the structured payload. Cannot restore."
                ),
            }
        try:
            payload = json.loads(source_note)
        except (json.JSONDecodeError, TypeError) as e:
            return {
                "error": f"edit_log {edit_log_id} source_note is not valid "
                         f"JSON: {e}",
            }

        table     = payload.get("table")
        row_id    = payload.get("row_id")
        changes   = payload.get("changes") or []
        prior_tool = payload.get("tool", "(unknown)")
        if not table:
            return {"error": f"edit_log {edit_log_id} has no table field."}
        if table.startswith("sqlite_") or table in _RESTORE_FORBIDDEN_TABLES:
            return {
                "error": f"Refusing to restore into table {table!r}: in the "
                         "forbidden set (would corrupt audit/history).",
                "forbidden_tables": sorted(_RESTORE_FORBIDDEN_TABLES),
            }

        # ── Validate table still exists ─────────────────────────────────────
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
            (table,),
        ).fetchone()
        if not exists:
            return {"error": f"Table {table!r} no longer exists."}

        pk_col = _resolve_pk_column(conn, table)
        if not pk_col:
            return {
                "error": f"Table {table!r} has no single primary-key column "
                         "— restore needs an unambiguous PK to target.",
            }

        col_names = {c["name"] for c in _table_columns(conn, table)}

        # ── Branch: DELETE-style snapshot vs INSERT-style snapshot vs
        #            UPDATE-style per-field changes ─────────────────────────
        # DELETE-style:  one change, field='__row__', new=None, old=snapshot
        #                (written by remove_inventory_item / remove_livestock /
        #                 a remove via direct_db_edit). Restore re-INSERTs.
        # INSERT-style:  one change, field='__row__', old=None, new=snapshot
        #                (written by insert_row). Restore DELETEs the row.
        # UPDATE-style:  per-field changes with old/new. Restore reverts.
        is_row_snapshot = (
            len(changes) == 1
            and isinstance(changes[0], dict)
            and changes[0].get("field") == "__row__"
        )
        is_delete = (
            is_row_snapshot
            and changes[0].get("new") is None
            and isinstance(changes[0].get("old"), dict)
        )
        is_insert = (
            is_row_snapshot
            and changes[0].get("old") is None
            and isinstance(changes[0].get("new"), dict)
        )

        if is_delete:
            return _restore_delete(
                conn, table, pk_col, row_id, changes[0]["old"],
                col_names, edit_log_id, prior_tool, reason,
            )
        elif is_insert:
            return _restore_insert_undo(
                conn, table, pk_col, row_id, changes[0]["new"],
                edit_log_id, prior_tool, reason,
            )
        else:
            return _restore_update(
                conn, table, pk_col, row_id, changes,
                col_names, edit_log_id, prior_tool, reason,
            )
    finally:
        conn.close()


def _restore_delete(
    conn:        sqlite3.Connection,
    table:       str,
    pk_col:      str,
    row_id,
    snapshot:    dict,
    col_names:   set,
    edit_log_id: int,
    prior_tool:  str,
    reason:      str,
) -> dict:
    """Re-INSERT a row from a delete-style snapshot."""
    # Drop snapshot keys that aren't real columns (schema drift defense).
    insert_cols = [c for c in snapshot.keys() if c in col_names]
    if not insert_cols:
        return {
            "error": f"Snapshot has no columns matching {table!r}'s current "
                     "schema. The table may have been recreated.",
        }

    # Check if a row with this PK already exists.
    existing = conn.execute(
        f"SELECT * FROM {table} WHERE {pk_col} = ?",
        (row_id,),
    ).fetchone()

    if existing:
        # Compare values — if everything matches, no-op.
        existing_d = dict(existing)
        if all(existing_d.get(c) == snapshot.get(c) for c in insert_cols):
            return {
                "ok":          True,
                "unchanged":   True,
                "mode":        "delete_reinsert",
                "table":       table,
                "row_id":      row_id,
                "note":        "Row already exists with snapshot values.",
            }
        # PK collision with a different row — re-insert without PK.
        cols_for_insert = [c for c in insert_cols if c != pk_col]
        placeholders    = ", ".join("?" for _ in cols_for_insert)
        col_list        = ", ".join(cols_for_insert)
        values          = [snapshot[c] for c in cols_for_insert]
        try:
            with conn:
                cur = conn.execute(
                    f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})",
                    values,
                )
                new_pk = cur.lastrowid
                _log_edit(conn, "restore_from_edit_log", table, new_pk, [{
                    "field":   "__row__",
                    "old":     None,
                    "new":     dict(snapshot, **{pk_col: new_pk}),
                }], (
                    f"Restored from edit_log#{edit_log_id} (originally "
                    f"{prior_tool}); PK collision on {pk_col}={row_id} "
                    f"forced new PK {pk_col}={new_pk}. {reason}"
                ).strip())
        except sqlite3.Error as e:
            return {"error": f"INSERT failed: {e}"}
        return {
            "ok":               True,
            "mode":             "delete_reinsert",
            "table":            table,
            "original_row_id":  row_id,
            "restored_pk":      new_pk,
            "pk_collision":     True,
            "note": (
                f"Original {pk_col}={row_id} was occupied by a different "
                f"row; restored as {pk_col}={new_pk} instead. References "
                f"to the old PK in other tables will need fix-up."
            ),
        }

    # Clean re-insert at the original PK.
    placeholders = ", ".join("?" for _ in insert_cols)
    col_list     = ", ".join(insert_cols)
    values       = [snapshot[c] for c in insert_cols]
    try:
        with conn:
            conn.execute(
                f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})",
                values,
            )
            _log_edit(conn, "restore_from_edit_log", table, row_id, [{
                "field": "__row__",
                "old":   None,
                "new":   snapshot,
            }], (
                f"Restored from edit_log#{edit_log_id} (originally "
                f"{prior_tool}). {reason}"
            ).strip())
    except sqlite3.Error as e:
        return {"error": f"INSERT failed: {e}"}
    return {
        "ok":            True,
        "mode":          "delete_reinsert",
        "table":         table,
        "row_id":        row_id,
        "restored_pk":   row_id,
        "pk_collision":  False,
        "restored_row":  snapshot,
    }


def _restore_insert_undo(
    conn:        sqlite3.Connection,
    table:       str,
    pk_col:      str,
    row_id,
    snapshot:    dict,
    edit_log_id: int,
    prior_tool:  str,
    reason:      str,
) -> dict:
    """
    Undo an insert_row entry by DELETing the row at the original PK.

    Idempotent: if the row is already gone, returns unchanged. Verifies
    the current row's columns still match the snapshot before deleting
    (defensive — refuses to delete a row that's been edited since the
    original insert, since that would lose data the snapshot doesn't
    have a copy of).
    """
    existing = conn.execute(
        f"SELECT * FROM {table} WHERE {pk_col} = ?",
        (row_id,),
    ).fetchone()

    if not existing:
        return {
            "ok":         True,
            "unchanged":  True,
            "mode":       "insert_undo",
            "table":      table,
            "row_id":     row_id,
            "note":       "Row already absent — insert was previously undone or the row was deleted by another path.",
        }

    existing_d = dict(existing)
    drift_fields = []
    for col, val in snapshot.items():
        if col not in existing_d:
            continue   # column was dropped from the schema, skip
        if existing_d.get(col) != val:
            drift_fields.append({
                "field":     col,
                "snapshot":  val,
                "current":   existing_d.get(col),
            })
    if drift_fields:
        return {
            "error": (
                f"Refusing to undo insert: row {pk_col}={row_id} in "
                f"{table!r} has drifted from the original snapshot. "
                "Inspect the differences (in `drift`) and either revert "
                "the intervening edits first or delete the row "
                "explicitly via remove_inventory_item / remove_livestock / "
                "direct SQL if you accept the data loss."
            ),
            "drift": drift_fields,
        }

    try:
        with conn:
            conn.execute(
                f"DELETE FROM {table} WHERE {pk_col} = ?",
                (row_id,),
            )
            _log_edit(conn, "restore_from_edit_log", table, row_id, [{
                "field": "__row__",
                "old":   existing_d,
                "new":   None,
            }], (
                f"Undid insert_row from edit_log#{edit_log_id} "
                f"(originally {prior_tool}). {reason}"
            ).strip())
    except sqlite3.Error as e:
        return {"error": f"DELETE failed: {e}"}

    return {
        "ok":         True,
        "mode":       "insert_undo",
        "table":      table,
        "row_id":     row_id,
        "deleted_row": existing_d,
    }


def _restore_update(
    conn:        sqlite3.Connection,
    table:       str,
    pk_col:      str,
    row_id,
    changes:     list,
    col_names:   set,
    edit_log_id: int,
    prior_tool:  str,
    reason:      str,
) -> dict:
    """Revert per-field changes by writing the `old` values back to the row."""
    existing = conn.execute(
        f"SELECT * FROM {table} WHERE {pk_col} = ?",
        (row_id,),
    ).fetchone()
    if not existing:
        return {
            "error": (
                f"Row {pk_col}={row_id} no longer exists in {table!r} — "
                "cannot revert an update on a deleted row. If the row was "
                "later deleted, restore the DELETE-style edit_log entry "
                "first, then chain into this UPDATE restore."
            ),
        }

    # Filter to changes that actually have a writable column + old value.
    effective: list[dict] = []
    skipped:   list[dict] = []
    for c in changes:
        if not isinstance(c, dict):
            continue
        field = c.get("field")
        if field not in col_names:
            skipped.append({"field": field, "reason": "not a column"})
            continue
        if field == pk_col:
            skipped.append({"field": field, "reason": "primary key"})
            continue
        if "old" not in c:
            skipped.append({"field": field, "reason": "no 'old' value"})
            continue
        effective.append(c)

    if not effective:
        return {
            "error":   "No revertible field changes in this edit_log entry.",
            "skipped": skipped,
        }

    # Inverse changes for the new edit_log entry: what we're writing now
    # is `old` becoming `new` from the restoration's point of view.
    inverse_changes = []
    actual_writes  = []
    for c in effective:
        current = existing[c["field"]]
        if current == c["old"]:
            # Already at the old value — skip without a write but record it
            # in inverse for completeness.
            continue
        inverse_changes.append({
            "field": c["field"],
            "old":   current,        # the just-now value pre-restore
            "new":   c["old"],       # the restored value
        })
        actual_writes.append(c)

    if not actual_writes:
        return {
            "ok":          True,
            "unchanged":   True,
            "mode":        "update_revert",
            "table":       table,
            "row_id":      row_id,
            "note":        "Row already at the snapshot's prior values.",
        }

    set_clause = ", ".join(f"{c['field']} = ?" for c in actual_writes)
    params     = [c["old"] for c in actual_writes] + [row_id]
    try:
        with conn:
            conn.execute(
                f"UPDATE {table} SET {set_clause} WHERE {pk_col} = ?",
                params,
            )
            _log_edit(conn, "restore_from_edit_log", table, row_id,
                      inverse_changes, (
                          f"Restored from edit_log#{edit_log_id} (originally "
                          f"{prior_tool}). {reason}"
                      ).strip())
    except sqlite3.Error as e:
        return {"error": f"UPDATE failed: {e}"}

    return {
        "ok":              True,
        "mode":            "update_revert",
        "table":           table,
        "row_id":          row_id,
        "fields_restored": [c["field"] for c in actual_writes],
        "skipped":         skipped,
        "changes":         inverse_changes,
    }


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: equip_item
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def equip_item(
    character_target: Annotated[
        str,
        "Character to equip — name (case-insensitive prefix match) or "
        "numeric character_id. Pass an exact id when names are ambiguous.",
    ],
    item_name_or_id: Annotated[
        str,
        "Item to equip — partial item name (within this character's "
        "inventory) or numeric inventory_id. Names are matched case-"
        "insensitively as a prefix; ambiguous matches return an error "
        "listing the candidates so the caller can pass an exact "
        "inventory_id.",
    ],
    slot: Annotated[
        str,
        "Target slot — one of: mainhand, offhand, head, body, cloak, belt, "
        "boots, gloves, ring1, ring2, neck, back. Pass empty string or "
        "'null' to unequip (set slot=NULL).",
    ],
) -> dict:
    """
    Set a character's inventory item into a specific equipment slot.

    Auto-vacates whatever previously held that slot for the character —
    the displaced item drops back to slot=NULL (stowed) but stays in the
    inventory. Pass slot='' or 'null' to simply unequip the item.

    Returns a dict with:
      previously_unequipped — array of items that were vacated (0 or 1)
      previous_slot         — the slot the equipped item came from (or null)
      now_equipped          — full state of the newly-equipped item
      loadout               — current full slot loadout (compact summary)

    The equipped_flag column is auto-synced (1 iff slot IS NOT NULL).
    """
    slot_norm: str | None = (slot or "").strip()
    if slot_norm == "" or slot_norm.lower() == "null":
        slot_norm = None

    try:
        result = db_equip_item(
            character_target=character_target,
            item_name_or_id=item_name_or_id,
            slot=slot_norm,
        )
        return {"ok": True, **result}
    except ValueError as e:
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: list_equipped
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def list_equipped(
    character_target: Annotated[
        str,
        "Character to inspect — name (case-insensitive prefix match) or "
        "numeric character_id.",
    ],
) -> dict:
    """
    Return ONLY equipped (slot IS NOT NULL) inventory rows organized by
    slot in the natural reading order.

    Output is bounded by 12 lines (one per canonical slot) — small enough
    to keep loaded mid-combat without burning context. Each entry includes
    a one-line summary like 'mainhand: +1 short sword (1d6+1, +1 to hit)'
    plus the full structured combat fields for math.

    Use this any time you need 'what is the PC actually wielding/wearing
    right now?' without the full get_character_state payload.
    """
    try:
        return db_list_equipped(character_target=character_target)
    except ValueError as e:
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: list_inventory
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def list_inventory(
    character_target: Annotated[
        str,
        "Character to inspect — name (case-insensitive prefix match) or "
        "numeric character_id.",
    ],
    magic_only: Annotated[
        bool,
        "If true, return only items with magic_flag=1. Combine with "
        "equipped_only for an instant 'magic items currently worn' view.",
    ] = False,
    equipped_only: Annotated[
        bool,
        "If true, return only items with a non-null slot. More detailed "
        "than list_equipped — includes value, weight notes, carry_notes, "
        "etc.",
    ] = False,
    summary_only: Annotated[
        bool,
        "If true, return only the 5 fields needed for cross-reference: "
        "inventory_id, name, slot, magic_flag, equipped. Cuts a 200-row "
        "inventory from ~75 KB to ~6 KB. Combines freely with magic_only "
        "and equipped_only.",
    ] = False,
) -> dict:
    """
    Return one character's inventory as a focused, filterable list.

    Solves the 'get_character_state is too large mid-session' problem.
    Default row shape carries inventory_id, item_id, name, item_type,
    magic_flag, value_gp, slot, equipped, quantity, the full combat
    fields (damage_dice, damage_bonus, to_hit_bonus, weapon_type,
    armor_class_bonus), item_notes, and carry_notes.

    Filters:
      magic_only=True    → only magic items (instant magic-item list)
      equipped_only=True → only currently-equipped items (more detail than
                          list_equipped — useful when you also need
                          item_notes / value / quantity)
      summary_only=True  → drop everything except inventory_id, name, slot,
                          magic_flag, equipped. Use this when scanning
                          large inventories or when downstream tools only
                          need the inventory_id to act.
    """
    try:
        result = db_list_inventory(
            character_target=character_target,
            magic_only=bool(magic_only),
            equipped_only=bool(equipped_only),
            summary_only=bool(summary_only),
        )
    except ValueError as e:
        return {"error": str(e)}
    # If the caller already asked for summary_only, no point auto-
    # degrading; return as-is. Otherwise apply the cap.
    if summary_only:
        return result
    return _cap_response(
        result,
        summary_fn=_summarize_inventory,
        tool_name="list_inventory",
    )


# ══════════════════════════════════════════════════════════════════════════════
# TOOL: search_inventory
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def search_inventory(
    character_target: Annotated[
        str,
        "Character to inspect — name (case-insensitive prefix match) or "
        "numeric character_id.",
    ],
    name_substring: Annotated[
        str,
        "Item-name substring to match. Case-insensitive substring (NOT "
        "prefix) — 'sword' matches 'Long Sword', '+1 short sword', "
        "'sword cane', etc. Whitespace is preserved.",
    ],
) -> dict:
    """
    Substring item-name search within one character's inventory.

    Solves the 'what is the exact stored name of X?' problem without
    dumping the whole inventory. Returns the compact 5-field summary
    shape (inventory_id, name, slot, magic_flag, equipped) so the
    result is small enough to inspect even when many items match.

    Feed the resulting inventory_id back into equip_item or
    update_inventory_item for any follow-up action.

    Returns {"error": "..."} on bad character or empty substring.
    """
    try:
        return db_search_inventory(
            character_target=character_target,
            name_substring=name_substring,
        )
    except ValueError as e:
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# AREA PRE-POPULATION  (Phase 7)
# ══════════════════════════════════════════════════════════════════════════════
#
# The world exists before the player encounters it. Every encounter and
# every treasure haul for a given location is rolled once at populate time
# and persisted in area_instances. Subsequent visits return the same
# monsters (same individual HP) and the same treasure, so retreating and
# returning is consistent. Once treasure is looted it does not respawn.
#
# Tools:
#   populate_area          — pre-roll all encounters + treasure for a location
#   get_area_encounters    — return everything pre-rolled (auto-populates if absent)
#   get_monster_instance   — fetch one monster's stats
#   update_monster_instance — write back HP / status / treasure_status
#   populate_npc           — roll full stat block for a named NPC
#
# start_combat already consults area_instances automatically: when called
# with a `location` that has a pending pre-rolled encounter for the same
# monster type, the pre-rolled HP values are used in place of fresh rolls.
# Combatants from a pre-rolled instance carry area_instance_id and
# monster_index so subsequent damage can be written back via
# update_monster_instance.
# ══════════════════════════════════════════════════════════════════════════════


@mcp.tool()
def populate_area(
    location_name: Annotated[
        str,
        "Name of the location to populate, e.g. 'Worker's Tunnel', "
        "'Quasquetan dungeon level 1'. Looked up against the locations table "
        "case-insensitively; if no row matches, the name is still recorded "
        "and the encounters are stored under that label.",
    ],
    dungeon_level: Annotated[
        int,
        "Dungeon level for the random encounter table (1-10).",
    ] = 1,
    num_rooms: Annotated[
        int,
        "How many rooms to auto-generate. Pass 0 to use the default of 4-6.",
    ] = 0,
    notes: Annotated[
        str,
        "Free-text notes about the area (theme, history, why it's being populated).",
    ] = "",
) -> dict:
    """
    Pre-roll every encounter and treasure haul for an area. Each room gets
    its own row in area_instances with individual HP per monster and a
    fully resolved treasure haul (coins by denomination, gems typed, jewelry
    valued, magic items resolved through the standard subtable pipeline).

    Idempotent: if the location already has rows, returns the existing
    encounters unchanged so calling this multiple times is safe. To force a
    fresh population, delete the existing rows first via direct_db_edit.

    Call this once per dungeon/area at session start (or whenever the player
    becomes aware of an area they may visit). Combat will auto-consult the
    pre-rolled HP via start_combat — no extra wiring needed.
    """
    try:
        result = db_populate_area(
            location_name=location_name,
            dungeon_level=int(dungeon_level),
            num_rooms=int(num_rooms) if num_rooms else None,
            notes=notes or None,
        )
        # Phase 35: one visual ref per unique monster type across all rooms.
        rooms = result.get("rooms") or []
        names = [room.get("monster_type") for room in rooms
                 if room.get("monster_type")]
        block = _visual_refs_block(names)
        if block:
            result["visual_refs"] = block
        return result
    except Exception as e:
        return {"error": str(e), "tool": "populate_area"}


@mcp.tool()
def get_area_encounters(
    location_name: Annotated[
        str,
        "Name of the location to fetch pre-rolled encounters for.",
    ],
    auto_populate: Annotated[
        bool,
        "If true (default) and the location has no pre-rolled rows yet, "
        "populate_area is called automatically with dungeon_level. If false, "
        "returns an empty rooms list when nothing is populated.",
    ] = True,
    dungeon_level: Annotated[
        int,
        "Dungeon level used by auto-populate. Ignored if auto_populate is false.",
    ] = 1,
) -> dict:
    """
    Return every pre-rolled room for a location: monster type, count,
    individual HP per monster, current alive/dead status per monster,
    treasure_status (intact / partially_looted / looted), and the full
    treasure haul (coins, gems, jewelry, magic items).

    Call this BEFORE narrating the area — the AI should know what's actually
    in each room, not improvise.
    """
    try:
        return db_get_area_encounters(
            location_name=location_name,
            auto_populate=bool(auto_populate),
            dungeon_level=int(dungeon_level),
        )
    except Exception as e:
        return {"error": str(e), "tool": "get_area_encounters"}


@mcp.tool()
def get_monster_instance(
    area_instance_id: Annotated[
        int,
        "area_instance_id of the encounter group (from get_area_encounters).",
    ],
    monster_index: Annotated[
        int,
        "0-based index of the specific monster within the group.",
    ] = 0,
) -> dict:
    """
    Return one specific pre-rolled monster's stats: current HP, alive/dead
    status, full monster reference data (HD, AC, attacks, damage, special
    abilities), and the room's shared treasure haul.
    """
    try:
        return db_get_monster_instance(
            area_instance_id=int(area_instance_id),
            monster_index=int(monster_index),
        )
    except Exception as e:
        return {"error": str(e), "tool": "get_monster_instance"}


@mcp.tool()
def update_monster_instance(
    area_instance_id: Annotated[
        int,
        "area_instance_id of the encounter group.",
    ],
    monster_index: Annotated[
        int,
        "0-based index of the monster to update. Pass -1 to update only "
        "room-level fields (treasure_status / encounter_status).",
    ] = -1,
    hp_current: Annotated[
        int,
        "New current HP for this monster. Pass -1 to leave HP unchanged. "
        "When HP drops to 0 the monster's status auto-flips to 'dead'.",
    ] = -1,
    status: Annotated[
        str,
        "Status: 'alive', 'dead', 'fled', 'fleeing'. Leave blank to leave unchanged.",
    ] = "",
    treasure_status: Annotated[
        str,
        "Treasure state: 'intact', 'partially_looted', 'looted'. Leave blank "
        "to leave unchanged. Once 'looted' the room treasure does NOT respawn.",
    ] = "",
    encounter_status: Annotated[
        str,
        "Encounter state: 'pending', 'engaged', 'cleared'. Auto-progresses to "
        "'cleared' when every monster is dead or fled. Leave blank to leave "
        "unchanged.",
    ] = "",
) -> dict:
    """
    Update a monster's HP / status, or the room's treasure_status /
    encounter_status, on a pre-rolled instance. Call this after combat damage
    or after the party loots the room so future visits reflect what happened.
    """
    try:
        return db_update_monster_instance(
            area_instance_id=int(area_instance_id),
            monster_index=int(monster_index) if int(monster_index) >= 0 else None,
            hp_current=int(hp_current) if int(hp_current) >= 0 else None,
            status=status or None,
            treasure_status=treasure_status or None,
            encounter_status=encounter_status or None,
        )
    except Exception as e:
        return {"error": str(e), "tool": "update_monster_instance"}


@mcp.tool()
def populate_npc(
    npc_name: Annotated[
        str,
        "Full name (or unique prefix) of the NPC. Must already exist in the "
        "characters table — call add_npc first if needed.",
    ],
    level: Annotated[
        int,
        "NPC level (1-20). Used to roll HP, set THAC0, and gate magic-item chance.",
    ] = 1,
    class_name: Annotated[
        str,
        "AD&D 1e class: Fighter, Cleric, Magic-User, Thief, Ranger, Paladin, "
        "Druid, Assassin, Monk, Bard. Defaults to Fighter.",
    ] = "Fighter",
    race: Annotated[
        str,
        "Race override. Defaults to whatever's already on the characters row.",
    ] = "",
    npc_tier: Annotated[
        str,
        "Stat-rolling tier. Three options:\n"
        "  'minion'   — 3d6 straight in order, no smart assignment. Use for "
        "cannon fodder, generic guards, and random-encounter mooks.\n"
        "  'standard' — 4d6 drop lowest with class-aware assignment. Default "
        "for most named NPCs.\n"
        "  'boss'     — 5d6 keep best 3 with class-aware assignment. Use for "
        "named villains, faction leaders, dungeon bosses, anyone with a "
        "title.\n"
        "Leave blank to auto-detect from level (>= 7 ⇒ boss, otherwise "
        "standard).",
    ] = "",
) -> dict:
    """
    Pre-roll a complete stat block for a named NPC and persist it across the
    standard tables (class_levels, character_abilities, character_status)
    plus a world_facts row carrying tier, THAC0, equipment, carried gold,
    and any personal magic items.

    Idempotent: if the NPC already has class_levels and character_abilities
    rows the existing stats are returned unchanged. To deliberately reroll,
    delete the rows via direct_db_edit first.

    Tier semantics:
      minion     3d6 straight, no smart assignment.            Mean ≈ 10.5
      standard   4d6 drop lowest, smart class assignment.      Mean ≈ 12.2
      boss       5d6 keep best 3, smart class assignment.      Mean ≈ 15.7

    Stats rolled per tier:
      - 6 abilities (smart-assigned for standard/boss, fixed-order for minion)
      - HP (per-class hit die × level + CON bonus)
      - AC (class default minus DEX bonus)
      - THAC0 (per-class progression)
      - Equipment list appropriate to class
      - Carried gold (level-scaled)
      - ~5%/level chance of a personal magic item (capped at 40%)
    """
    try:
        result = db_populate_npc(
            npc_name=npc_name,
            level=int(level),
            class_name=class_name,
            race=race or None,
            npc_tier=(npc_tier.strip() or None),
        )
        # Phase 35: one visual ref keyed to the NPC's canonical name. Prefer
        # the resolved name from the result over the input (the resolver
        # accepts a prefix), so 'Hadran' surfaces as 'Lord Hadran Velmire'.
        if isinstance(result, dict) and not result.get("error"):
            ref_name = result.get("name") or npc_name
            block = _visual_refs_block([ref_name])
            if block:
                result["visual_refs"] = block
        return result
    except Exception as e:
        return {"error": str(e), "tool": "populate_npc"}


# ══════════════════════════════════════════════════════════════════════════════
# CHARACTER ROSTER, XP GRANTS, CLASS-LEVEL MANAGEMENT  (Phase 8)
# ══════════════════════════════════════════════════════════════════════════════


@mcp.tool()
def list_characters() -> dict:
    """
    Return every character in the active campaign — id, name, type
    (PC/NPC/etc), race, alignment, class levels with XP, and a notes
    preview. Solves the "what's their character_id?" problem.

    Call this when you need to discover IDs for grant_xp, add_class_level,
    update_npc, or get_character_state(character_id=...). Cheap — single
    query, fixed columns.
    """
    try:
        return db_list_characters()
    except Exception as e:
        return {"error": str(e), "tool": "list_characters"}


@mcp.tool()
def grant_xp(
    character_targets: Annotated[
        list[str],
        "Array of character targets. Each entry is either a numeric "
        "character_id as a string ('1', '2') or a name prefix ('Theron', "
        "'Caiya', 'Ruk'). Mixed lists are fine. Use list_characters to "
        "discover names and IDs.",
    ],
    amount: Annotated[
        int,
        "XP to add to every targeted character's class_levels row(s). "
        "Negative amounts are accepted (XP debt / penalty); the row's xp "
        "is clamped to 0 minimum.",
    ],
    event_description: Annotated[
        str,
        "Short description of why XP was awarded — written to the "
        "world_facts xp_log audit row. Examples: 'Cleared the goblin "
        "warren', 'Recovered the Mayor's signet', 'Defeated Hill Giant'.",
    ] = "",
) -> dict:
    """
    Award XP to one or more party members and check each for a level-up.

    Per-character per-class result includes old_xp, new_xp,
    next_level_threshold, xp_to_next_level, and a `levelup_available`
    boolean. The level itself is NOT auto-incremented — call
    update_class_level (or add_class_level for a new class) to actually
    promote the character so HP/THAC0/spells are explicitly recomputed.

    Audit: writes a single category='xp_log' row to world_facts with
    timestamp, amount, event_description, and the resolved character_ids.

    Targets accept any mix of numeric IDs and name prefixes — both
    resolve through the same case-insensitive lookup.
    """
    try:
        return db_grant_xp(
            character_targets=list(character_targets or []),
            amount=int(amount),
            event_description=event_description or "",
        )
    except Exception as e:
        return {"error": str(e), "tool": "grant_xp"}


@mcp.tool()
def add_class_level(
    character_target: Annotated[
        str,
        "Character_id (numeric string) or name prefix. E.g. '2' or 'Caiya'. "
        "Use list_characters to discover IDs.",
    ],
    class_name: Annotated[
        str,
        "Class to add. Common: Fighter, Cleric, Magic-User, Thief, Ranger, "
        "Paladin, Druid, Assassin, Monk, Bard, Illusionist.",
    ],
    level: Annotated[
        int,
        "Starting level for this class. Default 1.",
    ] = 1,
    xp: Annotated[
        int,
        "Starting XP for this class. Default 0.",
    ] = 0,
) -> dict:
    """
    Insert a new row into class_levels for a character. Use this for:
      - Adding a multi-class line to an existing character
      - Recording a new henchman's class
      - Seeding a tracked NPC's class data so grant_xp can find a row

    Refuses to insert a duplicate (same character_id + class_name). To
    modify an existing row use update_class_level or direct_db_edit.

    Returns the new class_level_id and the inserted values, plus the next
    level XP threshold so you can see how much room is left before the
    character can level up.
    """
    try:
        return db_add_class_level(
            character_target=character_target,
            class_name=class_name,
            level=int(level),
            xp=int(xp),
        )
    except Exception as e:
        return {"error": str(e), "tool": "add_class_level"}


if __name__ == "__main__":
    mcp.run()
