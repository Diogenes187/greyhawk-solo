# Make Module Skill

When the player says "make module" or requests a new dungeon/location:

## Step 1 — Brief
Ask for (or infer from context): theme, size (small/medium/large = 6/12/20 rooms), 
location in Greyhawk, and character level. Confirm before proceeding.

## Step 2 — Design
Design the full module: name, hook, ecology, faction notes, room-by-room key.
Follow AD&D 1e stocking: 1/3 empty, logical monster placement, level-appropriate treasure.
Do NOT show the player room contents. Design it all internally first.

## Step 3 — Store
Call `create_module_scaffold` first.
Then call `store_room` for every room. Verify all rooms stored via `get_module_key_index`.

## Step 4 — DM Master Map
Generate a complete SVG map using show_widget — all rooms visible, numbered, with 
door/stair/trap symbols. Save a copy to the outputs folder.

## Step 5 — Player Starting Map  
Generate a second SVG showing ONLY the entrance room and connecting passages to 
the first doors. Everything else hatched/fogged. Room numbers hidden on player map.
Save to outputs folder.

## Step 6 — Confirm Ready
Report: module name, room count, all rooms stored (Y/N), DM map saved, player map saved.
State the module_key for future reference.

## During Play
- When player enters a new room: call `get_room` to read contents. Call `update_map_state` 
  with `explore_room`. Regenerate player map on request.
- When trap triggered: call `update_map_state` with `trigger_trap`.
- When secret found: call `update_map_state` with `find_secret`.
- When all monsters cleared: call `update_map_state` with `clear_room`.
- Player map request: call `get_map_state`, regenerate SVG showing only explored rooms.
- NEVER describe room contents from memory. Always call `get_room` first.
