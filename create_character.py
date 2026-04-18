"""
create_character.py
--------------------
Interactive AD&D 1e character creation for greyhawk-solo.

Flow:
  1. Enter character name
  2. Roll 5d6 keep best 3 -- see dice breakdown for all six stats
  3. Viable races and classes are highlighted from those scores
  4. Type "reroll" for fresh dice (previous scores shown alongside for comparison)
  5. Once happy, pick race and class
  6. Racial modifiers applied; final character sheet displayed
  7. Confirm, change race/class, or reroll entirely
  8. On confirmation, creates saves/<name>.db ready for Claude Desktop

Usage:
    python create_character.py

The resulting .db file appears in the switch_character.py list and can be
activated immediately. Reference tables (monsters, spells, etc.) can be
added later: sqlite3 saves/<name>.db < schema/starter.sql
"""

import json
import random
import re
import sqlite3
import sys
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT     = Path(__file__).parent
DATA     = ROOT / "data"
SAVES    = ROOT / "saves"
DDL_SQL  = ROOT / "schema" / "ddl.sql"

sys.path.insert(0, str(ROOT))
from engine.character import CharacterSheet, STR_TABLE, CON_TABLE, DEX_TABLE
from engine.db import _bootstrap_new_db

# ── Data ──────────────────────────────────────────────────────────────────────

def _load(filename):
    with open(DATA / filename, encoding="utf-8") as f:
        return json.load(f)


CLASSES_DATA = _load("classes.json")
RACES_DATA   = _load("races.json")

STAT_ORDER = ["str", "int", "wis", "dex", "con", "cha"]
STAT_UP    = ["STR", "INT", "WIS", "DEX", "CON", "CHA"]

# AD&D 1e minimum ability scores by class
CLASS_MINIMUMS = {
    "Fighter":    {"str": 9},
    "Cleric":     {"wis": 9},
    "Magic-User": {"int": 9},
    "Thief":      {"dex": 9},
}

# Starting gold (num_dice, die_sides) × 10 gp
GOLD_DICE = {
    "Fighter":    (3, 6),
    "Cleric":     (3, 6),
    "Magic-User": (2, 4),
    "Thief":      (2, 6),
}

# XP bonus threshold for prime requisite
XP_BONUS_THRESHOLD = 16


# ══════════════════════════════════════════════════════════════════════════════
# DICE ROLLING
# ══════════════════════════════════════════════════════════════════════════════

def _roll5d6_keep3():
    """Roll 5d6, keep best 3. Returns (total, all_five_dice_sorted_desc)."""
    dice = sorted([random.randint(1, 6) for _ in range(5)], reverse=True)
    return sum(dice[:3]), dice


def _roll4d6_drop1():
    """Roll 4d6, drop lowest. Returns (total, all_four_dice_sorted_desc)."""
    dice = sorted([random.randint(1, 6) for _ in range(4)], reverse=True)
    return sum(dice[:3]), dice


def roll_six_stats(method="5d6"):
    """
    Roll a full set of six ability scores.

    method="5d6" : 5d6 keep best 3 (default, recommended)
    method="4d6" : 4d6 drop lowest  (classic alternative)

    Returns dict: { 'str': (score, dice), 'int': (score, dice), ... }
    """
    roller = _roll5d6_keep3 if method == "5d6" else _roll4d6_drop1
    result = {}
    for stat in STAT_ORDER:
        score, dice = roller()
        result[stat] = (score, dice)
    return result


def roll_starting_gold(class_name):
    n, sides = GOLD_DICE.get(class_name, (3, 6))
    return sum(random.randint(1, sides) for _ in range(n)) * 10


# ══════════════════════════════════════════════════════════════════════════════
# MODIFIER STRINGS
# ══════════════════════════════════════════════════════════════════════════════

def _str_mods(score):
    hit, dmg, _, _ = STR_TABLE.get(score, (0, 0, 0, 0))
    parts = []
    if hit: parts.append(f"hit {hit:+d}")
    if dmg: parts.append(f"dmg {dmg:+d}")
    return ", ".join(parts)


def _con_mod(score):
    hp_mod = CON_TABLE.get(score, (0,))[0]
    return f"HP {hp_mod:+d}" if hp_mod else ""


def _dex_mod(score):
    ac_mod = DEX_TABLE.get(score, (0, 0))[1]
    return f"AC {ac_mod:+d}" if ac_mod else ""


def _int_mod(score):
    if score >= 18: return "read languages"
    if score >= 13: return f"+{min(score - 12, 7)} lang"
    return ""


def _wis_mod(score):
    # Magical defense adjustment
    if score >= 18: return "magic def +4"
    if score >= 15: return f"magic def +{score - 14}"
    if score <= 5:  return f"magic def -{6 - score}"
    return ""


def _cha_mod(score):
    if score <= 3:  return "react -5"
    if score <= 5:  return "react -3"
    if score <= 7:  return "react -1"
    if score <= 12: return ""
    if score <= 15: return "react +1"
    if score <= 17: return "react +3"
    return "react +7"


_MOD_FN = {
    "str": _str_mods,
    "int": _int_mod,
    "wis": _wis_mod,
    "dex": _dex_mod,
    "con": _con_mod,
    "cha": _cha_mod,
}


# ══════════════════════════════════════════════════════════════════════════════
# DISPLAY
# ══════════════════════════════════════════════════════════════════════════════

_SEP  = "  "
_LINE = "-" * 62


def _score_row(stat, score, dice=None, prev_score=None):
    """
    Build one formatted row of the scores table.
    Returns a string like:
      STR  16  hit +0, dmg +1   [6,5,5, drop 3,2]   ^ was 12
    """
    label  = stat.upper()
    mod    = _MOD_FN[stat](score)
    mod_str = f"  {mod}" if mod else ""

    # Dice column: keep (bold), drop (dim)
    if dice:
        kept   = ", ".join(str(d) for d in dice[:3])
        dropped = ", ".join(str(d) for d in dice[3:])
        dice_str = f"[{kept}  drop {dropped}]"
    else:
        dice_str = ""

    # Comparison column
    if prev_score is not None:
        diff = score - prev_score
        if diff > 0:
            arrow = f"  ^ {diff:+d} (was {prev_score})"
        elif diff < 0:
            arrow = f"  v {diff:+d} (was {prev_score})"
        else:
            arrow = f"  = (unchanged)"
    else:
        arrow = ""

    return f"  {label}  {score:2d}{mod_str:<22}  {dice_str:<28}{arrow}"


def display_scores(rolls, prev_rolls=None, header="ABILITY SCORES"):
    """Print the six scores, optionally with comparison to previous roll."""
    print()
    print(f"  {header}")
    print(f"  {_LINE}")

    for stat in STAT_ORDER:
        score, dice = rolls[stat]
        prev_score  = prev_rolls[stat][0] if prev_rolls else None
        print(_score_row(stat, score, dice, prev_score))

    scores = [rolls[s][0] for s in STAT_ORDER]
    total  = sum(scores)
    print(f"  {_LINE}")
    print(f"  Total: {total}  (avg {total/6:.1f})", end="")

    if prev_rolls:
        prev_total = sum(prev_rolls[s][0] for s in STAT_ORDER)
        diff = total - prev_total
        sign = "+" if diff >= 0 else ""
        print(f"   {sign}{diff} vs previous", end="")
    print()
    print()


# ── Viability helpers ─────────────────────────────────────────────────────────

def _viable_races(scores_dict):
    """Return races whose minimums and maximums all pass."""
    out = []
    for race, data in RACES_DATA.items():
        mins = data.get("ability_minimums", {})
        maxs = data.get("ability_maximums", {})
        if all(scores_dict.get(s.lower(), 0) >= v for s, v in mins.items()):
            if all(scores_dict.get(s.lower(), 0) <= v for s, v in maxs.items()):
                out.append(race)
    return out


def _viable_classes(scores_dict):
    """Return classes whose stat minimums all pass."""
    return [
        cls for cls, mins in CLASS_MINIMUMS.items()
        if all(scores_dict.get(s, 0) >= v for s, v in mins.items())
    ]


def _race_allows_class(race, cls):
    """True if the race's allowed_classes list includes this class."""
    allowed = RACES_DATA.get(race, {}).get("allowed_classes", [])
    return cls in allowed


def display_options(scores_dict, viable_races_list, viable_classes_list):
    """Print viable races and classes, flagging best fits."""
    print("  VIABLE RACES")
    if viable_races_list:
        # One per line with level limits note for non-humans
        for r in viable_races_list:
            limits = RACES_DATA[r].get("class_level_limits", {})
            note   = ""
            if limits:
                limit_strs = []
                for cls in viable_classes_list:
                    if cls in limits:
                        cap = limits[cls]
                        limit_strs.append(f"{cls} cap {cap}")
                if limit_strs:
                    note = f"  (level limits: {', '.join(limit_strs)})"
            print(f"    {r}{note}")
    else:
        print("    None — reroll recommended")
    print()

    print("  VIABLE CLASSES")
    if viable_classes_list:
        best_cls = max(
            viable_classes_list,
            key=lambda c: scores_dict.get(CLASSES_DATA[c]["prime_requisite"].lower(), 0)
        )
        for cls in viable_classes_list:
            pr       = CLASSES_DATA[cls]["prime_requisite"]
            pr_score = scores_dict.get(pr.lower(), 0)
            hd       = CLASSES_DATA[cls]["hit_die"]
            star     = ""
            if pr_score >= XP_BONUS_THRESHOLD:
                star = f"  ** +10% XP bonus (prime req {pr} = {pr_score})"
            elif pr_score >= 13:
                star = f"  * good prime req ({pr} = {pr_score})"
            else:
                star = f"  (prime req {pr} = {pr_score})"
            best_marker = " <-- best fit" if cls == best_cls else ""
            print(f"    {cls:<14}  HD {hd}{star}{best_marker}")
    else:
        print("    None — reroll recommended")
    print()


# ── Full character summary ────────────────────────────────────────────────────

def display_summary(name, race, cls, final_scores, sheet, gold):
    """Print the full pre-confirmation character sheet."""
    sv   = sheet.saving_throws
    ab   = final_scores
    hd   = CLASSES_DATA[cls]["hit_die"]

    print()
    print("  " + "=" * 58)
    print(f"  CHARACTER SHEET  --  {name}")
    print("  " + "=" * 58)
    print(f"  {race} {cls}  |  Level 1  |  Alignment: {sheet.alignment or 'to be chosen'}")
    print()
    con_mod_val = CON_TABLE.get(ab["con"], (0,))[0]
    con_note    = f", CON mod {con_mod_val:+d}" if con_mod_val else ""
    dex_ac_val  = DEX_TABLE.get(ab["dex"], (0, 0))[1]
    dex_note    = f", DEX mod {dex_ac_val:+d}" if dex_ac_val else ""
    print(f"  HP: {sheet.hp['max']}  (rolled {sheet._hp_rolls[0]} on {hd}{con_note})")
    print(f"  AC: {sheet.ac}  (base 10{dex_note})")
    print(f"  THAC0: {sheet.thac0}")
    print(f"  Starting gold: {gold} gp")
    print()
    print("  ABILITY SCORES (after racial modifiers)")
    print(f"  {'-'*40}")
    for stat in STAT_ORDER:
        score  = ab[stat]
        mod    = _MOD_FN[stat](score)
        mod_s  = f"  {mod}" if mod else ""
        print(f"  {stat.upper()}  {score:2d}{mod_s}")
    print()
    print("  SAVING THROWS")
    print(f"  {'-'*40}")
    print(f"  Death / Poison:       {sv['death']:2d}")
    print(f"  Wands:                {sv['wands']:2d}")
    print(f"  Paralysis / Petri:    {sv['paralysis']:2d}")
    print(f"  Breath Weapons:       {sv['breath']:2d}")
    print(f"  Spells / Staves:      {sv['spells']:2d}")
    print()
    print("  " + "=" * 58)
    print()


# ── Character sheet builder ───────────────────────────────────────────────────

def build_sheet(name, race, cls, raw_scores):
    """
    Apply race + class to a fresh CharacterSheet and calculate derived stats.
    raw_scores: dict { 'str': int, 'int': int, ... }  (pre-racial-modifier values)
    Returns (sheet, final_scores_dict).
    """
    sheet = CharacterSheet()
    sheet.name      = name
    sheet.level     = 1

    # Set raw scores before racial modifiers
    sheet.ability_scores = dict(raw_scores)

    # Apply race (modifies ability_scores in place)
    sheet.apply_race(race, RACES_DATA)

    # Apply class (loads tables)
    sheet.apply_class(cls, CLASSES_DATA)

    # Calculate HP, THAC0, saves, AC
    sheet.calculate_derived_stats()

    final = dict(sheet.ability_scores)  # post-racial
    return sheet, final


# ══════════════════════════════════════════════════════════════════════════════
# DATABASE WRITE
# ══════════════════════════════════════════════════════════════════════════════

# _bootstrap_new_db is imported from engine.db — single implementation shared
# with the MCP create_character tool.


def write_character_db(name, race, cls, final_scores, sheet, gold, alignment):
    """Create saves/<name>.db and populate with character data."""
    if not DDL_SQL.exists():
        raise FileNotFoundError(
            f"Cannot find {DDL_SQL}. Run from the project root."
        )

    SAVES.mkdir(exist_ok=True)
    slug     = re.sub(r"[^a-z0-9_]", "_", name.lower().strip()).strip("_")
    db_path  = SAVES / f"{slug}.db"

    if db_path.exists():
        ans = input(
            f"\n  {db_path.name} already exists. Overwrite? [y/N] "
        ).strip().lower()
        if ans != "y":
            print("  Cancelled.")
            return None

    print(f"\n  Creating {db_path.name} ...", end="", flush=True)

    _bootstrap_new_db(db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")

    # Campaign
    conn.execute(
        "INSERT INTO campaigns (campaign_id, name, setting, notes) VALUES (1, ?, ?, NULL)",
        (f"{name} Campaign", "World of Greyhawk, 576 CY"),
    )

    # PC character (character_id = 1)
    conn.execute(
        "INSERT INTO characters "
        "(character_id, campaign_id, name, character_type, race, alignment, notes) "
        "VALUES (1, 1, ?, 'PC', ?, ?, NULL)",
        (name, race, alignment or "Unaligned"),
    )

    # Class level
    conn.execute(
        "INSERT INTO class_levels (character_id, class_name, level, xp) VALUES (1, ?, 1, 0)",
        (cls,),
    )

    # HP, AC, movement
    conn.execute(
        "INSERT INTO character_status "
        "(character_id, hp_current, hp_max, ac, movement, attacks_per_round, status_notes) "
        "VALUES (1, ?, ?, ?, '12\"', '1', ?)",
        (
            sheet.hp["max"],
            sheet.hp["max"],
            sheet.ac,
            f"Level 1 {cls} — unarmored",
        ),
    )

    # Ability scores (post-racial-modifier values on the final sheet)
    ab = final_scores
    conn.execute(
        "INSERT INTO character_abilities "
        "(character_id, strength, intelligence, wisdom, dexterity, constitution, charisma) "
        "VALUES (1, ?, ?, ?, ?, ?, ?)",
        (ab["str"], ab["int"], ab["wis"], ab["dex"], ab["con"], ab["cha"]),
    )

    # Home base location
    conn.execute(
        "INSERT INTO locations "
        "(campaign_id, name, location_type, parent_location_id, status, notes) "
        "VALUES (1, 'Starting Location', 'Town', NULL, 'Active', "
        "'Rename with: update_location_status')",
    )

    # Starting treasury
    conn.execute(
        "INSERT INTO treasury_accounts "
        "(campaign_id, account_name, location_id, gp, sp, cp, pp, gems_gp_value, notes) "
        "VALUES (1, ?, 1, ?, 0, 0, 0, 0, 'Starting funds')",
        (f"{name} Treasury", gold),
    )

    conn.commit()
    conn.close()

    print(" done.")
    return db_path


# ══════════════════════════════════════════════════════════════════════════════
# INPUT HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _ask(prompt, valid=None):
    """Prompt until a non-empty response; optionally enforce a valid set."""
    while True:
        ans = input(prompt).strip()
        if not ans:
            continue
        if valid and ans.lower() not in valid:
            print(f"  Please enter one of: {', '.join(valid)}")
            continue
        return ans


def _parse_race_class(raw, viable_races_list, viable_classes_list):
    """
    Parse "Human Fighter" or "Elf Magic-User" etc.
    Returns (race, cls) on success, None on failure.
    Handles "Magic-User" with hyphen as a two-token class name.
    """
    tokens = raw.strip().split()
    if not tokens:
        return None

    # Try: first token = race, rest = class (handles "Elf Magic-User")
    if len(tokens) >= 2:
        candidate_race = tokens[0].title()
        candidate_cls  = " ".join(tokens[1:]).title()

        # Normalise "Magic-user" -> "Magic-User"
        candidate_cls = re.sub(r"(?i)magic.?user", "Magic-User", candidate_cls)

        if candidate_race in viable_races_list and candidate_cls in viable_classes_list:
            return candidate_race, candidate_cls

    # Try: single token matches a race or class
    if len(tokens) == 1:
        t = tokens[0].title()
        if t in viable_races_list:
            return t, None       # need to ask for class
        if t in viable_classes_list:
            return None, t       # need to ask for race

    return None


def _pick_from_list(prompt, options):
    """Show numbered list and return the selected item."""
    for i, opt in enumerate(options, 1):
        print(f"    {i}. {opt}")
    while True:
        raw = input(prompt).strip()
        if raw.lower() == "reroll":
            return "reroll"
        # Number pick
        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(options):
                return options[idx - 1]
        # Name match
        match = next((o for o in options if o.lower() == raw.lower()), None)
        if match:
            return match
        print(f"  Enter a number (1-{len(options)}) or a name from the list.")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN FLOW
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print()
    print("  ┌────────────────────────────────────────────────────┐")
    print("  │  greyhawk-solo -- Character Creation               │")
    print("  │  AD&D 1e / OSRIC                                   │")
    print("  └────────────────────────────────────────────────────┘")
    print()

    # ── Name ──────────────────────────────────────────────────────────────────
    name = _ask("  Character name: ").strip().title()
    print()

    # ── Rolling method ────────────────────────────────────────────────────────
    print("  Rolling method:")
    print("    1. 5d6 keep best 3  (recommended)")
    print("    2. 4d6 drop lowest  (classic alternative)")
    ans = _ask("  > ", valid={"1", "2", "5d6", "4d6"})
    roll_method = "4d6" if ans in ("2", "4d6") else "5d6"
    method_label = (
        "5d6 keep best 3" if roll_method == "5d6" else "4d6 drop lowest"
    )
    print(f"\n  Using: {method_label}")
    print()

    prev_rolls  = None
    rolls       = None
    chosen_race = None
    chosen_cls  = None

    # ── Main loop: roll → pick → summary → confirm ────────────────────────────
    while True:

        # ROLL PHASE
        prev_rolls = rolls
        rolls      = roll_six_stats(roll_method)

        scores_dict = {s: rolls[s][0] for s in STAT_ORDER}
        display_scores(rolls, prev_rolls)

        v_races   = _viable_races(scores_dict)
        v_classes = _viable_classes(scores_dict)
        display_options(scores_dict, v_races, v_classes)

        if not v_races or not v_classes:
            print("  No viable combinations with these scores.")
            input("  Press Enter to reroll ... ")
            continue

        # PICK LOOP (same dice, can change race/class without rerolling)
        while True:
            print("  Pick race and class (e.g. 'Human Fighter', 'Elf Magic-User')")
            print("  or type 'reroll' for new dice.")
            raw = input("\n  > ").strip()
            print()

            if raw.lower() in ("reroll", "r", "re"):
                break   # Back to ROLL PHASE

            parsed = _parse_race_class(raw, v_races, v_classes)

            if parsed is None:
                print("  Not recognised. Try: Human Fighter  or  Elf Magic-User")
                print(f"  Viable races:   {', '.join(v_races)}")
                print(f"  Viable classes: {', '.join(v_classes)}")
                print()
                continue

            race_pick, cls_pick = parsed

            # Prompt for whichever half is missing
            if race_pick is None:
                print(f"  Choose a race for {cls_pick}:")
                race_pick = _pick_from_list("  Race: ", v_races)
                if race_pick == "reroll":
                    break
                print()

            if cls_pick is None:
                # Filter classes allowed by the chosen race
                allowed_cls = [
                    c for c in v_classes if _race_allows_class(race_pick, c)
                ]
                if not allowed_cls:
                    print(f"  {race_pick}s cannot be any of the available classes. Choose another race.")
                    print()
                    continue
                print(f"  Choose a class for {race_pick}:")
                cls_pick = _pick_from_list("  Class: ", allowed_cls)
                if cls_pick == "reroll":
                    break
                print()

            # Validate the combination
            if not _race_allows_class(race_pick, cls_pick):
                print(
                    f"  {race_pick}s cannot be {cls_pick}s in AD&D 1e.\n"
                    f"  Allowed classes for {race_pick}: "
                    f"{', '.join(RACES_DATA[race_pick]['allowed_classes'])}"
                )
                print()
                continue

            chosen_race = race_pick
            chosen_cls  = cls_pick

            # SUMMARY PHASE
            sheet, final = build_sheet(name, chosen_race, chosen_cls, scores_dict)
            gold          = roll_starting_gold(chosen_cls)
            display_summary(name, chosen_race, chosen_cls, final, sheet, gold)

            # CONFIRM PHASE
            print("  Accept this character?")
            print("    yes    -- write saves/{}.db and finish".format(
                re.sub(r"[^a-z0-9_]", "_", name.lower()).strip("_")))
            print("    change -- keep these dice, pick a different race or class")
            print("    reroll -- new dice entirely")
            print()
            ans = _ask("  > ", valid={"yes", "y", "change", "c", "reroll", "r", "re"})
            print()

            if ans.lower() in ("yes", "y"):
                # Ask alignment (optional)
                print("  Alignment (optional — press Enter to skip):")
                print("  e.g. Lawful Good, True Neutral, Chaotic Evil, Neutral")
                alignment = input("  > ").strip().title() or ""
                print()

                db_path = write_character_db(
                    name, chosen_race, chosen_cls, final, sheet, gold, alignment
                )
                if db_path is None:
                    continue  # Overwrite declined — go back to confirm

                # Update config.json automatically
                config_path = ROOT / "config.json"
                try:
                    cfg = json.loads(config_path.read_text(encoding="utf-8")) \
                          if config_path.exists() else {}
                    rel = f"saves/{db_path.name}"
                    cfg["active_campaign_db"] = rel
                    config_path.write_text(
                        json.dumps(cfg, indent=2) + "\n", encoding="utf-8"
                    )
                    print(f"  config.json updated -> {rel}")
                except Exception as e:
                    print(f"  (config.json not updated: {e})")

                print()
                print(f"  {name} is ready.")
                print(f"  Restart Claude Desktop, then open a new chat and say:")
                print(f'  "Start a new campaign with {name}. Load my character state.')
                print(f'  I am a level 1 {chosen_race} {chosen_cls}."')
                print()
                print("  To populate reference tables (monsters, spells, combat matrices):")
                print(f"    sqlite3 {db_path} < schema/starter.sql")
                print()
                sys.exit(0)

            elif ans.lower() in ("change", "c"):
                # Keep same dice, loop back to pick phase
                display_scores(rolls)
                display_options(scores_dict, v_races, v_classes)
                continue  # stays in PICK LOOP

            else:
                # reroll
                break   # back to ROLL PHASE


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n  Cancelled.")
        sys.exit(0)
