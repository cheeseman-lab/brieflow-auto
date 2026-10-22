#!/usr/bin/env python3
"""brieflow_channels.py — per-channel colormap helpers.

Auto-size the CHANNEL_CMAPS palette to the channel count (the sbs/phenotype viz
cells IndexError on a wrong-length list). Pure stdlib; no dependency on other
plugin modules. Re-exported from brieflow_wizard. (Channel-mapping confirm/record
helpers live with the interview module; channel auto-probe lands here in Phase 3.)
"""

from __future__ import annotations

from typing import Iterable

__all__ = ["DEFAULT_CMAP_PALETTE", "default_channel_cmaps"]


# ---------------------------------------------------------------------------
# Channel colormap auto-sizing — CHANNEL_CMAPS must be exactly len(channel_names)
# entries or the sbs/phenotype viz cells IndexError. A fixed-length default
# (4 or 6) breaks any screen with more channels (multi-round phenotype: 2 rounds
# x 5 fluorophores = 10). Derive a correctly-sized palette from the channel
# count instead of shipping a literal list.
# ---------------------------------------------------------------------------


#: Base per-channel colormap palette, DAPI-first (nuclear channel → blue). These
#: are the only valid microfilm `pure_*` names; the viz cells reject anything else.
DEFAULT_CMAP_PALETTE = [
    "pure_blue",
    "pure_green",
    "pure_red",
    "pure_magenta",
    "pure_cyan",
    "pure_yellow",
]


def default_channel_cmaps(
    channel_names: Iterable[str],
    *,
    n_rounds: int = 1,
    base_palette: Iterable[str] | None = None,
) -> list[str]:
    """Return exactly len(channel_names) colormaps, auto-sized to the channel
    count so the viz cells never IndexError.

    Cycles `base_palette` (default DEFAULT_CMAP_PALETTE) to fill N entries. For
    multi-round phenotype (n_rounds > 1 and the channel count divides evenly),
    colors are aligned per fluorophore index across rounds — the same marker
    position gets the same color each round (snowdon: round 1 and round 2 share
    DAPI=blue, GFP=green, ...).

    Args:
        channel_names: the biological label list (CHANNEL_NAMES); only its
            length is used.
        n_rounds: number of phenotype rounds (len(phenotype_round_order)); 1 for
            SBS / single-round phenotype.
        base_palette: override the cycling palette.

    Returns:
        list[str] of length len(list(channel_names)).
    """
    names = list(channel_names)
    n = len(names)
    palette = list(DEFAULT_CMAP_PALETTE) if base_palette is None else list(base_palette)
    if not palette:
        raise ValueError("base_palette must be non-empty")
    if n_rounds and n_rounds > 1 and n % n_rounds == 0:
        per_round = n // n_rounds
        return [palette[i % len(palette)] for _ in range(n_rounds) for i in range(per_round)]
    return [palette[i % len(palette)] for i in range(n)]
