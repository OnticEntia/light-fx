"""The arithmetic of driving Nanoleaf panels: effect frames to RGB, and RGB to
the bytes of a stream packet.

Nothing here imports Home Assistant or talks to a device, so the tests load
this file on its own (see tests/test_nanoleaf_frames.py).

Stream packet (extControl v2, all big-endian):
    nPanels(u16) + per panel: id(u16) R G B W(u8 each) transition(u16, 100 ms units)
"""

from __future__ import annotations

import colorsys
import math
import struct
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .effects import Frame


def kelvin_to_rgb(kelvin: float) -> tuple[float, float, float]:
    """Approximate a colour temperature as 0-1 RGB (Tanner Helland's fit)."""
    t = kelvin / 100
    if t <= 66:
        r = 255.0
        g = 99.4708025861 * math.log(t) - 161.1195681661
        b = 0.0 if t <= 19 else 138.5177312231 * math.log(t - 10) - 305.0447927307
    else:
        r = 329.698727446 * (t - 60) ** -0.1332047592
        g = 288.1221695283 * (t - 60) ** -0.0755148492
        b = 255.0
    return tuple(min(max(c, 0.0), 255.0) / 255 for c in (r, g, b))


def frame_to_rgb(frame: Frame, ceiling: int) -> tuple[int, int, int]:
    """Turn an effect frame into panel RGB, folding brightness into the colour."""
    if frame.off:
        return (0, 0, 0)
    if frame.kelvin is not None:
        rgb = kelvin_to_rgb(frame.kelvin)
    elif frame.hs is not None:
        rgb = colorsys.hsv_to_rgb((frame.hs[0] % 360) / 360, min(max(frame.hs[1], 0), 100) / 100, 1.0)
    else:
        rgb = (1.0, 1.0, 1.0)
    level = (100 if frame.brightness is None else frame.brightness) * ceiling / 10000
    level = min(max(level, 0.0), 1.0)
    return tuple(round(c * level * 255) for c in rgb)


def build_packet(panel_ids: list[int], colors: list[tuple[int, int, int]], transition: float) -> bytes:
    """One stream packet giving each panel its colour.

    `colors` is in the same order as `panel_ids`; if the two differ in length
    the extra entries are left out, and the count in the packet says so.
    `transition` is in seconds.
    """
    steps = max(0, min(round(transition * 10), 0xFFFF))
    pairs = list(zip(panel_ids, colors))
    packet = bytearray(struct.pack(">H", len(pairs)))
    for pid, (r, g, b) in pairs:
        packet += struct.pack(">HBBBBH", pid, r, g, b, 0, steps)
    return bytes(packet)
