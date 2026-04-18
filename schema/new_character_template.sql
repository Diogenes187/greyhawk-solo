-- new_character_template.sql
-- ---------------------------
-- Fill in every value marked <<LIKE_THIS>>, then run:
--
--   sqlite3 saves/my_campaign.db < schema/starter.sql
--   sqlite3 saves/my_campaign.db < schema/new_character_template.sql
--
-- Run starter.sql FIRST so the schema exists.
-- Run this file ONCE per campaign. It is safe to re-run (DELETE + INSERT).
--
-- AD&D 1e ability score ranges reminder:
--   STR 3-18 (Fighters may roll 18/xx percentile — note in status_notes)
--   INT 3-18   WIS 3-18   DEX 3-18   CON 3-18   CHA 3-18
--
-- Common alignments: Lawful Good, Neutral Good, Chaotic Good,
--                    Lawful Neutral, True Neutral, Chaotic Neutral,
--                    Lawful Evil, Neutral Evil, Chaotic Evil
--
-- Common races: Human, Elf, Half-Elf, Dwarf, Halfling, Half-Orc, Gnome
--
-- Common classes: Fighter, Cleric, Magic-User, Thief
--   Multi-class (non-human only): add extra rows in class_levels
-- ─────────────────────────────────────────────────────────────────────────────

PRAGMA foreign_keys = ON;

-- ═══════════════════════════════════════════════════════════════════════════
-- 1. CAMPAIGN
--    One row per campaign. campaign_id=1 is the default used by the engine.
-- ═══════════════════════════════════════════════════════════════════════════

DELETE FROM campaigns WHERE campaign_id = 1;
INSERT INTO campaigns (campaign_id, name, setting, notes) VALUES (
    1,
    '<<Your Campaign Name>>',           -- e.g. 'Greyhawk 576 CY'
    '<<World / Setting>>',              -- e.g. 'World of Greyhawk'
    '<<Any campaign notes>>'            -- can be NULL
);


-- ═══════════════════════════════════════════════════════════════════════════
-- 2. PLAYER CHARACTER
--    character_id=1 is the PC — the engine hard-references this value.
--    character_type must be 'PC' for the MCP tools to find Theron-style data.
-- ═══════════════════════════════════════════════════════════════════════════

DELETE FROM characters WHERE character_id = 1;
INSERT INTO characters (character_id, campaign_id, name, character_type, race, alignment, notes) VALUES (
    1,
    1,
    '<<Character Name>>',               -- e.g. 'Aldric Vane'
    'PC',
    '<<Race>>',                         -- e.g. 'Human'
    '<<Alignment>>',                    -- e.g. 'Neutral Good'
    '<<Background notes>>'              -- can be NULL
);


-- ═══════════════════════════════════════════════════════════════════════════
-- 3. CLASS LEVELS
--    One row per class. Single-class PC = one row.
--    Multi-class PC (e.g. Elf Fighter/Magic-User) = two rows, same character_id.
--    XP starts at 0 for a fresh character.
-- ═══════════════════════════════════════════════════════════════════════════

DELETE FROM class_levels WHERE character_id = 1;
INSERT INTO class_levels (character_id, class_name, level, xp) VALUES (
    1,
    '<<Class Name>>',                   -- 'Fighter', 'Cleric', 'Magic-User', 'Thief'
    1,                                  -- starting level (usually 1)
    0                                   -- starting XP
);

-- Uncomment for a second class (multi-class characters only):
-- INSERT INTO class_levels (character_id, class_name, level, xp) VALUES (
--     1,
--     '<<Second Class>>',
--     1,
--     0
-- );


-- ═══════════════════════════════════════════════════════════════════════════
-- 4. CHARACTER STATUS  (HP, AC, movement)
--    hp_current = hp_max at start unless you're beginning mid-adventure.
--    AC: unarmored = 10. Subtract armor bonus manually (chainmail = AC 5, etc.)
--    movement: '12"' is standard unencumbered foot movement in AD&D 1e.
--    attacks_per_round: '1' for most classes at level 1.
-- ═══════════════════════════════════════════════════════════════════════════

DELETE FROM character_status WHERE character_id = 1;
INSERT INTO character_status (character_id, hp_current, hp_max, ac, movement, attacks_per_round, status_notes) VALUES (
    1,
    <<Starting HP>>,                    -- e.g. 8
    <<Max HP>>,                         -- same as starting HP at level 1
    <<AC>>,                             -- e.g. 10 (unarmored), 5 (chain), 2 (plate+shield)
    '12"',                              -- standard movement; adjust for encumbrance
    '1',                                -- attacks per round at level 1
    '<<Gear worn / conditions>>'        -- e.g. 'Chain mail, shield, longsword', or NULL
);


-- ═══════════════════════════════════════════════════════════════════════════
-- 5. ABILITY SCORES
--    Use post-racial-modifier values (the scores on your final sheet).
--    portrait_path: optional path to a character portrait image, or NULL.
-- ═══════════════════════════════════════════════════════════════════════════

DELETE FROM character_abilities WHERE character_id = 1;
INSERT INTO character_abilities (character_id, strength, intelligence, wisdom, dexterity, constitution, charisma, portrait_path, notes) VALUES (
    1,
    <<STR>>,                            -- 3-18
    <<INT>>,                            -- 3-18
    <<WIS>>,                            -- 3-18
    <<DEX>>,                            -- 3-18
    <<CON>>,                            -- 3-18
    <<CHA>>,                            -- 3-18
    NULL,                               -- portrait path, or e.g. 'assets/my_char.png'
    NULL                                -- extra notes, or NULL
);


-- ═══════════════════════════════════════════════════════════════════════════
-- 6. HOME BASE LOCATION
--    The place the PC operates from. Add more locations as they are discovered.
--    parent_location_id NULL = top-level location.
-- ═══════════════════════════════════════════════════════════════════════════

DELETE FROM locations WHERE campaign_id = 1;
INSERT INTO locations (campaign_id, name, location_type, parent_location_id, status, notes) VALUES (
    1,
    '<<Home Base Name>>',               -- e.g. 'Safeton', 'Verbobonc', 'My Keep'
    '<<Type>>',                         -- 'Town', 'Keep', 'Dungeon', 'Wilderness', etc.
    NULL,                               -- parent location id, or NULL
    'Active',
    '<<Description>>'                   -- e.g. 'Starting town on the Wild Coast'
);


-- ═══════════════════════════════════════════════════════════════════════════
-- 7. STARTING TREASURY
--    One account tracks the PC's coin. Add accounts for domain play later.
--    Default starting gold by class (3d6 x 10 gp):
--      Fighter 30-180 gp  |  Cleric 30-180 gp
--      Magic-User 20-80 gp (2d4 x 10)  |  Thief 20-120 gp (2d6 x 10)
-- ═══════════════════════════════════════════════════════════════════════════

DELETE FROM treasury_accounts WHERE campaign_id = 1;
INSERT INTO treasury_accounts (campaign_id, account_name, location_id, gp, sp, cp, pp, gems_gp_value, notes) VALUES (
    1,
    '<<Character Name>> Treasury',      -- e.g. 'Aldric Vane Treasury'
    1,                                  -- location_id = 1 (home base above)
    <<Starting GP>>,                    -- e.g. 90
    0,                                  -- silver pieces
    0,                                  -- copper pieces
    0,                                  -- platinum pieces
    0,                                  -- gems (in gp value)
    'Starting funds'
);


-- ═══════════════════════════════════════════════════════════════════════════
-- 8. STARTING INVENTORY  (optional — add gear bought with starting gold)
--    Run these after buying equipment. Repeat the pattern for each item.
--    equipped_flag: 1 = currently worn/wielded, 0 = carried/packed
-- ═══════════════════════════════════════════════════════════════════════════

-- Example (uncomment and fill in):
-- INSERT INTO items (campaign_id, name, item_type, magic_flag, value_gp, notes) VALUES
--     (1, '<<Item Name>>', '<<Type>>', 0, <<GP Value>>, '<<Notes>>');
-- INSERT INTO inventory (character_id, item_id, quantity, equipped_flag, notes) VALUES
--     (1, last_insert_rowid(), 1, <<0 or 1>>, '<<How carried>>');
