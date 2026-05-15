"""
engine/combat.py
----------------
Phase 38 — d6 Countdown Segment initiative.

This module owns the small pure-Python helpers the new combat tools need:
the DEX-to-initiative-modifier table, the AD&D 1e movement-rate parser, and
a factory for the per-round state object that lives inside the existing
active_combat world_fact.

Round flow:

    start_combat       — builds initial round_state; routes to phase
                         'surprise' (when one side is surprised) or
                         'declare' (when neither side is)
    resolve_surprise_  — repeated while phase='surprise'
      segment            consumes free_segments_remaining one at a time;
                         when 0 → phase='declare'
    declare_round      — phase='declare'. Player declares action;
                         spellcasters declare spell + casting_time
    roll_initiative    — phase='declare' → 'countdown'. Rolls d6+DEX-mod
                         per combatant; computes spell_resolves_segment;
                         decrements distance by enemy movement
    resolve_segment    — phase='countdown'. Walks segment 10 → 1; resolves
                         each combatant on their segment; gates melee on
                         in_melee_range; fires spells on resolution
                         segment; phase='complete' when done
    (round end)        — caller decides whether to call declare_round
                         again for the next round

The full state lives in world_facts active_combat under
round_state — see start_combat for the canonical shape.
"""

from __future__ import annotations

import math
import re
from typing import Any


# ── DEX-to-initiative-modifier table ─────────────────────────────────────────

def dex_initiative_mod(dex: int | float | None) -> int:
    """
    Return the AD&D 1e-style initiative modifier for a given DEX score.

      DEX ≤ 5  : -2
      DEX 6-8  : -1
      DEX 9-14 :  0
      DEX 15   : +1
      DEX 16-17: +2
      DEX 18+  : +3

    None / missing DEX defaults to 10 (no modifier).
    """
    try:
        d = int(dex) if dex is not None else 10
    except (TypeError, ValueError):
        d = 10
    if d <= 5:
        return -2
    if d <= 8:
        return -1
    if d <= 14:
        return 0
    if d == 15:
        return 1
    if d <= 17:
        return 2
    return 3


# ── Movement-rate parsing (AD&D 1e inches → feet/round) ──────────────────────

_MV_INT_RE = re.compile(r"-?\d+")


def parse_movement_rate(text: Any, default_ft_per_round: int = 60) -> int:
    """
    Best-effort conversion of an AD&D 1e movement-rate string to feet/round.

    Accepts:
      '9"'      → 90  (AD&D inches × 10 = outdoor feet/round)
      '12'      → 120 (bare integer ≤ 24 treated as inches)
      '60'      → 60  (bare integer > 24 treated as already feet/round)
      '12/24'   → 120 (first integer wins; flying/swim speeds ignored here)
      None / '' → default_ft_per_round (60).
    """
    if text is None:
        return default_ft_per_round
    s = str(text).strip()
    if not s:
        return default_ft_per_round
    m = _MV_INT_RE.search(s)
    if not m:
        return default_ft_per_round
    val = int(m.group())
    if val <= 0:
        return default_ft_per_round
    if '"' in s or "'" in s:
        return val * 10
    if val <= 24:
        return val * 10
    return val


# ── Distance & melee helpers ─────────────────────────────────────────────────

def rounds_to_close(distance_feet: int, movement_ft_per_round: int) -> int:
    """
    Number of full rounds an attacker moving at `movement_ft_per_round`
    needs to close `distance_feet` to melee (≤ 10 ft). 0 if already in
    melee range. Defaults to a sensible 1-round close when movement is
    non-positive.
    """
    if distance_feet <= 10:
        return 0
    if movement_ft_per_round <= 0:
        return 1
    return max(1, math.ceil((distance_feet - 10) / movement_ft_per_round))


def is_melee_possible(distance_feet: int) -> bool:
    """True when the current distance is at melee range (≤ 10 ft)."""
    return int(distance_feet or 0) <= 10


def is_round_distance_estimated(distance_feet: int) -> bool:
    """
    A distance is flagged 'estimated' (no penalty) when it lands on a
    round number — 10, 15, 20, 25, 30, 40, 50, 60, 80, 100, ...
    """
    d = int(distance_feet or 0)
    if d <= 0:
        return False
    return (d % 10 == 0) or (d in {15, 25, 35, 45})


# ── Initial round_state factory ──────────────────────────────────────────────

# Surprise modes accepted by start_combat.
SURPRISE_MODES = ("none", "player_surprised", "enemy_surprised", "roll")

# Actions accepted by declare_round.
DECLARE_ACTIONS = ("melee", "missile", "spell", "move", "item", "flee", "hold")


def make_round_state(
    round_number: int,
    phase: str,
    distance_feet: int,
    distance_estimated: bool,
    missile_rounds_before_melee: int,
    melee_possible: bool,
    surprise_active: bool,
    surprised_side: str | None,
    free_segments: int,
    combatants: list[dict],
) -> dict:
    """
    Build a canonical `round_state` dict ready to drop into the
    active_combat blob. All optional per-combatant fields default to the
    sentinel values the tools expect on the first call.
    """
    return {
        "round":            int(round_number),
        "phase":            phase,
        "current_segment":  None,
        "free_segments_remaining": int(free_segments),
        "surprise": {
            "active":             bool(surprise_active),
            "surprised_side":     surprised_side,    # 'player' / 'enemy' / None
            "free_segments":      int(free_segments),
            "free_segments_used": False,
        },
        "distance": {
            "feet":                        int(distance_feet),
            "estimated":                   bool(distance_estimated),
            "melee_possible":              bool(melee_possible),
            "missile_rounds_before_melee": int(missile_rounds_before_melee),
        },
        "combatants": combatants,
    }


def empty_combatant_round_entry(
    cid: str,
    name: str,
    side: str,
    dex: int | None,
    movement_rate: int,
    in_melee_range: bool,
) -> dict:
    """
    Return the per-combatant slot inside round_state.combatants for one
    fighter at the start of a round. Init/declare fields are nulled; the
    consumer (declare_round / roll_initiative) fills them in.
    """
    return {
        "id":                      cid,
        "name":                    name,
        "side":                    side,
        "dex":                     dex,
        "initiative_mod":          dex_initiative_mod(dex),
        "movement_rate":           int(movement_rate),
        "initiative_roll":         None,
        "initiative_score":        None,
        "declared_action":         None,
        "declared_action_detail":  None,
        "declared_spell":          None,
        "spell_casting_time":      None,
        "spell_resolves_segment":  None,
        "spell_interrupted":       False,
        "spell_carries_to_next":   False,
        "spell_carries_segment":   None,
        "in_melee_range":          bool(in_melee_range),
        "acted":                   False,
        "holding":                 False,
    }
