"""
engine/validator.py
-------------------
Phase 39 — lightweight DM-response validator.

A second Claude API call (claude-haiku-4-5) checks every DM narrative
response against the five hard rules before the player sees it. The
intent is fast, cheap (Haiku), one-paragraph context, ≤300-token reply.

Five rules checked:
  1. PLAYER AGENCY — never write the player's actions / decisions / speech.
  2. NPC NAMES     — no new NPCs invented in the response.
  3. VERBOSITY     — ≤ 4 sentences of narration (excluding dice / mech data).
  4. INVENTED LORE — no new factions / subplots / hooks beyond game state.
  5. DICE FUDGING  — no roll references without a matching roll_dice call.

`anthropic` is imported at module scope and a single Anthropic() client
is constructed once at import time, reused for every validate_dm_response
call. The SDK reads ANTHROPIC_API_KEY from the environment automatically;
construction does not require the key — only the `messages.create` call
does — so missing-key errors surface at call time with a useful message
instead of breaking module import.

Install (one-time, into the Python the MCP server actually launches):

    "C:\\Users\\<you>\\AppData\\Local\\Programs\\Python\\Python313\\python.exe" \\
        -m pip install anthropic

After install the MCP server picks up the module on next restart.
"""

from __future__ import annotations

import anthropic

VALIDATOR_MODEL = "claude-haiku-4-5"

VALIDATOR_PROMPT = """You are a strict rule-checker for an AD&D 1e solo RPG.
Examine the DM response below and check it against these rules:

1. PLAYER AGENCY — Does the response write the player's actions, decisions, thoughts, or speech?
2. NPC NAMES — Does the response introduce a new NPC name that was not in the player input or game state provided?
3. VERBOSITY — Is the response more than 4 sentences of narration (excluding dice results and mechanical data)?
4. INVENTED LORE — Does the response introduce factions, conspiracies, subplots, organizations, or plot hooks not already established in the game state?
5. DICE FUDGING — Does the response reference a dice result, save threshold, or mechanical number without a corresponding roll_dice call having occurred?

For each rule, respond with PASS or FAIL and one short reason if FAIL.
Then give a final verdict: CLEAN or VIOLATION.
If VIOLATION, identify which rules failed.

Do not rewrite the response. Only judge it."""


# Single module-level client, constructed once at import. Anthropic() does
# not require the API key at construction — it's read from
# ANTHROPIC_API_KEY when messages.create is called — so this is safe to
# create at module load even on a machine where the key is not yet set.
_CLIENT = anthropic.Anthropic()


def validate_dm_response(response_text: str, game_state_summary: str) -> dict:
    """
    Send the DM response + one-paragraph game-state summary to
    claude-haiku-4-5 and return a structured verdict dict.

    Success shape:
      {"clean": bool, "verdict": full text, "original_response": text,
       "available": True, "model": "claude-haiku-4-5"}

    API-call-failure shape (network error, missing API key, rate limit):
      {"clean": False, "available": False, "error": <reason>,
       "verdict": <short error blurb>, "original_response": text}

    The caller (validate_response MCP tool) is responsible for parsing
    `verdict` into a rules_failed list and presenting it to the DM.
    """
    text = response_text or ""
    state = game_state_summary or ""

    check_input = (
        "GAME STATE SUMMARY:\n"
        f"{state}\n\n"
        "DM RESPONSE TO CHECK:\n"
        f"{text}"
    )

    try:
        result = _CLIENT.messages.create(
            model=VALIDATOR_MODEL,
            max_tokens=300,
            system=VALIDATOR_PROMPT,
            messages=[{"role": "user", "content": check_input}],
        )
    except Exception as e:
        return {
            "clean":              False,
            "available":          False,
            "error":              f"Anthropic API call failed: {e}",
            "verdict":            "Validator unavailable — API call failed.",
            "original_response":  text,
        }

    # The SDK returns a list of content blocks; the first one carries text
    # for a plain prompt like this.
    try:
        verdict_text = result.content[0].text  # type: ignore[attr-defined]
    except (AttributeError, IndexError, TypeError):
        verdict_text = str(result)

    # CLEAN-only-when-explicitly-CLEAN keeps us safe against fuzzy answers.
    is_clean = "CLEAN" in (verdict_text or "").upper() \
               and "VIOLATION" not in (verdict_text or "").upper()

    return {
        "clean":             is_clean,
        "available":         True,
        "verdict":           verdict_text,
        "original_response": text,
        "model":             VALIDATOR_MODEL,
    }
