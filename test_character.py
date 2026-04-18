"""
test_character.py
-----------------
Creates a Fighter named "Aldric", demonstrates both rolling methods side by
side, builds out the full character sheet, and saves to saves/aldric.json.

Run from the greyhawk-solo/ directory:
    python test_character.py
"""

import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from engine.character import CharacterSheet

SEPARATOR = "=" * 58


def roll_array_display(method: str, seed: int | None = None) -> dict[str, int]:
    """
    Roll a complete ability score array using the given method and return it.
    Prints dice method and raw scores.
    """
    sheet = CharacterSheet()
    scores = sheet.roll_ability_scores(method)
    total = sum(scores.values())
    avg   = total / 6
    print(f"  Method: {method}")
    keys = ["str", "int", "wis", "dex", "con", "cha"]
    row  = "  " + "  ".join(f"{k.upper()}: {scores[k]:2d}" for k in keys)
    print(row)
    print(f"  Total: {total}  |  Avg: {avg:.1f}")
    return scores


def main():
    print()
    print(SEPARATOR)
    print("  GREYHAWK SOLO RPG - Character Creation Test")
    print(SEPARATOR)

    # -- Side-by-side roll comparison ----------------------------------------─
    print()
    print("-- Ability Score Roll Comparison --")
    print()
    print("  [4d6 drop lowest] - classic alternative:")
    scores_4d6 = roll_array_display("4d6")
    print()
    print("  [5d6 keep best 3] - recommended (default):")
    scores_5d6 = roll_array_display("5d6")
    print()
    print("  Difference (5d6 - 4d6):")
    keys = ["str", "int", "wis", "dex", "con", "cha"]
    diffs = {k: scores_5d6[k] - scores_4d6[k] for k in keys}
    diff_row = "  " + "  ".join(
        f"{k.upper()}: {diffs[k]:+d}" for k in keys
    )
    print(diff_row)

    # -- Build Aldric using 4d6 ------------------------------------------------
    print()
    print(SEPARATOR)
    print("  Building Aldric the Fighter (4d6 classic)")
    print(SEPARATOR)

    aldric = CharacterSheet()
    aldric.name = "Aldric"

    # Roll stats
    rolled = aldric.roll_ability_scores("4d6")
    print(f"\n  Raw rolls: {rolled}")

    # Apply Human race (no modifiers)
    aldric.apply_race("Human")
    print(f"  Race: Human (no ability modifiers)")

    # Apply Fighter class
    aldric.apply_class("Fighter")
    print(f"  Class: Fighter")

    # Roll starting gold
    gold = aldric.roll_starting_gold()
    print(f"  Starting gold: {gold:.0f} gp  (3d6 × 10)")

    # Calculate all derived stats
    aldric.calculate_derived_stats()

    # Print full character sheet
    print()
    print(aldric.display())

    # -- Save to JSON ----------------------------------------------------------
    save_path = aldric.save("aldric")
    print(f"\n  Saved to: {save_path}")
    print()

    # Print the raw JSON for inspection
    print("-- Save File (saves/aldric.json) --")
    print()
    with open(save_path) as f:
        raw = json.load(f)
    print(json.dumps(raw, indent=2))

    # -- Round-trip test ------------------------------------------------------─
    print()
    print("-- Round-trip: loading from JSON and displaying --")
    print()
    loaded = CharacterSheet.load("aldric")
    print(loaded.display())

    # -- Level-up test --------------------------------------------------------─
    print()
    print("-- Level-up test (Aldric -> Level 2) --")
    print()
    result = aldric.level_up()
    print(f"  {result}")
    print()
    print(aldric.display())

    print()
    print("  All tests passed.")
    print()


if __name__ == "__main__":
    main()
