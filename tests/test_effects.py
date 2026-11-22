"""Tests for the effects. Run: python3 -m unittest discover tests

effects.py has no Home Assistant imports, so it is loaded straight from its
file and these run without Home Assistant installed.
"""

import importlib.util
from pathlib import Path
import random
import sys
import unittest

_DIR = Path(__file__).parent.parent / "custom_components" / "light_fx"
_spec = importlib.util.spec_from_file_location("light_fx_effects", _DIR / "effects.py")
effects = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = effects  # dataclasses look the module up by name
_spec.loader.exec_module(effects)


def _yaml_effect_options():
    """The effect names offered in the action's dropdown (services.yaml)."""
    lines = (_DIR / "services.yaml").read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "options:")
    out = []
    for line in lines[start + 1 :]:
        if not line.strip().startswith("- "):
            break
        out.append(line.strip()[2:])
    return out


class EffectsTest(unittest.TestCase):
    def test_dropdown_lists_every_effect(self):
        # services.yaml repeats the effect names by hand: keep the two in step
        self.assertEqual(sorted(_yaml_effect_options()), sorted(effects.EFFECTS))

    def test_every_effect_gives_valid_frames(self):
        for name, effect in effects.EFFECTS.items():
            end = effect.duration if effect.duration is not None else 120
            for params in (effects.Params(), effects.Params(speed=3, brightness=40, hue=200, saturation=80)):
                rng = random.Random(1)
                for n in (1, 3, 9):
                    for i in range(n):
                        for step in range(41):
                            t = end * step / 40
                            with self.subTest(effect=name, t=t, i=i, n=n):
                                frame = effect.fn(t, i, n, params, rng)
                                self.assertIsInstance(frame, effects.Frame)
                                if frame.off:
                                    continue
                                if frame.brightness is not None:
                                    self.assertGreaterEqual(frame.brightness, 0)
                                    self.assertLessEqual(frame.brightness, 100)
                                if frame.hs is not None:
                                    self.assertGreaterEqual(frame.hs[1], 0)
                                    self.assertLessEqual(frame.hs[1], 100)
                                if frame.kelvin is not None:
                                    self.assertGreater(frame.kelvin, 1000)
                                    self.assertLess(frame.kelvin, 10000)

    def test_intervals_are_positive(self):
        for name, effect in effects.EFFECTS.items():
            with self.subTest(effect=name):
                self.assertGreater(effect.interval, 0)


if __name__ == "__main__":
    unittest.main()
