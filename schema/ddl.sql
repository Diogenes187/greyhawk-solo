-- greyhawk-solo: Starter Database
-- ---------------------------------
-- Run this once to create your campaign database:
--
--   sqlite3 saves/my_campaign.db < schema/starter.sql
--
-- This script creates the full schema and loads all AD&D 1e reference data
-- (monsters, spells, saving throws, treasure types, combat matrices).
-- It does NOT create any character, location, or campaign event data.
-- After running this, populate your character with new_character_template.sql.
--
-- Foreign key enforcement is disabled during load for portability.
-- The engine (engine/db.py) enables WAL mode and FK enforcement at runtime.

PRAGMA foreign_keys = OFF;

-- ============================================================
-- SCHEMA: TABLES
-- ============================================================

CREATE TABLE active_spell_effects (
            active_spell_effect_id INTEGER PRIMARY KEY,
            campaign_id INTEGER,
            spell_cast_event_id INTEGER,
            spell_name TEXT NOT NULL,
            caster_character_id INTEGER,
            caster_name TEXT,
            effect_type TEXT NOT NULL,
            effect_key TEXT NOT NULL,
            target_scope TEXT,
            duration_text TEXT,
            expiration_basis TEXT,
            status TEXT NOT NULL,
            effect_data_json TEXT,
            source_note TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (campaign_id) REFERENCES campaigns(campaign_id),
            FOREIGN KEY (spell_cast_event_id) REFERENCES spell_cast_events(spell_cast_event_id),
            FOREIGN KEY (caster_character_id) REFERENCES characters(character_id)
        );

CREATE TABLE adnd_1e_gems_jewelry (
            gems_jewelry_id INTEGER PRIMARY KEY,
            source_id TEXT NOT NULL,
            category TEXT NOT NULL,
            subtable_name TEXT NOT NULL,
            roll_low INTEGER,
            roll_high INTEGER,
            item_name TEXT NOT NULL,
            gp_value_text TEXT,
            description_text TEXT,
            variant_text TEXT,
            quantity_text TEXT,
            notes TEXT,
            source_url TEXT,
            imported_at TEXT NOT NULL,
            UNIQUE(source_id, category, subtable_name, roll_low, roll_high, item_name)
        );

CREATE TABLE adnd_1e_magic_item_subtables (
            magic_item_subtable_id INTEGER PRIMARY KEY,
            source_id TEXT NOT NULL,
            category TEXT NOT NULL,
            subtable_name TEXT NOT NULL,
            roll_low INTEGER,
            roll_high INTEGER,
            item_name TEXT NOT NULL,
            quantity_text TEXT,
            variant_text TEXT,
            charges_text TEXT,
            encumbrance_text TEXT,
            xp_value_text TEXT,
            gp_value_text TEXT,
            notes TEXT,
            source_url TEXT,
            imported_at TEXT NOT NULL,
            UNIQUE(source_id, category, subtable_name, roll_low, roll_high, item_name)
        );

CREATE TABLE adnd_1e_treasure_types (
            treasure_dataset_id INTEGER PRIMARY KEY,
            source_id TEXT NOT NULL,
            treasure_type TEXT NOT NULL,
            category TEXT,
            component TEXT NOT NULL,
            roll_low INTEGER,
            roll_high INTEGER,
            chance_percent INTEGER,
            quantity_text TEXT,
            value_text TEXT,
            item_text TEXT,
            notes TEXT,
            source_url TEXT,
            imported_at TEXT NOT NULL,
            UNIQUE(source_id, treasure_type, component)
        );

CREATE TABLE adventure_events (
    event_id INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL,
    session_id INTEGER,
    event_order INTEGER,
    event_type TEXT NOT NULL,
    location_id INTEGER,
    title TEXT NOT NULL,
    description TEXT,
    outcome TEXT,
    FOREIGN KEY (campaign_id) REFERENCES campaigns(campaign_id),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id),
    FOREIGN KEY (location_id) REFERENCES locations(location_id)
);

CREATE TABLE ai_conversation_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            last_response_id TEXT,
            conversation_id TEXT,
            vector_store_id TEXT,
            model_name TEXT,
            retrieval_enabled INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL
        );

CREATE TABLE ai_turns (
            turn_id INTEGER PRIMARY KEY,
            player_action TEXT NOT NULL,
            dm_response TEXT NOT NULL,
            response_id TEXT,
            previous_response_id TEXT,
            conversation_id TEXT,
            model_name TEXT,
            retrieved_snippets TEXT,
            created_at TEXT NOT NULL
        , turn_packet_json TEXT, structured_response_json TEXT, validation_errors_json TEXT);

CREATE TABLE campaigns (
    campaign_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    setting TEXT,
    notes TEXT
);

CREATE TABLE canon_audit (
    audit_id INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL,
    table_name TEXT NOT NULL,
    record_key TEXT,
    field_name TEXT,
    old_value TEXT,
    new_value TEXT,
    change_note TEXT,
    changed_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (campaign_id) REFERENCES campaigns(campaign_id)
);

CREATE TABLE character_abilities (
            character_id INTEGER PRIMARY KEY,
            strength INTEGER,
            intelligence INTEGER,
            wisdom INTEGER,
            dexterity INTEGER,
            constitution INTEGER,
            charisma INTEGER,
            portrait_path TEXT,
            notes TEXT,
            FOREIGN KEY (character_id) REFERENCES characters(character_id)
        );

CREATE TABLE character_encounter_magic_item_rules (
            rule_id INTEGER PRIMARY KEY,
            character_level INTEGER NOT NULL,
            magic_table_code TEXT NOT NULL,
            chance_percent INTEGER NOT NULL,
            item_count INTEGER NOT NULL,
            UNIQUE(character_level, magic_table_code)
        );

CREATE TABLE character_encounter_magic_item_table_entries (
            item_entry_id INTEGER PRIMARY KEY,
            magic_table_code TEXT NOT NULL,
            roll_value INTEGER NOT NULL,
            item_text TEXT NOT NULL,
            UNIQUE(magic_table_code, roll_value)
        );

CREATE TABLE character_encounter_rules (
            rule_key TEXT PRIMARY KEY,
            rule_text TEXT NOT NULL
        );

CREATE TABLE character_factions (
    character_faction_id INTEGER PRIMARY KEY,
    character_id INTEGER NOT NULL,
    faction_id INTEGER NOT NULL,
    role_name TEXT,
    notes TEXT,
    FOREIGN KEY (character_id) REFERENCES characters(character_id),
    FOREIGN KEY (faction_id) REFERENCES factions(faction_id)
);

CREATE TABLE character_spells (
    character_spell_id INTEGER PRIMARY KEY,
    character_id INTEGER NOT NULL,
    spell_id INTEGER NOT NULL,
    known_flag INTEGER DEFAULT 1,
    memorized_flag INTEGER DEFAULT 0,
    notes TEXT,
    FOREIGN KEY (character_id) REFERENCES characters(character_id),
    FOREIGN KEY (spell_id) REFERENCES spells(spell_id)
);

CREATE TABLE character_status (
    status_id INTEGER PRIMARY KEY,
    character_id INTEGER NOT NULL,
    hp_current INTEGER,
    ac INTEGER,
    movement TEXT,
    attacks_per_round TEXT,
    status_notes TEXT,
    updated_note TEXT, hp_max INTEGER,
    FOREIGN KEY (character_id) REFERENCES characters(character_id)
);

CREATE TABLE characters (
    character_id INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    character_type TEXT NOT NULL,
    race TEXT,
    alignment TEXT,
    notes TEXT,
    FOREIGN KEY (campaign_id) REFERENCES campaigns(campaign_id)
);

CREATE TABLE civic_policies (
    policy_id INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL,
    policy_name TEXT NOT NULL,
    category TEXT,
    policy_text TEXT NOT NULL,
    FOREIGN KEY (campaign_id) REFERENCES campaigns(campaign_id)
);

CREATE TABLE class_levels (
    class_level_id INTEGER PRIMARY KEY,
    character_id INTEGER NOT NULL,
    class_name TEXT NOT NULL,
    level INTEGER NOT NULL,
    xp INTEGER,
    FOREIGN KEY (character_id) REFERENCES characters(character_id)
);

CREATE TABLE combat_attack_matrices (
            matrix_code TEXT PRIMARY KEY,
            class_group TEXT NOT NULL
        );

CREATE TABLE combat_attack_matrix_entries (
            entry_id INTEGER PRIMARY KEY,
            matrix_code TEXT NOT NULL,
            level_min INTEGER NOT NULL,
            level_max INTEGER,
            armor_class INTEGER NOT NULL,
            target_roll INTEGER NOT NULL,
            UNIQUE(matrix_code, level_min, armor_class),
            FOREIGN KEY (matrix_code) REFERENCES combat_attack_matrices(matrix_code)
        );

CREATE TABLE conversation_participants (
            conversation_participant_id INTEGER PRIMARY KEY,
            conversation_id INTEGER NOT NULL,
            character_id INTEGER,
            display_name TEXT,
            role_in_conversation TEXT,
            FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id),
            FOREIGN KEY (character_id) REFERENCES characters(character_id)
        );

CREATE TABLE conversations (
            conversation_id INTEGER PRIMARY KEY,
            campaign_id INTEGER NOT NULL,
            location_id INTEGER,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            topic TEXT,
            summary TEXT NOT NULL,
            promises_made TEXT,
            threats_made TEXT,
            lies_told TEXT,
            secrets_revealed TEXT,
            outcome TEXT,
            follow_up_needed TEXT,
            notes TEXT,
            FOREIGN KEY (campaign_id) REFERENCES campaigns(campaign_id),
            FOREIGN KEY (location_id) REFERENCES locations(location_id)
        );

CREATE TABLE current_scene_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            current_turn_id INTEGER,
            current_player_action TEXT,
            current_dm_response TEXT,
            structured_state_json TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (current_turn_id) REFERENCES ai_turns(turn_id)
        );

CREATE TABLE domain_income_expenses (
    domain_ledger_id INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL,
    domain_turn_id INTEGER,
    treasury_id INTEGER,
    entry_type TEXT NOT NULL,
    amount_gp INTEGER DEFAULT 0,
    amount_sp INTEGER DEFAULT 0,
    amount_cp INTEGER DEFAULT 0,
    amount_pp INTEGER DEFAULT 0,
    description TEXT NOT NULL,
    related_project_id INTEGER,
    FOREIGN KEY (campaign_id) REFERENCES campaigns(campaign_id),
    FOREIGN KEY (domain_turn_id) REFERENCES domain_turns(domain_turn_id),
    FOREIGN KEY (treasury_id) REFERENCES treasury_accounts(treasury_id),
    FOREIGN KEY (related_project_id) REFERENCES projects(project_id)
);

CREATE TABLE domain_turns (
    domain_turn_id INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL,
    turn_label TEXT NOT NULL,
    start_date TEXT,
    end_date TEXT,
    summary TEXT,
    FOREIGN KEY (campaign_id) REFERENCES campaigns(campaign_id)
);

CREATE TABLE dungeon_monster_instances (
            dungeon_monster_instance_id INTEGER PRIMARY KEY,
            encounter_id INTEGER NOT NULL,
            monster_id INTEGER,
            source_result_name TEXT NOT NULL,
            display_name TEXT NOT NULL,
            monster_level_table TEXT,
            dungeon_level INTEGER,
            number_appearing_text TEXT,
            number_encountered INTEGER,
            status TEXT NOT NULL DEFAULT 'active',
            instance_data_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (encounter_id) REFERENCES encounters(encounter_id),
            FOREIGN KEY (monster_id) REFERENCES monsters(monster_id)
        );

CREATE TABLE dungeon_party_templates (
            template_id INTEGER PRIMARY KEY,
            template_code TEXT NOT NULL UNIQUE,
            dungeon_level INTEGER NOT NULL,
            figure_count INTEGER,
            possible_alignments TEXT,
            party_text TEXT NOT NULL
        );

CREATE TABLE dungeon_random_monster_level_matrix (
            matrix_entry_id INTEGER PRIMARY KEY,
            dungeon_level_min INTEGER NOT NULL,
            dungeon_level_max INTEGER,
            roll_min INTEGER NOT NULL,
            roll_max INTEGER NOT NULL,
            monster_level_table TEXT NOT NULL
        );

CREATE TABLE dungeon_random_monster_table_entries (
            table_entry_id INTEGER PRIMARY KEY,
            monster_level_table TEXT NOT NULL,
            roll_min INTEGER NOT NULL,
            roll_max INTEGER NOT NULL,
            result_name TEXT NOT NULL,
            number_appearing_text TEXT,
            branch_type TEXT,
            notes TEXT,
            UNIQUE(monster_level_table, roll_min, roll_max)
        );

CREATE TABLE dungeon_random_subtable_entries (
            subtable_entry_id INTEGER PRIMARY KEY,
            subtable_type TEXT NOT NULL,
            monster_level_table TEXT,
            roll_min INTEGER NOT NULL,
            roll_max INTEGER NOT NULL,
            result_name TEXT NOT NULL,
            result_detail TEXT,
            notes TEXT
        );

CREATE TABLE encounter_loot_sources (
            loot_source_id INTEGER PRIMARY KEY,
            encounter_id INTEGER NOT NULL,
            monster_instance_id INTEGER,
            monster_group_label TEXT,
            treasure_type TEXT NOT NULL,
            hidden_flag INTEGER NOT NULL DEFAULT 1,
            revealed_flag INTEGER NOT NULL DEFAULT 0,
            taken_flag INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'generated',
            source_notes TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (encounter_id) REFERENCES encounters(encounter_id),
            FOREIGN KEY (monster_instance_id) REFERENCES monster_instances(monster_instance_id)
        );

CREATE TABLE encounter_participants (
    encounter_participant_id INTEGER PRIMARY KEY,
    encounter_id INTEGER NOT NULL,
    character_id INTEGER,
    troop_id INTEGER,
    side TEXT,
    role_name TEXT,
    result_notes TEXT,
    FOREIGN KEY (encounter_id) REFERENCES encounters(encounter_id),
    FOREIGN KEY (character_id) REFERENCES characters(character_id),
    FOREIGN KEY (troop_id) REFERENCES troops(troop_id),
    CHECK (character_id IS NOT NULL OR troop_id IS NOT NULL)
);

CREATE TABLE encounters (
    encounter_id INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL,
    session_id INTEGER,
    event_id INTEGER,
    location_id INTEGER,
    encounter_type TEXT,
    encounter_name TEXT NOT NULL,
    result TEXT,
    notes TEXT,
    FOREIGN KEY (campaign_id) REFERENCES campaigns(campaign_id),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id),
    FOREIGN KEY (event_id) REFERENCES adventure_events(event_id),
    FOREIGN KEY (location_id) REFERENCES locations(location_id)
);

CREATE TABLE event_characters (
    event_character_id INTEGER PRIMARY KEY,
    event_id INTEGER NOT NULL,
    character_id INTEGER NOT NULL,
    role_in_event TEXT,
    notes TEXT,
    FOREIGN KEY (event_id) REFERENCES adventure_events(event_id),
    FOREIGN KEY (character_id) REFERENCES characters(character_id)
);

CREATE TABLE factions (
    faction_id INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    faction_type TEXT,
    status TEXT,
    notes TEXT, stance_toward_theron TEXT, strength_notes TEXT, goals TEXT,
    FOREIGN KEY (campaign_id) REFERENCES campaigns(campaign_id)
);

CREATE TABLE generated_loot_instances (
            generated_loot_id INTEGER PRIMARY KEY,
            loot_source_id INTEGER NOT NULL,
            treasure_type TEXT NOT NULL,
            generated_payload_json TEXT NOT NULL,
            resolved_summary_json TEXT,
            unresolved_summary_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (loot_source_id) REFERENCES encounter_loot_sources(loot_source_id)
        );

CREATE TABLE holdings (
    holding_id INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL,
    location_id INTEGER NOT NULL,
    holding_name TEXT NOT NULL,
    holding_type TEXT,
    control_status TEXT,
    description TEXT,
    FOREIGN KEY (campaign_id) REFERENCES campaigns(campaign_id),
    FOREIGN KEY (location_id) REFERENCES locations(location_id)
);

CREATE TABLE inventory (
    inventory_id INTEGER PRIMARY KEY,
    character_id INTEGER,
    location_id INTEGER,
    treasury_id INTEGER,
    item_id INTEGER NOT NULL,
    quantity INTEGER DEFAULT 1,
    equipped_flag INTEGER DEFAULT 0,
    notes TEXT,
    FOREIGN KEY (character_id) REFERENCES characters(character_id),
    FOREIGN KEY (location_id) REFERENCES locations(location_id),
    FOREIGN KEY (treasury_id) REFERENCES treasury_accounts(treasury_id),
    FOREIGN KEY (item_id) REFERENCES items(item_id),
    CHECK ((character_id IS NOT NULL) + (location_id IS NOT NULL) + (treasury_id IS NOT NULL) = 1)
);

CREATE TABLE items (
    item_id INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    item_type TEXT,
    magic_flag INTEGER DEFAULT 0,
    value_gp INTEGER,
    notes TEXT,
    FOREIGN KEY (campaign_id) REFERENCES campaigns(campaign_id)
);

CREATE TABLE livestock (
    livestock_id INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL,
    location_id INTEGER NOT NULL,
    animal_type TEXT NOT NULL,
    count INTEGER NOT NULL,
    notes TEXT,
    FOREIGN KEY (campaign_id) REFERENCES campaigns(campaign_id),
    FOREIGN KEY (location_id) REFERENCES locations(location_id)
);

CREATE TABLE locations (
    location_id INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    location_type TEXT,
    parent_location_id INTEGER,
    status TEXT,
    notes TEXT,
    FOREIGN KEY (campaign_id) REFERENCES campaigns(campaign_id),
    FOREIGN KEY (parent_location_id) REFERENCES locations(location_id)
);

CREATE TABLE loot_reveal_events (
            loot_reveal_event_id INTEGER PRIMARY KEY,
            loot_source_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            actor_text TEXT,
            event_payload_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (loot_source_id) REFERENCES encounter_loot_sources(loot_source_id)
        );

CREATE TABLE magic_items (
            magic_item_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            item_type TEXT,
            subtype TEXT,
            bonus TEXT,
            charges TEXT,
            value_gp TEXT,
            rarity TEXT,
            curse_flag INTEGER NOT NULL DEFAULT 0,
            description TEXT,
            notes TEXT,
            source_file TEXT,
            imported_at TEXT
        );

CREATE TABLE magic_weapon_hit_requirements (
            requirement_id INTEGER PRIMARY KEY,
            defender_requires TEXT NOT NULL,
            attacker_weapon_bonus_required TEXT,
            attacker_hit_dice_required TEXT,
            notes TEXT
        );

CREATE TABLE monster_attack_matrix_entries (
            entry_id INTEGER PRIMARY KEY,
            hd_band_code TEXT NOT NULL,
            hd_band_label TEXT NOT NULL,
            hd_min REAL,
            hd_max REAL,
            armor_class INTEGER NOT NULL,
            target_roll INTEGER NOT NULL,
            notes TEXT,
            UNIQUE(hd_band_code, armor_class)
        );

CREATE TABLE monster_instances (
            monster_instance_id INTEGER PRIMARY KEY,
            encounter_id INTEGER NOT NULL,
            monster_id INTEGER,
            display_name TEXT NOT NULL,
            instance_label TEXT NOT NULL,
            group_label TEXT,
            quantity_index INTEGER,
            hp_current INTEGER,
            hp_max INTEGER,
            armor_class INTEGER,
            hit_dice_text TEXT,
            effective_hit_dice REAL,
            attack_hd_override_text TEXT,
            side TEXT NOT NULL DEFAULT 'hostile',
            faction TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            conditions_json TEXT,
            source TEXT,
            source_data_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (encounter_id) REFERENCES encounters(encounter_id),
            FOREIGN KEY (monster_id) REFERENCES monsters(monster_id)
        );

CREATE TABLE monsters (
            monster_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            category TEXT,
            frequency TEXT,
            number_appearing TEXT,
            armor_class TEXT,
            move TEXT,
            hit_dice TEXT,
            percent_in_lair TEXT,
            treasure_type TEXT,
            number_of_attacks TEXT,
            damage TEXT,
            special_attacks TEXT,
            special_defenses TEXT,
            magic_resistance TEXT,
            intelligence TEXT,
            alignment TEXT,
            size TEXT,
            psionic_ability TEXT,
            attack_defense_modes TEXT,
            chance_speaking TEXT,
            chance_magic_use TEXT,
            chance_sleeping TEXT,
            image TEXT,
            image_path TEXT,
            description TEXT,
            notes TEXT,
            source_file TEXT,
            imported_at TEXT
        , attack_hd_override_text TEXT);

CREATE TABLE open_threads (
            thread_id INTEGER PRIMARY KEY,
            campaign_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            origin_type TEXT,
            origin_id INTEGER,
            status TEXT NOT NULL,
            urgency TEXT,
            last_updated TEXT NOT NULL,
            resolution_notes TEXT,
            FOREIGN KEY (campaign_id) REFERENCES campaigns(campaign_id)
        );

CREATE TABLE player_option_sets (
            option_set_id INTEGER PRIMARY KEY,
            source_turn_id INTEGER,
            active_flag INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            FOREIGN KEY (source_turn_id) REFERENCES ai_turns(turn_id)
        );

CREATE TABLE player_options (
            option_id INTEGER PRIMARY KEY,
            option_set_id INTEGER NOT NULL,
            option_number INTEGER NOT NULL,
            option_text TEXT NOT NULL,
            FOREIGN KEY (option_set_id) REFERENCES player_option_sets(option_set_id),
            UNIQUE(option_set_id, option_number)
        );

CREATE TABLE prisoners (
    prisoner_id INTEGER PRIMARY KEY,
    character_id INTEGER NOT NULL,
    held_at_location_id INTEGER,
    status TEXT NOT NULL,
    disposition TEXT,
    labor_assignment TEXT,
    notes TEXT,
    FOREIGN KEY (character_id) REFERENCES characters(character_id),
    FOREIGN KEY (held_at_location_id) REFERENCES locations(location_id)
);

CREATE TABLE procedural_generations (
            generation_id INTEGER PRIMARY KEY,
            trigger_key TEXT NOT NULL UNIQUE,
            generation_type TEXT NOT NULL,
            location_name TEXT,
            hidden_content_json TEXT NOT NULL,
            visible_content_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

CREATE TABLE project_updates (
    project_update_id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    session_id INTEGER,
    domain_turn_id INTEGER,
    update_note TEXT NOT NULL,
    status_after TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(project_id),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id),
    FOREIGN KEY (domain_turn_id) REFERENCES domain_turns(domain_turn_id)
);

CREATE TABLE projects (
    project_id INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    location_id INTEGER,
    project_type TEXT,
    status TEXT,
    cost_gp INTEGER,
    notes TEXT,
    FOREIGN KEY (campaign_id) REFERENCES campaigns(campaign_id),
    FOREIGN KEY (location_id) REFERENCES locations(location_id)
);

CREATE TABLE relationships (
    relationship_id INTEGER PRIMARY KEY,
    source_character_id INTEGER NOT NULL,
    target_character_id INTEGER NOT NULL,
    relationship_type TEXT NOT NULL,
    notes TEXT,
    FOREIGN KEY (source_character_id) REFERENCES characters(character_id),
    FOREIGN KEY (target_character_id) REFERENCES characters(character_id)
);

CREATE TABLE rules (
    rule_id INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL,
    category TEXT NOT NULL,
    rule_text TEXT NOT NULL,
    FOREIGN KEY (campaign_id) REFERENCES campaigns(campaign_id)
);

CREATE TABLE rules_resolutions (
            resolution_id INTEGER PRIMARY KEY,
            turn_id INTEGER,
            player_action TEXT,
            procedure TEXT NOT NULL,
            dice_expression TEXT NOT NULL,
            rolls_json TEXT NOT NULL,
            modifier INTEGER NOT NULL DEFAULT 0,
            total INTEGER NOT NULL,
            target INTEGER,
            outcome TEXT NOT NULL,
            reason TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (turn_id) REFERENCES ai_turns(turn_id)
        );

CREATE TABLE rumors (
            rumor_id INTEGER PRIMARY KEY,
            campaign_id INTEGER NOT NULL,
            source_type TEXT,
            source_name TEXT,
            location_id INTEGER,
            reported_at TEXT NOT NULL,
            claim_text TEXT NOT NULL,
            reliability TEXT,
            status TEXT NOT NULL,
            notes TEXT,
            FOREIGN KEY (campaign_id) REFERENCES campaigns(campaign_id),
            FOREIGN KEY (location_id) REFERENCES locations(location_id)
        );

CREATE TABLE saving_throw_entries (
            saving_throw_entry_id INTEGER PRIMARY KEY,
            save_table_id TEXT NOT NULL,
            save_category TEXT NOT NULL,
            target_number INTEGER NOT NULL,
            UNIQUE(save_table_id, save_category),
            FOREIGN KEY (save_table_id) REFERENCES saving_throw_tables(save_table_id)
        );

CREATE TABLE saving_throw_tables (
            save_table_id TEXT PRIMARY KEY,
            class_name TEXT NOT NULL,
            level_band TEXT NOT NULL,
            level_min INTEGER NOT NULL,
            level_max INTEGER,
            source_note TEXT,
            source_file TEXT,
            source_row INTEGER,
            imported_at TEXT NOT NULL
        );

CREATE TABLE session_summaries (
            session_id INTEGER PRIMARY KEY,
            campaign_id INTEGER NOT NULL,
            session_date TEXT NOT NULL,
            title TEXT,
            summary TEXT NOT NULL,
            major_events TEXT,
            unresolved_matters TEXT,
            notes TEXT,
            FOREIGN KEY (campaign_id) REFERENCES campaigns(campaign_id)
        );

CREATE TABLE sessions (
    session_id INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL,
    session_number INTEGER,
    session_date TEXT,
    title TEXT NOT NULL,
    summary TEXT,
    dm_notes TEXT,
    FOREIGN KEY (campaign_id) REFERENCES campaigns(campaign_id)
);

CREATE TABLE spell_cast_events (
            spell_cast_event_id INTEGER PRIMARY KEY,
            character_id INTEGER,
            character_spell_id INTEGER,
            spell_id INTEGER,
            spell_name TEXT NOT NULL,
            class_used TEXT,
            spell_level INTEGER,
            status TEXT NOT NULL,
            memorized_prepared INTEGER NOT NULL DEFAULT 0,
            expended INTEGER NOT NULL DEFAULT 0,
            requires_saving_throw_resolver INTEGER NOT NULL DEFAULT 0,
            requires_effect_resolver INTEGER NOT NULL DEFAULT 0,
            player_action TEXT,
            spell_facts_json TEXT,
            details_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (character_id) REFERENCES characters(character_id),
            FOREIGN KEY (character_spell_id) REFERENCES character_spells(character_spell_id),
            FOREIGN KEY (spell_id) REFERENCES spells(spell_id)
        );

CREATE TABLE spell_effect_events (
            spell_effect_event_id INTEGER PRIMARY KEY,
            active_spell_effect_id INTEGER,
            spell_cast_event_id INTEGER,
            spell_name TEXT NOT NULL,
            caster_name TEXT,
            target_summary TEXT,
            effect_type TEXT NOT NULL,
            status TEXT NOT NULL,
            save_applied INTEGER NOT NULL DEFAULT 0,
            save_succeeded INTEGER,
            durable_changes_json TEXT,
            debug_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (active_spell_effect_id) REFERENCES active_spell_effects(active_spell_effect_id),
            FOREIGN KEY (spell_cast_event_id) REFERENCES spell_cast_events(spell_cast_event_id)
        );

CREATE TABLE spell_effect_targets (
            spell_effect_target_id INTEGER PRIMARY KEY,
            active_spell_effect_id INTEGER,
            target_type TEXT NOT NULL,
            target_id INTEGER,
            target_label TEXT NOT NULL,
            save_applied INTEGER NOT NULL DEFAULT 0,
            save_succeeded INTEGER,
            hp_before INTEGER,
            hp_delta INTEGER,
            hp_after INTEGER,
            condition_key TEXT,
            changes_json TEXT,
            FOREIGN KEY (active_spell_effect_id) REFERENCES active_spell_effects(active_spell_effect_id)
        );

CREATE TABLE spell_preparation_events (
            preparation_event_id INTEGER PRIMARY KEY,
            character_id INTEGER,
            character_spell_id INTEGER,
            spell_id INTEGER,
            spell_name TEXT NOT NULL,
            class_used TEXT,
            event_type TEXT NOT NULL,
            status TEXT NOT NULL,
            player_action TEXT,
            details_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (character_id) REFERENCES characters(character_id),
            FOREIGN KEY (character_spell_id) REFERENCES character_spells(character_spell_id),
            FOREIGN KEY (spell_id) REFERENCES spells(spell_id)
        );

CREATE TABLE spells (
    spell_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    class_name TEXT NOT NULL,
    spell_level INTEGER NOT NULL,
    notes TEXT
, school TEXT, range_text TEXT, duration TEXT, area_of_effect TEXT, components TEXT, casting_time TEXT, saving_throw TEXT, description TEXT, source_file TEXT, imported_at TEXT, external_spell_id TEXT, normalized_name TEXT, source_row INTEGER, summary_text TEXT, combat_use_text TEXT, utility_use_text TEXT, audit_note TEXT);

CREATE TABLE state_change_proposals (
            proposal_id INTEGER PRIMARY KEY,
            turn_id INTEGER,
            operation TEXT NOT NULL,
            target_table TEXT NOT NULL,
            target_id TEXT,
            payload_json TEXT,
            status TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (turn_id) REFERENCES ai_turns(turn_id)
        );

CREATE TABLE story_memory (
            memory_id INTEGER PRIMARY KEY,
            scope TEXT NOT NULL DEFAULT 'campaign',
            summary TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

CREATE TABLE treasure_types (
            treasure_type_id INTEGER PRIMARY KEY,
            treasure_type TEXT NOT NULL UNIQUE,
            copper_1000s TEXT,
            silver_1000s TEXT,
            electrum_1000s TEXT,
            gold_1000s TEXT,
            platinum_100s TEXT,
            gems TEXT,
            jewelry TEXT,
            maps_or_magic TEXT,
            notes TEXT,
            source_file TEXT,
            imported_at TEXT
        );

CREATE TABLE treasury_accounts (
    treasury_id INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL,
    account_name TEXT NOT NULL,
    location_id INTEGER,
    gp INTEGER DEFAULT 0,
    sp INTEGER DEFAULT 0,
    cp INTEGER DEFAULT 0,
    pp INTEGER DEFAULT 0,
    gems_gp_value INTEGER,
    notes TEXT,
    FOREIGN KEY (campaign_id) REFERENCES campaigns(campaign_id),
    FOREIGN KEY (location_id) REFERENCES locations(location_id)
);

CREATE TABLE troops (
    troop_id INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL,
    location_id INTEGER,
    group_name TEXT NOT NULL,
    troop_type TEXT NOT NULL,
    count INTEGER NOT NULL,
    commander_character_id INTEGER,
    notes TEXT,
    FOREIGN KEY (campaign_id) REFERENCES campaigns(campaign_id),
    FOREIGN KEY (location_id) REFERENCES locations(location_id),
    FOREIGN KEY (commander_character_id) REFERENCES characters(character_id)
);

CREATE TABLE turn_packets (
            packet_id INTEGER PRIMARY KEY,
            player_action TEXT NOT NULL,
            packet_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

CREATE TABLE ui_activity_log (
            log_id INTEGER PRIMARY KEY,
            entry_type TEXT NOT NULL DEFAULT 'note',
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

CREATE TABLE world_facts (
    world_fact_id INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL,
    category TEXT NOT NULL,
    fact_text TEXT NOT NULL,
    source_note TEXT,
    FOREIGN KEY (campaign_id) REFERENCES campaigns(campaign_id)
);

-- ============================================================
-- SCHEMA: VIEWS
-- ============================================================

CREATE VIEW vw_canon_audit AS
SELECT
    changed_at,
    table_name,
    record_key,
    field_name,
    old_value,
    new_value,
    change_note
FROM canon_audit
ORDER BY changed_at DESC, table_name, record_key;

CREATE VIEW vw_character_levels AS
SELECT
    c.character_id,
    c.name,
    c.character_type,
    GROUP_CONCAT(cl.class_name || ' ' || cl.level, ' / ') AS classes,
    MAX(cs.hp_current) AS hp_current,
    MAX(cs.ac) AS ac,
    c.notes
FROM characters c
LEFT JOIN class_levels cl ON cl.character_id = c.character_id
LEFT JOIN character_status cs ON cs.character_id = c.character_id
GROUP BY c.character_id, c.name, c.character_type, c.notes;

CREATE VIEW vw_character_spells AS
        SELECT c.name AS character_name,
               s.class_name,
               s.spell_level,
               s.name AS spell_name,
               s.normalized_name,
               s.range_text,
               s.duration,
               s.area_of_effect,
               s.casting_time,
               s.saving_throw,
               s.summary_text,
               cs.known_flag,
               cs.memorized_flag,
               cs.notes
        FROM character_spells cs
        JOIN characters c ON c.character_id = cs.character_id
        JOIN spells s ON s.spell_id = cs.spell_id;

CREATE VIEW vw_domain_ledger AS
SELECT
    dt.turn_label,
    dt.start_date,
    dt.end_date,
    die.entry_type,
    die.amount_gp,
    die.amount_sp,
    die.amount_cp,
    die.amount_pp,
    die.description,
    ta.account_name,
    p.name AS related_project
FROM domain_income_expenses die
LEFT JOIN domain_turns dt ON dt.domain_turn_id = die.domain_turn_id
LEFT JOIN treasury_accounts ta ON ta.treasury_id = die.treasury_id
LEFT JOIN projects p ON p.project_id = die.related_project_id
ORDER BY dt.start_date DESC, die.entry_type, die.description;

CREATE VIEW vw_encounters AS
SELECT
    e.encounter_id,
    s.session_number,
    s.session_date,
    l.name AS location,
    e.encounter_type,
    e.encounter_name,
    e.result,
    e.notes
FROM encounters e
LEFT JOIN sessions s ON s.session_id = e.session_id
LEFT JOIN locations l ON l.location_id = e.location_id
ORDER BY COALESCE(s.session_date, '0000-00-00') DESC, e.encounter_name;

CREATE VIEW vw_faction_membership AS
SELECT
    c.name AS character_name,
    f.name AS faction_name,
    f.faction_type,
    f.status,
    cf.role_name,
    cf.notes
FROM character_factions cf
JOIN characters c ON c.character_id = cf.character_id
JOIN factions f ON f.faction_id = cf.faction_id
ORDER BY faction_name, character_name;

CREATE VIEW vw_forces_by_location AS
SELECT
    COALESCE(l.name, 'Unassigned') AS location,
    t.group_name,
    t.troop_type,
    t.count,
    c.name AS commander,
    t.notes
FROM troops t
LEFT JOIN locations l ON l.location_id = t.location_id
LEFT JOIN characters c ON c.character_id = t.commander_character_id
ORDER BY location, t.troop_type, t.group_name;

CREATE VIEW vw_inventory_by_owner AS
SELECT
    COALESCE(ch.name, l.name, ta.account_name, 'Unknown Owner') AS owner,
    CASE
        WHEN inv.character_id IS NOT NULL THEN 'Character'
        WHEN inv.location_id IS NOT NULL THEN 'Location'
        WHEN inv.treasury_id IS NOT NULL THEN 'Treasury'
        ELSE 'Unknown'
    END AS owner_type,
    i.name AS item_name,
    i.item_type,
    i.magic_flag,
    inv.quantity,
    inv.equipped_flag,
    inv.notes
FROM inventory inv
JOIN items i ON i.item_id = inv.item_id
LEFT JOIN characters ch ON ch.character_id = inv.character_id
LEFT JOIN locations l ON l.location_id = inv.location_id
LEFT JOIN treasury_accounts ta ON ta.treasury_id = inv.treasury_id
ORDER BY owner_type, owner, item_name;

CREATE VIEW vw_known_spells AS
        SELECT character_name,
               class_name,
               spell_level,
               spell_name,
               normalized_name,
               range_text,
               duration,
               area_of_effect,
               casting_time,
               saving_throw,
               summary_text,
               known_flag,
               memorized_flag,
               notes
        FROM vw_character_spells
        WHERE known_flag = 1;

CREATE VIEW vw_magic_item_lookup AS
        SELECT magic_item_id,
               name,
               item_type,
               subtype,
               bonus,
               charges,
               value_gp,
               rarity,
               curse_flag,
               description,
               notes,
               source_file
        FROM magic_items;

CREATE VIEW vw_memorized_spells AS
        SELECT character_name,
               class_name,
               spell_level,
               spell_name,
               normalized_name,
               range_text,
               duration,
               area_of_effect,
               casting_time,
               saving_throw,
               summary_text,
               notes
        FROM vw_character_spells
        WHERE memorized_flag = 1;

CREATE VIEW vw_monster_lookup AS
        SELECT monster_id,
               name,
               category,
               frequency,
               number_appearing,
               armor_class,
               move,
               hit_dice,
               percent_in_lair,
               treasure_type,
               number_of_attacks,
               damage,
               special_attacks,
               special_defenses,
               magic_resistance,
               intelligence,
               alignment,
               size,
               psionic_ability,
               attack_defense_modes,
               chance_speaking,
               chance_magic_use,
               chance_sleeping,
               image,
               image_path,
               description,
               notes,
               source_file
        FROM monsters;

CREATE VIEW vw_npcs_and_relationships AS
SELECT
    src.name AS source_name,
    src.character_type AS source_type,
    r.relationship_type,
    tgt.name AS target_name,
    r.notes
FROM relationships r
JOIN characters src ON src.character_id = r.source_character_id
JOIN characters tgt ON tgt.character_id = r.target_character_id
ORDER BY source_name, relationship_type, target_name;

CREATE VIEW vw_prisoners_status AS
SELECT
    ch.name AS prisoner_name,
    l.name AS held_at,
    p.status,
    p.disposition,
    p.labor_assignment,
    p.notes
FROM prisoners p
JOIN characters ch ON ch.character_id = p.character_id
LEFT JOIN locations l ON l.location_id = p.held_at_location_id
ORDER BY prisoner_name;

CREATE VIEW vw_project_updates AS
SELECT
    p.name AS project_name,
    pu.status_after,
    pu.update_note,
    s.session_number,
    s.session_date,
    dt.turn_label
FROM project_updates pu
JOIN projects p ON p.project_id = pu.project_id
LEFT JOIN sessions s ON s.session_id = pu.session_id
LEFT JOIN domain_turns dt ON dt.domain_turn_id = pu.domain_turn_id
ORDER BY COALESCE(s.session_date, '0000-00-00') DESC, COALESCE(dt.start_date, '0000-00-00') DESC, project_name;

CREATE VIEW vw_projects_status AS
SELECT
    p.project_id,
    p.name,
    l.name AS location,
    p.project_type,
    p.status,
    p.cost_gp,
    p.notes
FROM projects p
LEFT JOIN locations l ON l.location_id = p.location_id
ORDER BY p.status, p.name;

CREATE VIEW vw_recent_events AS
SELECT
    ae.event_id,
    s.session_number,
    s.session_date,
    ae.event_order,
    ae.event_type,
    l.name AS location,
    ae.title,
    ae.description,
    ae.outcome
FROM adventure_events ae
LEFT JOIN sessions s ON s.session_id = ae.session_id
LEFT JOIN locations l ON l.location_id = ae.location_id
ORDER BY COALESCE(s.session_date, '0000-00-00') DESC, COALESCE(s.session_number, 0) DESC, COALESCE(ae.event_order, 0) DESC;

CREATE VIEW vw_spell_catalog AS
        SELECT spell_id,
               external_spell_id,
               name,
               normalized_name,
               class_name,
               spell_level,
               school,
               range_text,
               duration,
               area_of_effect,
               components,
               casting_time,
               saving_throw,
               description,
               summary_text,
               combat_use_text,
               utility_use_text,
               audit_note,
               notes,
               source_file,
               source_row,
               imported_at
        FROM spells;

CREATE VIEW vw_treasure_lookup AS
        SELECT treasure_type_id,
               treasure_type,
               copper_1000s,
               silver_1000s,
               electrum_1000s,
               gold_1000s,
               platinum_100s,
               gems,
               jewelry,
               maps_or_magic,
               notes,
               source_file
        FROM treasure_types;

CREATE VIEW vw_treasury_summary AS
SELECT
    ta.treasury_id,
    ta.account_name,
    l.name AS location,
    ta.gp,
    ta.sp,
    ta.cp,
    ta.pp,
    ta.gems_gp_value,
    (COALESCE(ta.gp,0)
     + COALESCE(ta.sp,0)/10.0
     + COALESCE(ta.cp,0)/100.0
     + COALESCE(ta.pp,0)*5.0
     + COALESCE(ta.gems_gp_value,0)) AS estimated_total_gp,
    ta.notes
FROM treasury_accounts ta
LEFT JOIN locations l ON l.location_id = ta.location_id
ORDER BY estimated_total_gp DESC, ta.account_name;

CREATE VIEW vw_world_facts AS
SELECT
    world_fact_id,
    category,
    fact_text,
    source_note
FROM world_facts
ORDER BY category, world_fact_id;

-- ============================================================
-- SCHEMA: INDEXES
-- ============================================================

CREATE INDEX idx_active_spell_effects_status ON active_spell_effects(status, spell_name);

CREATE INDEX idx_adnd_gems_jewelry_lookup ON adnd_1e_gems_jewelry(category, subtable_name, roll_low, roll_high);

CREATE INDEX idx_adnd_magic_subtables_lookup ON adnd_1e_magic_item_subtables(category, subtable_name, roll_low, roll_high);

CREATE INDEX idx_adnd_treasure_types_lookup ON adnd_1e_treasure_types(treasure_type, component);

CREATE INDEX idx_character_spells_character_spell ON character_spells(character_id, spell_id);

CREATE INDEX idx_combat_attack_matrix_lookup
        ON combat_attack_matrix_entries(matrix_code, level_min, level_max, armor_class)
        ;

CREATE INDEX idx_domain_ledger_turn ON domain_income_expenses(domain_turn_id);

CREATE INDEX idx_dungeon_random_entries_lookup ON dungeon_random_monster_table_entries(monster_level_table, roll_min, roll_max);

CREATE INDEX idx_dungeon_random_matrix_lookup ON dungeon_random_monster_level_matrix(dungeon_level_min, dungeon_level_max, roll_min, roll_max);

CREATE INDEX idx_dungeon_subtable_lookup ON dungeon_random_subtable_entries(subtable_type, monster_level_table, roll_min, roll_max);

CREATE INDEX idx_encounter_loot_sources_encounter ON encounter_loot_sources(encounter_id, hidden_flag, revealed_flag, taken_flag);

CREATE INDEX idx_encounter_loot_sources_monster ON encounter_loot_sources(monster_instance_id, status);

CREATE INDEX idx_encounters_session ON encounters(session_id);

CREATE INDEX idx_events_session ON adventure_events(session_id, event_order);

CREATE INDEX idx_generated_loot_instances_source ON generated_loot_instances(loot_source_id);

CREATE INDEX idx_inventory_character ON inventory(character_id);

CREATE INDEX idx_inventory_location ON inventory(location_id);

CREATE INDEX idx_locations_parent ON locations(parent_location_id);

CREATE INDEX idx_magic_items_name ON magic_items(name);

CREATE INDEX idx_magic_items_type ON magic_items(item_type);

CREATE INDEX idx_monster_attack_matrix_lookup
        ON monster_attack_matrix_entries(hd_band_code, armor_class)
        ;

CREATE INDEX idx_monster_instances_encounter ON monster_instances(encounter_id, status);

CREATE INDEX idx_monster_instances_label ON monster_instances(instance_label, group_label, status);

CREATE INDEX idx_monsters_name ON monsters(name);

CREATE INDEX idx_monsters_treasure_type ON monsters(treasure_type);

CREATE INDEX idx_saving_throw_entries_lookup ON saving_throw_entries(save_table_id, save_category);

CREATE INDEX idx_saving_throw_tables_lookup ON saving_throw_tables(class_name, level_min, level_max);

CREATE INDEX idx_spell_cast_events_character ON spell_cast_events(character_id, created_at);

CREATE INDEX idx_spell_effect_targets_target ON spell_effect_targets(target_type, target_id);

CREATE UNIQUE INDEX idx_spells_external_spell_id ON spells(external_spell_id) WHERE external_spell_id IS NOT NULL;

CREATE INDEX idx_spells_lookup ON spells(class_name, spell_level, name);

CREATE INDEX idx_spells_normalized_lookup ON spells(normalized_name, class_name, spell_level);

CREATE INDEX idx_treasure_types_code ON treasure_types(treasure_type);

CREATE INDEX idx_troops_location ON troops(location_id);

-- ============================================================

-- ============================================================
-- To populate AD&D 1e reference tables (monsters, spells, etc.)
-- after creating a character database, run the full starter.sql:
--   sqlite3 saves/mychar.db < schema/starter.sql
-- ============================================================
