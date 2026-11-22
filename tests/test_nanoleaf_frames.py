"""Tests for the Nanoleaf colour and packet arithmetic.

Run: python3 -m unittest discover -s tests

nanoleaf_frames.py has no Home Assistant imports, so it is loaded straight
from its file, like effects.py in test_effects.py.
"""

import importlib.util
from pathlib import Path
import struct
import sys
import unittest

_DIR = Path(__file__).parent.parent / "custom_components" / "light_fx"


def _load(name):
    module_name = f"light_fx_{name}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, _DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module  # dataclasses look the module up by name
    spec.loader.exec_module(module)
    return module


effects = _load("effects")
frames = _load("nanoleaf_frames")
Frame = effects.Frame


class KelvinTest(unittest.TestCase):
    def test_stays_in_range(self):
        for kelvin in range(1000, 10001, 250):
            with self.subTest(kelvin=kelvin):
                for c in frames.kelvin_to_rgb(kelvin):
                    self.assertGreaterEqual(c, 0.0)
                    self.assertLessEqual(c, 1.0)

    def test_warm_is_red_and_cool_is_blue(self):
        r, g, b = frames.kelvin_to_rgb(2700)
        self.assertEqual(r, 1.0)
        self.assertGreater(r, g)
        self.assertGreater(g, b)
        r, g, b = frames.kelvin_to_rgb(9000)
        self.assertEqual(b, 1.0)
        self.assertGreater(b, r)

    def test_about_6600_is_near_white(self):
        for c in frames.kelvin_to_rgb(6600):
            self.assertGreater(c, 0.95)


class FrameToRgbTest(unittest.TestCase):
    def test_off_is_black(self):
        self.assertEqual(frames.frame_to_rgb(Frame(off=True, hs=(0, 100), brightness=100), 100), (0, 0, 0))

    def test_primary_colours(self):
        self.assertEqual(frames.frame_to_rgb(Frame(hs=(0, 100), brightness=100), 100), (255, 0, 0))
        self.assertEqual(frames.frame_to_rgb(Frame(hs=(120, 100), brightness=100), 100), (0, 255, 0))
        self.assertEqual(frames.frame_to_rgb(Frame(hs=(240, 100), brightness=100), 100), (0, 0, 255))

    def test_hue_wraps_and_saturation_is_clamped(self):
        self.assertEqual(frames.frame_to_rgb(Frame(hs=(360, 100), brightness=100), 100), (255, 0, 0))
        self.assertEqual(frames.frame_to_rgb(Frame(hs=(-120, 150), brightness=100), 100), (0, 0, 255))

    def test_brightness_and_ceiling_multiply(self):
        self.assertEqual(frames.frame_to_rgb(Frame(hs=(0, 100), brightness=50), 100), (128, 0, 0))
        self.assertEqual(frames.frame_to_rgb(Frame(hs=(0, 100), brightness=100), 50), (128, 0, 0))
        self.assertEqual(frames.frame_to_rgb(Frame(hs=(0, 100), brightness=50), 50), (64, 0, 0))

    def test_no_brightness_means_full_and_no_colour_means_white(self):
        self.assertEqual(frames.frame_to_rgb(Frame(hs=(0, 100)), 100), (255, 0, 0))
        self.assertEqual(frames.frame_to_rgb(Frame(brightness=100), 100), (255, 255, 255))

    def test_kelvin_wins_over_hs(self):
        r, g, b = frames.frame_to_rgb(Frame(kelvin=2700, hs=(240, 100), brightness=100), 100)
        self.assertEqual(r, 255)
        self.assertGreater(g, b)

    def test_every_effect_frame_gives_bytes(self):
        import random

        for name, effect in effects.EFFECTS.items():
            end = effect.duration if effect.duration is not None else 60
            rng = random.Random(1)
            for step in range(21):
                with self.subTest(effect=name, step=step):
                    frame = effect.fn(end * step / 20, 1, 4, effects.Params(), rng)
                    rgb = frames.frame_to_rgb(frame, 100)
                    self.assertEqual(len(rgb), 3)
                    for c in rgb:
                        self.assertIsInstance(c, int)
                        self.assertGreaterEqual(c, 0)
                        self.assertLessEqual(c, 255)


class PacketTest(unittest.TestCase):
    def test_layout(self):
        packet = frames.build_packet([7, 300], [(1, 2, 3), (255, 254, 253)], 0.5)
        self.assertEqual(
            packet,
            bytes([0, 2, 0, 7, 1, 2, 3, 0, 0, 5, 1, 44, 255, 254, 253, 0, 0, 5]),
        )

    def test_length_is_two_plus_eight_per_panel(self):
        for n in (0, 1, 12):
            packet = frames.build_packet(list(range(n)), [(0, 0, 0)] * n, 1)
            self.assertEqual(len(packet), 2 + 8 * n)
            self.assertEqual(struct.unpack(">H", packet[:2])[0], n)

    def test_count_matches_the_panels_written(self):
        # more colours than panels, or the other way round: never a count that
        # promises more panels than the packet holds
        for ids, colors in (([1, 2], [(9, 9, 9)] * 3), ([1, 2, 3], [(9, 9, 9)])):
            packet = frames.build_packet(ids, colors, 0)
            count = struct.unpack(">H", packet[:2])[0]
            self.assertEqual(count, min(len(ids), len(colors)))
            self.assertEqual(len(packet), 2 + 8 * count)

    def test_transition_is_tenths_of_a_second_and_clamped(self):
        def steps(transition):
            return struct.unpack(">H", frames.build_packet([1], [(0, 0, 0)], transition)[-2:])[0]

        self.assertEqual(steps(0), 0)
        self.assertEqual(steps(0.36), 4)
        self.assertEqual(steps(4.5), 45)
        self.assertEqual(steps(-1), 0)
        self.assertEqual(steps(1e9), 0xFFFF)


if __name__ == "__main__":
    unittest.main()
