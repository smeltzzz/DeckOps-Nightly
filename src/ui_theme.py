"""Shared, Qt-independent DeckOps palette.

The Linux app and the PyQt5 offline launcher both import this module so the
shipped launcher has the same visual identity as the main window.
"""

# Warm charcoal foundations; faction colors are accents, never body surfaces.
C_BG = "#1B1A19"
C_CARD = "#252523"
C_SURFACE = "#2C2B28"
C_LINE = "#59574F"
C_TEXT = "#FFFFFF"
C_DIM = "#C4C0B8"
C_IW = "#01AD10"       # Modern Warfare
C_TREY = "#F96800"     # Black Ops / Nightly
C_DARK_BTN = "#3B3934"
C_RED_BTN = "#963535"  # Destructive actions remain distinct from faction orange.
C_BLUE_BTN = C_DARK_BTN  # Compatibility alias; no blue in the palette.
C_FOCUS = "#FFFFFF"
