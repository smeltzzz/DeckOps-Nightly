"""guide_text.py - wording for the Setup Complete screen (no Qt, so it can be tested)."""


def how_to_start_text(setup: dict, lcd: bool) -> str:
    """What to press to start each set-up game (SetupCompleteScreen). setup is the
    setup_games record. Empty when there is nothing to say."""
    def steam(key):
        return key in setup and setup[key].get("source", "steam") == "steam"

    lines = []
    if "cod4sp" in setup or "cod4mp" in setup:
        line = 'MW1: Play starts the campaign (pick the "Player" profile the first time).'
        if steam("cod4mp") and setup["cod4mp"].get("client") == "cod4r":
            line += (" The Multiplayer button in its main menu starts CoD4R. To go back to the "
                     "campaign, quit and press Play again.")
        elif steam("cod4mp") and setup["cod4mp"].get("client") == "cod4x":
            # Existing CoD4x setups still use their separate shortcut.
            line += " Multiplayer (CoD4x) is the separate DeckOps entry in your Steam library."
        lines.append(line)
    if steam("iw4mp"):
        lines.append("MW2: Play starts the campaign. The Multiplayer button in its main menu starts IW4x.")
    if steam("iw5mp"):
        lines.append("MW3: Play starts the campaign. The Multiplayer button in its main menu starts Plutonium.")
    names = [n for n, keys in (("WaW", ("t4sp", "t4mp")), ("BO1", ("t5sp", "t5mp")),
                               ("BO2", ("t6sp", "t6mp", "t6zm")))
             if any(steam(k) for k in keys)]
    if names:
        lines.append(f"{', '.join(names[:-1]) + ' and ' if len(names) > 1 else ''}{names[-1]}: "
                     "press Play and pick Campaign, Zombies or Multiplayer from the menu.")
    if lcd and steam("t4mp"):
        lines.append("WaW multiplayer is a separate DeckOps entry on the LCD Steam Deck.")
    if any(e.get("source") == "own" for e in setup.values()):
        lines.append("Games you added as non-Steam have a separate DeckOps entry for each mode.")
    return "\n\n".join(lines)
