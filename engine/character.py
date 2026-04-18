"""
engine/character.py
-------------------
CharacterSheet class — full AD&D 1e / OSRIC character model.
Handles creation, stat rolling, derived stats, leveling, and JSON persistence.

Design rule: this module is STANDALONE. It uses data/classes.json and data/races.json
but never touches saves/theron.db. New characters go to saves/<name>.json only.
"""

import json
import os
import random
from pathlib import Path

# Resolve data paths relative to this file
_ENGINE_DIR = Path(__file__).parent
_DATA_DIR = _ENGINE_DIR.parent / "data"
_SAVES_DIR = _ENGINE_DIR.parent / "saves"


def _load_json(filename: str) -> dict:
    path = _DATA_DIR / filename
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ── CON modifier table (AD&D 1e / OSRIC) ─────────────────────────────────────
# (con_score): (hp_modifier, system_shock_%, resurrection_survival_%)
CON_TABLE = {
    1:  (-3, 25,  30),
    2:  (-2, 30,  35),
    3:  (-2, 35,  40),
    4:  (-1, 40,  45),
    5:  (-1, 45,  50),
    6:  (-1, 50,  55),
    7:  ( 0, 55,  60),
    8:  ( 0, 60,  65),
    9:  ( 0, 65,  70),
    10: ( 0, 70,  75),
    11: ( 0, 75,  80),
    12: ( 0, 80,  85),
    13: ( 0, 85,  90),
    14: ( 0, 88,  92),
    15: ( 1, 91,  94),
    16: ( 2, 95,  96),
    17: ( 3, 97,  98),
    18: ( 4, 99,  100),
    19: ( 5, 99,  100),
    20: ( 5, 99,  100),
    21: ( 6, 99,  100),
    22: ( 6, 99,  100),
    23: ( 6, 99,  100),
    24: ( 7, 99,  100),
    25: ( 7, 99,  100),
}

# ── DEX modifier table ────────────────────────────────────────────────────────
# (dex_score): (reaction_attack_adj, defensive_adj)
DEX_TABLE = {
    1:  (-3,  4), 2:  (-2,  3), 3:  (-1,  3), 4:  ( 0,  2), 5:  ( 0,  2),
    6:  ( 0,  1), 7:  ( 0,  0), 8:  ( 0,  0), 9:  ( 0,  0), 10: ( 0,  0),
    11: ( 0,  0), 12: ( 0,  0), 13: ( 0,  0), 14: ( 0,  0), 15: ( 0, -1),
    16: ( 1, -2), 17: ( 2, -3), 18: ( 2, -4), 19: ( 3, -4), 20: ( 3, -4),
    21: ( 4, -5), 22: ( 4, -5), 23: ( 4, -5), 24: ( 5, -6), 25: ( 5, -6),
}

# ── STR modifier table (OSRIC / AD&D 1e) ─────────────────────────────────────
# (str_score): (to_hit_adj, dmg_adj, open_doors_d6, bend_bars_pct)
STR_TABLE = {
    1:  (-5, -4, 1,  0),  2:  (-3, -2, 1,  0),  3:  (-3, -1, 1,  0),
    4:  (-2, -1, 1,  0),  5:  (-2, -1, 1,  0),  6:  (-1,  0, 1,  0),
    7:  (-1,  0, 1,  0),  8:  ( 0,  0, 1,  1),  9:  ( 0,  0, 1,  1),
    10: ( 0,  0, 2,  2), 11: ( 0,  0, 2,  2), 12: ( 0,  0, 2,  4),
    13: ( 0,  0, 2,  4), 14: ( 0,  0, 2,  7), 15: ( 0,  0, 2,  7),
    16: ( 0, +1, 3, 10), 17: (+1, +1, 3, 13), 18: (+1, +2, 3, 16),
    # 18/xx handled separately for Fighters; base 18 used for all other classes
    19: (+3, +7, 4, 50), 20: (+3, +8, 4, 60), 21: (+4, +9, 4, 70),
    22: (+4, +10, 5, 80), 23: (+5, +11, 5, 90), 24: (+6, +12, 5, 95),
    25: (+7, +14, 6, 99),
}

# ── CHA reaction adjustment ───────────────────────────────────────────────────
CHA_TABLE = {
    1: -5, 2: -4, 3: -3, 4: -2, 5: -2, 6: -1, 7: -1, 8:  0,
    9:  0, 10: 0, 11:  0, 12:  0, 13:  1, 14:  1, 15:  3, 16:  5,
    17:  6, 18:  7,
}


def _roll_dice(num: int, sides: int) -> list[int]:
    return [random.randint(1, sides) for _ in range(num)]


def _con_hp_mod(con: int) -> int:
    return CON_TABLE.get(con, (0, 0, 0))[0]


def _dex_ac_mod(dex: int) -> int:
    """Returns the AC adjustment from DEX. Negative = better AC."""
    return DEX_TABLE.get(dex, (0, 0))[1]


class CharacterSheet:
    """
    Full AD&D 1e / OSRIC character. Stateless between calls — call methods
    in sequence to build a character, then call to_json() to persist.
    """

    def __init__(self):
        # Basic identity
        self.name: str = ""
        self.race: str = ""
        self.character_class: str = ""
        self.level: int = 1
        self.alignment: str = ""
        self.xp: int = 0
        self.xp_next_level: int = 0

        # Ability scores (raw, before racial mods applied)
        self.ability_scores: dict[str, int] = {
            "str": 0, "int": 0, "wis": 0, "dex": 0, "con": 0, "cha": 0
        }

        # Derived stats
        self.hp: dict[str, int] = {"current": 0, "max": 0}
        self.thac0: int = 20
        self.ac: int = 10
        self.saving_throws: dict[str, int] = {
            "death": 0, "wands": 0, "paralysis": 0, "breath": 0, "spells": 0
        }

        # Equipment & spells
        self.inventory: list = []
        self.gold: float = 0.0
        self.armor_bonus: int = 0          # from equipped armor
        self.spells: dict = {}             # {"level_1": [...], ...}
        self.conditions: list = []

        # HD rolled at each level (for auditing)
        self._hp_rolls: list[int] = []

        # References loaded lazily
        self._class_data: dict = {}
        self._race_data: dict = {}

    # ── Rolling ───────────────────────────────────────────────────────────────

    def roll_ability_scores(self, method: str = "5d6") -> dict[str, int]:
        """
        Roll a set of 6 ability scores (AD&D 1e / OSRIC).

        method="5d6"  : Roll 5d6, keep best 3. (default, recommended)
        method="4d6"  : Roll 4d6, drop lowest die. (classic alternative)

        Returns the raw scores dict and sets self.ability_scores.
        The caller assigns scores to abilities after rolling (or we auto-assign
        highest to prime requisite if called via apply_class first).
        """
        if method not in ("4d6", "5d6"):
            raise ValueError(f"Unknown method '{method}'. Use '5d6' or '4d6'.")

        scores = []
        for _ in range(6):
            if method == "4d6":
                rolls = _roll_dice(4, 6)
                total = sum(sorted(rolls)[1:])   # drop lowest 1
            else:  # 5d6
                rolls = _roll_dice(5, 6)
                total = sum(sorted(rolls)[2:])   # drop lowest 2
            scores.append(total)

        # Store in order: STR, INT, WIS, DEX, CON, CHA
        keys = ["str", "int", "wis", "dex", "con", "cha"]
        self.ability_scores = dict(zip(keys, scores))
        return dict(self.ability_scores)

    def roll_starting_gold(self) -> float:
        """Roll 3d6 x 10 gp as per AD&D 1e. Override per class if desired."""
        self.gold = sum(_roll_dice(3, 6)) * 10.0
        return self.gold

    # ── Race ─────────────────────────────────────────────────────────────────

    def apply_race(self, race_name: str, races_data: dict | None = None) -> None:
        """
        Apply racial ability modifiers and store race metadata.
        Loads races.json automatically if races_data not provided.
        """
        if races_data is None:
            races_data = _load_json("races.json")

        if race_name not in races_data:
            raise ValueError(f"Race '{race_name}' not found in races data.")

        self.race = race_name
        self._race_data = races_data[race_name]

        for stat, mod in self._race_data.get("ability_modifiers", {}).items():
            key = stat.lower()
            if key in self.ability_scores:
                self.ability_scores[key] = max(3, self.ability_scores[key] + mod)

    # ── Class ─────────────────────────────────────────────────────────────────

    def apply_class(self, class_name: str, classes_data: dict | None = None) -> None:
        """
        Set class, load THAC0 table, saves table, XP table, and HD type.
        Call after roll_ability_scores() and apply_race().
        """
        if classes_data is None:
            classes_data = _load_json("classes.json")

        if class_name not in classes_data:
            raise ValueError(f"Class '{class_name}' not found in classes data.")

        self.character_class = class_name
        self._class_data = classes_data[class_name]

        xp_table = self._class_data["xp_table"]
        # xp_next_level = XP required to reach level 2 (index 1)
        if len(xp_table) > 1:
            self.xp_next_level = xp_table[1]
        else:
            self.xp_next_level = 0

    # ── Derived Stats ─────────────────────────────────────────────────────────

    def calculate_derived_stats(self) -> None:
        """
        Calculate HP (roll HD + CON mod), AC, THAC0, saving throws.
        Must be called after apply_class() and apply_race().
        """
        if not self._class_data:
            raise RuntimeError("Call apply_class() before calculate_derived_stats().")

        self._roll_hit_points()
        self._calculate_thac0()
        self._calculate_saving_throws()
        self._calculate_ac()

    def _roll_hit_points(self) -> None:
        """Roll level 1 HD + CON modifier, minimum 1 HP."""
        sides = self._class_data["hit_die_sides"]
        con_mod = _con_hp_mod(self.ability_scores.get("con", 10))
        roll = random.randint(1, sides)
        self._hp_rolls = [roll]
        hp = max(1, roll + con_mod)
        self.hp = {"current": hp, "max": hp}

    def _calculate_thac0(self) -> None:
        thac0_table = self._class_data.get("thac0_by_level", {})
        level_key = str(self.level)
        self.thac0 = thac0_table.get(level_key, 20)

    def _calculate_saving_throws(self) -> None:
        saves_table = self._class_data.get("saves_by_level", {})
        level_key = str(self.level)
        if level_key in saves_table:
            self.saving_throws = dict(saves_table[level_key])
        else:
            # Fallback to closest lower level
            for lvl in range(self.level, 0, -1):
                if str(lvl) in saves_table:
                    self.saving_throws = dict(saves_table[str(lvl)])
                    break

    def _calculate_ac(self) -> None:
        """Base AC = 10 + DEX modifier (negative = better). Armor applied separately."""
        dex = self.ability_scores.get("dex", 10)
        dex_mod = _dex_ac_mod(dex)
        self.ac = 10 + dex_mod - self.armor_bonus

    # ── Serialization ─────────────────────────────────────────────────────────

    def to_json(self) -> dict:
        """Return a dict matching the save file structure in BLUEPRINT.md."""
        xp_table = self._class_data.get("xp_table", [])

        return {
            "version": "0.1",
            "character": {
                "name":           self.name,
                "race":           self.race,
                "class":          self.character_class,
                "level":          self.level,
                "xp":             self.xp,
                "xp_next_level":  self.xp_next_level,
                "ability_scores": dict(self.ability_scores),
                "hp":             dict(self.hp),
                "thac0":          self.thac0,
                "saving_throws":  dict(self.saving_throws),
                "ac":             self.ac,
                "inventory":      list(self.inventory),
                "gold":           self.gold,
                "spells":         dict(self.spells),
                "conditions":     list(self.conditions),
                "_hp_rolls":      list(self._hp_rolls),
            },
            "world": {
                "calendar_date":    "Fireseek 1, 576 CY",
                "location":         {"hex": "D8", "region": "Wild Coast", "place": "Safeton"},
                "explored_hexes":   ["D8"],
                "known_locations":  {},
                "known_npcs":       [],
                "faction_standing": {},
                "recent_events":    [],
            },
            "domain": None,
            "log":    [],
        }

    @classmethod
    def from_json(cls, data: dict) -> "CharacterSheet":
        """Reconstruct a CharacterSheet from a save file dict."""
        sheet = cls()
        ch = data.get("character", {})

        sheet.name              = ch.get("name", "")
        sheet.race              = ch.get("race", "")
        sheet.character_class   = ch.get("class", "")
        sheet.level             = ch.get("level", 1)
        sheet.xp                = ch.get("xp", 0)
        sheet.xp_next_level     = ch.get("xp_next_level", 0)
        sheet.ability_scores    = ch.get("ability_scores", {})
        sheet.hp                = ch.get("hp", {"current": 1, "max": 1})
        sheet.thac0             = ch.get("thac0", 20)
        sheet.saving_throws     = ch.get("saving_throws", {})
        sheet.ac                = ch.get("ac", 10)
        sheet.inventory         = ch.get("inventory", [])
        sheet.gold              = ch.get("gold", 0.0)
        sheet.spells            = ch.get("spells", {})
        sheet.conditions        = ch.get("conditions", [])
        sheet._hp_rolls         = ch.get("_hp_rolls", [])

        # Reload class/race data so level_up() works
        try:
            classes_data = _load_json("classes.json")
            if sheet.character_class in classes_data:
                sheet._class_data = classes_data[sheet.character_class]
        except FileNotFoundError:
            pass

        try:
            races_data = _load_json("races.json")
            if sheet.race in races_data:
                sheet._race_data = races_data[sheet.race]
        except FileNotFoundError:
            pass

        return sheet

    def save(self, name: str | None = None) -> Path:
        """Write save file to saves/<name>.json. Returns the path."""
        _SAVES_DIR.mkdir(exist_ok=True)
        filename = (name or self.name or "unnamed").lower().replace(" ", "_")
        path = _SAVES_DIR / f"{filename}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_json(), f, indent=2)
        return path

    @classmethod
    def load(cls, name: str) -> "CharacterSheet":
        """Load from saves/<name>.json."""
        filename = name.lower().replace(" ", "_")
        path = _SAVES_DIR / f"{filename}.json"
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_json(data)

    # ── Level Up ──────────────────────────────────────────────────────────────

    def level_up(self) -> dict:
        """
        Advance one level. Rolls new HP die (or fixed value above name level),
        updates THAC0 and saves from tables, updates XP threshold.

        Returns a summary dict of what changed.
        """
        if not self._class_data:
            raise RuntimeError("Class data not loaded. Call apply_class() first.")

        old_level = self.level
        self.level += 1
        new_level_str = str(self.level)

        # ── HP ────────────────────────────────────────────────────────────────
        con_mod = _con_hp_mod(self.ability_scores.get("con", 10))
        name_level = self._class_data.get("name_level", 9)
        hp_after   = self._class_data.get("hp_after_name_level", 2)

        if self.level > name_level:
            # Above name level: fixed HP per level (no die roll)
            hp_gain = max(1, hp_after + con_mod)
        else:
            sides   = self._class_data["hit_die_sides"]
            roll    = random.randint(1, sides)
            self._hp_rolls.append(roll)
            hp_gain = max(1, roll + con_mod)

        self.hp["max"]     += hp_gain
        self.hp["current"] += hp_gain

        # ── THAC0 ─────────────────────────────────────────────────────────────
        thac0_table  = self._class_data.get("thac0_by_level", {})
        old_thac0    = self.thac0
        self.thac0   = thac0_table.get(new_level_str, self.thac0)

        # ── Saves ─────────────────────────────────────────────────────────────
        saves_table  = self._class_data.get("saves_by_level", {})
        old_saves    = dict(self.saving_throws)
        if new_level_str in saves_table:
            self.saving_throws = dict(saves_table[new_level_str])

        # ── XP threshold ─────────────────────────────────────────────────────
        xp_table = self._class_data.get("xp_table", [])
        if self.level < len(xp_table):
            self.xp_next_level = xp_table[self.level]  # index = new level
        else:
            # Beyond table: extrapolate last increment
            if len(xp_table) >= 2:
                last_increment = xp_table[-1] - xp_table[-2]
                self.xp_next_level = xp_table[-1] + last_increment * (self.level - len(xp_table) + 1)

        return {
            "old_level":    old_level,
            "new_level":    self.level,
            "hp_gain":      hp_gain,
            "new_hp_max":   self.hp["max"],
            "old_thac0":    old_thac0,
            "new_thac0":    self.thac0,
            "saves_changed": {k: self.saving_throws[k] for k in self.saving_throws
                              if self.saving_throws[k] != old_saves.get(k)},
        }

    # ── Display ───────────────────────────────────────────────────────────────

    def display(self) -> str:
        """Return a formatted character sheet string for terminal output."""
        ab = self.ability_scores
        sv = self.saving_throws

        # STR modifiers
        str_score = ab.get("str", 10)
        str_hit_mod   = STR_TABLE.get(str_score, (0,0,0,0))[0]
        str_dmg_mod   = STR_TABLE.get(str_score, (0,0,0,0))[1]
        con_mod_str   = f"{_con_hp_mod(ab.get('con',10)):+d}"
        dex_ac_str    = f"{_dex_ac_mod(ab.get('dex',10)):+d}"

        lines = [
            "=" * 58,
            f"  {self.name or '(unnamed)'}  |  {self.race} {self.character_class}  |  Level {self.level}",
            "=" * 58,
            f"  XP: {self.xp:,}  /  {self.xp_next_level:,}  (next level)",
            f"  HP: {self.hp['current']} / {self.hp['max']}    AC: {self.ac}    THAC0: {self.thac0}",
            f"  Gold: {self.gold:.0f} gp",
            "-" * 58,
            "  ABILITY SCORES",
            f"    STR {ab.get('str',0):2d}  (hit {str_hit_mod:+d}, dmg {str_dmg_mod:+d})",
            f"    INT {ab.get('int',0):2d}",
            f"    WIS {ab.get('wis',0):2d}",
            f"    DEX {ab.get('dex',0):2d}  (AC mod {dex_ac_str})",
            f"    CON {ab.get('con',0):2d}  (HP mod {con_mod_str})",
            f"    CHA {ab.get('cha',0):2d}",
            "-" * 58,
            "  SAVING THROWS",
            f"    Death/Poison:       {sv.get('death',0):2d}",
            f"    Wands:              {sv.get('wands',0):2d}",
            f"    Paralysis/Petri:    {sv.get('paralysis',0):2d}",
            f"    Breath Weapons:     {sv.get('breath',0):2d}",
            f"    Spells:             {sv.get('spells',0):2d}",
            "-" * 58,
            f"  HP die rolls: {self._hp_rolls}",
            "=" * 58,
        ]
        return "\n".join(lines)
