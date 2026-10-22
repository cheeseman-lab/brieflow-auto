"""Fixture notebook (unpatchable): STRAY_KNOB has a user-bucket interview row but
is assigned OUTSIDE any marker block, so patch_marker_block_value can't reach it
— the detector must flag it as UNPATCHABLE (the patch would silently miss).
"""

# === OPERATOR PARAMETERS ===
SAMPLE_FRACTION = 0.1
# === END OPERATOR PARAMETERS ===

STRAY_KNOB = 7
