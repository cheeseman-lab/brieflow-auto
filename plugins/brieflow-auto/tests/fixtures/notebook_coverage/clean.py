"""Fixture notebook (clean): every operator var is inside a marker block and has
a matching interview row; CONFIG_FILE_PATH is an allowlisted infra constant, and
DERIVED_THING is derived downstream (not an operator knob). The detector must
find NO gaps and NO unpatchable vars here.
"""

# === OPERATOR PARAMETERS ===
CONFIG_FILE_PATH = "config/config.yml"
SAMPLE_FRACTION = 0.1
CELL_DIAMETER = 30
# === END OPERATOR PARAMETERS ===

# Derived downstream from an operator input — not an operator knob, so no row.
DERIVED_THING = CELL_DIAMETER * 2
