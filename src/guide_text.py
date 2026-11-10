"""guide_text.py - wording for the Setup Complete screen (no Qt, so it can be tested)."""


def how_to_start_text(setup: dict, lcd: bool) -> str:
    """What to press to start each set-up game (SetupCompleteScreen). setup is the
    setup_games record. Empty when there is nothing to say."""
    # Hand-edited or corrupt entries are not set-up games (as in config.check_setup)
    setup = {k: v for k, v in (setup or {}).items() if isinstance(v, dict)}

    def steam(key):
        return key in setup and setup[key].get("source", "steam") == "steam"

    def plut(key):
        return steam(key) and setup[key].get("client", "plutonium") == "plutonium"

    lines = []
    # Own copies are covered by the non-Steam line at the end
    if steam("cod4sp") or steam("cod4mp"):
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
    if steam("iw5mp_ds"):
        lines.append("MW3 Dedicated Server: press Play on its own Steam tile and pick "
                     "Play Plutonium Online to start the server.")

    # WaW is one Steam tile for every mode. BO1 / BO2 keep MP / Zombies on
    # their own tiles; only on OLED / Other does the campaign tile's menu get
    # the Plutonium MP / ZM entries too (plutonium.HOST_MENU; never on LCD).
    menu_names = []
    tile_lines = []
    if steam("t4sp") or steam("t4mp"):
        menu_names.append("WaW")
    for name, sp, others in (("BO1", "t5sp", (("t5mp", "Multiplayer"),)),
                             ("BO2", "t6sp", (("t6mp", "Multiplayer"), ("t6zm", "Zombies")))):
        modes = [label for k, label in others if steam(k)]
        if not steam(sp) and not modes:
            continue
        if not lcd and any(plut(k) for k, _ in others):
            menu_names.append(name)
            continue
        parts = []
        if steam(sp):
            parts.append("press Play and pick Campaign & Zombies from the menu." if sp == "t5sp"
                         else "Play starts the campaign (T6SP-Mod).")
        if modes:
            what = " and ".join(modes)
            parts.append(f"{what} {'are' if len(modes) > 1 else 'is'} on "
                         f"{'their' if len(modes) > 1 else 'its'} own Steam "
                         f"{'tiles' if len(modes) > 1 else 'tile'}; press Play there.")
        tile_lines.append(f"{name}: " + " ".join(parts))
    if menu_names:
        lines.append(f"{', '.join(menu_names[:-1]) + ' and ' if len(menu_names) > 1 else ''}{menu_names[-1]}: "
                     "press Play and pick Campaign, Zombies or Multiplayer from the menu.")
    lines += tile_lines
    if lcd and steam("t4mp"):
        lines.append("WaW multiplayer is a separate DeckOps entry on the LCD Steam Deck.")
    if any(e.get("source") == "own" for e in setup.values()):
        lines.append("Games you added as non-Steam have a separate DeckOps entry for each mode.")
    return "\n\n".join(lines)
